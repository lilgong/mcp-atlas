import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


MCP_EVAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_EVAL_DIR))

from test_server_v1 import (  # noqa: E402
    DataMismatch,
    load_target_servers,
    make_caller,
    probe_airtable,
)
from test_servers import resolve_mcp_server_url  # noqa: E402


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


class EnvLoadingTests(unittest.TestCase):
    def test_expands_port_reference_from_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env_path = Path(raw) / ".env"
            env_path.write_text(
                "MCP_SHARED_PORT=3984\n"
                "MCP_SERVER_URL=http://localhost:${MCP_SHARED_PORT}\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"MCP_SERVER_URL": "", "MCP_SHARED_PORT": ""},
            ):
                self.assertEqual(
                    ("http://localhost:3984", 3984),
                    resolve_mcp_server_url(None, env_path),
                )

    def test_missing_url_fails_instead_of_using_default_port(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            env_path = Path(raw) / ".env"
            env_path.write_text("", encoding="utf-8")
            with patch.dict(os.environ, {"MCP_SERVER_URL": ""}):
                with self.assertRaisesRegex(ValueError, "缺少 MCP 服务地址"):
                    resolve_mcp_server_url(None, env_path)

    def test_url_without_explicit_port_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须显式包含端口"):
            resolve_mcp_server_url("http://localhost")


if __name__ == "__main__":
    unittest.main()
