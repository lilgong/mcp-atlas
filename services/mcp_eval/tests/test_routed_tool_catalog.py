import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp_completion.main import _list_isolated_routes, build_routed_tool_catalog


class FakeTool:
    def __init__(self, name):
        self.name = name

    def model_copy(self, *, update):
        return FakeTool(update.get("name", self.name))

    def model_dump(self, **_kwargs):
        return {
            "name": self.name,
            "description": "test",
            "inputSchema": {"type": "object"},
        }


def test_routed_catalog_adds_task_only_mongodb_schema(monkeypatch):
    shared = SimpleNamespace(list_tools=AsyncMock(return_value=[FakeTool("notion_API-post-search")]))
    monkeypatch.setattr(
        "mcp_completion.main.SandboxMCPClient",
        lambda *_args, **_kwargs: shared,
    )

    async def isolated(local_servers, network_servers):
        assert local_servers == {"mongodb"}
        assert network_servers == set()
        return [FakeTool("mongodb_list-databases"), FakeTool("mongodb_find")]

    monkeypatch.setattr("mcp_completion.main._list_isolated_routes", isolated)
    monkeypatch.setattr("mcp_completion.main.TASK_LOCAL_SERVERS", frozenset({"mongodb"}))
    monkeypatch.setattr("mcp_completion.main.TASK_NETWORK_SERVERS", frozenset())
    with patch.dict(
        "mcp_completion.main.os.environ",
        {
            "MCP_TASK_ISOLATION_ENABLED": "true",
            "MCP_TASK_MONGO_IMAGE": "mongo:test",
        },
    ):
        result = asyncio.run(build_routed_tool_catalog())

    assert [tool["name"] for tool in result["tools"]] == [
        "mongodb_find", "mongodb_list-databases", "notion_API-post-search",
    ]
    assert result["server_sources"] == {
        "mongodb": "task_local",
        "notion": "shared",
    }


def test_routed_catalog_does_not_claim_mongodb_without_fixture_image(monkeypatch):
    shared = SimpleNamespace(list_tools=AsyncMock(return_value=[FakeTool("notion_API-post-search")]))
    monkeypatch.setattr(
        "mcp_completion.main.SandboxMCPClient",
        lambda *_args, **_kwargs: shared,
    )
    isolated = AsyncMock(return_value=[])
    monkeypatch.setattr("mcp_completion.main._list_isolated_routes", isolated)
    monkeypatch.setattr("mcp_completion.main.TASK_LOCAL_SERVERS", frozenset({"mongodb"}))
    monkeypatch.setattr("mcp_completion.main.TASK_NETWORK_SERVERS", frozenset())
    with patch.dict(
        "mcp_completion.main.os.environ",
        {
            "MCP_TASK_ISOLATION_ENABLED": "true",
            "MCP_TASK_MONGO_IMAGE": "",
        },
    ):
        result = asyncio.run(build_routed_tool_catalog())

    isolated.assert_not_awaited()
    assert result["server_sources"] == {"notion": "shared"}


def test_isolated_catalog_uses_one_sandbox_and_canonicalizes_single_server_names(
    monkeypatch,
):
    class FakeSandbox:
        local_url = "http://local"
        local_container_name = "local-container"
        network_url = "http://network"
        network_container_name = "network-container"

        def __init__(self):
            self.started = False
            self.closed = False

        async def start(self):
            self.started = True

        async def close(self):
            self.closed = True

    sandbox = FakeSandbox()
    from_servers = patch(
        "mcp_completion.main.TaskSandbox.from_servers",
        return_value=sandbox,
    )

    def client(_url, *, enabled_tools, container_name):
        assert enabled_tools is None
        names = {
            "local-container": [FakeTool("read_file")],
            "network-container": [FakeTool("search_papers")],
        }
        return SimpleNamespace(list_tools=AsyncMock(return_value=names[container_name]))

    monkeypatch.setattr("mcp_completion.main.SandboxMCPClient", client)
    monkeypatch.setattr(
        "mcp_completion.main.server_for_tool",
        lambda name: (
            "filesystem" if name.startswith("filesystem_") else
            "arxiv" if name.startswith("arxiv_") else
            None
        ),
    )
    with from_servers as factory:
        result = asyncio.run(
            _list_isolated_routes({"filesystem"}, {"arxiv"})
        )

    factory.assert_called_once()
    assert sandbox.started is True
    assert sandbox.closed is True
    assert [tool.name for tool in result] == [
        "filesystem_read_file",
        "arxiv_search_papers",
    ]


def test_isolated_catalog_closes_sandbox_when_listing_fails(monkeypatch):
    class FakeSandbox:
        local_url = "http://local"
        local_container_name = "local-container"
        network_url = None
        network_container_name = None

        def __init__(self):
            self.closed = False

        async def start(self):
            return None

        async def close(self):
            self.closed = True

    sandbox = FakeSandbox()
    monkeypatch.setattr(
        "mcp_completion.main.TaskSandbox.from_servers",
        lambda *_args, **_kwargs: sandbox,
    )
    monkeypatch.setattr(
        "mcp_completion.main.SandboxMCPClient",
        lambda *_args, **_kwargs: SimpleNamespace(
            list_tools=AsyncMock(side_effect=RuntimeError("broken list-tools"))
        ),
    )

    async def exercise():
        try:
            await _list_isolated_routes({"filesystem"}, set())
        except RuntimeError as exc:
            assert str(exc) == "broken list-tools"
        else:
            raise AssertionError("expected list-tools failure")

    asyncio.run(exercise())
    assert sandbox.closed is True
