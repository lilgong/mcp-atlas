#!/usr/bin/env python3
"""Authenticated, allow-listed NCBI/Wikipedia residential egress relay."""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
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
    "eutils.ncbi.nlm.nih.gov": ("ncbi", "/entrez/eutils/"),
    "www.ncbi.nlm.nih.gov": ("ncbi", "/pmc/articles/"),
    "en.wikipedia.org": ("wikipedia", "/w/api.php"),
}
TOKEN = (os.getenv("PUBMED_RELAY_TOKEN") or "").strip()
IPWO_PROXY_HOST = (os.getenv("IPWO_PROXY_HOST") or "").strip()
IPWO_PROXY_PORT = (os.getenv("IPWO_PROXY_PORT") or "").strip()
IPWO_PROXY_USERNAME = (os.getenv("IPWO_PROXY_USERNAME") or "").strip()
IPWO_PROXY_PASSWORD = (os.getenv("IPWO_PROXY_PASSWORD") or "").strip()
IPWO_PROXY_COUNTRY = (os.getenv("IPWO_PROXY_COUNTRY") or "").strip().upper()
BIND = (os.getenv("PUBMED_RELAY_BIND") or "127.0.0.1").strip()
PORT = int(os.getenv("PUBMED_RELAY_PORT") or "3985")
MIN_INTERVAL = float(os.getenv("PUBMED_RELAY_MIN_INTERVAL_SECONDS") or "1.0")
UPSTREAM_TIMEOUT = float(os.getenv("PUBMED_RELAY_UPSTREAM_TIMEOUT_SECONDS") or "45")
MAX_RESPONSE_BYTES = int(os.getenv("PUBMED_RELAY_MAX_RESPONSE_BYTES") or str(64 * 1024 * 1024))
LOG_PATH = Path(os.getenv("PUBMED_RELAY_USAGE_LOG") or "/var/log/pubmed-relay/usage.jsonl")
WIKIPEDIA_LANES = 4


class RelayAccountError(RuntimeError):
    """The configured egress account cannot serve further requests."""


class RelayUpstreamError(RuntimeError):
    """A proxy or target connection failed without an HTTP response."""


def _missing_ipwo_config() -> tuple[str, ...]:
    values = {
        "IPWO_PROXY_HOST": IPWO_PROXY_HOST,
        "IPWO_PROXY_PORT": IPWO_PROXY_PORT,
        "IPWO_PROXY_USERNAME": IPWO_PROXY_USERNAME,
        "IPWO_PROXY_PASSWORD": IPWO_PROXY_PASSWORD,
    }
    return tuple(name for name, value in values.items() if not value)


def _validate_ipwo_config() -> None:
    missing = _missing_ipwo_config()
    if missing:
        raise ValueError(
            "IPWO proxy configuration is incomplete; missing " + ", ".join(missing)
        )


def _new_ipwo_session_id() -> str:
    return str(secrets.randbelow(90_000_000) + 10_000_000)


def _ipwo_username_for_request(session_id: str | None = None) -> str:
    username = IPWO_PROXY_USERNAME
    if IPWO_PROXY_COUNTRY:
        username = re.sub(
            r"_zone_[^_]+",
            f"_zone_{IPWO_PROXY_COUNTRY}",
            username,
            count=1,
        )
    session_id = session_id or _new_ipwo_session_id()
    if re.search(r"_sid_[^_]+", username):
        username = re.sub(
            r"_sid_[^_]+",
            f"_sid_{session_id}",
            username,
            count=1,
        )
    else:
        # A dashboard-generated rotating username has no sid/time suffix.
        # Pin it for ten minutes so one API workflow keeps a stable identity;
        # Controller rotates the sid immediately when this exit actually fails.
        username = f"{username}_sid_{session_id}_time_10"
    return username


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class Controller:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_started = 0.0
        self.blocked_until = 0.0
        self.account_error: str | None = None
        self.session_id = _new_ipwo_session_id()
        self.rotation_count = 0

    def current_account_error(self) -> str | None:
        with self.lock:
            return self.account_error

    def _wait_for_slot(self) -> None:
        """Pace request starts without serializing their network wait time."""
        while True:
            with self.lock:
                if self.account_error:
                    raise RelayAccountError(self.account_error)
                now = time.monotonic()
                delay = max(
                    self.last_started + MIN_INTERVAL - now,
                    self.blocked_until - now,
                    0.0,
                )
                if delay <= 0:
                    self.last_started = now
                    return
            time.sleep(delay)

    def _set_cooldown(self, seconds: float) -> None:
        with self.lock:
            until = time.monotonic() + max(seconds, 0.0)
            self.blocked_until = max(self.blocked_until, until)

    def _latch_account_error(self, message: str) -> None:
        with self.lock:
            if not self.account_error:
                self.account_error = message
            error = self.account_error
        raise RelayAccountError(error)

    def _current_session_id(self) -> str:
        with self.lock:
            return self.session_id

    def _rotate_session(self) -> None:
        with self.lock:
            previous = self.session_id
            while self.session_id == previous:
                self.session_id = _new_ipwo_session_id()
            self.rotation_count += 1

    def current_rotation_count(self) -> int:
        with self.lock:
            return self.rotation_count

    def fetch(self, url: str, allow_redirects: bool) -> tuple[int, dict[str, str], bytes, str, int, float]:
        started = time.monotonic()
        queued_ms: float | None = None
        final: tuple[int, dict[str, str], bytes, str] | None = None
        for attempt in range(3):
            self._wait_for_slot()
            if queued_ms is None:
                queued_ms = (time.monotonic() - started) * 1000
            try:
                final = _fetch_once(
                    url,
                    allow_redirects,
                    session_id=self._current_session_id(),
                )
            except RelayAccountError as exc:
                self._latch_account_error(str(exc))
            except RelayUpstreamError:
                if attempt == 2:
                    raise
                self._rotate_session()
                self._set_cooldown(5.0 * (2**attempt))
                continue
            status, headers, _, _ = final
            if status == 407:
                self._latch_account_error(
                    "IPWO_PROXY_AUTH_FAILED: IPWO rejected the proxy credential"
                )
            if _is_abuse_response(final):
                if attempt < 2:
                    self._rotate_session()
                    self._set_cooldown(5.0 * (2**attempt))
                    continue
                return (*final, attempt + 1, queued_ms)
            if status != 429 and status < 500:
                return (*final, attempt + 1, queued_ms)
            if attempt < 2:
                self._rotate_session()
                retry_after = _retry_after(headers)
                self._set_cooldown(max(retry_after, 5.0 * (2**attempt)))
        assert final is not None
        if final[0] == 429:
            cooldown = max(_retry_after(final[1]), 60.0)
            self._set_cooldown(cooldown)
        return (*final, 3, queued_ms or 0.0)


class ControllerPool:
    """Round-robin requests across independent sticky residential exits."""

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("controller pool size must be positive")
        self.controllers = tuple(Controller() for _ in range(size))
        self.lock = threading.Lock()
        self.next_index = 0
        self.account_error: str | None = None

    def current_account_error(self) -> str | None:
        with self.lock:
            if self.account_error:
                return self.account_error
        return next(
            (
                error
                for controller in self.controllers
                if (error := controller.current_account_error())
            ),
            None,
        )

    def current_rotation_count(self) -> int:
        return sum(
            controller.current_rotation_count()
            for controller in self.controllers
        )

    def fetch(
        self, url: str, allow_redirects: bool,
    ) -> tuple[int, dict[str, str], bytes, str, int, float]:
        with self.lock:
            if self.account_error:
                raise RelayAccountError(self.account_error)
            controller = self.controllers[self.next_index]
            self.next_index = (self.next_index + 1) % len(self.controllers)
        try:
            return controller.fetch(url, allow_redirects)
        except RelayAccountError as exc:
            with self.lock:
                if not self.account_error:
                    self.account_error = str(exc)
                error = self.account_error
            raise RelayAccountError(error) from None


CONTROLLERS = {
    "ncbi": Controller(),
    # Wikipedia Action API calls are stateless. Four independently paced,
    # sticky exits prevent Atlas evaluation and P2 from building a multi-minute
    # queue behind one residential IP. NCBI remains single-exit because one
    # PubMed workflow benefits from a stable identity.
    "wikipedia": ControllerPool(WIKIPEDIA_LANES),
}


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


def _validate_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    target = ALLOWED_TARGETS.get((parsed.hostname or "").lower())
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or not target
        or not parsed.path.startswith(target[1])
    ):
        raise ValueError("target is not an allowed NCBI/Wikipedia API endpoint")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("target URL contains forbidden components")
    return target[0]


def _proxy_tunnel_status(exc: Exception) -> int | None:
    reason = str(getattr(exc, "reason", exc))
    match = re.search(r"Tunnel connection failed:\s*(\d{3})\b", reason)
    return int(match.group(1)) if match else None


def _fetch_once(
    url: str,
    allow_redirects: bool,
    *,
    session_id: str | None = None,
) -> tuple[int, dict[str, str], bytes, str]:
    # Proxy credentials stay in this relay and are never passed into task
    # containers. IPWO tunnels the target TLS connection normally.
    proxy_url = (
        "http://"
        f"{urllib.parse.quote(_ipwo_username_for_request(session_id), safe='')}:"
        f"{urllib.parse.quote(IPWO_PROXY_PASSWORD, safe='')}@"
        f"{IPWO_PROXY_HOST}:{IPWO_PROXY_PORT}"
    )
    handlers: list[Any] = [
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    ]
    if not allow_redirects:
        handlers.append(NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": "mcp_atlas_egress_relay/1.0"})
    try:
        response = opener.open(request, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as exc:
        response = exc
    except Exception as exc:
        tunnel_status = _proxy_tunnel_status(exc)
        if tunnel_status in {401, 407}:
            raise RelayAccountError(
                "IPWO_PROXY_AUTH_FAILED: IPWO rejected the proxy credential"
            ) from None
        if tunnel_status == 403:
            raise RelayAccountError(
                "IPWO_PROXY_ACCESS_DENIED: IPWO denied the proxy tunnel; "
                "check subaccount status, traffic allocation, and whitelist"
            ) from None
        if tunnel_status == 431:
            raise RelayAccountError(
                "IPWO_PROXY_ACCOUNT_LIMIT: IPWO rejected the proxy tunnel; "
                "check subaccount traffic allocation and session settings"
            ) from None
        # Proxy exceptions can embed the authenticated proxy URL. Preserve only
        # the exception type for retry/diagnostics so credentials never leak.
        raise RelayUpstreamError(type(exc).__name__) from None
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

    def _json(self, status: int, payload: dict[str, Any]) -> bool:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError):
            # The task was cancelled or its timeout expired while the relay was
            # still working. This is not a relay/upstream failure and there is
            # no live connection on which a second error response could work.
            return False

    def do_GET(self) -> None:
        if self.path == "/health":
            account_error = next(
                (
                    error
                    for controller in CONTROLLERS.values()
                    if (error := controller.current_account_error())
                ),
                None,
            )
            if account_error:
                self._json(503, {"status": "blocked", "error": account_error})
            else:
                self._json(200, {
                    "status": "ok",
                    "egress_backend": "ipwo",
                    "min_interval_seconds": MIN_INTERVAL,
                    "ip_policy": "sticky_pool_until_failure",
                    "lane_counts": {
                        "ncbi": 1,
                        "wikipedia": WIKIPEDIA_LANES,
                    },
                    "rotation_counts": {
                        name: controller.current_rotation_count()
                        for name, controller in CONTROLLERS.items()
                    },
                })
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
            target_group = _validate_url(url)
            query = urllib.parse.urlencode(params, doseq=True)
            if query:
                separator = "&" if urllib.parse.urlsplit(url).query else "?"
                url = f"{url}{separator}{query}"
            status, headers, body, final_url, attempts, queued_ms = CONTROLLERS[
                target_group
            ].fetch(
                url, bool(payload.get("allow_redirects", False))
            )
            delivered = self._json(200, {
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
                "egress_backend": "ipwo",
                "attempts": attempts,
                "queued_ms": round(queued_ms, 1),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "client_disconnected": not delivered,
            })
        except RelayAccountError as exc:
            parsed = urllib.parse.urlsplit(url)
            _append_log({
                "client": self.client_address[0],
                "host": parsed.hostname,
                "path": parsed.path,
                "status": 402,
                "egress_backend": "ipwo",
                "account_error_code": str(exc).split(":", 1)[0],
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            })
            self._json(402, {
                "code": "relay_account_error",
                "error": str(exc),
            })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            # URL-opening exceptions can include the authenticated proxy URL.
            # Return only the exception class so credentials never reach logs or
            # MCP trajectories.
            _append_log({
                "client": self.client_address[0],
                "status": 502,
                "egress_backend": "ipwo",
                "error_type": type(exc).__name__,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            })
            self._json(502, {"error": f"upstream request failed: {type(exc).__name__}"})


def main() -> None:
    if not TOKEN:
        raise SystemExit("PUBMED_RELAY_TOKEN must be set")
    try:
        _validate_ipwo_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(
        f"NCBI/Wikipedia relay (ipwo) listening on http://{BIND}:{PORT}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
