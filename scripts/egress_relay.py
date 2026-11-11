#!/usr/bin/env python3
"""Authenticated public HTTPS MCP residential egress relay."""

from __future__ import annotations

import base64
import contextlib
import hmac
import hashlib
import ipaddress
import http.client
import json
import math
import select
import socket
from collections import deque
import os
import re
import secrets
import uuid
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


# Known upstream hosts share category-wide quotas; paths are transparent to MCPs.
UPSTREAM_GROUPS = {
    "eutils.ncbi.nlm.nih.gov": "ncbi",
    "www.ncbi.nlm.nih.gov": "ncbi",
    "pmc.ncbi.nlm.nih.gov": "ncbi",
    "en.wikipedia.org": "wikipedia",
    "export.arxiv.org": "arxiv",
    "arxiv.org": "arxiv",
    "www.arxiv.org": "arxiv",
    "nominatim.openstreetmap.org": "osm-nominatim",
    "overpass-api.de": "osm-overpass",
    "overpass.private.coffee": "osm-overpass",
    "maps.mail.ru": "osm-overpass",
    "router.project-osrm.org": "osm-routing",
    "routing.openstreetmap.de": "osm-routing",
}
TOKEN = (os.getenv("MCP_RELAY_TOKEN") or "").strip()
CONTROL_TOKEN = (os.getenv("MCP_RELAY_CONTROL_TOKEN") or "").strip()
IPWO_PROXY_HOST = (os.getenv("IPWO_PROXY_HOST") or "").strip()
IPWO_PROXY_PORT = (os.getenv("IPWO_PROXY_PORT") or "").strip()
IPWO_PROXY_USERNAME = (os.getenv("IPWO_PROXY_USERNAME") or "").strip()
IPWO_PROXY_PASSWORD = (os.getenv("IPWO_PROXY_PASSWORD") or "").strip()
IPWO_PROXY_COUNTRY = (os.getenv("IPWO_PROXY_COUNTRY") or "").strip().upper()
BIND = (os.getenv("MCP_RELAY_BIND") or "127.0.0.1").strip()
PORT = int(os.getenv("MCP_RELAY_PORT") or "3985")
MIN_INTERVAL = float(os.getenv("MCP_RELAY_MIN_INTERVAL_SECONDS") or "1.0")
REQUEST_TIMEOUT = float(os.getenv("MCP_RELAY_REQUEST_TIMEOUT_SECONDS") or "85")
UPSTREAM_TIMEOUT = float(os.getenv("MCP_RELAY_UPSTREAM_TIMEOUT_SECONDS") or "45")
ARXIV_IMMEDIATE_TIMEOUT_RETRY = os.getenv("MCP_RELAY_ARXIV_IMMEDIATE_TIMEOUT_RETRY", "false").lower() in {"true", "1", "yes"}
MAX_RESPONSE_BYTES = int(os.getenv("MCP_RELAY_MAX_RESPONSE_BYTES") or str(64 * 1024 * 1024))
LOG_PATH = Path(os.getenv("MCP_RELAY_USAGE_LOG") or "./data/relay/usage.jsonl")
WIKIPEDIA_LANES = 1
LIMITS_PATH = LOG_PATH.parent / "limits.json"
RELAY_USER_AGENT = os.getenv("MCP_RELAY_USER_AGENT") or "MCPAtlasRelay/1.0"
CONFIG_LOCK = threading.Lock()


class RelayAccountError(RuntimeError):
    """The configured egress account cannot serve further requests."""


class RelayUpstreamError(RuntimeError):
    """A proxy or target connection failed without an HTTP response."""


class RelayTargetError(ValueError):
    """The URL or its resolved address is not a public HTTPS destination."""


class RelayEndpointCooling(RelayUpstreamError):
    """An Overpass node is cooling; the MCP can try its next configured node."""


class RelayUpstreamTimeout(RelayUpstreamError):
    """One network attempt timed out while its total request budget remains."""


class RelayRequestExpired(RuntimeError):
    """A request has exhausted its total queue/retry/network budget."""

    def __init__(self, phase: str):
        self.phase = phase
        super().__init__(f"Relay {phase} deadline exceeded")


class RelayRequestCancelled(RuntimeError):
    """The downstream caller disconnected before this request completed."""


class RequestBudget:
    def __init__(self, seconds: float, disconnected=None):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self.deadline = time.monotonic() + seconds
        self.disconnected = disconnected
        self.queued_ms = 0.0
        self.attempts = 0
        self.public_targets: dict[tuple[str, int], tuple[str, ...]] = {}
        self.request_id = uuid.uuid4().hex
        self.cooldown_origin: tuple[str, int] | None = None

    def remaining(self, phase="queue") -> float:
        if self.disconnected and self.disconnected():
            raise RelayRequestCancelled("Relay client disconnected")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RelayRequestExpired(phase)
        return remaining


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
    # Honor the operator's IPWO mode. A rotating username has no sid/time;
    # never silently turn it into a sticky session.
    if re.search(r"_sid_[^_]+", username):
        username = re.sub(
            r"_sid_[^_]+",
            f"_sid_{session_id or _new_ipwo_session_id()}",
            username,
            count=1,
        )
    return username


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, budget: RequestBudget | None = None):
        super().__init__()
        self.budget = budget

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _validate_url(newurl, self.budget)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Controller:
    def __init__(self, min_interval: float | None = None) -> None:
        self.lock = threading.Lock()
        self.last_started = 0.0
        self.blocked_until = 0.0
        self.node_blocked_until: dict[tuple[str, int], float] = {}
        self.account_error: str | None = None
        self.session_id = _new_ipwo_session_id()
        self.rotation_count = 0
        self.min_interval = MIN_INTERVAL if min_interval is None else min_interval
        self.max_concurrency: int | None = None
        self.active = 0
        self.slots = threading.Condition(self.lock)
        self.pending = deque()

    def current_account_error(self) -> str | None:
        with self.lock:
            return self.account_error

    def _wait_for_slot(self, budget: RequestBudget) -> None:
        """FIFO admission for each HTTP attempt, with bounded cancellable waits."""
        ticket = object()
        started = time.monotonic()
        with self.slots:
            self.pending.append(ticket)
            reported = False
            try:
                while True:
                    remaining = budget.remaining("queue")
                    if self.account_error:
                        raise RelayAccountError(self.account_error)
                    now = time.monotonic()
                    # A cooling Overpass node must not occupy the shared FIFO head.
                    node_until = self.node_blocked_until.get(budget.cooldown_origin, 0.0)
                    if node_until > now:
                        _append_log({"event": "endpoint_cooldown", "parent_request_id": budget.request_id,
                                     "host": budget.cooldown_origin[0],
                                     "retry_after_seconds": round(node_until - now, 3)})
                        raise RelayEndpointCooling("Overpass endpoint cooling down")
                    delay = max(self.last_started + self.min_interval - now,
                                self.blocked_until - now, 0.0)
                    capacity = self.max_concurrency is None or self.active < self.max_concurrency
                    if self.pending[0] is ticket and capacity and delay <= 0:
                        self.pending.popleft()
                        self.last_started = now
                        self.active += 1
                        self.slots.notify_all()
                        return
                    if not reported:
                        _append_log({
                            "event": "queue_wait", "parent_request_id": budget.request_id,
                            "active": self.active, "queued_ahead": self.pending.index(ticket),
                            "max_concurrency": self.max_concurrency,
                            "min_interval_ms": round(self.min_interval * 1000),
                            "interval_wait_ms": round(max(self.last_started + self.min_interval - now, 0) * 1000),
                            "cooldown_wait_ms": round(max(self.blocked_until - now, 0) * 1000),
                            "remaining_ms": round(remaining * 1000),
                        })
                        reported = True
                    self.slots.wait(timeout=min(remaining, delay or 0.2, 0.2))
            finally:
                budget.queued_ms += (time.monotonic() - started) * 1000
                if ticket in self.pending:
                    self.pending.remove(ticket)
                    self.slots.notify_all()

    def _set_cooldown(self, seconds: float, origin: tuple[str, int] | None = None) -> None:
        with self.lock:
            until = time.monotonic() + max(seconds, 0.0)
            if origin is None:
                self.blocked_until = max(self.blocked_until, until)
            else:
                self.node_blocked_until[origin] = max(self.node_blocked_until.get(origin, 0.0), until)
            self.slots.notify_all()

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

    def fetch(self, url: str, allow_redirects: bool, *, budget=None, **kwargs):
        budget = budget or RequestBudget(REQUEST_TIMEOUT)
        return self._fetch(url, allow_redirects, budget=budget, **kwargs)

    def _fetch(
        self, url: str, allow_redirects: bool, *, method: str = "GET",
        body: bytes | None = None, headers: dict[str, str] | None = None,
        budget: RequestBudget,
    ) -> tuple[int, dict[str, str], bytes, str, int, float]:
        final = None
        parsed_target = urllib.parse.urlsplit(url)
        metadata = {"parent_request_id": budget.request_id,
                    "host": parsed_target.hostname, "path": parsed_target.path}
        group = UPSTREAM_GROUPS.get(parsed_target.hostname)
        overpass = group == "osm-overpass"
        rotating_arxiv = group == "arxiv" and not re.search(r"_sid_[^_]+", IPWO_PROXY_USERNAME)
        # A rotating proxy uses a new exit for each request. A single failed exit
        # must not make an Overpass node unavailable to every other caller.
        cool_node = overpass and bool(re.search(r"_sid_[^_]+", IPWO_PROXY_USERNAME))
        origin = ((parsed_target.hostname or "").lower(), parsed_target.port or 443) if cool_node else None
        budget.cooldown_origin = origin
        # Overpass MCP already walks its configured fallback nodes. Do not spend
        # the entire per-node budget repeating a failing endpoint inside Relay.
        attempt_limit = 1 if overpass else 3
        immediate_timeouts = ARXIV_IMMEDIATE_TIMEOUT_RETRY and UPSTREAM_GROUPS.get(parsed_target.hostname) == "arxiv"
        def cooldown(seconds):
            if rotating_arxiv or (overpass and not cool_node):
                return
            if origin is None:
                self._set_cooldown(seconds)
            else:
                self._set_cooldown(seconds, origin)
            _append_log({**metadata, "event": "cooldown", "seconds": seconds,
                         "scope": "endpoint" if origin else "upstream_group"})

        for attempt in range(attempt_limit):
            queued = time.monotonic()
            _append_log({**metadata, "event": "request_queued", "attempt": attempt + 1})
            self._wait_for_slot(budget)
            budget.attempts = attempt + 1
            try:
                budget.remaining("upstream")
                request_options = {"session_id": self._current_session_id(), "budget": budget}
                if method != "GET" or body is not None or headers:
                    request_options.update(method=method, body=body, headers=headers)
                request_id = uuid.uuid4().hex
                attempt_started = time.monotonic()
                _append_log({**metadata, "event": "http_started", "request_id": request_id,
                             "attempt": attempt + 1,
                             "queued_ms": round((attempt_started - queued) * 1000, 1)})
                try:
                    final = _fetch_once(url, allow_redirects, **request_options)
                except Exception as exc:
                    category = ("client_cancelled" if isinstance(exc, RelayRequestCancelled)
                                else f"{exc.phase}_timeout" if isinstance(exc, RelayRequestExpired)
                                else "network_error")
                    _append_log({**metadata, "event": "http_finished", "request_id": request_id,
                                 "category": category, "error_type": type(exc).__name__,
                                 "duration_ms": round((time.monotonic() - attempt_started) * 1000, 1)})
                    raise
                status, response_headers, response_body, _ = final
                _append_log({**metadata, "event": "http_finished", "request_id": request_id,
                             "status": status, "attempt": attempt + 1,
                             "category": "rate_limit" if status == 429 else "upstream_error" if status >= 400 else "success",
                             "duration_ms": round((time.monotonic() - attempt_started) * 1000, 1),
                             "retry_after_seconds": _retry_after(response_headers),
                             **_failure_response_metadata(status, response_headers, response_body)})
                if status == 407:
                    self._latch_account_error("IPWO_PROXY_AUTH_FAILED: IPWO rejected the proxy credential")
                abuse = _is_abuse_response(final)
                if not abuse and status != 429 and status < 500:
                    return (*final, attempt + 1, budget.queued_ms)
                if attempt == attempt_limit - 1:
                    if status == 429:
                        cooldown(max(_retry_after(response_headers), 60.0))
                    elif overpass:
                        cooldown(max(_retry_after(response_headers), 5.0))
                    return (*final, attempt + 1, budget.queued_ms)
                self._rotate_session()
                if rotating_arxiv or (immediate_timeouts and status == 504 and _retry_after(response_headers) == 0):
                    reason = "rotating_exit" if rotating_arxiv else "upstream_timeout"
                    _append_log({**metadata, "event": "retry_scheduled", "reason": reason, "delay_seconds": 0, "next_attempt": attempt + 2})
                else:
                    cooldown(max(_retry_after(response_headers), 5.0 * (2**attempt)))
            except RelayAccountError as exc:
                self._latch_account_error(str(exc))
            except RelayRequestExpired as exc:
                if overpass and exc.phase == "upstream":
                    cooldown(5.0)
                raise
            except RelayUpstreamError as exc:
                if attempt == attempt_limit - 1:
                    if overpass:
                        cooldown(5.0)
                    raise
                self._rotate_session()
                if rotating_arxiv or (immediate_timeouts and isinstance(exc, RelayUpstreamTimeout)):
                    reason = "rotating_exit" if rotating_arxiv else "upstream_timeout"
                    _append_log({**metadata, "event": "retry_scheduled", "reason": reason, "delay_seconds": 0, "next_attempt": attempt + 2})
                else:
                    cooldown(5.0 * (2**attempt))
            finally:
                # Publish any cooldown before releasing capacity. A retry joins
                # behind already queued requests rather than holding the slot.
                with self.slots:
                    self.active -= 1
                    self.slots.notify_all()
        raise AssertionError("unreachable")


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
        self, url: str, allow_redirects: bool, *, method: str = "GET",
        body: bytes | None = None, headers: dict[str, str] | None = None,
        budget: RequestBudget | None = None,
    ) -> tuple[int, dict[str, str], bytes, str, int, float]:
        with self.lock:
            if self.account_error:
                raise RelayAccountError(self.account_error)
            controller = self.controllers[self.next_index]
            self.next_index = (self.next_index + 1) % len(self.controllers)
        try:
            return controller.fetch(
                url, allow_redirects, method=method, body=body, headers=headers, budget=budget,
            )
        except RelayAccountError as exc:
            with self.lock:
                if not self.account_error:
                    self.account_error = str(exc)
                error = self.account_error
            raise RelayAccountError(error) from None


DEFAULT_LIMITS = {
    "ncbi": {"minIntervalMs": 334},
    "ncbi-key": {"minIntervalMs": 100},
    "ncbi-pmc": {"minIntervalMs": 1000},
    "wikipedia": {"minIntervalMs": 300, "concurrency": 1},
    "arxiv": {"minIntervalMs": 0},
    "osm-nominatim": {"minIntervalMs": 1000, "concurrency": 1},
    "osm-overpass": {"minIntervalMs": 0},
    "osm-routing": {"minIntervalMs": 1000},
}
CONTROLLERS = {name: Controller(0) for name in DEFAULT_LIMITS}
HTTPS_CONTROLLERS: dict[tuple[str, int], Controller] = {}


def apply_limits(values: dict, *, persist: bool = True) -> None:
    if not isinstance(values, dict) or set(values) != set(DEFAULT_LIMITS):
        raise ValueError("Supply limits for every supported upstream category")
    normalized = {}
    for name, item in values.items():
        if not isinstance(item, dict) or set(item) - {"minIntervalMs", "concurrency"}:
            raise ValueError("Invalid upstream limit")
        interval = item.get("minIntervalMs", 0)
        concurrency = item.get("concurrency")
        if type(interval) is not int or not 0 <= interval <= 600000:
            raise ValueError("Invalid request interval")
        if concurrency is not None and (type(concurrency) is not int or not 1 <= concurrency <= 1000):
            raise ValueError("Invalid request concurrency")
        normalized[name] = {"minIntervalMs": interval}
        if concurrency is not None:
            normalized[name]["concurrency"] = concurrency
    with CONFIG_LOCK:
        if persist:
            LIMITS_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = LIMITS_PATH.with_suffix(".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(normalized, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, LIMITS_PATH)
            directory_fd = os.open(LIMITS_PATH.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        for name, item in normalized.items():
            controller = CONTROLLERS[name]
            with controller.slots:
                controller.min_interval = item["minIntervalMs"] / 1000
                controller.max_concurrency = item.get("concurrency")
                controller.slots.notify_all()


def current_limits() -> dict:
    with CONFIG_LOCK:
        return {name: {"minIntervalMs": round(controller.min_interval * 1000), **({"concurrency": controller.max_concurrency} if controller.max_concurrency is not None else {})} for name, controller in CONTROLLERS.items()}


def controller_for_url(url: str, group: str) -> Controller:
    if group == "https":
        parsed = urllib.parse.urlsplit(url)
        origin = ((parsed.hostname or "").lower().rstrip("."), parsed.port or 443)
        with CONFIG_LOCK:
            if origin not in HTTPS_CONTROLLERS:
                HTTPS_CONTROLLERS[origin] = Controller(0)
            return HTTPS_CONTROLLERS[origin]
    if group != "ncbi":
        return CONTROLLERS[group]
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != "eutils.ncbi.nlm.nih.gov":
        return CONTROLLERS["ncbi-pmc"]
    has_key = bool(urllib.parse.parse_qs(parsed.query).get("api_key"))
    return CONTROLLERS["ncbi-key" if has_key else "ncbi"]


apply_limits(json.loads(LIMITS_PATH.read_text()) if LIMITS_PATH.exists() else DEFAULT_LIMITS, persist=False)


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


def _public_addresses(host: str, port: int, budget: RequestBudget) -> tuple[str, ...]:
    key = (host, port)
    if key in budget.public_targets:
        return budget.public_targets[key]
    try:
        addresses = (ipaddress.ip_address(host),)
    except ValueError:
        if (not host or "%" in host or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))):
            raise RelayTargetError("Target must be a public HTTPS address") from None
        # Bound DNS work by the same request deadline, without blocking the
        # handler on an unresponsive OS resolver. Never log resolver exceptions.
        ready = threading.Event()
        result: list[Any] = []
        def lookup():
            try:
                result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except OSError:
                result.append(None)
            finally:
                ready.set()
        threading.Thread(target=lookup, daemon=True).start()
        deadline = time.monotonic() + min(5.0, budget.remaining("upstream"))
        while not ready.wait(min(0.05, budget.remaining("upstream"))):
            if time.monotonic() >= deadline:
                budget.remaining("upstream")
                raise RelayUpstreamTimeout("Target DNS lookup timed out")
        budget.remaining("upstream")
        if not result[0]:
            raise RelayUpstreamError("Target DNS lookup failed")
        addresses = tuple(ipaddress.ip_address(item[4][0]) for item in result[0])
    if not addresses or any(not address.is_global or address.is_multicast
                            or (getattr(address, "ipv4_mapped", None) is not None
                                and not address.ipv4_mapped.is_global)
                            for address in addresses):
        raise RelayTargetError("Target must be a public HTTPS address")
    # CONNECT is pinned to one validated address, while TLS SNI and the actual
    # HTTPS Host header keep the original hostname. DNS cannot rebind the proxy
    # tunnel to an internal address after validation.
    values = tuple(dict.fromkeys(str(address) for address in addresses))
    budget.public_targets[key] = values
    return values


def _validate_url(url: str, budget: RequestBudget | None = None) -> str:
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
        raise RelayTargetError("Invalid target URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or 443
    except ValueError:
        raise RelayTargetError("Invalid target URL") from None
    if (parsed.scheme != "https" or not host or parsed.port == 0
            or parsed.username is not None or parsed.password is not None or "#" in url):
        raise RelayTargetError("Target must be HTTPS without user information or fragments")
    _public_addresses(host, port, budget or RequestBudget(REQUEST_TIMEOUT))
    return UPSTREAM_GROUPS.get(host, "https")


def _proxy_tunnel_status(exc: Exception) -> int | None:
    reason = str(getattr(exc, "reason", exc))
    match = re.search(r"Tunnel connection failed:\s*(\d{3})\b", reason)
    return int(match.group(1)) if match else None


@contextlib.contextmanager
def _watch_socket(upstream_socket, budget: RequestBudget):
    """Interrupt response reads on deadline/cancellation, including trickle bodies."""
    if upstream_socket is None:
        yield
        return
    finished = threading.Event()

    def watch():
        while not finished.wait(0.1):
            try:
                budget.remaining("upstream")
            except (RelayRequestExpired, RelayRequestCancelled):
                try:
                    current = upstream_socket() if callable(upstream_socket) else upstream_socket
                    if current is not None:
                        current.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        yield
    finally:
        finished.set()
        watcher.join(0.3)


class BudgetHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, budget: RequestBudget, **kwargs):
        self.budget = budget
        super().__init__(host, **kwargs)

    def _tunnel(self):
        hostname, port = self._tunnel_host, self._tunnel_port
        original_headers = self._tunnel_headers
        addresses = _public_addresses(hostname.lower().rstrip("."), port, self.budget)
        address = next((value for value in addresses if ":" not in value), addresses[0])
        self._tunnel_host = f"[{address}]" if ":" in address else address
        self._tunnel_headers = {
            key: value for key, value in original_headers.items() if key.lower() != "host"
        }
        self._tunnel_headers["Host"] = f"{self._tunnel_host}:{port}"
        try:
            return super()._tunnel()
        finally:
            self._tunnel_host = hostname
            self._tunnel_headers = original_headers

    def connect(self):
        try:
            with _watch_socket(lambda: self.sock, self.budget):
                super().connect()
            self.budget.remaining("upstream")
        except Exception:
            self.close()
            self.budget.remaining("upstream")
            raise

    def getresponse(self):
        with _watch_socket(lambda: self.sock, self.budget):
            response = None
            try:
                response = super().getresponse()
                self.budget.remaining("upstream")
                return response
            except Exception:
                if response is not None:
                    response.close()
                self.close()
                self.budget.remaining("upstream")
                raise


class BudgetHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, budget: RequestBudget):
        super().__init__()
        self.budget = budget

    def https_open(self, request):
        return self.do_open(
            lambda host, **kwargs: BudgetHTTPSConnection(host, budget=self.budget, **kwargs),
            request, context=self._context,
        )


def _fetch_once(
    url: str,
    allow_redirects: bool,
    *,
    session_id: str | None = None,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    budget: RequestBudget | None = None,
) -> tuple[int, dict[str, str], bytes, str]:
    budget = budget or RequestBudget(REQUEST_TIMEOUT)
    budget.remaining("upstream")
    # Proxy credentials stay in this relay and are never passed into task
    # containers. IPWO tunnels the target TLS connection normally.
    proxy_url = (
        "http://"
        f"{urllib.parse.quote(_ipwo_username_for_request(session_id), safe='')}:"
        f"{urllib.parse.quote(IPWO_PROXY_PASSWORD, safe='')}@"
        f"{IPWO_PROXY_HOST}:{IPWO_PROXY_PORT}"
    )
    handlers: list[Any] = [
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}),
        BudgetHTTPSHandler(budget)
    ]
    handlers.append(SafeRedirect(budget) if allow_redirects else NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    forwarded = {
        key: value for key, value in (headers or {}).items()
        if key.lower() in {"accept", "content-type", "range", "user-agent"}
    }
    forwarded.setdefault("User-Agent", RELAY_USER_AGENT)
    request = urllib.request.Request(
        url, data=body or None, headers=forwarded, method=method,
    )
    try:
        response = opener.open(request, timeout=min(UPSTREAM_TIMEOUT, budget.remaining("upstream")))
    except urllib.error.HTTPError as exc:
        response = exc
    except RelayTargetError:
        raise
    except Exception as exc:
        budget.remaining("upstream")
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
        failure = RelayUpstreamTimeout if isinstance(getattr(exc, "reason", exc), TimeoutError) else RelayUpstreamError
        raise failure(type(exc).__name__) from None
    try:
        raw = getattr(getattr(response, "fp", None), "raw", None)
        with response, _watch_socket(getattr(raw, "_sock", None), budget):
            chunks = []
            size = 0
            while True:
                remaining = budget.remaining("upstream")
                # HTTPResponse exposes its socket through the buffered reader.
                raw = getattr(getattr(response, "fp", None), "raw", None)
                upstream_socket = getattr(raw, "_sock", None)
                if upstream_socket is not None:
                    upstream_socket.settimeout(min(UPSTREAM_TIMEOUT, remaining))
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("upstream response exceeds relay size limit")
                chunks.append(chunk)
            budget.remaining("upstream")
            body = b"".join(chunks)
            return int(response.status), dict(response.headers.items()), body, response.geturl()
    except (RelayRequestCancelled, RelayRequestExpired):
        raise
    except (OSError, urllib.error.URLError) as exc:
        budget.remaining("upstream")
        failure = RelayUpstreamTimeout if isinstance(getattr(exc, "reason", exc), TimeoutError) else RelayUpstreamError
        raise failure(type(exc).__name__) from None


def _failure_response_metadata(status: int, headers: dict[str, str], body: bytes) -> dict[str, Any]:
    """Fixed-vocabulary diagnostics only: never persist response text or arbitrary headers."""
    if status < 400:
        return {}
    normalized = {name.lower(): str(value) for name, value in headers.items()}
    mime = normalized.get("content-type", "").split(";", 1)[0].strip().lower()
    known_mimes = {"text/xml", "application/xml", "application/json", "text/html", "text/plain", "application/octet-stream"}
    sample = body[:8192].decode("utf-8", errors="replace").lower()
    server = normalized.get("server", "").lower()
    product = next((name for name in ("nginx", "envoy", "squid", "cloudflare", "apache", "ncbi")
                    if re.search(r"(?:^|[ /])" + name + r"(?:$|[ /])", server)), "unknown")
    ncbi_xml = bool(re.search(r"<(?:esearchresult|efetchresult|einforesult)\b", sample))
    proxy_document = any(marker in sample for marker in (
        "err_tunnel_connection_failed", "proxy authentication required", "err_connect_fail", "err_cache_access_denied", "the proxy server received an invalid response"))
    kind = ("xml" if ncbi_xml or sample.lstrip().startswith("<?xml")
            else "html" if "<html" in sample or "<!doctype html" in sample
            else "json" if sample.lstrip().startswith(("{", "["))
            else "empty" if not body else "other")
    if status == 429 or any(marker in sample for marker in ("api rate limit exceeded", "too many requests", "rate limit exceeded")):
        reason = "rate_limit"
    elif any(marker in sample for marker in ("invalid api key", "api key invalid", "api key is invalid")):
        reason = "invalid_api_key"
    elif proxy_document:
        reason = "proxy_error"
    elif any(marker in sample for marker in ("blocked for possible abuse", "access denied")):
        reason = "access_denied"
    elif any(marker in sample for marker in ("invalid query", "query syntax", "error in query", "query translation error", "search term too long")):
        reason = "query_error"
    elif ncbi_xml and re.search(r"<(?:error|errorlist)\b", sample):
        reason = "ncbi_application_error"
    else:
        reason = "unclassified_http_error"
    return {
        "response_content_type": mime if mime in known_mimes else "other",
        "response_server_product": product,
        "response_via_present": bool(normalized.get("via")),
        "response_body_kind": kind,
        "response_source_hint": "proxy_document" if proxy_document else "ncbi_xml" if ncbi_xml else "unknown",
        "response_error_reason": reason,
        "response_body_sha256": hashlib.sha256(body).hexdigest(),
        "response_bytes": len(body),
    }


def _append_log(event: dict[str, Any]) -> None:
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        encoded = line.encode("utf-8")
        if os.write(fd, encoded) != len(encoded):
            raise OSError("Incomplete relay audit write")
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(LOG_PATH.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class Handler(BaseHTTPRequestHandler):
    server_version = "MCPAtlasRelay/1.0"

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

    def _client_disconnected(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def _authorized(self) -> bool:
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}")

    def do_GET(self) -> None:
        if self.path == "/v1/limits":
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            self._json(200, {"limits": current_limits()})
            return
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
                    "target_policy": "public_https",
                    "ip_policy": "sticky_until_failure" if re.search(r"_sid_[^_]+", IPWO_PROXY_USERNAME) else "rotating_per_request",
                    "arxiv_timeout_retry": "immediate" if ARXIV_IMMEDIATE_TIMEOUT_RETRY else "backoff",
                    "lane_counts": {
                        "ncbi": 1,
                        "wikipedia": WIKIPEDIA_LANES,
                        "arxiv": 1,
                        "osm-nominatim": 1,
                        "osm-overpass": 1,
                        "osm-routing": 1,
                    },
                    "rotation_counts": {
                        name: controller.current_rotation_count()
                        for name, controller in CONTROLLERS.items()
                    },
                })
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        if self.path != "/v1/limits":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            apply_limits(payload.get("limits"))
            _append_log({"event": "limits_updated", "limits": current_limits()})
            self._json(200, {"limits": current_limits()})
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": str(exc)})
        except OSError:
            self._json(500, {"error": "Unable to persist relay limits"})

    def do_POST(self) -> None:
        global TOKEN
        if self.path == "/v1/token":
            if not self._authorized() or not CONTROL_TOKEN or not hmac.compare_digest(
                self.headers.get("X-Relay-Control", ""), CONTROL_TOKEN
            ):
                self._json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("invalid request size")
                token = json.loads(self.rfile.read(length)).get("token")
                if not isinstance(token, str) or not token or len(token) > 16384 or any(c.isspace() for c in token):
                    raise ValueError("invalid token")
                _append_log({"event": "token_reset", "status": 200})
                TOKEN = token
                self._json(200, {"success": True})
            except (ValueError, TypeError, AttributeError):
                self._json(400, {"error": "Invalid token"})
            except OSError:
                self._json(500, {"error": "Unable to persist relay audit"})
            return
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
            seconds = payload.get("timeout_seconds", REQUEST_TIMEOUT)
            if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                    or not math.isfinite(seconds) or seconds <= 0):
                raise ValueError("Invalid request deadline")
            budget = RequestBudget(min(float(seconds), REQUEST_TIMEOUT), self._client_disconnected)
            request_id = budget.request_id
            url = str(payload["url"])
            method = str(payload.get("method") or "GET").upper()
            if method not in {"GET", "POST"}:
                raise ValueError("method must be GET or POST")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            request_headers = payload.get("headers") or {}
            if not isinstance(request_headers, dict):
                raise ValueError("headers must be an object")
            encoded_body = str(payload.get("body_base64") or "")
            try:
                request_body = base64.b64decode(encoded_body, validate=True)
            except ValueError as exc:
                raise ValueError("body_base64 is invalid") from exc
            if len(request_body) > 65536:
                raise ValueError("upstream request body exceeds relay size limit")
            target_group = "https"
            target_group = _validate_url(url, budget)
            query = urllib.parse.urlencode(params, doseq=True)
            if query:
                separator = "&" if urllib.parse.urlsplit(url).query else "?"
                url = f"{url}{separator}{query}"
            status, headers, body, final_url, attempts, queued_ms = controller_for_url(url, target_group).fetch(
                url, bool(payload.get("allow_redirects", False)),
                budget=budget,
                method=method,
                body=request_body,
                headers={str(key): str(value) for key, value in request_headers.items()},
            )
            _validate_url(final_url, budget)
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
                "event": "http_completed",
                "request_id": request_id,
                "category": "rate_limit" if status == 429 else "upstream_error" if status >= 400 else "success",
                "upstream_group": target_group,
                "retry_after_seconds": _retry_after(headers),
                "status": status,
                "response_bytes": len(body),
                "egress_backend": "ipwo",
                "attempts": attempts,
                "queued_ms": round(queued_ms, 1),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "client_disconnected": not delivered,
                **_failure_response_metadata(status, headers, body),
            })
        except (RelayRequestExpired, RelayRequestCancelled) as exc:
            cancelled = isinstance(exc, RelayRequestCancelled)
            status = 499 if cancelled else 504
            category = "client_cancelled" if cancelled else f"{exc.phase}_timeout"
            parsed = urllib.parse.urlsplit(url)
            _append_log({
                "event": "http_completed", "request_id": request_id,
                "host": parsed.hostname, "path": parsed.path,
                "upstream_group": target_group, "status": status, "category": category,
                "attempts": budget.attempts, "queued_ms": round(budget.queued_ms, 1),
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "client_disconnected": cancelled,
            })
            if not cancelled:
                self._json(status, {"code": category, "error": str(exc)})
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
            _append_log({"event": "request_rejected", "status": 400, "error_type": type(exc).__name__})
            self._json(400, {"error": "Invalid relay request"})
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
        raise SystemExit("MCP_RELAY_TOKEN must be set")
    try:
        _validate_ipwo_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(
        f"MCP egress relay (ipwo) listening on http://{BIND}:{PORT}",
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
