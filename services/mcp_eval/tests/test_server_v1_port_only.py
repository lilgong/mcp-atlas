import json
import sys
import unittest
from pathlib import Path

import httpx


MCP_EVAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_EVAL_DIR))

from test_server_v1 import (  # noqa: E402
    DataMismatch,
    load_target_servers,
    make_caller,
    probe_airtable,
)


def tool_response(payload) -> str:
    return json.dumps([{"type": "text", "text": json.dumps(payload)}])


class AirtablePortOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_airtable_probe_uses_only_supplied_gateway_caller(self) -> None:
        calls = []

        async def call(tool, args):
            calls.append((tool, args))
            if tool == "airtable_list_bases":
                return tool_response([{"id": "app-test", "name": "Car Dealership"}])
            if tool == "airtable_list_records":
                return tool_response([{"fields": {"Page Name": "Inventory"}}])
            if tool == "airtable_search_records":
                return tool_response([
                    {"fields": {"Customer ID": "6NbOYtn9"}},
                ])
            self.fail(f"unexpected tool call: {tool}")

        detail = await probe_airtable(call)

        self.assertIn("会员有效", detail)
        self.assertEqual(
            calls,
            [
                ("airtable_list_bases", {}),
                (
                    "airtable_list_records",
                    {
                        "base_id": "app-test",
                        "table_name": "Digital Analytics",
                    },
                ),
                (
                    "airtable_search_records",
                    {
                        "base_id": "app-test",
                        "table_name": "Customer Feedback",
                        "field_name": "Customer ID",
                        "value": "6NbOYtn9",
                    },
                ),
            ],
        )

    async def test_airtable_probe_reports_missing_deep_record(self) -> None:
        async def call(tool, args):
            if tool == "airtable_list_bases":
                return tool_response([{"id": "app-test", "name": "Car Dealership"}])
            if tool == "airtable_list_records":
                return tool_response([{"fields": {"Page Name": "Inventory"}}])
            if tool == "airtable_search_records":
                return tool_response([])
            self.fail(f"unexpected tool call: {tool}")

        with self.assertRaisesRegex(DataMismatch, "会员疑似已失效"):
            await probe_airtable(call)


class GatewayRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_caller_disables_gateway_cache(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "http://gateway:1984/call-tool")
            self.assertEqual(
                json.loads(request.content),
                {
                    "tool_name": "calculator_calculate",
                    "tool_args": {"expression": "2 + 2"},
                    "use_cache": False,
                },
            )
            return httpx.Response(200, json=[{"type": "text", "text": "4"}])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            call = make_caller(
                client,
                "http://gateway:1984/call-tool",
                timeout=1,
            )
            await call("calculator_calculate", {"expression": "2 + 2"})

    async def test_server_discovery_uses_target_gateway(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                str(request.url),
                "http://gateway:1984/enabled-servers",
            )
            return httpx.Response(
                200,
                json={
                    "servers": [
                        ["airtable", "OK"],
                        ["weather", "ERROR_NOT_ONLINE"],
                    ],
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await load_target_servers(
                client,
                "http://gateway:1984",
                timeout=1,
            )

        self.assertEqual(servers, ["airtable", "weather"])


if __name__ == "__main__":
    unittest.main()
