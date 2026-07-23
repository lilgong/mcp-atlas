import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp_completion.errors import MCPClientToolExecutionError
from mcp_completion.mcp_client.isolated_client import IsolatedMCPClient
from mcp_completion.mcp_client.sandbox_client import SandboxMCPClient
from mcp_completion.task_sandbox import (
    TaskSandboxError,
    inspect_mongo_fixture_image,
)
from mcp_completion.tool_policy import ToolRoute


class SandboxClientAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_time_allowlist_blocks_before_http(self):
        client = SandboxMCPClient(
            "http://127.0.0.1:1",
            enabled_tools=["filesystem_read_text_file"],
        )
        with self.assertRaises(MCPClientToolExecutionError):
            await client.call_tool(
                "filesystem_write_file",
                {"path": "/data/x", "content": "x"},
            )

    async def test_mongodb_database_is_forced_to_store(self):
        client = IsolatedMCPClient(
            task_id="mongo-route-test",
            shared_url="http://127.0.0.1:1",
            enabled_tools=["mongodb_find"],
        )
        backend = AsyncMock()
        backend.call_tool.return_value = SimpleNamespace(is_error=False)
        client._entered = True
        client.allowed_tools = {"mongodb_find"}
        client._clients[ToolRoute.TASK_LOCAL] = backend
        with patch(
            "mcp_completion.mcp_client.isolated_client.write_runtime_event"
        ):
            await client.call_tool(
                "mongodb_find",
                {"database": "video_game_store", "collection": "Inventory"},
            )
        backend.call_tool.assert_awaited_once_with(
            "mongodb_find",
            {"database": "store", "collection": "Inventory"},
        )

    async def test_mongo_fixture_contract_requires_content_digest(self):
        labels = (
            '{"mcp-atlas.fixture-id":"synthetic-v1",'
            '"mcp-atlas.logical-database":"store",'
            f'"mcp-atlas.fixture-sha256":"{"a" * 64}"}}'
        )
        with patch(
            "mcp_completion.task_sandbox._run",
            new=AsyncMock(
                return_value=(f"sha256:{'b' * 64}\n{labels}", "", 0)
            ),
        ):
            fixture = await inspect_mongo_fixture_image(
                "mcp-task-mongo:synthetic-v1"
            )
        self.assertEqual("synthetic-v1", fixture["fixture_id"])

        missing_digest = (
            '{"mcp-atlas.fixture-id":"synthetic-v1",'
            '"mcp-atlas.logical-database":"store"}'
        )
        with patch(
            "mcp_completion.task_sandbox._run",
            new=AsyncMock(
                return_value=(
                    f"sha256:{'b' * 64}\n{missing_digest}",
                    "",
                    0,
                )
            ),
        ):
            with self.assertRaisesRegex(
                TaskSandboxError,
                "synthetic store fixture contract",
            ):
                await inspect_mongo_fixture_image(
                    "mcp-task-mongo:missing-digest"
                )


if __name__ == "__main__":
    unittest.main()
