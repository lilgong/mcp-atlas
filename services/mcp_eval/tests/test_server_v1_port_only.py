import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx


MCP_EVAL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MCP_EVAL_DIR))

from test_server_v1 import (  # noqa: E402
    DataMismatch,
    load_target_servers,
    main as run_legacy_checks,
    make_caller,
    probe_airtable,
)
from test_server_v2 import main as run_isolated_checks  # noqa: E402
from mcp_server_probe import (  # noqa: E402
    probe_slack,
    probe_slack_timestamp_alignment,
    resolve_completion_input,
)
from test_servers import resolve_mcp_server_url  # noqa: E402


def tool_response(payload) -> str:
    return json.dumps([{"type": "text", "text": json.dumps(payload)}])


def text_response(text: str) -> str:
    return json.dumps([{"type": "text", "text": text}])


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

    async def test_v1_calls_e2b_directly_through_shared_gateway(self) -> None:
        calls = []
        endpoint = []

        class FakeHttpClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        async def call(tool, args):
            calls.append((tool, args))
            return tool_response({"stdout": "4"})

        def fake_make_caller(client, base_url, timeout, retries):
            endpoint.append(base_url)
            return call

        output = StringIO()
        with (
            patch("mcp_server_probe.httpx.AsyncClient", FakeHttpClient),
            patch(
                "mcp_server_probe.load_target_servers",
                return_value=["e2b-server"],
            ),
            patch(
                "mcp_server_probe.make_caller",
                side_effect=fake_make_caller,
            ),
            redirect_stdout(output),
        ):
            await run_legacy_checks(
                "http://gateway:1984",
                timeout=1,
                concurrency=5,
                only="e2b-server",
                data_only=False,
                smoke_only=False,
            )

        self.assertEqual(["http://gateway:1984/call-tool"], endpoint)
        self.assertEqual("e2b-server_run_code", calls[0][0])
        self.assertIn("OK        e2b-server", output.getvalue())

    async def test_mongodb_probe_uses_isolated_route_when_shared_is_offline(
        self,
    ) -> None:
        calls = []
        requested = []

        class FakeIsolatedClient:
            def __init__(self, **kwargs):
                requested.extend(kwargs["enabled_tools"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def call_tool(self, tool, args):
                calls.append((tool, args))
                item = SimpleNamespace(
                    model_dump=lambda **kwargs: {
                        "type": "text",
                        "text": "Found 10 documents",
                    }
                )
                return SimpleNamespace(content=[item], is_error=False)

        output = StringIO()
        with (
            patch(
                "mcp_server_probe.load_target_servers",
                return_value=["airtable"],
            ),
            patch(
                "mcp_server_probe.IsolatedMCPClient",
                FakeIsolatedClient,
            ),
            redirect_stdout(output),
        ):
            await run_isolated_checks(
                "http://gateway:1984",
                timeout=1,
                concurrency=20,
                only="mongodb",
                data_only=False,
                smoke_only=False,
            )

        self.assertEqual(["mongodb_count"], requested)
        self.assertEqual("mongodb_count", calls[0][0])
        self.assertIn("DATA OK   mongodb", output.getvalue())

    async def test_e2b_is_called_through_v2_instead_of_policy_skipped(
        self,
    ) -> None:
        calls = []
        requested = []

        class FakeIsolatedClient:
            def __init__(self, **kwargs):
                requested.extend(kwargs["enabled_tools"])

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def call_tool(self, tool, args):
                calls.append((tool, args))
                item = SimpleNamespace(
                    model_dump=lambda **kwargs: {
                        "type": "text",
                        "text": "4",
                    }
                )
                return SimpleNamespace(content=[item], is_error=False)

        output = StringIO()
        with (
            patch(
                "mcp_server_probe.load_target_servers",
                return_value=["e2b-server"],
            ),
            patch(
                "mcp_server_probe.IsolatedMCPClient",
                FakeIsolatedClient,
            ),
            redirect_stdout(output),
        ):
            await run_isolated_checks(
                "http://gateway:1984",
                timeout=1,
                concurrency=20,
                only="e2b-server",
                data_only=False,
                smoke_only=False,
            )

        self.assertEqual(["e2b-server_run_code"], requested)
        self.assertEqual("e2b-server_run_code", calls[0][0])
        self.assertIn("OK        e2b-server", output.getvalue())
        self.assertNotIn("POLICY SKIP", output.getvalue())


class SlackIdentityProbeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _call(tool, args):
        if tool == "slack_channels_list":
            return text_response(
                "ID,Name,Topic,Purpose,MemberCount,Cursor\n"
                "C1,#movie-suggestions,,,8,\n"
                "C2,#gaming-suggestions,,,8,\n"
            )
        if tool == "slack_conversations_history" and args["channel_id"] == "C1":
            return text_response(
                "UserID,UserName,RealName,Channel,ThreadTs,Text,Time,Cursor\n"
                "U1,hiphopluvr1989,Omari West,C1,,Akira,1.0,\n"
            )
        if tool == "slack_conversations_history" and args["channel_id"] == "C2":
            return text_response(
                "UserID,UserName,RealName,Channel,ThreadTs,Text,Time,Cursor\n"
                "U2,shinsplints7070,steve_shins,C2,,Apex Legends,2.0,\n"
            )
        raise AssertionError(f"unexpected tool call: {tool} {args}")

    async def test_requires_imported_user_real_name(self) -> None:
        detail = await probe_slack(self._call)
        self.assertIn("真实姓名可解析", detail)

    async def test_rejects_blank_imported_user_real_name(self) -> None:
        async def call(tool, args):
            response = await self._call(tool, args)
            if tool == "slack_conversations_history" and args["channel_id"] == "C2":
                return response.replace("steve_shins", "")
            return response

        with self.assertRaisesRegex(DataMismatch, "Steve Shins"):
            await probe_slack(call)


class SlackTimestampAlignmentTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _write_input(path: Path, timestamp: str) -> None:
        path.write_text(
            "TASK,TRAJECTORY,GTFA_CLAIMS\n"
            f'task-1,\"slack_conversations_history\",'
            f'\"Napoleon Dynamite posted on {timestamp}.\"\n',
            encoding="utf-8",
        )

    @staticmethod
    async def _call(tool, args):
        if tool == "slack_channels_list":
            return text_response(
                "ID,Name,Topic,Purpose,MemberCount,Cursor\n"
                "C123,#movie-suggestions,,,8,\n"
            )
        if tool == "slack_conversations_history":
            return text_response(
                "UserID,UserName,RealName,Channel,ThreadTs,Text,Time,Cursor\n"
                "U123,mcpdumle,,C123,,You cant go wrong with Napoleon Dynamite,"
                "1783615136.421649,\n"
            )
        if tool == "slack_conversations_search_messages":
            return text_response(
                "UserID,UserName,RealName,Channel,ThreadTs,Text,Time,Cursor\n"
                "U123,mcpdumle,,C123,,You cant go wrong with Napoleon Dynamite,"
                "1783615136.421649,\n"
            )
        raise AssertionError(f"unexpected tool call: {tool} {args}")

    async def test_selected_csv_matches_cloud_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "aligned.csv"
            self._write_input(path, "2026-07-09 at 16:38:56.421649+00:00")

            detail = await probe_slack_timestamp_alignment(self._call, path)

        self.assertIn("2026-07-09T16:38:56.421649+00:00", detail)

    async def test_selected_csv_mismatch_is_data_bad(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "official.csv"
            self._write_input(path, "2025-06-27 at 16:38:56.421649+00:00")

            with self.assertRaisesRegex(DataMismatch, "时间不对应"):
                await probe_slack_timestamp_alignment(self._call, path)

    async def test_non_utc_slack_date_filter_is_data_bad(self) -> None:
        async def call(tool, args):
            if tool == "slack_conversations_search_messages":
                return text_response(
                    "UserID,UserName,RealName,Channel,ThreadTs,Text,Time,Cursor\n"
                )
            return await self._call(tool, args)

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "aligned.csv"
            self._write_input(path, "2026-07-09 at 16:38:56.421649+00:00")

            with self.assertRaisesRegex(DataMismatch, "个人时区"):
                await probe_slack_timestamp_alignment(call, path)

    def test_explicit_input_path_has_highest_priority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "chosen.csv"
            path.write_text("TASK,TRAJECTORY,GTFA_CLAIMS\n", encoding="utf-8")
            self.assertEqual(path.resolve(), resolve_completion_input(str(path)))


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
