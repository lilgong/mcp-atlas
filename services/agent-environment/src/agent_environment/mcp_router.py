from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Callable, Protocol

import mcp.types
from mcp.shared.exceptions import McpError

from .logger import create_logger


logger = create_logger(__name__)

DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 30.0
# Three Overpass endpoints may each consume the shim's bounded 45-second
# attempt before succeeding or falling back. Keep enough room for all three
# without weakening the outer evaluator's 180-second deadline.
DEFAULT_TOOL_CALL_TIMEOUT_SECONDS = 150.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0
DEFAULT_STARTUP_CONCURRENCY = 12
DEFAULT_REFRESH_MIN_INTERVAL_SECONDS = 60.0
DEFAULT_FAILED_REFRESH_RETRY_SECONDS = 5.0
RETIRE_MCP_ERROR_CODES = {408, mcp.types.CONNECTION_CLOSED}


class RouterClient(Protocol):
    async def __aenter__(self) -> "RouterClient": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    async def list_tools(self) -> list[mcp.types.Tool]: ...

    async def call_tool_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        timeout: float | int | None = None,
    ) -> mcp.types.CallToolResult: ...

    async def close(self) -> None: ...


ClientFactory = Callable[[str, dict[str, Any]], RouterClient]


class UnknownToolError(LookupError):
    pass


class RouterTimeoutError(TimeoutError):
    pass


@dataclass(eq=False)
class ClientGeneration:
    client: RouterClient
    generation: int
    in_flight: int = 0
    retired: bool = False
    close_scheduled: bool = False
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.drained.set()


@dataclass
class ServerState:
    name: str
    server_config: dict[str, Any]
    current: ClientGeneration
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tools: dict[str, mcp.types.Tool] = field(default_factory=dict)
    error: str | None = None
    in_flight: int = 0
    last_refresh_monotonic: float | None = None
    last_refresh_at: float | None = None
    last_success_at: float | None = None
    last_error_at: float | None = None


class DirectMCPRouter:
    """Route prefixed tool names directly to one MCP server.

    Tool inventory is cached per server. Calls lease the current client
    generation without holding a server lock, so one MCP session can multiplex
    concurrent requests. A broken generation is replaced atomically and closed
    only after its in-flight requests have drained.
    """

    def __init__(
        self,
        config: dict[str, Any],
        client_factory: ClientFactory,
        *,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        tool_call_timeout: float = DEFAULT_TOOL_CALL_TIMEOUT_SECONDS,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
        startup_concurrency: int = DEFAULT_STARTUP_CONCURRENCY,
        refresh_min_interval: float = DEFAULT_REFRESH_MIN_INTERVAL_SECONDS,
        failed_refresh_retry: float = DEFAULT_FAILED_REFRESH_RETRY_SECONDS,
    ) -> None:
        server_configs = config.get("mcpServers", {})
        self._client_factory = client_factory
        self._states = {
            name: ServerState(
                name=name,
                server_config=server_config,
                current=ClientGeneration(
                    client=client_factory(name, server_config),
                    generation=0,
                ),
            )
            for name, server_config in server_configs.items()
        }
        self._routes: dict[str, tuple[str, str]] = {}
        self._public_tools: dict[str, mcp.types.Tool] = {}
        self._start_lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._live_generations = {
            state.current for state in self._states.values()
        }
        self._discovery_timeout = discovery_timeout
        self._tool_call_timeout = tool_call_timeout
        self._close_timeout = close_timeout
        self._startup_concurrency = max(1, startup_concurrency)
        self._refresh_min_interval = max(0.0, refresh_min_interval)
        self._failed_refresh_retry = max(0.0, failed_refresh_retry)
        self._started = False
        self._closing = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            self._closing = False
            for state in self._states.values():
                async with state.lock:
                    if state.current.retired:
                        state.current = ClientGeneration(
                            client=self._client_factory(
                                state.name,
                                state.server_config,
                            ),
                            generation=state.current.generation + 1,
                        )
                        self._live_generations.add(state.current)
            semaphore = asyncio.Semaphore(self._startup_concurrency)

            async def discover(server_name: str) -> None:
                async with semaphore:
                    await self.refresh_server(server_name, force=True)

            await asyncio.gather(
                *(discover(server_name) for server_name in self._states)
            )
            self._started = True

    async def close(self) -> None:
        self._closing = True
        for state in self._states.values():
            async with state.lock:
                generation = state.current
                generation.retired = True

        try:
            async with asyncio.timeout(
                self._tool_call_timeout + self._close_timeout
            ):
                await asyncio.gather(
                    *(
                        generation.drained.wait()
                        for generation in list(self._live_generations)
                    )
                )
        except TimeoutError:
            logger.error(
                "Timed out waiting for in-flight MCP calls during shutdown"
            )

        for generation in list(self._live_generations):
            self._schedule_close(generation)

        while self._cleanup_tasks:
            await asyncio.gather(
                *list(self._cleanup_tasks),
                return_exceptions=True,
            )
        self._started = False

    def list_tools(self) -> list[mcp.types.Tool]:
        return list(self._public_tools.values())

    def server_statuses(self) -> list[tuple[str, str]]:
        return [
            (
                state.name,
                (
                    "OK"
                    if state.tools and state.error is None
                    else "ERROR_NOT_ONLINE"
                ),
            )
            for state in sorted(self._states.values(), key=lambda item: item.name)
        ]

    def server_details(self) -> list[dict[str, Any]]:
        details = []
        for state in sorted(self._states.values(), key=lambda item: item.name):
            details.append(
                {
                    "name": state.name,
                    "status": (
                        "OK"
                        if state.tools and state.error is None
                        else "ERROR_NOT_ONLINE"
                    ),
                    "tool_count": len(state.tools),
                    "generation": state.current.generation,
                    "in_flight": state.in_flight,
                    "last_error": state.error,
                    "last_refresh_at": state.last_refresh_at,
                    "last_success_at": state.last_success_at,
                    "last_error_at": state.last_error_at,
                }
            )
        return details

    async def refresh_server(
        self,
        server_name: str,
        *,
        force: bool = False,
    ) -> bool:
        state = self._states[server_name]
        async with state.refresh_lock:
            now = asyncio.get_running_loop().time()
            retry_interval = (
                self._failed_refresh_retry
                if state.error is not None and not state.tools
                else self._refresh_min_interval
            )
            if (
                not force
                and state.last_refresh_monotonic is not None
                and now - state.last_refresh_monotonic < retry_interval
            ):
                return False

            state.last_refresh_monotonic = now
            state.last_refresh_at = time.time()
            generation = await self._acquire_generation(state)
            try:
                async with asyncio.timeout(self._discovery_timeout):
                    async with generation.client:
                        tools = await generation.client.list_tools()
            except TimeoutError:
                message = (
                    f"Tool discovery timed out after "
                    f"{self._discovery_timeout:g}s"
                )
                await self._mark_failure_and_retire(
                    state,
                    generation,
                    message,
                )
                logger.error(
                    "Failed to discover tools for MCP server '%s': %s",
                    server_name,
                    message,
                )
                return False
            except Exception as exc:
                await self._mark_failure_and_retire(
                    state,
                    generation,
                    str(exc),
                )
                logger.error(
                    "Failed to discover tools for MCP server '%s': %s",
                    server_name,
                    exc,
                )
                return False
            finally:
                await self._release_generation(state, generation)

            state.tools = {tool.name: tool for tool in tools}
            self._replace_server_routes(server_name, tools)
            await self._mark_success(state, generation)
            return True

    async def call_tool(
        self, public_name: str, arguments: dict[str, Any]
    ) -> mcp.types.CallToolResult:
        if self._closing:
            raise RuntimeError("MCP router is shutting down")
        if not self._started:
            await self.start()

        route = self._routes.get(public_name)
        if route is None:
            server_name = self._server_for_public_name(public_name)
            if server_name is None:
                raise UnknownToolError(f"Unknown tool: {public_name}")
            await self.refresh_server(server_name)
            route = self._routes.get(public_name)
            if route is None:
                raise UnknownToolError(f"Unknown tool: {public_name}")

        server_name, raw_name = route
        state = self._states[server_name]
        generation = await self._acquire_generation(state)
        request_timeout = max(
            0.001,
            self._tool_call_timeout - min(1.0, self._tool_call_timeout * 0.1),
        )
        try:
            async with asyncio.timeout(self._tool_call_timeout):
                async with generation.client:
                    result = await generation.client.call_tool_mcp(
                        raw_name,
                        arguments,
                        timeout=request_timeout,
                    )
        except TimeoutError as exc:
            message = (
                f"Tool '{public_name}' timed out after "
                f"{self._tool_call_timeout:g}s"
            )
            await self._mark_failure_and_retire(
                state,
                generation,
                message,
            )
            raise RouterTimeoutError(message) from exc
        except McpError as exc:
            if exc.error.code not in RETIRE_MCP_ERROR_CODES:
                # A structured JSON-RPC error proves the session responded.
                # Invalid params, method errors, and server validation failures
                # must not be treated as transport failures.
                await self._mark_success(state, generation)
                raise
            if exc.error.code == 408:
                message = (
                    f"Tool '{public_name}' timed out after "
                    f"{self._tool_call_timeout:g}s"
                )
                await self._mark_failure_and_retire(
                    state,
                    generation,
                    message,
                )
                raise RouterTimeoutError(message) from exc
            await self._mark_failure_and_retire(
                state,
                generation,
                str(exc),
            )
            raise
        except Exception as exc:
            await self._mark_failure_and_retire(
                state,
                generation,
                str(exc),
            )
            raise
        else:
            await self._mark_success(state, generation)
            return result
        finally:
            await self._release_generation(state, generation)

    async def _acquire_generation(
        self,
        state: ServerState,
    ) -> ClientGeneration:
        async with state.lock:
            if self._closing:
                raise RuntimeError("MCP router is shutting down")
            generation = state.current
            if generation.in_flight == 0:
                generation.drained.clear()
            generation.in_flight += 1
            state.in_flight += 1
            return generation

    async def _release_generation(
        self,
        state: ServerState,
        generation: ClientGeneration,
    ) -> None:
        should_close = False
        async with state.lock:
            generation.in_flight = max(0, generation.in_flight - 1)
            state.in_flight = max(0, state.in_flight - 1)
            if generation.in_flight == 0:
                generation.drained.set()
            should_close = generation.retired and generation.in_flight == 0
        if should_close:
            self._schedule_close(generation)

    async def _mark_success(
        self,
        state: ServerState,
        generation: ClientGeneration,
    ) -> None:
        async with state.lock:
            if state.current is generation:
                state.error = None
                state.last_success_at = time.time()

    async def _mark_failure_and_retire(
        self,
        state: ServerState,
        generation: ClientGeneration,
        error: str,
    ) -> None:
        should_close = False
        async with state.lock:
            if state.current is not generation:
                return
            state.error = error
            state.last_error_at = time.time()
            generation.retired = True
            if not self._closing:
                state.current = ClientGeneration(
                    client=self._client_factory(state.name, state.server_config),
                    generation=generation.generation + 1,
                )
                self._live_generations.add(state.current)
            should_close = generation.in_flight == 0
        if should_close:
            self._schedule_close(generation)

    def _schedule_close(self, generation: ClientGeneration) -> None:
        if generation.close_scheduled:
            return
        generation.close_scheduled = True
        task = asyncio.create_task(self._close_client(generation.client))
        self._cleanup_tasks.add(task)

        def cleanup_finished(done: asyncio.Task[None]) -> None:
            self._cleanup_tasks.discard(done)
            self._live_generations.discard(generation)

        task.add_done_callback(cleanup_finished)

    async def _close_client(self, client: RouterClient) -> None:
        try:
            async with asyncio.timeout(self._close_timeout):
                await client.close()
        except TimeoutError:
            logger.error(
                "Timed out closing retired MCP client after %ss",
                f"{self._close_timeout:g}",
            )
        except Exception as exc:
            logger.warning("Failed to close retired MCP client: %s", exc)

    def _server_for_public_name(self, public_name: str) -> str | None:
        matches = [
            name
            for name in self._states
            if public_name.startswith(f"{name}_")
        ]
        return max(matches, key=len) if matches else None

    def _replace_server_routes(
        self,
        server_name: str,
        tools: list[mcp.types.Tool],
    ) -> None:
        names = [
            public_name
            for public_name, route in self._routes.items()
            if route[0] == server_name
        ]
        for public_name in names:
            self._routes.pop(public_name, None)
            self._public_tools.pop(public_name, None)

        for tool in tools:
            public_name = f"{server_name}_{tool.name}"
            self._routes[public_name] = (server_name, tool.name)
            self._public_tools[public_name] = tool.model_copy(
                update={"name": public_name}
            )
