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
from http.client import IncompleteRead
from typing import Any

import httpx


RELAY_URL = (os.getenv("MCP_RELAY_URL") or "").strip().rstrip("/")
RELAY_TOKEN = (os.getenv("MCP_RELAY_TOKEN") or "").strip()
RELAY_TIMEOUT = float(os.getenv("MCP_RELAY_TIMEOUT_SECONDS") or "90")


class RelayNetworkError(RuntimeError):
    """A transient Relay transport/upstream failure, safe for endpoint fallback."""


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


def fetch(
    method: str,
    url: str,
    *,
    params: Any = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    allow_redirects: bool = True,
    timeout_seconds: float | None = None,
) -> RelayResponse:
    if not enabled():
        raise RuntimeError("egress relay is not configured")
    timeout = RELAY_TIMEOUT
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Relay request timeout must be positive and finite")
        timeout = min(timeout, timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("MCP_RELAY_TIMEOUT_SECONDS must be positive and finite")
    # Queue, retries and upstream I/O must finish before the caller's own deadline.
    budget = max(timeout * 0.9, timeout - 5.0)
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
            "timeout_seconds": budget,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{RELAY_URL}/v1/fetch",
        data=payload,
        headers={
            "Authorization": f"Bearer {RELAY_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
        with response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        error = json.loads(exc.read() or b"{}")
        message = str(error.get("error") or f"egress relay HTTP {exc.code}")
        # Keep account/authentication failures fatal even if a gateway happens
        # to wrap their structured response in a 5xx status.
        if exc.code in {502, 503, 504} and error.get("code") != "relay_account_error":
            raise RelayNetworkError(message) from None
        raise RuntimeError(message) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
        # Transport exceptions may include authenticated URLs. Emit only the
        # exception class, never their original message or chained traceback.
        raise RelayNetworkError(f"Relay network request failed: {type(exc).__name__}") from None
    return RelayResponse(
        status_code=int(result["status_code"]),
        headers={str(key): str(value) for key, value in result.get("headers", {}).items()},
        body=base64.b64decode(result.get("body_base64", "")),
        url=str(result.get("url") or url),
    )
