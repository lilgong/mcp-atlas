"""Recognize account failures that cannot recover within the current run."""

from __future__ import annotations

import json
from typing import Any


class FatalAccountError(RuntimeError):
    """A model or MCP credential cannot make further successful calls."""


_FATAL_ACCOUNT_MARKERS = (
    # Stable marker emitted by this service when the underlying provider text
    # is not safe or useful to expose to the outer batch runner.
    "fatal_account_error",
    # Authentication / credential failures.
    "authenticationerror",
    "invalid token",
    "token is invalid",
    "token has expired",
    "expired token",
    "invalid api key",
    "api key is invalid",
    "incorrect api key",
    "invalid credentials",
    "api key has expired",
    "api key expired",
    "invalid_auth",
    "authentication failed",
    "unauthorized",
    "401 unauthorized",
    # Explicit balance, credit, billing, or quota exhaustion. Generic 429 and
    # "rate limit exceeded" are intentionally absent: those can recover.
    "insufficient balance",
    "balance is insufficient",
    "insufficient funds",
    "insufficient credit",
    "not enough credits",
    "out of credits",
    "credits exhausted",
    "credit balance exhausted",
    "no credits remaining",
    "insufficient_quota",
    "quota exhausted",
    "payment required",
    "402 payment required",
    "exceeded your current quota",
    "quota has been exceeded",
    "quota exceeded",
    "exceeded quota",
    "monthly quota reached",
    "monthly quota exceeded",
    "usage limit has been reached",
    "billing hard limit has been reached",
    "账户余额不足",
    "余额不足",
    "额度不足",
    "额度已用完",
    "配额已用完",
)


def is_fatal_account_error(value: Any) -> bool:
    """Return whether an actual error describes unusable credentials/funds."""
    text = str(value or "").casefold()
    return any(marker in text for marker in _FATAL_ACCOUNT_MARKERS)


def is_fatal_tool_result(result: Any) -> bool:
    """Recognize account failures returned inside a successful MCP envelope.

    Some MCP servers return ``Error: ...`` as text with HTTP 200 and do not set
    ``is_error``. Requiring an error-shaped envelope/text avoids interpreting a
    normal search result that merely mentions account terminology as fatal.
    """
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif isinstance(result, dict):
        payload = result
    else:
        return False

    serialized = json.dumps(payload, ensure_ascii=False)
    if not is_fatal_account_error(serialized):
        return False
    if bool(payload.get("isError") or payload.get("is_error")):
        return True

    texts = [
        item.get("text", "")
        for item in payload.get("content", [])
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    error_prefixes = (
        "error:",
        "authentication error",
        "authorization error",
        "http error",
        "unauthorized",
        "payment required",
        "invalid token",
        "token is invalid",
        "invalid api key",
        "api key is invalid",
        "incorrect api key",
        "invalid credentials",
        "authentication failed",
        "insufficient balance",
        "insufficient funds",
        "insufficient credit",
        "not enough credits",
        "out of credits",
        "quota exceeded",
        "quota exhausted",
    )
    return any(text.lstrip().casefold().startswith(error_prefixes) for text in texts)
