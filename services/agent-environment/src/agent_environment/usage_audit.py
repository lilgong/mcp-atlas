"""Durable metadata-only accounting at the actual upstream request boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def usage_event(service: str, key: str, event: str, request_id: str, **result) -> None:
    now = datetime.now(timezone.utc)
    # A fingerprint identifies rotations without writing any part of the key.
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
    root = Path(os.getenv("MCP_USAGE_LOG_DIR") or "mcp_usage_log")
    directory = root / now.strftime("%Y-%m")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = {
        "ts": now.isoformat().replace("+00:00", "Z"),
        "service": service,
        "credential_id": fingerprint,
        "event": event,
        "request_id": request_id,
        "task_id": os.getenv("MCP_TASK_ID") or None,
        **result,
    }
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    fd = os.open(
        directory / f"{service}_{fingerprint}_{now:%Y%m%d}.jsonl",
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        if os.write(fd, line) != len(line):
            raise OSError("Incomplete usage audit write")
        os.fsync(fd)
    finally:
        os.close(fd)
    # Persist newly created file/directory entries, too.
    for path in (directory, root, root.parent):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if os.getenv("MCP_USAGE_LOG_STDERR") == "true":
        print("[mcp-usage] " + line.decode().rstrip(), file=sys.stderr, flush=True)


def start_usage(service: str, key: str) -> str:
    request_id = uuid.uuid4().hex
    usage_event(service, key, "started", request_id)
    return request_id
