"""Preserve safe TwelveData errors across the upstream MCP layer.

The pinned MCP server calls ``raise_for_status()`` before reading the JSON
error body.  Consequently, both a recoverable per-minute 429 and an exhausted
daily allowance are exposed as the same generic HTTP error.  Keep the upstream
tools and schemas unchanged, but emit a stable marker when the provider
explicitly says that the daily credit allowance is exhausted.  Other HTTP
errors are also reduced to a status and provider message because httpx's
default exception includes the request URL, including its ``apikey`` query.
"""

from __future__ import annotations

from importlib.metadata import version
from typing import Any

import httpx


EXPECTED_TWELVEDATA_MCP_VERSION = "0.2.5"
DAILY_CREDITS_EXHAUSTED_MARKER = "TWELVEDATA_DAILY_CREDITS_EXHAUSTED"


def daily_credit_error(response: Any) -> str | None:
    """Return a safe fatal marker only for an explicit daily-limit response."""
    if getattr(response, "status_code", None) != 429:
        return None
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    message = str(payload.get("message") or "")
    folded = message.casefold()
    if "run out of api credits for the day" not in folded:
        return None
    return (
        f"{DAILY_CREDITS_EXHAUSTED_MARKER}: "
        "TwelveData daily API credit limit reached"
    )


def safe_upstream_error(response: Any) -> str | None:
    """Return a credential-free error for a failed TwelveData API request."""
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or status < 400:
        return None
    request = getattr(response, "request", None)
    url = getattr(request, "url", None)
    if getattr(url, "host", None) != "api.twelvedata.com":
        return None

    daily_error = daily_credit_error(response)
    if daily_error:
        return daily_error

    message = ""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
    suffix = f": {message[:300]}" if message else ""
    return f"TwelveData upstream HTTP {status}{suffix}"


def install_daily_credit_patch() -> None:
    """Patch only the HTTP error boundary used by the pinned upstream server."""
    actual_version = version("mcp-server-twelve-data")
    if actual_version != EXPECTED_TWELVEDATA_MCP_VERSION:
        raise RuntimeError(
            "TwelveData compatibility patch requires "
            f"mcp-server-twelve-data=={EXPECTED_TWELVEDATA_MCP_VERSION}, "
            f"found {actual_version}"
        )

    original_raise_for_status = httpx.Response.raise_for_status

    def raise_for_status(response: httpx.Response) -> httpx.Response:
        error = safe_upstream_error(response)
        if error:
            raise RuntimeError(error)
        return original_raise_for_status(response)

    httpx.Response.raise_for_status = raise_for_status


def install_market_state_patch() -> None:
    """Keep the validated market-state JSON in text, without an invalid list envelope.

    The pinned tool returns a RootModel[list]. FastMCP validates that model,
    but then puts its dumped list in structuredContent, which only accepts
    objects. An explicit text result avoids that incompatible envelope while
    preserving the original tool schema, model validation and JSON text.
    """
    from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
    from mcp.types import CallToolResult
    from mcp_server_twelve_data.response_models import GetMarketState200Response

    original_convert = FuncMetadata.convert_result
    if getattr(original_convert, "_twelvedata_market_state_patch", False):
        return

    def convert_result(metadata: Any, result: Any) -> Any:
        converted = original_convert(metadata, result)
        if (
            metadata.output_model is GetMarketState200Response
            and isinstance(converted, tuple)
            and len(converted) == 2
            and isinstance(converted[1], list)
        ):
            return CallToolResult(content=converted[0])
        return converted

    convert_result._twelvedata_market_state_patch = True
    FuncMetadata.convert_result = convert_result


def main() -> None:
    install_daily_credit_patch()
    install_market_state_patch()
    from mcp_server_twelve_data import main as upstream_main

    upstream_main()


if __name__ == "__main__":
    main()
