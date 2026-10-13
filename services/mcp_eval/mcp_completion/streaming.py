"""Lossless aggregation helpers for streamed LLM responses."""

from __future__ import annotations

import inspect
import os
from typing import Any, Iterable

import litellm


class IncompleteModelStream(RuntimeError):
    """The provider stream ended without a complete assistant response."""


def llm_streaming_enabled() -> bool:
    """Return whether completion and scoring should use streamed responses."""
    return os.getenv("LLM_STREAMING_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _chunk_has_finish_reason(chunk: Any) -> bool:
    choices = (
        chunk.get("choices")
        if isinstance(chunk, dict)
        else getattr(chunk, "choices", None)
    ) or []
    return any(
        (
            choice.get("finish_reason")
            if isinstance(choice, dict)
            else getattr(choice, "finish_reason", None)
        )
        is not None
        for choice in choices
    )


def _normalize_reasoning_alias(response: Any) -> Any:
    """Keep the provider's ``reasoning`` text in LiteLLM's canonical field.

    The stream builder only combines ``reasoning_content``. Normalize each
    delta before aggregation, retaining the original field and preferring an
    existing canonical value so providers emitting both do not get duplicated.
    """
    def get(value: Any, name: str) -> Any:
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

    for choice in get(response, "choices") or []:
        for field in ("delta", "message"):
            message = get(choice, field)
            if message is None:
                continue
            canonical = get(message, "reasoning_content")
            alias = get(message, "reasoning")
            if canonical in (None, "") and isinstance(alias, str) and alias:
                if isinstance(message, dict):
                    message["reasoning_content"] = alias
                else:
                    message.reasoning_content = alias
    return response


async def _normalize_raw_provider_chunks(stream: Any):
    """Normalize aliases before LiteLLM can discard reasoning-only chunks."""
    try:
        async for chunk in stream:
            yield _normalize_reasoning_alias(chunk)
    finally:
        close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


async def collect_litellm_response(
    response: Any,
    *,
    messages: Iterable[dict[str, Any]],
) -> Any:
    """Collect a LiteLLM async stream into a normal complete response.

    Reasoning, content, and tool-call arguments arrive as independent deltas.
    Downstream agent code must only see them after the assistant turn has been
    reconstructed completely. Providers that ignore ``stream=true`` and return
    a regular response remain compatible.
    """

    if not hasattr(response, "__aiter__"):
        return _normalize_reasoning_alias(response)

    if isinstance(response, litellm.CustomStreamWrapper):
        # The SDK filters deltas before yielding them to this collector. Its
        # non-empty check recognizes reasoning_content, but not reasoning.
        # fetch_stream also initializes lazily opened HTTP streams without
        # issuing a second model request.
        raw_stream = await response.fetch_stream()
        if hasattr(raw_stream, "__aiter__"):
            response.completion_stream = _normalize_raw_provider_chunks(raw_stream)

    chunks = [_normalize_reasoning_alias(chunk) async for chunk in response]
    if not chunks:
        raise IncompleteModelStream("model stream returned no chunks")
    if not any(_chunk_has_finish_reason(chunk) for chunk in chunks):
        # LiteLLM's builder can supply a default finish reason. Validate the
        # raw chunks first so a disconnected partial body is never accepted.
        raise IncompleteModelStream("model stream ended before a finish reason")

    complete = litellm.stream_chunk_builder(chunks, messages=list(messages))
    if complete is None or not getattr(complete, "choices", None):
        raise IncompleteModelStream(
            "model stream ended without a complete response"
        )
    if getattr(complete.choices[0], "finish_reason", None) is None:
        raise IncompleteModelStream("model stream ended before a finish reason")
    return complete
