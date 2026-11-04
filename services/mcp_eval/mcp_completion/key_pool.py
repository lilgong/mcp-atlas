"""Model credential pool, coordinated with pipeline processes via opaque SQL IDs."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3


class KeyPoolExhausted(RuntimeError):
    pass


def keys(primary):
    raw = os.environ.get("LLM_API_KEYS", "")
    extra = json.loads(raw) if raw.strip().startswith("[") else raw.split(",")
    if not isinstance(extra, list) or any(not isinstance(k, str) for k in extra):
        raise ValueError("LLM_API_KEYS must be a JSON string array or comma-separated keys")
    return list(dict.fromkeys(k.strip() for k in [primary, *extra] if k.strip()))


def _identity(base, pool):
    provider = base.rstrip("/").removesuffix("/chat/completions").removesuffix("/v1")
    return hashlib.sha256((provider + "\0" + "\0".join(pool)).encode()).hexdigest()


def _connect():
    path = Path(os.environ["LLM_KEY_POOL_DB"]).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("LLM key pool database must not be a symlink")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("CREATE TABLE IF NOT EXISTS pools (id TEXT PRIMARY KEY, cursor INTEGER NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS exhausted (pool TEXT, slot INTEGER, PRIMARY KEY(pool, slot))")
    return connection


def choose(base, primary, excluded=()):
    pool = keys(primary)
    if not pool:
        raise KeyPoolExhausted("insufficient quota: model_key_pool_exhausted")
    if len(pool) == 1:
        if pool[0] in excluded:
            raise KeyPoolExhausted("insufficient quota: model_key_pool_exhausted")
        return pool[0]
    if not os.environ.get("LLM_KEY_POOL_DB"):
        raise ValueError("LLM_KEY_POOL_DB is required for multiple model keys")
    with _connect() as db:
        identity = _identity(base, pool)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT cursor FROM pools WHERE id=?", (identity,)).fetchone()
        cursor = row[0] if row else 0
        blocked = {r[0] for r in db.execute("SELECT slot FROM exhausted WHERE pool=?", (identity,))}
        for offset in range(len(pool)):
            slot = (cursor + offset) % len(pool)
            if slot not in blocked and pool[slot] not in excluded:
                db.execute("INSERT OR REPLACE INTO pools VALUES (?,?)", (identity, slot + 1))
                return pool[slot]
    raise KeyPoolExhausted("insufficient quota: model_key_pool_exhausted")


def exhaust(base, primary, key):
    pool = keys(primary)
    if len(pool) > 1:
        with _connect() as db:
            db.execute("INSERT OR IGNORE INTO exhausted VALUES (?,?)", (_identity(base, pool), pool.index(key)))
