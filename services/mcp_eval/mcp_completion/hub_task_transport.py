"""Generic Hub tool-http-v1 task connections; task ownership remains local."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import uuid
from urllib.parse import urlsplit
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

from .mcp_client.base_client import MCPClient
from .schema import CallToolResponse, ToolDefinition
from .runtime_log import write_runtime_event

router = APIRouter()
# Opaque route IDs bind one server to one task client, never a user-supplied URL.
_bindings: dict[str, tuple[str, MCPClient, frozenset[str]]] = {}
_active_calls: dict[str, set[asyncio.Task]] = {}


def _revoke_binding(binding: str) -> None:
    _bindings.pop(binding, None)
    for call in _active_calls.pop(binding, set()):
        call.cancel()


async def _while_connected(request: Request, binding: str, operation):
    await request.body()
    if binding not in _bindings:
        raise HTTPException(403, "Task connection unavailable")
    call = asyncio.create_task(operation())
    _active_calls.setdefault(binding, set()).add(call)

    async def watch():
        while not call.done():
            if binding not in _bindings or await request.is_disconnected():
                call.cancel()
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch())
    try:
        return await call
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        calls = _active_calls.get(binding)
        if calls is not None:
            calls.discard(call)
            if not calls:
                _active_calls.pop(binding, None)


def task_hub_enabled() -> bool:
    return os.getenv("MCP_TOOL_TRANSPORT", "direct").lower() == "hub" and os.getenv(
        "MCPHUB_TASK_ROUTING", "false"
    ).lower() in {"1", "true", "yes"}


def _root(name: str) -> str:
    value = os.getenv(name, "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} must be an explicit HTTP(S) root URL")
    return value


@router.post("/internal/task-bridge/{binding_id}/{operation}")
async def task_bridge(binding_id: str, operation: str, request: Request) -> Any:
    entry = _bindings.get(binding_id)
    if not entry or not secrets.compare_digest(
        request.headers.get("authorization", ""), f"Bearer {entry[0]}"
    ):
        raise HTTPException(403, "Task connection unavailable")
    _, client, allowed = entry
    if operation == "list-tools":
        return [
            tool.model_dump(by_alias=True)
            for tool in await _while_connected(request, binding_id, client.list_tools)
            if tool.name in allowed
        ]
    if operation != "call-tool":
        raise HTTPException(404, "Unknown operation")
    body = await request.json()
    if (
        not isinstance(body, dict)
        or body.get("tool_name") not in allowed
        or not isinstance(body.get("tool_args"), dict)
    ):
        raise HTTPException(400, "Invalid tool call")
    result = await _while_connected(
        request,
        binding_id,
        lambda: client.call_tool(body["tool_name"], body["tool_args"]),
    )
    return result.model_dump(by_alias=True)


class HubTaskClient(MCPClient):
    """Routes canonical tools through Hub leases, preserving task isolation."""

    def __init__(
        self,
        upstream: MCPClient,
        tools: list[str],
        server_for_tool,
        *,
        task_id: str,
    ):
        self.upstream = upstream
        self.tools = frozenset(tools)
        self.server_for_tool = server_for_tool
        self.root = _root(
            "MCPHUB_CONFIG_URL"
            if os.getenv("MCPHUB_CONFIG_URL", "").strip()
            else "MCPHUB_URL"
        )
        self.callback = _root("MCPHUB_TASK_CALLBACK_URL")
        self.target = os.getenv("MCPHUB_TARGET", "").strip()
        if not self.target:
            raise RuntimeError("Task routing requires an explicit MCPHUB_TARGET")
        token = os.getenv("MCPHUB_TOKEN", "").strip()
        if not token or any(c.isspace() for c in token):
            raise RuntimeError("Task registration requires a Hub operator Bearer Key")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-MCPHub-Target": self.target,
        }
        # Unique even when the same benchmark task is run concurrently by models.
        self.task_id = (
            re.sub(r"[^A-Za-z0-9_.:-]", "_", task_id)[:80] + ":" + uuid.uuid4().hex
        )
        self.connections: dict[str, dict] = {}
        self.binding_ids: list[str] = []
        self.renewal: asyncio.Task | None = None
        self.failed = False
        self.http = httpx.AsyncClient(timeout=180, follow_redirects=False)

    async def start(self) -> "HubTaskClient":
        try:
            # Prime canonical-to-backend mapping before making the callback visible.
            await self.upstream.list_tools()
            servers = {self.server_for_tool(tool) for tool in self.tools}
            if None in servers:
                raise RuntimeError("Unknown task tool server")
            for server in sorted(servers):
                binding = uuid.uuid4().hex
                callback_token = secrets.token_hex(32)
                allowed = frozenset(
                    tool for tool in self.tools if self.server_for_tool(tool) == server
                )
                _bindings[binding] = (callback_token, self.upstream, allowed)
                self.binding_ids.append(binding)
                response = await self.http.post(
                    f"{self.root}/api/task-connections",
                    headers=self.headers,
                    json={
                        "server": server,
                        "target": self.target,
                        "taskId": self.task_id,
                        "url": f"{self.callback}/internal/task-bridge/{binding}",
                        "token": callback_token,
                    },
                    timeout=15,
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Hub rejected task registration for {server} (HTTP {response.status_code})"
                    )
                body = response.json()
                data = body.get("data")
                if (
                    body.get("success") is not True
                    or not isinstance(data, dict)
                    or not all(
                        isinstance(data.get(key), str)
                        for key in ("id", "token", "path")
                    )
                    or data["path"] != f"/gateway/v1/tasks/{data['id']}"
                ):
                    raise RuntimeError("Invalid Hub task registration response")
                self.connections[server] = data
                write_runtime_event(
                    "tools",
                    "hub_task_registered",
                    task_id=self.task_id,
                    server=server,
                    target=self.target,
                    connection_id=data["id"],
                )
            self.renewal = asyncio.create_task(self._renew())
            return self
        except BaseException:
            await asyncio.shield(self.close())
            raise

    async def _renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(120)
                for data in self.connections.values():
                    response = await self.http.put(
                        f"{self.root}/api/task-connections/{data['id']}",
                        headers=self.headers,
                        timeout=15,
                    )
                    if response.status_code != 200:
                        raise RuntimeError("Task lease renewal rejected")
        except asyncio.CancelledError:
            raise
        except Exception:
            write_runtime_event(
                "tools",
                "hub_task_lease_failed",
                task_id=self.task_id,
                target=self.target,
            )
            # Never continue against a recycled container or silently use direct mode.
            self.failed = True
            for binding in self.binding_ids:
                _revoke_binding(binding)

    async def close(self) -> None:
        if self.renewal:
            self.renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.renewal
            self.renewal = None
        self.failed = True
        for binding in self.binding_ids:
            _revoke_binding(binding)
        self.binding_ids.clear()
        try:
            for data in self.connections.values():
                with contextlib.suppress(Exception):
                    await self.http.delete(
                        f"{self.root}/api/task-connections/{data['id']}",
                        headers=self.headers,
                        timeout=5,
                    )
        finally:
            self.connections.clear()
            await self.http.aclose()

    async def _request(self, server: str, operation: str, body: dict) -> Any:
        if self.failed or server not in self.connections:
            raise RuntimeError("Hub task connection unavailable")
        data = self.connections[server]
        response = await self.http.post(
            f"{self.root}{data['path']}/{operation}",
            headers={
                "Authorization": f"Bearer {data['token']}",
                "X-MCPHub-Target": self.target,
            },
            json=body,
        )
        if response.status_code != 200:
            write_runtime_event(
                "tools",
                "hub_task_request_failed",
                task_id=self.task_id,
                server=server,
                target=self.target,
                http_status=response.status_code,
            )
            raise RuntimeError(f"Hub task request failed (HTTP {response.status_code})")
        return response.json()

    async def list_tools(self) -> list[ToolDefinition]:
        results = await asyncio.gather(
            *(self._request(server, "list-tools", {}) for server in self.connections)
        )
        return [
            ToolDefinition(**tool)
            for result in results
            for tool in result
            if tool.get("name") in self.tools
        ]

    async def call_tool(self, tool_name: str, args: Any) -> CallToolResponse:
        if tool_name not in self.tools or not isinstance(args, dict):
            raise RuntimeError("Unknown task tool or invalid arguments")
        body = await self._request(
            self.server_for_tool(tool_name),
            "call-tool",
            {"tool_name": tool_name, "tool_args": args},
        )
        return CallToolResponse(**body)
