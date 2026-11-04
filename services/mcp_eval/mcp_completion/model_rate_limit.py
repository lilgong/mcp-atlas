"""Host-shared request pacing; persists only a hashed provider/account identity."""

import hashlib
import math
import os
import sqlite3
import time
from pathlib import Path


def reserve_delay(base_url, api_key, *, now=None):
    rpm = float(os.environ.get("LLM_MAX_RPM", "0"))
    if not math.isfinite(rpm) or rpm < 0:
        raise ValueError("LLM_MAX_RPM must be finite and nonnegative")
    if rpm == 0:
        return 0.0
    raw_path = os.environ.get("LLM_RATE_LIMIT_DB", "")
    if not raw_path:
        raise ValueError("LLM_RATE_LIMIT_DB is required when LLM_MAX_RPM > 0")
    path = Path(raw_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("rate-limit database must not be a symlink")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    provider = base_url.rstrip("/").removesuffix("/v1")
    scope = hashlib.sha256((provider + "\0" + api_key).encode()).hexdigest()
    current = time.time() if now is None else now
    with sqlite3.connect(path, timeout=30) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS request_slots (scope TEXT PRIMARY KEY, next_at REAL NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT next_at FROM request_slots WHERE scope=?", (scope,)).fetchone()
        slot = max(current, row[0] if row else current)
        connection.execute("INSERT OR REPLACE INTO request_slots VALUES (?, ?)", (scope, slot + 60.0 / rpm))
    return max(0.0, slot - current)
