from __future__ import annotations

import asyncio
import socket
from unittest.mock import patch

import pytest
import uvicorn
from fastmcp import FastMCP

from agent_environment.mcphub_transport import hub_server_configs
from agent_environment.mcp_client import create_server_client
from agent_environment.mcp_router import DirectMCPRouter


ENV = {
    "MCP_TOOL_TRANSPORT": "hub",
    "MCPHUB_URL": "http://127.0.0.1:3002",
    "MCPHUB_TOKEN": "fake-hub-token",
    "MCPHUB_SERVERS": "brave-search",
    "MCPHUB_NAME_SEPARATOR": "_",
}


def test_direct_does_not_require_hub_configuration():
    assert hub_server_configs({}, {"brave-search"}) == {}
    assert (
        hub_server_configs({**ENV, "MCP_TOOL_TRANSPORT": "direct"}, {"brave-search"})
        == {}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"MCP_TOOL_TRANSPORT": "bad"},
        {"MCPHUB_TOKEN": ""},
        {"MCPHUB_URL": "file:///tmp/a"},
        {"MCPHUB_URL": "http://user:secret@hub"},
        {"MCPHUB_URL": "http://hub?key=secret"},
        {"MCPHUB_SERVERS": "filesystem"},
        {"MCPHUB_SERVERS": "unknown"},
        {"MCPHUB_SERVERS": ""},
    ],
)
def test_invalid_hub_config_fails_closed(changes):
    with pytest.raises(ValueError):
        hub_server_configs({**ENV, **changes}, {"brave-search", "filesystem"})


def test_hub_config_has_no_upstream_keys_or_local_command():
    result = hub_server_configs(ENV, {"brave-search", "exa"})
    assert set(result) == {"brave-search"}
    assert result["brave-search"]["url"].endswith("/mcp/brave-search")
    assert "command" not in result["brave-search"]
    assert result["brave-search"]["headers"] == {
        "Authorization": "Bearer fake-hub-token"
    }


def test_real_streamable_http_preserves_atlas_names_and_auth():
    async def run():
        mcp = FastMCP("mock-hub")

        @mcp.tool(name="brave-search_brave_web_search")
        def search(query: str) -> str:
            return "fake:" + query

        app = mcp.http_app(path="/mcp/brave-search")
        seen_auth = []

        class Auth:
            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    headers = dict(scope["headers"])
                    seen_auth.append(headers.get(b"authorization"))
                    if headers.get(b"authorization") != b"Bearer fake-hub-token":
                        await send(
                            {
                                "type": "http.response.start",
                                "status": 401,
                                "headers": [],
                            }
                        )
                        await send(
                            {"type": "http.response.body", "body": b"unauthorized"}
                        )
                        return
                await app(scope, receive, send)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(Auth(), log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        router = None
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            configs = hub_server_configs(
                {**ENV, "MCPHUB_URL": f"http://127.0.0.1:{port}"}, {"brave-search"}
            )
            router = DirectMCPRouter({"mcpServers": configs}, create_server_client)
            await router.start()
            assert [tool.name for tool in router.list_tools()] == [
                "brave-search_brave_web_search"
            ]
            result = await router.call_tool(
                "brave-search_brave_web_search", {"query": "test"}
            )
            assert "fake:test" in result.content[0].text
            assert seen_auth and all(
                auth == b"Bearer fake-hub-token" for auth in seen_auth
            )
        finally:
            if router:
                await router.close()
            server.should_exit = True
            await task
            sock.close()

    asyncio.run(run())


def test_bad_hub_prefix_is_rejected():
    # Even a valid session must not silently change Atlas's public tool names.
    from fastmcp import Client
    import mcp.types
    from agent_environment.mcp_client import HubMCPClient

    client = HubMCPClient(FastMCP("bad-prefix"))
    client.hub_prefix = "brave-search_"

    async def run():
        with patch.object(
            Client,
            "list_tools",
            return_value=[
                mcp.types.Tool(
                    name="wrong_search",
                    inputSchema={"type": "object"},
                )
            ],
        ):
            with pytest.raises(ValueError, match="prefix mismatch"):
                await client.list_tools()

    asyncio.run(run())


def test_target_routes_without_reporting_labels_and_direct_mode_ignores_it():
    environment = {**ENV, "MCPHUB_TARGET": "another-benchmark/suite-2", "MCPHUB_BENCHMARK": "another-benchmark", "MCPHUB_MODEL": "test-model", "MCPHUB_RUN_ID": "run-2"}
    headers = hub_server_configs(environment, {"brave-search"})["brave-search"]["headers"]
    assert headers["X-MCPHub-Target"] == "another-benchmark/suite-2"
    assert set(headers) == {"Authorization", "X-MCPHub-Target"}
    assert hub_server_configs({**environment, "MCP_TOOL_TRANSPORT": "direct"}, {"brave-search"}) == {}


@pytest.mark.parametrize("target", ["x" * 161, "atlas/official\nother"])
def test_invalid_target_is_rejected_instead_of_truncated(target):
    with pytest.raises(ValueError, match="MCPHUB_TARGET"):
        hub_server_configs({**ENV, "MCPHUB_TARGET": target}, {"brave-search"})


@pytest.mark.parametrize("server,key_name", [("context7", "CONTEXT7_API_KEYS"), ("twelvedata", "TWELVE_DATA_API_KEYS")])
def test_hub_does_not_apply_atlas_upstream_key_pool(server, key_name, monkeypatch):
    from agent_environment import mcp_client
    monkeypatch.setenv(key_name, '["old-key-1", "old-key-2"]')
    config = {"atlas_hub_prefix": server + "-"}
    marker = object()
    with patch.object(mcp_client, "_create_direct_client", return_value=marker) as create:
        assert create_server_client(server, config) is marker
        create.assert_called_once_with(server, config)


def test_hub_calls_never_use_local_tool_result_cache(monkeypatch):
    from agent_environment.main import should_cache_tool
    monkeypatch.setenv("MCP_TOOL_TRANSPORT", "hub")
    assert should_cache_tool("brave-search_brave_web_search") is False
    assert should_cache_tool("wikipedia_search") is False
