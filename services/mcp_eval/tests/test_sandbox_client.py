import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp_completion.errors import MCPClientToolExecutionError
from mcp_completion.mcp_client import isolated_client
from mcp_completion.mcp_client.isolated_client import (
    IsolatedMCPClient,
    ToolPolicyError,
)
from mcp_completion.mcp_client.sandbox_client import SandboxMCPClient
from mcp_completion.task_sandbox import (
    TaskSandboxError,
    inspect_mongo_fixture_image,
)
from mcp_completion.tool_policy import ToolRoute


class SandboxClientAllowlistTests(unittest.IsolatedAsyncioTestCase):
    def test_public_server_policies_match_upstream_limits(self):
        self.assertEqual(
            (1, 1.2, 2.0, 10.0),
            isolated_client.SERVER_CALL_POLICIES["brave-search"],
        )
        self.assertEqual(
            (1, 3.0, 15.0, 60.0),
            isolated_client.SERVER_CALL_POLICIES["arxiv"],
        )
        self.assertEqual(
            (1, 1.0, 15.0, 60.0),
            isolated_client.SERVER_CALL_POLICIES["twelvedata"],
        )

    def test_rate_limit_detection_requires_error_shaped_result(self):
        rate_limited = {
            "content": [
                {
                    "type": "text",
                    "text": "Error: Page request resulted in HTTP 429",
                }
            ],
            "isError": False,
        }
        nested_error = {
            "content": [
                {
                    "type": "text",
                    "text": '{"detail":"execution failed: 429 Too Many Requests"}',
                }
            ],
            "isError": False,
        }
        normal_data = {
            "content": [
                {
                    "type": "text",
                    "text": '{"close":429,"status":"ok"}',
                }
            ],
            "isError": False,
        }
        self.assertTrue(
            isolated_client._is_rate_limited_tool_result(rate_limited)
        )
        self.assertTrue(
            isolated_client._is_rate_limited_tool_result(nested_error)
        )
        self.assertFalse(
            isolated_client._is_rate_limited_tool_result(normal_data)
        )

    def test_rate_limit_backoff_grows_and_success_recovers_gradually(self):
        gate = isolated_client.ServerCallGate(1, 3.0, 15.0, 60.0)
        with patch(
            "mcp_completion.mcp_client.isolated_client.time.monotonic",
            return_value=100.0,
        ):
            self.assertEqual(15.0, gate.observe_rate_limit(True))
            self.assertEqual(30.0, gate.observe_rate_limit(True))
            self.assertEqual(60.0, gate.observe_rate_limit(True))
            self.assertEqual(0.0, gate.observe_rate_limit(False))
        self.assertEqual(2, gate.consecutive_rate_limits)
        self.assertEqual(160.0, gate.cooldown_until)

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

    async def test_unknown_tool_uses_upstream_style_without_name_hint(self):
        client = IsolatedMCPClient(
            task_id="tool-name-test",
            shared_url="http://127.0.0.1:1",
            enabled_tools=["weather-data_weather_astronomy"],
        )
        client._entered = True
        client.allowed_tools = {"weather-data_weather_astronomy"}

        with self.assertRaisesRegex(
            ToolPolicyError,
            r"^Unknown tool: weather-data-weather_astronomy$",
        ) as raised:
            await client.call_tool("weather-data-weather_astronomy", {})

        self.assertNotIn("did you mean", str(raised.exception).casefold())
        self.assertNotIn(
            "weather-data_weather_astronomy", str(raised.exception)
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

    async def test_public_arxiv_calls_are_paced_across_tasks(self):
        starts = []

        async def record_call(*_args):
            starts.append(time.monotonic())
            return SimpleNamespace(is_error=False)

        clients = []
        for index in range(2):
            client = IsolatedMCPClient(
                task_id=f"arxiv-rate-test-{index}",
                shared_url="http://127.0.0.1:1",
                enabled_tools=["arxiv_search_papers"],
            )
            backend = AsyncMock(side_effect=record_call)
            client._entered = True
            client.allowed_tools = {"arxiv_search_papers"}
            client._clients[ToolRoute.TASK_NETWORK] = SimpleNamespace(
                call_tool=backend
            )
            clients.append(client)

        test_gate = isolated_client.ServerCallGate(1, 0.02)
        with (
            patch.dict(
                isolated_client._SERVER_CALL_GATES,
                {"arxiv": test_gate},
                clear=False,
            ),
            patch(
                "mcp_completion.mcp_client.isolated_client.write_runtime_event"
            ),
        ):
            await asyncio.gather(
                *(client.call_tool("arxiv_search_papers", {}) for client in clients)
            )

        self.assertEqual(2, len(starts))
        self.assertGreaterEqual(starts[1] - starts[0], 0.018)

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
