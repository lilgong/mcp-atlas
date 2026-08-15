"""Cross-process pacing for public MCP backends sharing one host egress."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator


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
class SharedRateGate:
    """Serialize and pace calls across independent Python processes."""

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
    async def slot(self) -> AsyncIterator[SharedRateLease]:
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
