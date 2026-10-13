"""Route the official weather-data MCP HTTP client through Yibu when configured.

The upstream ``weather-mcp-server`` remains responsible for MCP tool names,
schemas, argument validation, response parsing, and error conversion.  This
module only rewrites its requests from ``api.weatherapi.com`` to Yibu and swaps
the query-string WeatherAPI key for a Bearer token.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


OFFICIAL_ORIGIN = "https://api.weatherapi.com"
DEFAULT_MCP_TOOL_BASE_URL = "https://yibuapi.com"


def _yibu_base_url() -> str:
    return (os.getenv("MCP_TOOL_BASE_URL") or DEFAULT_MCP_TOOL_BASE_URL).rstrip("/")


def _yibu_key() -> str:
    return (os.getenv("WEATHER_YIBU_API_KEY") or "").strip()


# uvx starts a separate environment; load the audit helper beside this preload.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from usage_audit import start_usage, usage_event


def _rewrite_request(url: object, kwargs: dict) -> tuple[str, dict]:
    key = _yibu_key()
    value = str(url)
    parsed = urlsplit(value)
    if not key or f"{parsed.scheme}://{parsed.netloc}" != OFFICIAL_ORIGIN:
        return value, kwargs
    if not parsed.path.startswith("/v1/"):
        raise RuntimeError(f"unsupported WeatherAPI path: {parsed.path}")

    rewritten = dict(kwargs)
    params = dict(rewritten.get("params") or {})
    params.pop("key", None)
    headers = dict(rewritten.get("headers") or {})
    headers["Authorization"] = f"Bearer {key}"
    rewritten["params"] = params
    rewritten["headers"] = headers
    endpoint = parsed.path.removeprefix("/v1/")
    return f"{_yibu_base_url()}/weatherapi/v1/{endpoint}", rewritten


def _install() -> None:
    key = _yibu_key()
    if not key:
        return

    # The official server checks this variable before it calls HTTPX.  Supplying
    # the Yibu token here satisfies that check; the token is removed from query
    # parameters by ``_rewrite_request`` and is sent only as a Bearer header.
    os.environ["WEATHER_API_KEY"] = key

    import httpx

    original_get = httpx.AsyncClient.get
    if getattr(original_get, "_mcp_atlas_weatherapi_yibu", False):
        return

    async def yibu_get(client, url, *args, **kwargs):
        rewritten_url, rewritten_kwargs = _rewrite_request(url, kwargs)
        if rewritten_url == str(url):
            return await original_get(client, url, *args, **kwargs)
        request_id = start_usage("weatherapi", key)
        started = time.monotonic()
        status = 0
        error_name = None
        try:
            response = await original_get(
                client, rewritten_url, *args, **rewritten_kwargs
            )
            status = response.status_code
            return response
        except Exception as exc:
            error_name = type(exc).__name__
            raise
        finally:
            usage_event(
                "weatherapi", key, "finished", request_id,
                status=status, duration_ms=int((time.monotonic() - started) * 1000),
                error=error_name,
            )

    yibu_get._mcp_atlas_weatherapi_yibu = True
    httpx.AsyncClient.get = yibu_get


_install()
