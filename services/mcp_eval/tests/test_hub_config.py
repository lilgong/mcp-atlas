import os
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from mcp_completion.hub_config import hub_manages_policy, managed_task_relay


class HubConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_only_selected_hub_servers_delegate_limits(self):
        with patch.dict(os.environ, {"MCP_TOOL_TRANSPORT": "hub", "MCPHUB_SERVERS": "brave-search, exa", "MCPHUB_MANAGE_RATE_LIMITS": "true"}, clear=True):
            self.assertTrue(hub_manages_policy("brave-search"))
            self.assertFalse(hub_manages_policy("wikipedia"))
            os.environ["MCP_TOOL_TRANSPORT"] = "direct"
            self.assertFalse(hub_manages_policy("brave-search"))

    async def test_selected_hub_calls_bypass_only_the_outer_atlas_gate(self):
        from mcp_completion.mcp_client import isolated_client
        with patch.dict(os.environ, {"MCP_TOOL_TRANSPORT": "hub", "MCPHUB_SERVERS": "brave-search", "MCPHUB_MANAGE_RATE_LIMITS": "true"}, clear=True):
            self.assertIsNone(isolated_client._server_call_gate("brave-search_brave_web_search"))
            async with isolated_client._server_call_slot("brave-search_brave_web_search") as slot:
                self.assertEqual(slot.queued_ms, 0)
            self.assertIsNotNone(isolated_client._server_call_gate("twelvedata_get_stock_price"))
            os.environ["MCP_TOOL_TRANSPORT"] = "direct"
            self.assertIsNotNone(isolated_client._server_call_gate("brave-search_brave_web_search"))

    async def test_direct_mode_never_reads_hub(self):
        with patch.dict(os.environ, {"MCP_TOOL_TRANSPORT": "direct", "MCPHUB_MANAGE_RELAY": "true"}, clear=True), patch("httpx.AsyncClient") as client:
            self.assertIsNone(await managed_task_relay(["pubmed"]))
            client.assert_not_called()

    async def test_snapshot_is_scoped_and_never_passes_hub_token(self):
        env = {"MCP_TOOL_TRANSPORT": "hub", "MCPHUB_MANAGE_RELAY": "true", "MCPHUB_CONFIG_URL": "http://localhost:3002", "MCPHUB_TOKEN": "operator-synthetic"}
        values = {"PUBMED_RELAY_URL": "http://relay:9123", "PUBMED_RELAY_TOKEN": "relay-synthetic", "PUBMED_RELAY_TIMEOUT_SECONDS": "90"}
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, json={"success": True, "data": values})
        with patch.dict(os.environ, env, clear=True), patch("httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            self.assertEqual(await managed_task_relay(["pubmed", "arxiv"]), values)
            self.assertEqual(client.get.await_count, 2)
            self.assertEqual(client.get.await_args.kwargs["headers"], {"Authorization": "Bearer operator-synthetic"})
            client.get.return_value = httpx.Response(403)
            with self.assertRaisesRegex(RuntimeError, "denied"):
                await managed_task_relay(["pubmed"])

    async def test_rejects_extra_secrets_in_snapshot(self):
        env = {"MCP_TOOL_TRANSPORT": "hub", "MCPHUB_MANAGE_RELAY": "true", "MCPHUB_URL": "http://hub:3002", "MCPHUB_TOKEN": "synthetic"}
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, json={"success": True, "data": {"MCPHUB_TOKEN": "forbidden"}})
        with patch.dict(os.environ, env, clear=True), patch("httpx.AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            with self.assertRaisesRegex(RuntimeError, "Invalid"):
                await managed_task_relay(["arxiv"])
