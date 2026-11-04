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
        await self._check_tasks()

    async def test_namespaced_tasks_preserve_names_without_target_selector(self):
        await self._check_tasks(catalog="atlas")

    async def _check_tasks(self, catalog=""):
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
            if request.headers["authorization"] != "Bearer cap-" + identity:
                return httpx.Response(403)
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
            "MCPHUB_CATALOG": catalog,
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
                    clients.append(client)
                await asyncio.gather(*(client.start() for client in clients))
                self.assertNotEqual(
                    registrations[0][0]["taskId"], registrations[1][0]["taskId"]
                )
                self.assertNotEqual(
                    registrations[0][0]["url"], registrations[1][0]["url"]
                )
                self.assertNotEqual(
                    registrations[0][0]["token"], registrations[1][0]["token"]
                )
                self.assertNotEqual(
                    clients[0].connections["filesystem"]["id"],
                    clients[1].connections["filesystem"]["id"],
                )
                self.assertFalse(
                    {"x-mcphub-model", "x-mcphub-benchmark", "x-mcphub-run-id"}
                    & registrations[0][1].keys()
                )
                for body, headers in registrations:
                    self.assertNotIn("target", body)
                    self.assertNotIn("x-mcphub-target", headers)
                self.assertEqual(registrations[0][0]["server"], "filesystem")
                self.assertEqual(registrations[0][0].get("catalog", ""), catalog)
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
                # A lease URL combined with another task's capability cannot dispatch.
                first = clients[0].connections["filesystem"]
                second = clients[1].connections["filesystem"]
                denied = await clients[0].http.post(
                    "http://hub" + first["path"] + "/list-tools",
                    headers={"Authorization": "Bearer " + second["token"]},
                    json={},
                )
                self.assertEqual(denied.status_code, 403)
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

    async def test_missing_key_rejected_and_direct_mode_disabled(self):
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
            with self.assertRaisesRegex(RuntimeError, "Bearer Key"):
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


class LeaseRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from mcp_completion import hub_task_transport as module

        self.module = module
        self.now = 0.0
        self.env = patch.dict(
            os.environ,
            {
                "MCPHUB_URL": "http://hub",
                "MCPHUB_TASK_CALLBACK_URL": "http://completion",
                "MCPHUB_TOKEN": "fixture",
            },
            clear=True,
        )
        self.env.start()
        self.clock = patch.object(
            module, "time", SimpleNamespace(monotonic=lambda: self.now)
        )
        self.clock.start()
        self.events = patch.object(module, "write_runtime_event").start()
        self.client = HubTaskClient(
            FakeTask("task"),
            ["filesystem_read_file"],
            lambda _: "filesystem",
            task_id="fixture",
        )
        for server in ("filesystem", "pubmed"):
            self.client.connections[server] = {
                "id": server,
                "token": "cap",
                "path": "/gateway/v1/tasks/" + server,
            }
            self.client.lease_deadlines[server] = 300
            self.client.server_bindings[server] = server
            self.client.binding_ids.append(server)
            _bindings[server] = ("callback", self.client.upstream, frozenset())

    async def asyncTearDown(self):
        await self.client.close()
        patch.stopall()

    async def test_timeout_then_502_recovers_without_revoking_callback(self):
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == 4:
                raise asyncio.CancelledError()
            self.now += delay

        responses = iter(
            [
                httpx.ReadTimeout("synthetic"),
                httpx.Response(502),
                httpx.Response(200, json={"success": True}),
            ]
        )

        async def hub(request):
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(hub))
        with patch.object(self.module.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.client._renew_connection("filesystem")
        self.assertEqual(sleeps, [120, 2, 4, 120])
        self.assertEqual(self.client.lease_deadlines["filesystem"], 426)
        self.assertIn("filesystem", _bindings)
        self.assertFalse(self.client.failed_servers)

    async def test_permanent_rejection_revokes_only_affected_connection(self):
        from unittest.mock import AsyncMock

        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(403))
        )
        with patch.object(self.module.asyncio, "sleep", new=AsyncMock()):
            await self.client._renew_connection("pubmed")
        self.assertEqual(self.client.failed_servers, {"pubmed"})
        self.assertNotIn("pubmed", _bindings)
        self.assertIn("filesystem", _bindings)
        self.assertFalse(self.client.failed)
        self.client.http = self._replace_http_for_success(self.client.http)
        self.assertEqual(await self.client._request("filesystem", "list-tools", {}), [])

    def _replace_http_for_success(self, previous):
        # Retain the previous client for teardown without leaking its resources.
        self.addAsyncCleanup(previous.aclose)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
        )

    async def test_continuous_timeouts_stop_at_confirmed_deadline(self):
        calls = []

        async def sleep(delay):
            self.now += delay

        async def hub(request):
            calls.append(self.now)
            raise httpx.ReadTimeout("synthetic")

        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(hub))
        with patch.object(self.module.asyncio, "sleep", side_effect=sleep):
            await self.client._renew_connection("pubmed")
        self.assertEqual(self.now, 300)
        self.assertTrue(all(at < 300 for at in calls))
        self.assertEqual(self.client.lease_deadlines["pubmed"], 300)
        self.assertNotIn("pubmed", _bindings)
        self.assertIn("filesystem", _bindings)

    async def test_late_success_does_not_revive_expired_lease(self):
        from unittest.mock import AsyncMock

        async def hub(request):
            self.now = 301
            return httpx.Response(200, json={"success": True})

        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(hub))
        with patch.object(self.module.asyncio, "sleep", new=AsyncMock()):
            await self.client._renew_connection("pubmed")
        self.assertIn("pubmed", self.client.failed_servers)
        self.assertEqual(self.client.lease_deadlines["pubmed"], 300)

    async def test_expired_connection_rejects_tool_without_dispatch(self):
        from unittest.mock import AsyncMock

        self.now = 301
        self.client.http.post = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            await self.client.call_tool("filesystem_read_file", {})
        self.client.http.post.assert_not_called()
        self.assertNotIn("filesystem", _bindings)

    async def test_parallel_renewal_failure_does_not_stop_other_server_and_close_cancels_workers(
        self,
    ):
        failed = asyncio.Event()
        renewed = asyncio.Event()
        blocked = asyncio.Event()
        sleeps = {}
        original_sleep = asyncio.sleep

        async def sleep(delay):
            task = asyncio.current_task()
            sleeps[task] = sleeps.get(task, 0) + 1
            if sleeps[task] > 1:
                await blocked.wait()
            await original_sleep(0)

        def event(stream, name, **fields):
            if name == "hub_task_lease_failed":
                failed.set()

        async def hub(request):
            if request.method == "DELETE":
                return httpx.Response(200)
            if request.method == "POST":
                return httpx.Response(200, json=[])
            if request.url.path.endswith("pubmed"):
                return httpx.Response(403)
            renewed.set()
            return httpx.Response(200, json={"success": True})

        self.events.side_effect = event
        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(hub))
        with patch.object(self.module.asyncio, "sleep", side_effect=sleep):
            self.client.renewal = asyncio.create_task(self.client._renew())
            await asyncio.wait_for(failed.wait(), 1)
            await asyncio.wait_for(renewed.wait(), 1)
            self.assertEqual(
                await self.client._request("filesystem", "list-tools", {}), []
            )
            self.assertEqual(self.client.failed_servers, {"pubmed"})
            await self.client.close()
        self.assertTrue(all(task.done() for task in sleeps))
        self.assertFalse(_bindings)

    async def test_slow_registration_of_other_servers_does_not_delay_first_renewal(
        self,
    ):
        self.now = 150
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) > 1:
                raise asyncio.CancelledError()
            self.now += delay

        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"success": True})
            )
        )
        with patch.object(self.module.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                await self.client._renew_connection("pubmed")
        self.assertEqual(delays, [0, 120])
        self.assertEqual(self.client.lease_deadlines["pubmed"], 450)

    async def test_http_500_is_temporary_and_does_not_extend_unconfirmed_lease(self):
        from unittest.mock import AsyncMock

        results = iter([500, 403])
        deadlines = []

        async def hub(request):
            deadlines.append(self.client.lease_deadlines["pubmed"])
            return httpx.Response(next(results))

        await self.client.http.aclose()
        self.client.http = httpx.AsyncClient(transport=httpx.MockTransport(hub))
        with patch.object(self.module.asyncio, "sleep", new=AsyncMock()):
            await self.client._renew_connection("pubmed")
        self.assertEqual(deadlines, [300, 300])
        self.assertEqual(self.client.failed_servers, {"pubmed"})
