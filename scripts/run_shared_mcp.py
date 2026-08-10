#!/usr/bin/env python3
"""Run the shared MCP container using host/port settings from the root .env."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
DEFAULT_RUNTIME_IMAGE = "mcp-atlas-runtime:latest"


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
    if not ENV_FILE.is_file():
        raise RuntimeError(f"missing environment file: {ENV_FILE}")
    load_dotenv(ENV_FILE, override=False)
    port = configured_shared_port()
    host = os.getenv("MCP_SHARED_HOST", "0.0.0.0")
    image = os.getenv("MCP_SHARED_AGENT_IMAGE", DEFAULT_RUNTIME_IMAGE)
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
        "--env-file", str(ENV_FILE),
        "--env", "MCP_ATLAS_SHARED_RUNTIME=true",
        "--env", "MCP_USAGE_LOG_DIR=/mcp-usage-log",
        "--volume", f"{usage_log_dir}:/mcp-usage-log:rw",
        image,
        "/agent-environment/.venv/bin/python", "-m", "uvicorn",
        "agent_environment.main:app",
        "--host", host,
        "--port", str(port),
    ]
    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
