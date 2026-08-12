from __future__ import annotations

import asyncio
import os
import time
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import mcp.types
from fastmcp.client.transports import StdioTransport
from mcp.shared.exceptions import McpError

from agent_environment.mcp_client import (
    CLIENT_INIT_TIMEOUT_SECONDS,
    configured_client_init_timeout_seconds,
    create_server_client,
)
from agent_environment.mcp_router import (
    DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    DirectMCPRouter,
    RouterTimeoutError,
    UnknownToolError,
    configured_discovery_timeout_seconds,
)


def make_tool(name: str) -> mcp.types.Tool:
    return mcp.types.Tool(
        name=name,
        description=f"Test tool {name}",
        inputSchema={"type": "object", "properties": {}},
    )


@dataclass
class FakeBackend:
    tools: list[mcp.types.Tool]
    list_calls: int = 0
    call_names: list[str] = field(default_factory=list)
    failures_remaining: int = 0
    list_delay: float = 0.0
    call_delay: float = 0.0
    active_calls: int = 0
    max_active_calls: int = 0
    block_started: asyncio.Event | None = None
    block_release: asyncio.Event | None = None
    closed_while_active: bool = False


class FakeClient:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.close_calls = 0

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    async def list_tools(self) -> list[mcp.types.Tool]:
        self.backend.list_calls += 1
        if self.backend.list_delay:
            await asyncio.sleep(self.backend.list_delay)
        return self.backend.tools

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float | int | None = None,
    ) -> mcp.types.CallToolResult:
        del timeout
        self.backend.call_names.append(name)
        self.backend.active_calls += 1
        self.backend.max_active_calls = max(
            self.backend.max_active_calls,
            self.backend.active_calls,
        )
        try:
            if arguments.get("block"):
                assert self.backend.block_started is not None
                assert self.backend.block_release is not None
                self.backend.block_started.set()
                await self.backend.block_release.wait()
            if arguments.get("fail") or self.backend.failures_remaining:
                if self.backend.failures_remaining:
                    self.backend.failures_remaining -= 1
                raise RuntimeError("simulated broken session")
            if "mcp_error_code" in arguments:
                raise McpError(
                    mcp.types.ErrorData(
                        code=arguments["mcp_error_code"],
                        message="simulated MCP protocol error",
                    )
                )
            if self.backend.call_delay:
                await asyncio.sleep(self.backend.call_delay)
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(
                        type="text",
                        text=f"{name}:{arguments}",
                    )
                ]
            )
        finally:
            self.backend.active_calls -= 1

    async def close(self) -> None:
        if self.backend.active_calls:
            self.backend.closed_while_active = True
        self.close_calls += 1


class FakeFactory:
    def __init__(self, backends: dict[str, FakeBackend]) -> None:
        self.backends = backends
        self.clients: dict[str, list[FakeClient]] = {
            name: [] for name in backends
        }

    def __call__(
        self, server_name: str, server_config: dict[str, Any]
    ) -> FakeClient:
        del server_config
        client = FakeClient(self.backends[server_name])
        self.clients[server_name].append(client)
        return client


class DirectMCPRouterTests(unittest.IsolatedAsyncioTestCase):
    def make_router(
        self,
        **router_options: Any,
    ) -> tuple[DirectMCPRouter, dict[str, FakeBackend], FakeFactory]:
        backends = {
            "calculator": FakeBackend([make_tool("calculate")]),
            "slow-server": FakeBackend([make_tool("lookup")]),
        }
        factory = FakeFactory(backends)
        router = DirectMCPRouter(
            {
                "mcpServers": {
                    "calculator": {"command": "calculator"},
                    "slow-server": {"command": "slow-server"},
                }
            },
            factory,
            **router_options,
        )
        return router, backends, factory

    async def test_inventory_is_cached_and_call_only_hits_owner(self) -> None:
        router, backends, _ = self.make_router()
        await router.start()

        self.assertEqual(1, backends["calculator"].list_calls)
        self.assertEqual(1, backends["slow-server"].list_calls)
        self.assertEqual(
            ["calculator_calculate", "slow-server_lookup"],
            [tool.name for tool in router.list_tools()],
        )

        result = await router.call_tool(
            "calculator_calculate",
            {"left": 1, "right": 1},
        )

        self.assertFalse(result.isError)
        self.assertEqual(["calculate"], backends["calculator"].call_names)
        self.assertEqual([], backends["slow-server"].call_names)
        self.assertEqual(1, backends["calculator"].list_calls)
        self.assertEqual(1, backends["slow-server"].list_calls)

    async def test_same_server_calls_run_concurrently(self) -> None:
        router, backends, _ = self.make_router()
        backends["calculator"].call_delay = 0.05
        await router.start()

        started = time.monotonic()
        await asyncio.gather(
            *(
                router.call_tool("calculator_calculate", {"value": value})
                for value in range(4)
            )
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(4, backends["calculator"].max_active_calls)

    async def test_retired_client_waits_for_in_flight_calls(self) -> None:
        router, backends, factory = self.make_router()
        backend = backends["calculator"]
        backend.block_started = asyncio.Event()
        backend.block_release = asyncio.Event()
        await router.start()
        old_client = factory.clients["calculator"][0]

        blocking_call = asyncio.create_task(
            router.call_tool("calculator_calculate", {"block": True})
        )
        await backend.block_started.wait()

        with self.assertRaisesRegex(RuntimeError, "broken session"):
            await router.call_tool("calculator_calculate", {"fail": True})

        self.assertEqual(2, len(factory.clients["calculator"]))
        self.assertEqual(0, old_client.close_calls)
        await router.call_tool("calculator_calculate", {"new": True})

        backend.block_release.set()
        await blocking_call
        await asyncio.sleep(0)
        self.assertEqual(1, old_client.close_calls)
        self.assertFalse(backend.closed_while_active)

    async def test_broken_client_is_replaced_without_replaying_call(self) -> None:
        router, backends, factory = self.make_router()
        await router.start()
        backends["calculator"].failures_remaining = 1

        with self.assertRaisesRegex(RuntimeError, "broken session"):
            await router.call_tool("calculator_calculate", {})
        await asyncio.sleep(0)

        self.assertEqual(["calculate"], backends["calculator"].call_names)
        self.assertEqual(2, len(factory.clients["calculator"]))
        self.assertEqual(1, factory.clients["calculator"][0].close_calls)

        await router.call_tool("calculator_calculate", {})
        self.assertEqual(
            ["calculate", "calculate"],
            backends["calculator"].call_names,
        )

    async def test_application_mcp_errors_do_not_retire_live_client(self) -> None:
        router, _, factory = self.make_router()
        await router.start()
        original_client = factory.clients["calculator"][0]

        for _ in range(3):
            with self.assertRaisesRegex(McpError, "protocol error"):
                await router.call_tool(
                    "calculator_calculate",
                    {"mcp_error_code": mcp.types.INVALID_PARAMS},
                )

        self.assertEqual(1, len(factory.clients["calculator"]))
        self.assertEqual(0, original_client.close_calls)
        self.assertEqual(
            "OK",
            dict(router.server_statuses())["calculator"],
        )
        details = {
            item["name"]: item for item in router.server_details()
        }
        self.assertIsNone(details["calculator"]["last_error"])

    async def test_connection_closed_mcp_error_retires_client(self) -> None:
        router, _, factory = self.make_router()
        await router.start()

        with self.assertRaisesRegex(McpError, "protocol error"):
            await router.call_tool(
                "calculator_calculate",
                {"mcp_error_code": mcp.types.CONNECTION_CLOSED},
            )
        await asyncio.sleep(0)

        self.assertEqual(2, len(factory.clients["calculator"]))
        self.assertEqual(
            "ERROR_NOT_ONLINE",
            dict(router.server_statuses())["calculator"],
        )

        await router.call_tool("calculator_calculate", {})
        self.assertEqual(
            "OK",
            dict(router.server_statuses())["calculator"],
        )

    async def test_mcp_timeout_reports_configured_gateway_budget(self) -> None:
        router, _, _ = self.make_router(tool_call_timeout=0.5)
        await router.start()

        with self.assertRaisesRegex(
            RouterTimeoutError,
            r"timed out after 0\.5s$",
        ):
            await router.call_tool(
                "calculator_calculate",
                {"mcp_error_code": 408},
            )

    async def test_tool_call_times_out_before_caller_deadline(self) -> None:
        router, backends, factory = self.make_router(tool_call_timeout=0.03)
        backends["calculator"].call_delay = 0.2
        await router.start()

        with self.assertRaisesRegex(RouterTimeoutError, "timed out"):
            await router.call_tool("calculator_calculate", {})
        await asyncio.sleep(0)

        self.assertEqual(2, len(factory.clients["calculator"]))
        details = {
            item["name"]: item for item in router.server_details()
        }
        self.assertIn("timed out", details["calculator"]["last_error"])

    async def test_hung_discovery_does_not_block_startup_forever(self) -> None:
        router, backends, factory = self.make_router(
            discovery_timeout=0.03,
            startup_concurrency=2,
        )
        backends["calculator"].list_delay = 0.2

        started = time.monotonic()
        await router.start()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(2, len(factory.clients["calculator"]))
        self.assertEqual(
            "ERROR_NOT_ONLINE",
            dict(router.server_statuses())["calculator"],
        )

    async def test_startup_discovery_is_concurrent(self) -> None:
        router, backends, _ = self.make_router(startup_concurrency=2)
        backends["calculator"].list_delay = 0.1
        backends["slow-server"].list_delay = 0.1

        started = time.monotonic()
        await router.start()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.17)

    async def test_unknown_tool_refresh_is_throttled_per_server(self) -> None:
        router, backends, _ = self.make_router(refresh_min_interval=0.05)
        await router.start()
        backends["calculator"].tools.append(make_tool("new-tool"))

        with self.assertRaisesRegex(UnknownToolError, "Unknown tool"):
            await router.call_tool("calculator_new-tool", {})
        with self.assertRaisesRegex(UnknownToolError, "Unknown tool"):
            await router.call_tool("calculator_other-hallucination", {})
        self.assertEqual(1, backends["calculator"].list_calls)

        await asyncio.sleep(0.06)
        await router.call_tool("calculator_new-tool", {})

        self.assertEqual(2, backends["calculator"].list_calls)
        self.assertEqual(1, backends["slow-server"].list_calls)
        self.assertEqual(["new-tool"], backends["calculator"].call_names)

        with self.assertRaisesRegex(UnknownToolError, "Unknown tool"):
            await router.call_tool("missing_tool", {})

    async def test_shutdown_waits_for_in_flight_generation(self) -> None:
        router, backends, factory = self.make_router(
            tool_call_timeout=1.0,
            close_timeout=0.2,
        )
        backend = backends["calculator"]
        backend.block_started = asyncio.Event()
        backend.block_release = asyncio.Event()
        await router.start()
        calculator_client = factory.clients["calculator"][0]

        blocking_call = asyncio.create_task(
            router.call_tool("calculator_calculate", {"block": True})
        )
        await backend.block_started.wait()
        shutdown = asyncio.create_task(router.close())
        await asyncio.sleep(0)

        self.assertFalse(shutdown.done())
        self.assertEqual(0, calculator_client.close_calls)

        backend.block_release.set()
        await blocking_call
        await shutdown
        self.assertEqual(1, calculator_client.close_calls)
        self.assertFalse(backend.closed_while_active)

    async def test_factory_uses_direct_transport_timeout_and_roots(self) -> None:
        client = create_server_client(
            "example",
            {"command": "python", "args": ["-c", "pass"]},
        )

        self.assertIsInstance(client.transport, StdioTransport)
        self.assertEqual(CLIENT_INIT_TIMEOUT_SECONDS, client._init_timeout)
        roots_callback = client._session_kwargs["list_roots_callback"]
        self.assertIsNotNone(roots_callback)
        roots_result = await roots_callback(None)
        self.assertEqual([], roots_result.roots)
        await client.close()

    def test_startup_timeouts_are_configurable_and_positive(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MCP_CLIENT_INIT_TIMEOUT_SECONDS": "47.5",
                "MCP_DISCOVERY_TIMEOUT_SECONDS": "53.5",
            },
        ):
            self.assertEqual(47.5, configured_client_init_timeout_seconds())
            self.assertEqual(53.5, configured_discovery_timeout_seconds())

        with patch.dict(
            os.environ,
            {
                "MCP_CLIENT_INIT_TIMEOUT_SECONDS": "",
                "MCP_DISCOVERY_TIMEOUT_SECONDS": "",
            },
        ):
            with self.assertRaises(ValueError):
                configured_client_init_timeout_seconds()
            with self.assertRaises(ValueError):
                configured_discovery_timeout_seconds()

        self.assertEqual(45.0, CLIENT_INIT_TIMEOUT_SECONDS)
        self.assertEqual(50.0, DEFAULT_DISCOVERY_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
