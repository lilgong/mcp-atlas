"""Small, allow-listed HTTP client for MCP services using the egress relay."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import dataclass
from typing import Any


RELAY_URL = (os.getenv("PUBMED_RELAY_URL") or "").strip().rstrip("/")
RELAY_TOKEN = (os.getenv("PUBMED_RELAY_TOKEN") or "").strip()
RELAY_TIMEOUT = float(os.getenv("PUBMED_RELAY_TIMEOUT_SECONDS") or "90")


@dataclass(frozen=True)
class RelayResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    url: str


def enabled() -> bool:
    if bool(RELAY_URL) != bool(RELAY_TOKEN):
        raise RuntimeError("PUBMED_RELAY_URL and PUBMED_RELAY_TOKEN must be configured together")
    return bool(RELAY_URL)


def fetch(
    method: str,
    url: str,
    *,
    params: Any = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    allow_redirects: bool = True,
) -> RelayResponse:
    if not enabled():
        raise RuntimeError("egress relay is not configured")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "http":
        url = urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}{'&' if urllib.parse.urlsplit(url).query else '?'}{query}"
        params = None
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
        response = urllib.request.urlopen(request, timeout=RELAY_TIMEOUT)
    except urllib.error.HTTPError as exc:
        error = json.loads(exc.read() or b"{}")
        raise RuntimeError(str(error.get("error") or f"egress relay HTTP {exc.code}")) from None
    with response:
        result = json.loads(response.read())
    return RelayResponse(
        status_code=int(result["status_code"]),
        headers={str(key): str(value) for key, value in result.get("headers", {}).items()},
        body=base64.b64decode(result.get("body_base64", "")),
        url=str(result.get("url") or url),
    )
