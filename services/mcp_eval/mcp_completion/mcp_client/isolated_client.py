"""Composite MCP client with per-task disposable local environments."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .base_client import MCPClient
from .sandbox_client import SandboxMCPClient
from ..runtime_log import jsonable, write_runtime_event
from ..account_guard import (
    FatalAccountError,
    credential_envs_for_mcp_server,
    is_fatal_tool_result,
)
from ..schema import CallToolResponse, ToolDefinition
from ..task_sandbox import TaskSandbox
from ..tool_policy import (
    TASK_MONGODB_DATABASE,
    ToolRoute,
    partition_tools,
    route_for_tool,
    server_for_tool,
)


class ToolPolicyError(RuntimeError):
    pass


_SANDBOX_SEMAPHORE = asyncio.Semaphore(
    int(os.getenv("MCP_TASK_SANDBOX_CONCURRENCY", "20"))
)


# Public services publish aggregate client limits.  All concurrent evaluations
# in this process pass through this client, so pace them here without changing
# MCP schemas or wrapping the server implementation.
SERVER_CALL_POLICIES: dict[str, tuple[int, float]] = {
    "arxiv": (1, 3.0),
    "brave-search": (1, 1.0),
    "osm-mcp-server": (1, 1.0),
}


@dataclass
class ServerCallGate:
    concurrency: int
    min_interval: float
    semaphore: asyncio.Semaphore = field(init=False)
    schedule_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_started: float = 0.0

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.concurrency)

    @contextlib.asynccontextmanager
    async def slot(self):
        queued_at = time.monotonic()
        async with self.semaphore:
            async with self.schedule_lock:
                delay = self.min_interval - (time.monotonic() - self.last_started)
                if delay > 0:
                    await asyncio.sleep(delay)
                self.last_started = time.monotonic()
            yield int((time.monotonic() - queued_at) * 1000)


_SERVER_CALL_GATES = {
    server: ServerCallGate(concurrency, min_interval)
    for server, (concurrency, min_interval) in SERVER_CALL_POLICIES.items()
}


@contextlib.asynccontextmanager
async def _server_call_slot(tool_name: str):
    gate = _SERVER_CALL_GATES.get(server_for_tool(tool_name) or "")
    if gate is None:
        yield 0
        return
    async with gate.slot() as queued_ms:
        yield queued_ms


class IsolatedMCPClient(MCPClient):
    """Route cloud reads to the shared service and local state to task containers."""

    def __init__(
        self,
        *,
        task_id: str,
        shared_url: str,
        enabled_tools: Optional[List[str]],
    ):
        self.task_id = task_id
        self.shared_url = shared_url.rstrip("/")
        self.requested_tools = list(dict.fromkeys(enabled_tools or []))
        self.allowed_tools: set[str] = set()
        self.blocked_tools: set[str] = set()
        self._route_tools: dict[ToolRoute, list[str]] = {}
        self._clients: dict[ToolRoute, SandboxMCPClient] = {}
        self._sandbox: Optional[TaskSandbox] = None
        self._semaphore_acquired = False
        self._entered = False

    async def __aenter__(self) -> "IsolatedMCPClient":
        if self._entered:
            return self
        self._entered = True

        requested = self.requested_tools
        if not requested:
            catalog_client = SandboxMCPClient(self.shared_url, enabled_tools=None)
            requested = [tool.name for tool in await catalog_client.list_tools()]

        self._route_tools = partition_tools(requested)
        self.blocked_tools = set(
            self._route_tools[ToolRoute.BLOCKED_CLOUD_WRITE]
        ) | set(self._route_tools[ToolRoute.BLOCKED_UNSUPPORTED])
        self.allowed_tools = set(requested) - self.blocked_tools

        if self.blocked_tools:
            write_runtime_event(
                "tools",
                "cloud_write_tools_blocked",
                task_id=self.task_id,
                tools=sorted(self.blocked_tools),
            )

        isolation_enabled = (
            os.getenv("MCP_TASK_ISOLATION_ENABLED", "true").lower()
            not in {"0", "false", "no"}
        )
        try:
            if isolation_enabled:
                await self._start_isolated_clients()
            else:
                # Explicit compatibility escape hatch. Cloud writes remain blocked.
                safe_tools = sorted(self.allowed_tools)
                shared_client = SandboxMCPClient(
                    self.shared_url, enabled_tools=safe_tools
                )
                for route in (
                    ToolRoute.CLOUD,
                    ToolRoute.TASK_LOCAL,
                    ToolRoute.TASK_NETWORK,
                ):
                    self._clients[route] = shared_client
                write_runtime_event(
                    "sandbox",
                    "task_isolation_disabled",
                    task_id=self.task_id,
                )
        except BaseException:
            await asyncio.shield(self.close())
            raise

        write_runtime_event(
            "tools",
            "task_tool_routes_ready",
            task_id=self.task_id,
            routes={
                route.value: tools
                for route, tools in self._route_tools.items()
                if tools
            },
            allowed_count=len(self.allowed_tools),
            blocked_count=len(self.blocked_tools),
        )
        return self

    async def _start_isolated_clients(self) -> None:
        local_tools = self._route_tools[ToolRoute.TASK_LOCAL]
        network_tools = self._route_tools[ToolRoute.TASK_NETWORK]
        cloud_tools = self._route_tools[ToolRoute.CLOUD]

        if local_tools or network_tools:
            await _SANDBOX_SEMAPHORE.acquire()
            self._semaphore_acquired = True

            local_servers = {
                server
                for tool in local_tools
                if (server := server_for_tool(tool)) is not None
            }
            network_servers = {
                server
                for tool in network_tools
                if (server := server_for_tool(tool)) is not None
            }
            self._sandbox = TaskSandbox.from_servers(
                self.task_id,
                local_servers=local_servers,
                network_servers=network_servers,
            )
            await self._sandbox.start()

            if local_tools:
                if not self._sandbox.local_url:
                    raise RuntimeError("Task-local sandbox started without a URL")
                self._clients[ToolRoute.TASK_LOCAL] = SandboxMCPClient(
                    self._sandbox.local_url,
                    enabled_tools=local_tools,
                    container_name=self._sandbox.local_container_name,
                )
            if network_tools:
                if not self._sandbox.network_url:
                    raise RuntimeError("Task-network sandbox started without a URL")
                self._clients[ToolRoute.TASK_NETWORK] = SandboxMCPClient(
                    self._sandbox.network_url,
                    enabled_tools=network_tools,
                )

        if cloud_tools:
            self._clients[ToolRoute.CLOUD] = SandboxMCPClient(
                self.shared_url,
                enabled_tools=cloud_tools,
            )

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        # A cancelled request (client hang-up, timeout) would otherwise abort
        # teardown at its first await and orphan the sandbox containers, so let
        # the shielded cleanup finish even while the cancellation propagates.
        cleanup = asyncio.ensure_future(self.close())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.wait({cleanup})
            raise

    async def close(self) -> None:
        try:
            if self._sandbox is not None:
                await self._sandbox.close()
                self._sandbox = None
        finally:
            if self._semaphore_acquired:
                _SANDBOX_SEMAPHORE.release()
                self._semaphore_acquired = False

    async def list_tools(self) -> List[ToolDefinition]:
        if not self._entered:
            raise RuntimeError("IsolatedMCPClient must be entered before use")

        lists = await asyncio.gather(
            *(client.list_tools() for client in self._clients.values())
        )
        by_name: dict[str, ToolDefinition] = {}
        for tools in lists:
            for tool in tools:
                if tool.name in self.allowed_tools:
                    by_name.setdefault(tool.name, tool)
        return list(by_name.values())

    async def call_tool(self, tool_name: str, args: Any) -> CallToolResponse:
        if tool_name in self.blocked_tools or route_for_tool(
            tool_name
        ) in {
            ToolRoute.BLOCKED_CLOUD_WRITE,
            ToolRoute.BLOCKED_UNSUPPORTED,
        }:
            route = route_for_tool(tool_name)
            reason = (
                "Cloud account write tool is disabled"
                if route == ToolRoute.BLOCKED_CLOUD_WRITE
                else "Tool is unavailable in the deterministic offline runtime"
            )
            raise ToolPolicyError(
                f"{reason}: {tool_name}"
            )
        if tool_name not in self.allowed_tools:
            raise ToolPolicyError(
                f"Tool is not enabled for task {self.task_id}: {tool_name}"
            )

        route = route_for_tool(tool_name)
        client = self._clients.get(route)
        if client is None:
            raise ToolPolicyError(
                f"No {route.value} backend is available for tool {tool_name}"
            )
        if (
            server_for_tool(tool_name) == "mongodb"
            and isinstance(args, dict)
            and "database" in args
        ):
            args = {**args, "database": TASK_MONGODB_DATABASE}

        call_id = uuid.uuid4().hex
        async with _server_call_slot(tool_name) as rate_limit_queued_ms:
            started = time.monotonic()
            write_runtime_event(
                "tools",
                "tool_call_started",
                task_id=self.task_id,
                call_id=call_id,
                tool_name=tool_name,
                route=route.value,
                arguments=args,
                rate_limit_queued_ms=rate_limit_queued_ms,
            )
            try:
                response = await client.call_tool(tool_name, args)
            except Exception as exc:
                write_runtime_event(
                    "tools",
                    "tool_call_failed",
                    task_id=self.task_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    route=route.value,
                    duration_seconds=round(time.monotonic() - started, 3),
                    error=str(exc),
                )
                raise

        serialized = jsonable(response)
        if is_fatal_tool_result(response):
            server = server_for_tool(tool_name) or tool_name
            write_runtime_event(
                "tools",
                "tool_account_failure",
                task_id=self.task_id,
                call_id=call_id,
                tool_name=tool_name,
                route=route.value,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            raise FatalAccountError(
                "MCP credential is invalid or out of funds",
                source_kind="mcp",
                source_name=server,
                credential_envs=credential_envs_for_mcp_server(server),
            )
        write_runtime_event(
            "tools",
            "tool_call_completed",
            task_id=self.task_id,
            call_id=call_id,
            tool_name=tool_name,
            route=route.value,
            duration_seconds=round(time.monotonic() - started, 3),
            is_error=response.is_error,
            result_chars=len(str(serialized)),
        )
        return response
