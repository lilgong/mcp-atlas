"""Route the official weather-data MCP HTTP client through Yibu when configured.

The upstream ``weather-mcp-server`` remains responsible for MCP tool names,
schemas, argument validation, response parsing, and error conversion.  This
module only rewrites its requests from ``api.weatherapi.com`` to Yibu and swaps
the query-string WeatherAPI key for a Bearer token.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


OFFICIAL_ORIGIN = "https://api.weatherapi.com"
YIBU_BASE_URL = "https://yibuapi.com/weatherapi/v1"


def _yibu_key() -> str:
    return (os.getenv("WEATHER_YIBU_API_KEY") or "").strip()


def _usage_log(
    key: str,
    url: str,
    status: int,
    duration_ms: int,
    error_name: str | None,
) -> None:
    try:
        now = datetime.now(timezone.utc)
        suffix = key[-8:] if key else "no-key"
        directory = Path(
            os.getenv("MCP_USAGE_LOG_DIR") or "mcp_usage_log"
        ) / now.strftime("%Y-%m")
        directory.mkdir(parents=True, exist_ok=True)
        parsed = urlsplit(url)
        path = directory / (
            f"weatherapi_{suffix}_{now.strftime('%Y%m%d')}.jsonl"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": now.isoformat().replace("+00:00", "Z"),
                "service": "weatherapi",
                "key_suffix": suffix,
                "host": parsed.hostname,
                "path": parsed.path,
                "status": status or 0,
                "duration_ms": duration_ms,
                "error": error_name,
                "task_id": os.getenv("MCP_TASK_ID") or None,
            }, ensure_ascii=False) + "\n")
    except Exception as exc:  # usage accounting must not change MCP behavior
        print(f"[mcp-usage-log] {exc}", file=sys.stderr)


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
    return f"{YIBU_BASE_URL}/{endpoint}", rewritten


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
            _usage_log(
                key,
                rewritten_url,
                status,
                int((time.monotonic() - started) * 1000),
                error_name,
            )

    yibu_get._mcp_atlas_weatherapi_yibu = True
    httpx.AsyncClient.get = yibu_get


_install()
