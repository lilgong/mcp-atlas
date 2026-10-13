"""Small, allow-listed HTTP client for MCP services using the egress relay."""

from __future__ import annotations

import base64
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import httpx


RELAY_URL = (os.getenv("MCP_RELAY_URL") or "").strip().rstrip("/")
RELAY_TOKEN = (os.getenv("MCP_RELAY_TOKEN") or "").strip()
RELAY_TIMEOUT = float(os.getenv("MCP_RELAY_TIMEOUT_SECONDS") or "90")


class RelayRequestError(RuntimeError):
    """Classified relay failure; it is not retried outside the relay."""

    def __init__(self, message: str, *, code: str, status: int):
        self.code = code
        self.status = status
        super().__init__(f"{code}: {message}")


def request_budget_seconds() -> float:
    if not math.isfinite(RELAY_TIMEOUT) or RELAY_TIMEOUT <= 0:
        raise ValueError("MCP_RELAY_TIMEOUT_SECONDS must be positive and finite")
    # Reserve response-delivery time before the HTTP client's own timeout.
    return max(RELAY_TIMEOUT * 0.9, RELAY_TIMEOUT - 5.0)


@dataclass(frozen=True)
class RelayResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    url: str


def enabled() -> bool:
    if bool(RELAY_URL) != bool(RELAY_TOKEN):
        raise RuntimeError("MCP_RELAY_URL and MCP_RELAY_TOKEN must be configured together")
    return bool(RELAY_URL)


def _request(
    method: str,
    url: str,
    *,
    params: Any = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    allow_redirects: bool = True,
) -> urllib.request.Request:
    if not enabled():
        raise RuntimeError("egress relay is not configured")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "http":
        url = urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}{'&' if urllib.parse.urlsplit(url).query else '?'}{query}"
        params = None
    # Match normal httpx URL handling even for callers that supply a raw URL.
    # Reapplying this conversion preserves existing escapes and arXiv's +TO+.
    url = str(httpx.URL(url))
    if body is not None and not isinstance(body, bytes):
        if isinstance(body, str):
            body = body.encode()
        elif isinstance(body, dict):
            body = urllib.parse.urlencode(body, doseq=True).encode()
        else:
            raise TypeError("relay request body must be bytes, text, or a form mapping")
    payload = json.dumps(
        {
            "method": method.upper(),
            "url": url,
            "params": {},
            "body_base64": base64.b64encode(body or b"").decode("ascii"),
            "headers": headers or {},
            "allow_redirects": allow_redirects,
            "timeout_seconds": request_budget_seconds(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return urllib.request.Request(
        f"{RELAY_URL}/v1/fetch",
        data=payload,
        headers={
            "Authorization": f"Bearer {RELAY_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )


def _decode_response(result: dict[str, Any], url: str) -> RelayResponse:
    return RelayResponse(
        status_code=int(result["status_code"]),
        headers={str(key): str(value) for key, value in result.get("headers", {}).items()},
        body=base64.b64decode(result.get("body_base64", "")),
        url=str(result.get("url") or url),
    )


def fetch(method: str, url: str, **kwargs) -> RelayResponse:
    request = _request(method, url, **kwargs)
    try:
        response = urllib.request.urlopen(request, timeout=RELAY_TIMEOUT)
    except urllib.error.HTTPError as exc:
        error = json.loads(exc.read() or b"{}")
        raise RelayRequestError(
            str(error.get("error") or f"egress relay HTTP {exc.code}"),
            code=str(error.get("code") or "relay_http_error"),
            status=exc.code,
        ) from None
    with response:
        result = json.loads(response.read())
    return _decode_response(result, url)


async def fetch_async(method: str, url: str, **kwargs) -> RelayResponse:
    """Native async HTTP closes the relay connection when a tool is cancelled."""
    request = _request(method, url, **kwargs)
    async with httpx.AsyncClient(timeout=RELAY_TIMEOUT, follow_redirects=False) as client:
        response = await client.post(
            request.full_url, content=request.data, headers=dict(request.header_items()),
        )
        result = response.json()
        if response.is_error:
            raise RelayRequestError(
                str(result.get("error") or f"egress relay HTTP {response.status_code}"),
                code=str(result.get("code") or "relay_http_error"),
                status=response.status_code,
            )
        return _decode_response(result, url)
