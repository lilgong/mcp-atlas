import asyncio
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from mcp_completion.hub_task_transport import (
    HubTaskClient,
    router,
    _bindings,
    task_hub_enabled,
)
from mcp_completion.mcp_client.base_client import MCPClient
from mcp_completion.schema import CallToolResponse, ToolDefinition, TextContent


class FakeTask(MCPClient):
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def list_tools(self):
        return [
            ToolDefinition(
                name="filesystem_read_file",
                description="Fixture",
                inputSchema={"type": "object"},
            )
        ]

    async def call_tool(self, tool_name, args):
        self.calls += 1
        return CallToolResponse(content=[TextContent(type="text", text=self.value)])


class HubTaskTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tasks_use_distinct_leases_and_callbacks_and_cleanup(self):
        app = FastAPI()
        app.include_router(router)
        bridge_http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://completion"
        )
        leases = {}
        registrations = []

        async def hub(request):
            path = request.url.path
            if path == "/api/task-connections":
                import json

                body = json.loads(request.content)
                identity = str(len(registrations))
                registrations.append((body, dict(request.headers)))
                leases[identity] = body
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "id": identity,
                            "token": "cap-" + identity,
                            "path": "/gateway/v1/tasks/" + identity,
                        },
                    },
                )
            if path.startswith("/api/task-connections/"):
                identity = path.rsplit("/", 1)[-1]
                if request.method == "DELETE":
                    leases.pop(identity, None)
                return httpx.Response(200, json={"success": True})
            identity, operation = path.split("/")[-2:]
            if identity not in leases:
                return httpx.Response(403)
            body = leases[identity]
            assert request.headers["authorization"] == "Bearer cap-" + identity
            response = await bridge_http.post(
                body["url"].replace("http://completion", "") + "/" + operation,
                content=request.content,
                headers={"authorization": "Bearer " + body["token"]},
            )
            return httpx.Response(response.status_code, json=response.json())

        factory = httpx.AsyncClient
        clients = []
        env = {
            "MCP_TOOL_TRANSPORT": "hub",
            "MCPHUB_TASK_ROUTING": "true",
            "MCPHUB_URL": "http://hub",
            "MCPHUB_TASK_CALLBACK_URL": "http://completion",
            "MCPHUB_TARGET": "atlas/official",
            "MCPHUB_BENCHMARK": "MCP-Atlas",
            "MCPHUB_TOKEN": "operator-fixture",
        }
        try:
            with patch.dict(os.environ, env, clear=True), patch(
                "mcp_completion.hub_task_transport.httpx.AsyncClient",
                side_effect=lambda **kwargs: factory(
                    transport=httpx.MockTransport(hub), **kwargs
                ),
            ):
                self.assertTrue(task_hub_enabled())
                for value in ["task-a", "task-b"]:
                    client = HubTaskClient(
                        FakeTask(value),
                        ["filesystem_read_file"],
                        lambda name: "filesystem",
                        task_id="same-benchmark-task",
                    )
                    await client.start()
                    clients.append(client)
                self.assertNotEqual(
                    registrations[0][0]["taskId"], registrations[1][0]["taskId"]
                )
                self.assertFalse(
                    {"x-mcphub-model", "x-mcphub-benchmark", "x-mcphub-run-id"}
                    & registrations[0][1].keys()
                )
                self.assertEqual(registrations[0][0]["target"], "atlas/official")
                for value, client in zip(["task-a", "task-b"], clients):
                    self.assertEqual(
                        (await client.list_tools())[0].name, "filesystem_read_file"
                    )
                    self.assertEqual(
                        (await client.call_tool("filesystem_read_file", {}))
                        .content[0]
                        .text,
                        value,
                    )
                # Untrusted or cross-task callback credentials never reach clients.
                body = registrations[0][0]
                response = await bridge_http.post(
                    body["url"] + "/call-tool",
                    headers={"authorization": "Bearer invalid"},
                    json={"tool_name": "filesystem_read_file", "tool_args": {}},
                )
                self.assertEqual(response.status_code, 403)
                await clients[0].close()
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    await clients[0].call_tool("filesystem_read_file", {})
                response = await bridge_http.post(
                    body["url"] + "/list-tools",
                    headers={"authorization": "Bearer " + body["token"]},
                )
                self.assertEqual(response.status_code, 403)
        finally:
            for client in clients:
                await client.close()
            await bridge_http.aclose()
        self.assertFalse(leases)
        self.assertFalse(_bindings)

    async def test_missing_target_rejected_and_direct_mode_disabled(self):
        with patch.dict(os.environ, {"MCP_TOOL_TRANSPORT": "direct"}, clear=True):
            self.assertFalse(task_hub_enabled())
        with patch.dict(
            os.environ,
            {
                "MCPHUB_URL": "http://hub",
                "MCPHUB_TASK_CALLBACK_URL": "http://completion",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "explicit MCPHUB_TARGET"):
                HubTaskClient(FakeTask("x"), [], lambda name: "filesystem", task_id="x")

    async def test_completion_uses_the_shared_hub_address(self):
        env = {
            "MCPHUB_URL": "http://host-hub:3002",
            "MCPHUB_TASK_CALLBACK_URL": "http://completion",
            "MCPHUB_TARGET": "another/suite",
            "MCPHUB_TOKEN": "operator-fixture",
        }
        with patch.dict(os.environ, env, clear=True):
            client = HubTaskClient(
                FakeTask("x"), [], lambda name: "filesystem", task_id="x"
            )
            try:
                self.assertEqual(client.root, "http://host-hub:3002")
            finally:
                await client.close()


class CallbackControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonlocal_clients_can_only_attempt_authenticated_task_callbacks(self):
        from mcp_completion.main import app

        with patch.dict(
            os.environ, {"MCP_TASK_ISOLATION_ENABLED": "true"}, clear=False
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("172.17.0.2", 10000)),
                base_url="http://completion",
            ) as c:
                self.assertEqual((await c.get("/health")).status_code, 403)
                self.assertEqual(
                    (await c.post("/v2/mcp_eval/run_agent", json={})).status_code, 403
                )
                self.assertEqual(
                    (
                        await c.post(
                            "/internal/task-bridge/unknown/list-tools", json={}
                        )
                    ).status_code,
                    403,
                )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 10000)),
                base_url="http://completion",
            ) as c:
                self.assertEqual((await c.get("/health")).status_code, 200)
