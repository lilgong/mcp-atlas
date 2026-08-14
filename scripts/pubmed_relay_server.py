#!/usr/bin/env python3
"""Authenticated, rate-limited HTTP egress relay for NCBI E-utilities/PMC."""

from __future__ import annotations

import base64
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ALLOWED_TARGETS = {
    "eutils.ncbi.nlm.nih.gov": "/entrez/eutils/",
    "www.ncbi.nlm.nih.gov": "/pmc/articles/",
}
TOKEN = (os.getenv("PUBMED_RELAY_TOKEN") or "").strip()
BIND = (os.getenv("PUBMED_RELAY_BIND") or "127.0.0.1").strip()
PORT = int(os.getenv("PUBMED_RELAY_PORT") or "3985")
MIN_INTERVAL = float(os.getenv("PUBMED_RELAY_MIN_INTERVAL_SECONDS") or "1.0")
UPSTREAM_TIMEOUT = float(os.getenv("PUBMED_RELAY_UPSTREAM_TIMEOUT_SECONDS") or "45")
MAX_RESPONSE_BYTES = int(os.getenv("PUBMED_RELAY_MAX_RESPONSE_BYTES") or str(64 * 1024 * 1024))
LOG_PATH = Path(os.getenv("PUBMED_RELAY_USAGE_LOG") or "/var/log/pubmed-relay/usage.jsonl")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class Controller:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_started = 0.0
        self.blocked_until = 0.0
        self.blocked_response: tuple[int, dict[str, str], bytes, str] | None = None

    def _wait(self) -> None:
        now = time.monotonic()
        delay = max(
            self.last_started + MIN_INTERVAL - now,
            self.blocked_until - now,
            0.0,
        )
        if delay:
            time.sleep(delay)
        self.last_started = time.monotonic()

    def fetch(self, url: str, allow_redirects: bool) -> tuple[int, dict[str, str], bytes, str, int, float]:
        started = time.monotonic()
        with self.lock:
            queued_ms = (time.monotonic() - started) * 1000
            if time.monotonic() < self.blocked_until and self.blocked_response:
                return (*self.blocked_response, 0, queued_ms)
            self.blocked_response = None
            final: tuple[int, dict[str, str], bytes, str] | None = None
            for attempt in range(3):
                self._wait()
                final = _fetch_once(url, allow_redirects)
                status, headers, _, _ = final
                if _is_abuse_response(final):
                    self.blocked_until = time.monotonic() + 300.0
                    self.blocked_response = final
                    return (*final, attempt + 1, queued_ms)
                if status != 429 and status < 500:
                    return (*final, attempt + 1, queued_ms)
                if attempt < 2:
                    retry_after = _retry_after(headers)
                    self.blocked_until = time.monotonic() + max(retry_after, 5.0 * (2**attempt))
            assert final is not None
            if final[0] == 429:
                self.blocked_until = time.monotonic() + max(_retry_after(final[1]), 60.0)
                self.blocked_response = final
            return (*final, 3, queued_ms)


CONTROLLER = Controller()


def _retry_after(headers: dict[str, str]) -> float:
    try:
        return max(float(headers.get("Retry-After", "0")), 0.0)
    except ValueError:
        return 0.0


def _is_abuse_response(response: tuple[int, dict[str, str], bytes, str]) -> bool:
    _status, headers, body, final_url = response
    location = headers.get("Location", "").lower()
    sample = body[:4096].lower()
    return (
        "misuse.ncbi.nlm.nih.gov" in location
        or "misuse.ncbi.nlm.nih.gov" in final_url.lower()
        or b"blocked for possible abuse" in sample
    )


def _validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    prefix = ALLOWED_TARGETS.get((parsed.hostname or "").lower())
    if parsed.scheme != "https" or parsed.port not in (None, 443) or not prefix or not parsed.path.startswith(prefix):
        raise ValueError("target is not an allowed NCBI E-utilities/PMC endpoint")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("target URL contains forbidden components")


def _fetch_once(url: str, allow_redirects: bool) -> tuple[int, dict[str, str], bytes, str]:
    opener = urllib.request.build_opener() if allow_redirects else urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "mcp_atlas_pubmed_relay/1.0"})
    try:
        response = opener.open(request, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("upstream response exceeds relay size limit")
        return int(response.status), dict(response.headers.items()), body, response.geturl()


def _append_log(event: dict[str, Any]) -> None:
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(line)


class Handler(BaseHTTPRequestHandler):
    server_version = "PubMedRelay/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "min_interval_seconds": MIN_INTERVAL})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        started = time.monotonic()
        if self.path != "/v1/fetch":
            self._json(404, {"error": "not found"})
            return
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {TOKEN}"
        if not TOKEN or not hmac.compare_digest(supplied, expected):
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 65536:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            url = str(payload["url"])
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            _validate_url(url)
            query = urllib.parse.urlencode(params, doseq=True)
            if query:
                separator = "&" if urllib.parse.urlsplit(url).query else "?"
                url = f"{url}{separator}{query}"
            status, headers, body, final_url, attempts, queued_ms = CONTROLLER.fetch(
                url, bool(payload.get("allow_redirects", False))
            )
            self._json(200, {
                "status_code": status,
                "headers": headers,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "url": final_url,
            })
            parsed = urllib.parse.urlsplit(url)
            _append_log({
                "client": self.client_address[0],
                "host": parsed.hostname,
                "path": parsed.path,
                "status": status,
                "response_bytes": len(body),
                "attempts": attempts,
                "queued_ms": round(queued_ms, 1),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(502, {"error": f"upstream request failed: {exc}"})


def main() -> None:
    if not TOKEN:
        raise SystemExit("PUBMED_RELAY_TOKEN must be set")
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"PubMed relay listening on http://{BIND}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
