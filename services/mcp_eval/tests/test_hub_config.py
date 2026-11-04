import os
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from mcp_completion.hub_config import hub_manages_policy, managed_task_relay


class HubConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_only_selected_hub_servers_delegate_limits(self):
        with patch.dict(
            os.environ,
            {
                "MCP_TOOL_TRANSPORT": "hub",
                "MCPHUB_SERVERS": "brave-search, exa",
                "MCPHUB_MANAGE_RATE_LIMITS": "true",
            },
            clear=True,
        ):
            self.assertTrue(hub_manages_policy("brave-search"))
            self.assertFalse(hub_manages_policy("wikipedia"))
            os.environ["MCP_TOOL_TRANSPORT"] = "direct"
            self.assertFalse(hub_manages_policy("brave-search"))

    async def test_selected_hub_calls_bypass_only_the_outer_atlas_gate(self):
        from mcp_completion.mcp_client import isolated_client

        with patch.dict(
            os.environ,
            {
                "MCP_TOOL_TRANSPORT": "hub",
                "MCPHUB_SERVERS": "brave-search",
                "MCPHUB_MANAGE_RATE_LIMITS": "true",
            },
            clear=True,
        ):
            self.assertIsNone(
                isolated_client._server_call_gate("brave-search_brave_web_search")
            )
            async with isolated_client._server_call_slot(
                "brave-search_brave_web_search"
            ) as slot:
                self.assertEqual(slot.queued_ms, 0)
            self.assertIsNotNone(
                isolated_client._server_call_gate("twelvedata_get_stock_price")
            )
            os.environ["MCP_TOOL_TRANSPORT"] = "direct"
            self.assertIsNotNone(
                isolated_client._server_call_gate("brave-search_brave_web_search")
            )

    async def test_direct_mode_never_reads_hub(self):
        with patch.dict(
            os.environ,
            {"MCP_TOOL_TRANSPORT": "direct", "MCPHUB_MANAGE_RELAY": "true"},
            clear=True,
        ), patch("httpx.AsyncClient") as client:
            self.assertIsNone(await managed_task_relay(["pubmed"]))
            client.assert_not_called()

    async def test_snapshot_is_scoped_and_never_passes_hub_token(self):
        env = {
            "MCP_TOOL_TRANSPORT": "hub",
            "MCPHUB_MANAGE_RELAY": "true",
            "MCPHUB_URL": "http://localhost:3002",
            "MCPHUB_TOKEN": "operator-synthetic",
        }
        values = {
            "MCP_RELAY_URL": "http://relay:9123",
            "MCP_RELAY_TOKEN": "relay-synthetic",
            "MCP_RELAY_TIMEOUT_SECONDS": "90",
            "MCP_RELAY_URL": "http://relay:9123",
            "MCP_RELAY_TOKEN": "relay-synthetic",
            "MCP_RELAY_TIMEOUT_SECONDS": "90",
        }
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, json={"success": True, "data": {"env": values}}
        )
        with patch.dict(os.environ, env, clear=True), patch(
            "httpx.AsyncClient"
        ) as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(await managed_task_relay(["pubmed", "arxiv"]), values)
            self.assertEqual(client.get.await_count, 2)
            self.assertEqual(
                client.get.await_args.kwargs["headers"],
                {"Authorization": "Bearer operator-synthetic"},
            )
            client.get.return_value = httpx.Response(403)
            with self.assertRaisesRegex(RuntimeError, "denied"):
                await managed_task_relay(["pubmed"])

    async def test_rejects_extra_secrets_in_snapshot(self):
        env = {
            "MCP_TOOL_TRANSPORT": "hub",
            "MCPHUB_MANAGE_RELAY": "true",
            "MCPHUB_URL": "http://hub:3002",
            "MCPHUB_TOKEN": "synthetic",
        }
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, json={"success": True, "data": {"env": {"MCPHUB_TOKEN": "forbidden"}}}
        )
        with patch.dict(os.environ, env, clear=True), patch(
            "httpx.AsyncClient"
        ) as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaisesRegex(RuntimeError, "Invalid"):
                await managed_task_relay(["arxiv"])


class PerTaskRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_wrappers_keep_independent_relay_snapshots(self):
        from mcp_completion.hub_config import managed_task_relays

        on = {
            "MCP_RELAY_URL": "http://relay",
            "MCP_RELAY_TOKEN": "fixture",
            "MCP_RELAY_TIMEOUT_SECONDS": "90",
        }
        off = {
            "MCP_RELAY_URL": "",
            "MCP_RELAY_TOKEN": "",
            "MCP_RELAY_TIMEOUT_SECONDS": "90",
        }
        with patch(
            "mcp_completion.hub_config.managed_task_relay",
            new=AsyncMock(side_effect=[off, on]),
        ):
            result = await managed_task_relays(["pubmed", "arxiv"])
        self.assertEqual(result, {"arxiv": off, "pubmed": on})

    def test_task_template_isolates_relay_environment_and_does_not_embed_credentials(
        self,
    ):
        import json
        import tempfile
        from pathlib import Path
        from mcp_completion.task_sandbox import TaskSandbox

        sandbox = TaskSandbox.from_servers(
            "fixture", local_servers=[], network_servers=["arxiv", "pubmed"]
        )
        with tempfile.TemporaryDirectory() as directory:
            sandbox.task_workspace = Path(directory)
            values = sandbox._configure_network_relays(
                {
                    "arxiv": {"MCP_RELAY_URL": "", "MCP_RELAY_TOKEN": ""},
                    "pubmed": {
                        "MCP_RELAY_URL": "http://relay",
                        "MCP_RELAY_TOKEN": "private-fixture-token",
                    },
                }
            )
            template = json.loads(sandbox.network_template.read_text())
            self.assertEqual(sandbox.network_template.stat().st_mode & 0o777, 0o644)
            a = template["mcpServers"]["arxiv"]["env"]
            b = template["mcpServers"]["pubmed"]["env"]
            self.assertNotEqual(a["MCP_RELAY_URL"], b["MCP_RELAY_URL"])
            self.assertEqual(values[a["MCP_RELAY_URL"][2:-1]], "")
            self.assertEqual(values[b["MCP_RELAY_URL"][2:-1]], "http://relay")
            self.assertNotIn(
                "private-fixture-token", sandbox.network_template.read_text()
            )


class WorkerStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_worker_credentials_are_not_persisted_or_confused_with_relay(
        self,
    ):
        from mcp_completion.hub_config import managed_task_startup_env, task_hub_servers

        env = {
            "MCP_TOOL_TRANSPORT": "hub",
            "MCPHUB_TASK_ROUTING": "true",
            "MCPHUB_TASK_SERVERS": "arxiv,pubmed",
            "MCPHUB_URL": "http://hub",
            "MCPHUB_TOKEN": "fixture",
            "MCPHUB_TARGET": "atlas/official",
        }
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200,
            json={
                "success": True,
                "data": {"startupEnv": {"NCBI_API_KEY": "provider-fixture"}, "env": {}},
            },
        )
        with patch.dict(os.environ, env, clear=True), patch(
            "httpx.AsyncClient"
        ) as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(
                await managed_task_startup_env(["pubmed"]),
                {"NCBI_API_KEY": "provider-fixture"},
            )
            self.assertNotIn("NCBI_API_KEY", os.environ)
            self.assertTrue(hub_manages_policy("pubmed"))
            self.assertFalse(hub_manages_policy("filesystem"))
            client.get.return_value = httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"startupEnv": {"MCPHUB_TOKEN": "never"}},
                },
            )
            with self.assertRaisesRegex(RuntimeError, "Invalid"):
                await managed_task_startup_env(["pubmed"])


class NamespacedTaskConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_snapshots_omit_workspace_and_task_selector(self):
        from mcp_completion.hub_config import managed_task_startup_env

        env = {
            "MCP_TOOL_TRANSPORT": "hub",
            "MCPHUB_CATALOG": "atlas",
            "MCPHUB_TARGET": "atlas/fixture3",
            "MCPHUB_TASK_ROUTING": "true",
            "MCPHUB_MANAGE_RELAY": "true",
            "MCPHUB_URL": "http://hub:3003",
            "MCPHUB_TOKEN": "fixture",
        }
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, json={"success": True, "data": {"env": {}, "startupEnv": {}}}
        )
        with patch.dict(os.environ, env, clear=True), patch(
            "httpx.AsyncClient"
        ) as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(await managed_task_relay(["arxiv", "pubmed"]), {})
            self.assertEqual(await managed_task_startup_env(["arxiv", "pubmed"]), {})
            self.assertEqual(client.get.await_count, 4)
            for call in client.get.await_args_list:
                self.assertEqual(
                    call.kwargs["params"],
                    {"catalog": "atlas"},
                )
                self.assertIn(
                    call.args[0],
                    [
                        "http://hub:3003/api/servers/arxiv/runtime",
                        "http://hub:3003/api/servers/pubmed/runtime",
                    ],
                )

    def test_invalid_catalog_fails_closed(self):
        from mcp_completion.hub_config import hub_catalog

        for catalog in ["atlas/official", "x" * 49]:
            with self.subTest(catalog=catalog), patch.dict(
                os.environ, {"MCPHUB_CATALOG": catalog}, clear=True
            ):
                with self.assertRaises(RuntimeError):
                    hub_catalog()


class SetupRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from mcp_completion import hub_config

        self.module = hub_config
        self.events = patch.object(hub_config, "write_runtime_event").start()
        self.pause = patch.object(hub_config.asyncio, "sleep", new=AsyncMock()).start()

    async def asyncTearDown(self):
        patch.stopall()

    async def test_network_timeout_and_502_retry_then_success(self):
        client = AsyncMock()
        client.get.side_effect = [
            httpx.ReadTimeout("must not log secret"),
            httpx.Response(502),
            httpx.Response(200, json={}),
        ]
        result = await self.module._runtime_snapshot(
            client, "http://hub", "secret-token", "arxiv", "task setup"
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.get.await_count, 3)
        self.assertEqual(self.pause.await_count, 2)
        self.assertNotIn("secret-token", repr(self.events.call_args_list))
        self.assertNotIn("must not log secret", repr(self.events.call_args_list))
        self.assertEqual(
            self.events.call_args_list[0].kwargs["error_type"], "ReadTimeout"
        )
        self.assertEqual(self.events.call_args_list[1].kwargs["http_status"], 502)

    async def test_retries_are_capped_at_three_attempts(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(503)
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            await self.module._runtime_snapshot(
                client, "http://hub", "key", "pubmed", "task setup"
            )
        self.assertEqual(client.get.await_count, 3)

    async def test_auth_and_configuration_http_failures_are_not_retried(self):
        for status in (400, 401, 403, 404):
            client = AsyncMock()
            client.get.return_value = httpx.Response(status)
            with self.subTest(status=status), self.assertRaisesRegex(
                RuntimeError, f"HTTP {status}"
            ):
                await self.module._runtime_snapshot(
                    client, "http://hub", "key", "pubmed", "task setup"
                )
            self.assertEqual(client.get.await_count, 1)

    async def test_slow_requests_share_a_thirty_second_budget(self):
        from types import SimpleNamespace

        now = 0.0
        timeouts = []

        async def get(*args, **kwargs):
            nonlocal now
            timeouts.append(kwargs["timeout"])
            now += kwargs["timeout"]
            raise httpx.ReadTimeout("synthetic")

        async def sleep(delay):
            nonlocal now
            now += delay

        client = AsyncMock()
        client.get.side_effect = get
        self.pause.side_effect = sleep
        with patch.object(self.module, "time", SimpleNamespace(monotonic=lambda: now)):
            with self.assertRaisesRegex(RuntimeError, "ReadTimeout"):
                await self.module._runtime_snapshot(
                    client, "http://hub", "key", "pubmed", "task setup"
                )
        self.assertEqual(timeouts, [10, 10, 7])
        self.assertEqual(now, 30)

    async def test_startup_credentials_and_relay_both_use_retry_reader(self):
        from mcp_completion.hub_config import managed_task_startup_env

        env = {
            "MCP_TOOL_TRANSPORT": "hub",
            "MCPHUB_TASK_ROUTING": "true",
            "MCPHUB_MANAGE_RELAY": "true",
            "MCPHUB_URL": "http://hub",
            "MCPHUB_TOKEN": "synthetic",
        }
        client = AsyncMock()
        good = httpx.Response(
            200, json={"success": True, "data": {"env": {}, "startupEnv": {}}}
        )
        client.get.side_effect = [
            httpx.Response(504),
            good,
            httpx.ReadTimeout("synthetic"),
            good,
        ]
        with patch.dict(os.environ, env, clear=True), patch(
            "httpx.AsyncClient"
        ) as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(await managed_task_relay(["pubmed"]), {})
            self.assertEqual(await managed_task_startup_env(["pubmed"]), {})
        self.assertEqual(client.get.await_count, 4)

    async def test_invalid_success_payload_is_not_retried(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, json={"success": True, "data": {"env": {"MCPHUB_TOKEN": "forbidden"}}}
        )
        env = {
            "MCP_TOOL_TRANSPORT": "hub",
            "MCPHUB_MANAGE_RELAY": "true",
            "MCPHUB_URL": "http://hub",
            "MCPHUB_TOKEN": "synthetic",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "httpx.AsyncClient"
        ) as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaisesRegex(RuntimeError, "Invalid"):
                await managed_task_relay(["pubmed"])
        self.assertEqual(client.get.await_count, 1)
