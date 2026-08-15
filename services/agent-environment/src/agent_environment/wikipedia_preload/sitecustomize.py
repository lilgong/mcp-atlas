"""Pace Wikipedia HTTP requests without changing MCP tools or responses."""

from __future__ import annotations

import contextlib
import base64
import fcntl
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator


MIN_INTERVAL_SECONDS = 6.0
DEFAULT_RATE_LIMIT_SECONDS = 60.0


def _relay_settings() -> tuple[str, str, float] | None:
    url = (os.getenv("WIKIPEDIA_RELAY_URL") or "").strip().rstrip("/")
    token = (os.getenv("WIKIPEDIA_RELAY_TOKEN") or "").strip()
    if not url and not token:
        return None
    if not url or not token:
        raise RuntimeError(
            "WIKIPEDIA_RELAY_URL and WIKIPEDIA_RELAY_TOKEN must be set together"
        )
    try:
        timeout = float(os.getenv("WIKIPEDIA_RELAY_TIMEOUT_SECONDS") or "90")
    except ValueError:
        timeout = 90.0
    return url, token, timeout


def _relay_fetch(url: str, params: dict) -> tuple[int, dict, bytes, str]:
    settings = _relay_settings()
    if settings is None:
        raise RuntimeError("Wikipedia relay is not configured")
    relay_url, token, timeout = settings
    payload = json.dumps(
        {"url": url, "params": params, "allow_redirects": False},
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{relay_url}/v1/fetch",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        try:
            envelope = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Wikipedia relay returned an invalid response") from exc
        if response.status != 200:
            raise RuntimeError(
                str(envelope.get("error") or envelope.get("code") or "Wikipedia relay failed")
            )
    return (
        int(envelope["status_code"]),
        dict(envelope.get("headers") or {}),
        base64.b64decode(envelope["body_base64"]),
        str(envelope.get("url") or url),
    )


def _gate_path() -> Path:
    configured = (os.getenv("MCP_SHARED_RATE_LIMIT_DIR") or "").strip()
    directory = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir())
        / f"mcp-atlas-rate-gates-{os.getuid()}"
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / "wikipedia-http.lock"


def _read_state(fd: int) -> dict[str, float]:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 4096)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(fd: int, state: dict[str, float]) -> None:
    payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)


@contextlib.contextmanager
def _request_slot() -> Iterator[tuple[int, dict[str, float]]]:
    fd = os.open(_gate_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _read_state(fd)
        now = time.time()
        last_started = float(state.get("last_started", 0.0))
        cooldown_until = float(state.get("cooldown_until", 0.0))
        if last_started > now + 300 or cooldown_until > now + 300:
            state = {}
            last_started = cooldown_until = 0.0
        ready_at = max(last_started + MIN_INTERVAL_SECONDS, cooldown_until)
        if ready_at > now:
            time.sleep(ready_at - now)
        state["last_started"] = time.time()
        _write_state(fd, state)
        yield fd, state
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _retry_after_seconds(value: object) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_SECONDS
    return max(MIN_INTERVAL_SECONDS, seconds)


def _record_rate_limit(
    fd: int, state: dict[str, float], retry_after: object,
) -> None:
    state["cooldown_until"] = max(
        float(state.get("cooldown_until", 0.0)),
        time.time() + _retry_after_seconds(retry_after),
    )
    _write_state(fd, state)


def _install() -> None:
    import httpx
    import requests
    from wikipediaapi._http_client.sync_http_client import SyncHTTPClient
    from wikipediaapi.exceptions import WikiRateLimitError

    original_request = requests.sessions.Session.request
    if not getattr(original_request, "_mcp_atlas_wikipedia_paced", False):
        def paced_request(session, method, url, *args, **kwargs):
            if ".wikipedia.org/w/api.php" not in str(url):
                return original_request(session, method, url, *args, **kwargs)
            if _relay_settings() is not None:
                status, headers, body, final_url = _relay_fetch(
                    str(url), kwargs.get("params") or {},
                )
                response = requests.Response()
                response.status_code = status
                response.headers = requests.structures.CaseInsensitiveDict(headers)
                response._content = body
                response.url = final_url
                response.request = requests.Request(
                    method=method,
                    url=str(url),
                    params=kwargs.get("params"),
                ).prepare()
                response.encoding = requests.utils.get_encoding_from_headers(
                    response.headers
                )
                return response
            with _request_slot() as (fd, state):
                response = original_request(session, method, url, *args, **kwargs)
                if response.status_code == 429:
                    _record_rate_limit(fd, state, response.headers.get("Retry-After"))
                return response

        paced_request._mcp_atlas_wikipedia_paced = True
        requests.sessions.Session.request = paced_request

    original_do_get = SyncHTTPClient._do_get
    if not getattr(original_do_get, "_mcp_atlas_wikipedia_paced", False):
        def paced_do_get(client, url, params):
            if _relay_settings() is not None:
                status, headers, body, final_url = _relay_fetch(url, params)
                response = httpx.Response(
                    status,
                    headers=headers,
                    content=body,
                    request=httpx.Request("GET", final_url),
                )
                return client._process_response(response, url)
            with _request_slot() as (fd, state):
                try:
                    return original_do_get(client, url, params)
                except WikiRateLimitError as exc:
                    _record_rate_limit(fd, state, exc.retry_after)
                    raise

        paced_do_get._mcp_atlas_wikipedia_paced = True
        SyncHTTPClient._do_get = paced_do_get


_install()
