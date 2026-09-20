#!/usr/bin/env python3
"""Run the shared MCP container using host/port settings from the root .env."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_IMAGE = "mcp-atlas-runtime:latest"


def secret_values(environment: dict[str, str]) -> list[str]:
    """Return configured secrets for exact-value log redaction."""
    markers = (
        "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "USERNAME",
        "EMAIL", "CLIENT_ID", "PAGE_ID",
    )
    values = {
        value for name, value in environment.items()
        if value and len(value) >= 4 and any(marker in name.upper() for marker in markers)
    }
    connection = environment.get("MONGODB_CONNECTION_STRING", "")
    if len(connection) >= 4:
        values.add(connection)
    return sorted(values, key=len, reverse=True)


def redact_line(line: str, secrets: list[str]) -> str:
    for value in secrets:
        line = line.replace(value, "<redacted>")
    return line


def configured_env_file() -> Path:
    """Allow an orchestrator-owned ephemeral env file without CLI flags."""
    configured = os.getenv("MCP_ATLAS_RUNTIME_ENV_FILE")
    return Path(configured).expanduser().resolve() if configured else ROOT / ".env"


def validate_shared_bind_host(host: str) -> None:
    if host.strip().lower() not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(
            "MCP_SHARED_HOST must be a loopback address when task isolation is "
            "enabled; use 127.0.0.1 so networked task containers cannot bypass "
            "their tool allowlist"
        )


def configured_shared_port() -> int:
    explicit = (os.getenv("MCP_SHARED_PORT") or "").strip()
    if explicit:
        port = int(explicit)
    else:
        url = (os.getenv("MCP_SERVER_URL") or "").strip()
        port = urlsplit(url).port if url else 1984
        port = port or 1984
    if not 1 <= port <= 65535:
        raise ValueError("MCP_SHARED_PORT must be between 1 and 65535")
    return port


def main() -> int:
    env_file = configured_env_file()
    if not env_file.is_file():
        raise RuntimeError(f"missing environment file: {env_file}")
    load_dotenv(env_file, override=False)
    port = configured_shared_port()
    host = os.getenv("MCP_SHARED_HOST", "127.0.0.1")
    isolation_enabled = (
        os.getenv("MCP_TASK_ISOLATION_ENABLED", "true").lower()
        not in {"0", "false", "no"}
    )
    if isolation_enabled:
        validate_shared_bind_host(host)
    image = os.getenv("MCP_AGENT_IMAGE", DEFAULT_RUNTIME_IMAGE)
    usage_log_dir = Path(
        os.getenv("MCP_USAGE_LOG_DIR") or ROOT / "mcp_usage_log"
    ).expanduser().resolve()
    usage_log_dir.mkdir(parents=True, exist_ok=True)
    inspected = subprocess.run(
        [
            "docker", "image", "inspect", image,
            "--format", "{{json .Config.Labels}}\n{{json .Config.Volumes}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    labels_text, _, volumes_text = inspected.partition("\n")
    labels = json.loads(labels_text or "{}") or {}
    volumes = json.loads(volumes_text or "{}") or {}
    if (
        labels.get("mcp-atlas.runtime") != "true"
        or labels.get("mcp-atlas.data-contract") != "external-data-v1"
        or labels.get("mcp-atlas.contains-fixture") != "false"
        or "/data" not in volumes
    ):
        raise RuntimeError(
            f"{image} is not a fixture-free MCP-Atlas runtime image"
        )
    command = [
        "docker", "run", "--rm", "--network", "host",
        "--add-host=host.docker.internal:host-gateway",
        "--env-file", str(env_file),
        "--env", "MCP_ATLAS_SHARED_RUNTIME=true",
        "--env", "MCP_USAGE_LOG_DIR=/mcp-usage-log",
        "--volume", f"{usage_log_dir}:/mcp-usage-log:rw",
        image,
        "/agent-environment/.venv/bin/python", "-m", "uvicorn",
        "agent_environment.main:app",
        "--host", host,
        "--port", str(port),
    ]
    # Some third-party MCP SDKs dump request headers on failures.  Proxy the
    # container output so credential values never reach terminal/runtime logs.
    secrets = secret_values(dict(os.environ))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def forward_signal(signum, _frame):
        if process.poll() is None:
            process.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(redact_line(line, secrets))
        sys.stdout.flush()
    return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
