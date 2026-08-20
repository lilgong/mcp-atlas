"""Lossless aggregation helpers for streamed LLM responses."""

from __future__ import annotations

import os
from typing import Any, Iterable

import litellm


class IncompleteModelStream(RuntimeError):
    """The provider stream ended without a complete assistant response."""


def llm_streaming_enabled() -> bool:
    """Return the single transport-mode switch shared by eval and scoring."""
    return os.getenv("LLM_STREAMING_ENABLED", "false").strip().lower() in {
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
    """Collect a LiteLLM async stream into its normal ``ModelResponse``.

    Tool calls and reasoning arrive as independent deltas.  The existing agent
    loop must only see them after LiteLLM has reconstructed the whole assistant
    turn.  A non-stream response is accepted for provider compatibility and to
    keep this boundary usable with gateways that ignore ``stream=true``.
    """

    if not hasattr(response, "__aiter__"):
        return response

    chunks = [chunk async for chunk in response]
    if not chunks:
        raise IncompleteModelStream("model stream returned no chunks")
    if not any(_chunk_has_finish_reason(chunk) for chunk in chunks):
        # LiteLLM's builder defaults a missing finish reason to ``stop``. Check
        # the raw chunks first so a disconnected partial body is never accepted
        # as a complete assistant answer.
        raise IncompleteModelStream(
            "model stream ended before a finish reason"
        )

    complete = litellm.stream_chunk_builder(chunks, messages=list(messages))
    if complete is None or not getattr(complete, "choices", None):
        raise IncompleteModelStream(
            "model stream ended without a complete response"
        )
    if getattr(complete.choices[0], "finish_reason", None) is None:
        raise IncompleteModelStream(
            "model stream ended before a finish reason"
        )
    return complete
