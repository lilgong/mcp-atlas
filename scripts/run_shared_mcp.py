#!/usr/bin/env python3
"""Run the shared MCP container using host/port settings from the root .env."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


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
    image = os.getenv("MCP_SHARED_AGENT_IMAGE", "agent-environment:latest")
    command = [
        "docker", "run", "--rm", "--network", "host",
        "--add-host=host.docker.internal:host-gateway",
        "--env-file", str(ENV_FILE),
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
