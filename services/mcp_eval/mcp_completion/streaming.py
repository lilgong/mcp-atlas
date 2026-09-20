"""Lossless aggregation helpers for streamed LLM responses."""

from __future__ import annotations

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
        return response

    chunks = [chunk async for chunk in response]
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
