"""Recognize account failures that cannot recover within the current run."""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional


class FatalAccountError(RuntimeError):
    """A model or MCP credential cannot make further successful calls."""

    def __init__(
        self,
        message: str,
        *,
        source_kind: Optional[str] = None,
        source_name: Optional[str] = None,
        credential_envs: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.source_kind = source_kind
        self.source_name = source_name
        self.credential_envs = tuple(credential_envs)


_MCP_CREDENTIAL_ENVS = {
    "airtable": ("AIRTABLE_API_KEY",),
    "alchemy": ("ALCHEMY_API_KEY",),
    "brave-search": ("BRAVE_API_KEY",),
    "context7": ("CONTEXT7_API_KEY",),
    "e2b-server": ("E2B_API_KEY",),
    "exa": ("EXA_API_KEY",),
    "github": ("GITHUB_TOKEN",),
    "google-maps": ("GOOGLE_MAPS_API_KEY",),
    "google-workspace": (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
    ),
    "lara-translate": (
        "LARA_YIBU_API_KEY",
        "LARA_ACCESS_KEY_ID",
        "LARA_ACCESS_KEY_SECRET",
    ),
    "national-parks": ("NPS_API_KEY",),
    "notion": ("NOTION_TOKEN",),
    "oxylabs": ("OXYLABS_USERNAME", "OXYLABS_PASSWORD"),
    "pubmed": ("IPWO_PROXY_USERNAME", "IPWO_PROXY_PASSWORD"),
    "wikipedia": ("IPWO_PROXY_USERNAME", "IPWO_PROXY_PASSWORD"),
    "slack": ("SLACK_MCP_XOXC_TOKEN", "SLACK_MCP_XOXD_TOKEN"),
    "twelvedata": ("TWELVE_DATA_API_KEY",),
    "weather-data": ("WEATHER_YIBU_API_KEY", "WEATHER_API_KEY"),
}


def credential_envs_for_mcp_server(server: str) -> tuple[str, ...]:
    """Return public env names only; credential values are never included."""
    return _MCP_CREDENTIAL_ENVS.get(server, ())


def is_fatal_mcp_account_error(server: str, result: Any) -> bool:
    """Recognize account failures only for MCPs that actually use credentials.

    Local and public MCPs use words such as ``token``, ``authorization``, and
    ``quota`` in parsers, source code, and ordinary domain responses. They have
    no account that this runner could repair, so those strings must never stop
    a batch as an account failure.
    """
    if not credential_envs_for_mcp_server(server):
        return False

    # E2B can wrap this account-level HTTP 403 in an otherwise successful MCP
    # content envelope whose text starts with JSON rather than ``Error:``.
    # Keep the exception server-specific so an unrelated credentialed MCP that
    # merely mentions payment methods in ordinary content cannot stop a run.
    if server == "e2b-server":
        serialized = _serialize_tool_result(result).casefold()
        if "team is blocked: missing payment method" in serialized:
            return True

    return is_fatal_tool_result(result)


def describe_fatal_account_error(error: FatalAccountError) -> str:
    """Build a safe, actionable log line without exposing credential values."""
    parts = []
    if error.source_kind:
        parts.append(f"source={error.source_kind}")
    if error.source_name:
        parts.append(f"name={error.source_name}")
    if error.credential_envs:
        parts.append(f"credential_env={','.join(error.credential_envs)}")
    metadata = " ".join(parts)
    return f"{metadata}: {error}" if metadata else str(error)


_FATAL_ACCOUNT_MARKERS = (
    # Stable marker emitted by this service when the underlying provider text
    # is not safe or useful to expose to the outer batch runner.
    "fatal_account_error",
    "ipwo_proxy_auth_failed",
    "twelvedata_daily_credits_exhausted",
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
    "bad credentials",
    "this token has no access to model",
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
    "exceeded your credits limit",
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
    "team is blocked: missing payment method",
    "账户余额不足",
    "余额不足",
    "额度不足",
    "额度已用完",
    "配额已用完",
)

# Relay-owned markers are unambiguous even when an MCP serializes its failure
# as a JSON string inside an otherwise successful content block; it does not
# need to start with ``Error:`` to stop a run that cannot recover.
_STABLE_TOOL_ACCOUNT_MARKERS = (
    "ipwo_proxy_auth_failed",
    "twelvedata_daily_credits_exhausted",
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
    serialized_folded = serialized.casefold()
    if any(
        marker in serialized_folded
        for marker in _STABLE_TOOL_ACCOUNT_MARKERS
    ):
        return True
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
        "monthly quota reached",
        "monthly quota exceeded",
        "quota exceeded",
        "quota exhausted",
        "web_search_exa error",
    )
    return any(text.lstrip().casefold().startswith(error_prefixes) for text in texts)


def _serialize_tool_result(result: Any) -> str:
    """Serialize an MCP result for exact, server-scoped account markers."""
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif isinstance(result, dict):
        payload = result
    else:
        return str(result or "")
    return json.dumps(payload, ensure_ascii=False)
