"""Append-only JSONL logs for model calls and isolated sandbox activity."""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_SECRET_ENV_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH")


def _configured_secrets() -> tuple[str, ...]:
    values: list[str] = []
    for name, value in os.environ.items():
        if value and len(value) >= 8 and any(marker in name.upper() for marker in _SECRET_ENV_MARKERS):
            values.append(value)
    return tuple(sorted(set(values), key=len, reverse=True))


def _redact_string(value: str) -> str:
    redacted = value
    for secret in _configured_secrets():
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def jsonable(value: Any) -> Any:
    """Convert common SDK/Pydantic values into redacted JSON-compatible data."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return jsonable(value.model_dump())
        except Exception:
            pass
    return _redact_string(str(value))


def _log_root() -> Path:
    configured = os.getenv("MCP_RUNTIME_LOG_DIR", "completion_results/runtime_logs")
    root = Path(configured).expanduser()
    return root / dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def write_runtime_event(stream: str, event: str, **fields: Any) -> Path:
    """Write one durable event and return its path.

    A start record is written before every model HTTP call, so a killed process or
    timeout still leaves evidence that the call was attempted.
    """

    now = dt.datetime.now(dt.timezone.utc)
    root = _log_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stream}_{now.strftime('%Y%m%d')}.jsonl"
    record = {
        "timestamp": now.isoformat(),
        "event": event,
        **{key: jsonable(value) for key, value in fields.items()},
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
    return path


def container_log_path(task_id: str, kind: str, container_name: str) -> Path:
    now = dt.datetime.now(dt.timezone.utc)
    safe_task = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in task_id)
    root = _log_root() / "containers" / (safe_task or "unknown")
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{now.strftime('%H%M%S')}_{kind}_{container_name}.log"
