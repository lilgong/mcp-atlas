"""Semantic validation for responses returned by the live Atlas agent."""

from typing import Any


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _assistant_message_is_empty(message: dict[str, Any]) -> bool:
    fields = (
        "content",
        "reasoning",
        "reasoning_content",
        "tool_calls",
        "function_call",
    )
    if any(_has_value(message.get(field)) for field in fields):
        return False
    original = message.get("original_message")
    if isinstance(original, dict) and any(
        _has_value(original.get(field)) for field in fields
    ):
        return False
    return True


def is_completely_empty_agent_response(payload: Any) -> bool:
    """Return true only when an HTTP-200 payload contains no agent work at all.

    An empty list is an empty response.  A non-empty payload qualifies only when
    every item is an assistant message and every assistant content, reasoning,
    and tool-call field is empty.  Tool messages, error events, and unknown
    response shapes deliberately make the result non-empty so a partially
    executed trajectory is never retried under this rule.
    """
    if payload == []:
        return True
    if not isinstance(payload, list) or not payload:
        return False

    for item in payload:
        if not isinstance(item, dict) or item.get("type") != "message":
            return False
        message = item.get("data")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return False
        if not _assistant_message_is_empty(message):
            return False
    return True
