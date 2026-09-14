"""Cross-process pacing for public MCP backends sharing one host egress."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

import redis.asyncio as redis


_REDIS_KEY_PREFIX = "mcp-atlas:rate-limit:v1"
_REDIS_LOCK_TTL_MS = 600_000
_REDIS_LOCK_RENEW_SECONDS = 60
_REDIS_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
_REDIS_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_REDIS_COMMIT_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('hset', KEYS[2],
    'last_started', ARGV[2],
    'cooldown_until', ARGV[3],
    'consecutive_rate_limits', ARGV[4])
  redis.call('del', KEYS[1])
  return 1
end
return 0
"""


def _default_gate_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"mcp-atlas-rate-gates-{os.getuid()}"


def _gate_dir() -> Path:
    configured = (os.getenv("MCP_SHARED_RATE_LIMIT_DIR") or "").strip()
    path = Path(configured).expanduser() if configured else _default_gate_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _read_state(fd: int) -> dict[str, float | int]:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 4096)
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(fd: int, state: dict[str, float | int]) -> None:
    payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)


@dataclass
class SharedRateLease:
    """One globally serialized call whose outcome can update shared cooldown."""

    fd: int
    state: dict[str, float | int]
    rate_limit_backoff: float
    max_rate_limit_backoff: float
    completion_spacing: float
    observed: bool = False

    def observe_rate_limit(self, rate_limited: bool) -> float:
        consecutive = max(0, int(self.state.get("consecutive_rate_limits", 0)))
        if rate_limited and self.rate_limit_backoff > 0:
            consecutive += 1
            delay = min(
                self.rate_limit_backoff * (2 ** (consecutive - 1)),
                self.max_rate_limit_backoff or self.rate_limit_backoff,
            )
            self.state["cooldown_until"] = max(
                float(self.state.get("cooldown_until", 0.0)),
                time.time() + delay,
            )
        else:
            consecutive = max(0, consecutive - 1)
            delay = 0.0
        if self.completion_spacing > 0:
            self.state["cooldown_until"] = max(
                float(self.state.get("cooldown_until", 0.0)),
                time.time() + self.completion_spacing,
            )
        self.state["consecutive_rate_limits"] = consecutive
        _write_state(self.fd, self.state)
        self.observed = True
        return delay


@dataclass
class RedisRateLease:
    """One Redis-owned lease whose state is committed when it is released."""

    state: dict[str, float | int]
    redis_now: float
    started_monotonic: float
    rate_limit_backoff: float
    max_rate_limit_backoff: float
    completion_spacing: float
    observed: bool = False

    def observe_rate_limit(self, rate_limited: bool) -> float:
        observed_at = self.redis_now + (
            time.monotonic() - self.started_monotonic
        )
        consecutive = max(
            0, int(self.state.get("consecutive_rate_limits", 0))
        )
        if rate_limited and self.rate_limit_backoff > 0:
            consecutive += 1
            delay = min(
                self.rate_limit_backoff * (2 ** (consecutive - 1)),
                self.max_rate_limit_backoff or self.rate_limit_backoff,
            )
            self.state["cooldown_until"] = max(
                float(self.state.get("cooldown_until", 0.0)),
                observed_at + delay,
            )
        else:
            consecutive = max(0, consecutive - 1)
            delay = 0.0
        if self.completion_spacing > 0:
            self.state["cooldown_until"] = max(
                float(self.state.get("cooldown_until", 0.0)),
                observed_at + self.completion_spacing,
            )
        self.state["consecutive_rate_limits"] = consecutive
        self.observed = True
        return delay


class RateLease(Protocol):
    """Common result contract for the file and Redis gate backends."""

    def observe_rate_limit(self, rate_limited: bool) -> float: ...


def _redis_url() -> str:
    return (os.getenv("REDIS_URL") or "").strip()


def _redis_client(url: str) -> Any:
    return redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
    )


async def _redis_now(client: Any) -> float:
    seconds, microseconds = await client.time()
    return float(seconds) + float(microseconds) / 1_000_000


@dataclass
class SharedRateGate:
    """Serialize locally by file lock or globally when REDIS_URL is present."""

    server: str
    min_interval: float
    rate_limit_backoff: float = 0.0
    max_rate_limit_backoff: float = 0.0
    completion_spacing: float = 0.0
    poll_interval: float = 0.05
    path: Path = field(init=False)

    def __post_init__(self) -> None:
        safe_name = "".join(
            char if char.isalnum() or char in "-_" else "_"
            for char in self.server
        )
        self.path = _gate_dir() / f"{safe_name}.lock"

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[RateLease]:
        redis_url = _redis_url()
        if redis_url:
            async with self._redis_slot(redis_url) as lease:
                yield lease
            return
        async with self._file_slot() as lease:
            yield lease

    @contextlib.asynccontextmanager
    async def _file_slot(self) -> AsyncIterator[SharedRateLease]:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    await asyncio.sleep(self.poll_interval)

            state = _read_state(fd)
            now = time.time()
            last_started = float(state.get("last_started", 0.0))
            cooldown_until = float(state.get("cooldown_until", 0.0))
            # A wall-clock correction or a stale file from another boot must
            # not create an unbounded wait; legitimate cooldowns are <= 60s.
            if last_started > now + 300 or cooldown_until > now + 300:
                state = {}
                last_started = cooldown_until = 0.0
            ready_at = max(last_started + self.min_interval, cooldown_until)
            if ready_at > now:
                await asyncio.sleep(ready_at - now)
            state["last_started"] = time.time()
            _write_state(fd, state)
            lease = SharedRateLease(
                fd,
                state,
                self.rate_limit_backoff,
                self.max_rate_limit_backoff,
                self.completion_spacing,
            )
            try:
                yield lease
            finally:
                if not lease.observed:
                    lease.observe_rate_limit(False)
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @property
    def _redis_base_key(self) -> str:
        safe_name = "".join(
            char if char.isalnum() or char in "-_" else "_"
            for char in self.server
        )
        return f"{_REDIS_KEY_PREFIX}:{safe_name}"

    async def _renew_redis_lock(
        self, client: Any, lock_key: str, token: str,
    ) -> None:
        while True:
            await asyncio.sleep(_REDIS_LOCK_RENEW_SECONDS)
            renewed = await client.eval(
                _REDIS_RENEW,
                1,
                lock_key,
                token,
                _REDIS_LOCK_TTL_MS,
            )
            if not renewed:
                raise RuntimeError("Redis rate-limit lease was lost")

    @contextlib.asynccontextmanager
    async def _redis_slot(
        self, redis_url: str,
    ) -> AsyncIterator[RedisRateLease]:
        client = _redis_client(redis_url)
        lock_key = f"{self._redis_base_key}:lock"
        state_key = f"{self._redis_base_key}:state"
        token = uuid.uuid4().hex
        acquired = False
        renewer: asyncio.Task[None] | None = None
        try:
            while not acquired:
                acquired = bool(
                    await client.set(
                        lock_key,
                        token,
                        nx=True,
                        px=_REDIS_LOCK_TTL_MS,
                    )
                )
                if not acquired:
                    await asyncio.sleep(self.poll_interval)

            renewer = asyncio.create_task(
                self._renew_redis_lock(client, lock_key, token)
            )
            raw = await client.hgetall(state_key)
            state: dict[str, float | int] = {
                "last_started": float(raw.get("last_started", 0.0)),
                "cooldown_until": float(raw.get("cooldown_until", 0.0)),
                "consecutive_rate_limits": int(
                    raw.get("consecutive_rate_limits", 0)
                ),
            }
            now = await _redis_now(client)
            last_started = float(state["last_started"])
            cooldown_until = float(state["cooldown_until"])
            if last_started > now + 300 or cooldown_until > now + 300:
                state = {
                    "last_started": 0.0,
                    "cooldown_until": 0.0,
                    "consecutive_rate_limits": 0,
                }
                last_started = cooldown_until = 0.0
            ready_at = max(last_started + self.min_interval, cooldown_until)
            if ready_at > now:
                await asyncio.sleep(ready_at - now)
            now = await _redis_now(client)
            state["last_started"] = now
            lease = RedisRateLease(
                state,
                now,
                time.monotonic(),
                self.rate_limit_backoff,
                self.max_rate_limit_backoff,
                self.completion_spacing,
            )
            try:
                yield lease
            finally:
                if not lease.observed:
                    lease.observe_rate_limit(False)
                committed = await client.eval(
                    _REDIS_COMMIT_RELEASE,
                    2,
                    lock_key,
                    state_key,
                    token,
                    lease.state["last_started"],
                    lease.state["cooldown_until"],
                    lease.state["consecutive_rate_limits"],
                )
                if not committed:
                    raise RuntimeError(
                        "Redis rate-limit lease expired before completion"
                    )
                acquired = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"MCP-Atlas Redis rate limiter unavailable: {exc}"
            ) from exc
        finally:
            if renewer is not None:
                renewer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewer
            if acquired:
                with contextlib.suppress(Exception):
                    await client.eval(_REDIS_RELEASE, 1, lock_key, token)
            with contextlib.suppress(Exception):
                await client.aclose()
