"""HTTP-over-docker-exec transport for loopback-only task containers."""

from __future__ import annotations

import asyncio
import json
from typing import Any


_CONTAINER_POST_SCRIPT = r"""
import json
import sys
import urllib.error
import urllib.request

request_data = json.load(sys.stdin)
body = json.dumps(request_data.get("body", {})).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:1984" + request_data["path"],
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=request_data["timeout"]) as response:
        result = {
            "status": response.status,
            "body": response.read().decode("utf-8", errors="replace"),
        }
except urllib.error.HTTPError as error:
    result = {
        "status": error.code,
        "body": error.read().decode("utf-8", errors="replace"),
    }
print(json.dumps(result))
"""


class DockerHTTPError(RuntimeError):
    pass


async def docker_post_json(
    container_name: str,
    path: str,
    body: Any,
    *,
    timeout: float,
) -> tuple[int, str]:
    """POST to a loopback-only service inside a task container."""

    process = await asyncio.create_subprocess_exec(
        "docker",
        "exec",
        "-i",
        container_name,
        "/agent-environment/.venv/bin/python",
        "-c",
        _CONTAINER_POST_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    payload = json.dumps(
        {"path": path, "body": body, "timeout": timeout}
    ).encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise DockerHTTPError(
            f"docker exec HTTP request timed out after {timeout:.0f}s"
        ) from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise DockerHTTPError(
            f"docker exec HTTP request failed: {detail or process.returncode}"
        )
    try:
        response = json.loads(stdout.decode("utf-8"))
        return int(response["status"]), str(response["body"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DockerHTTPError("Invalid docker exec HTTP response") from exc
