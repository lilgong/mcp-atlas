import asyncio
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mcp_completion.runtime_probe import router
from mcp_completion.config import config
from mcp_server_probe import Result, run_isolated


def test_hub_cli_sends_probe_to_completion_without_local_sandbox():
    requests = []
    async def run():
        async def response(request):
            import json
            requests.append(json.loads(request.content))
            assert request.url.path == "/v2/mcp_eval/runtime-probe"
            assert request.url.port == 3001
            return httpx.Response(200, json={"schema_version": 1, "runtime_identity": {},
                                             "results": [{"server": "arxiv", "kind": "smoke", "status": "DATA OK", "detail": "ok"}]})
        factory = httpx.AsyncClient
        with patch.dict(os.environ, {"MCP_TOOL_TRANSPORT": "hub", "PORT": "3001"}, clear=True), patch(
            "mcp_server_probe.httpx.AsyncClient",
            side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(response), **kwargs),
        ), patch("mcp_server_probe.IsolatedMCPClient") as isolated:
            result = await run_isolated("http://127.0.0.1:2987", 1, 5, "arxiv", False, True)
            assert result[0].server == "arxiv"
            isolated.assert_not_called()
    asyncio.run(run())
    assert requests[0]["smoke_only"] is True


def test_hub_cli_never_falls_back_to_direct_on_server_failure():
    async def run():
        factory = httpx.AsyncClient
        with patch.dict(os.environ, {"MCP_TOOL_TRANSPORT": "hub", "PORT": "3001"}, clear=True), patch(
            "mcp_server_probe.httpx.AsyncClient",
            side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(lambda _: httpx.Response(503)), **kwargs),
        ), patch("mcp_server_probe.IsolatedMCPClient") as isolated:
            with pytest.raises(Exception, match="HTTP 503.*no direct fallback"):
                await run_isolated("http://127.0.0.1:2987", 1, 5, "arxiv", False, True)
            isolated.assert_not_called()
    asyncio.run(run())


def test_completion_endpoint_owns_execution_and_keeps_controls_loopback_only():
    async def run():
        app = FastAPI()
        app.include_router(router)
        mocked = AsyncMock(return_value=[Result("pubmed", "smoke", "DATA OK", detail="ok")])
        payload = {"shared_url": config.MCP_SERVER_URL, "server": "pubmed"}
        with patch("mcp_server_probe.run_isolated", mocked):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)), base_url="http://completion") as client:
                result = await client.post("/v2/mcp_eval/runtime-probe", json=payload)
                assert result.status_code == 200
                assert result.json()["results"][0]["server"] == "pubmed"
                assert mocked.await_args.kwargs == {"completion_owned": True}
                response = await client.post("/v2/mcp_eval/runtime-probe", json={**payload, "shared_url": "http://other-runtime:1984"})
                assert response.status_code == 409
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("172.20.0.2", 123)), base_url="http://completion") as client:
                assert (await client.post("/v2/mcp_eval/runtime-probe", json=payload)).status_code == 403
            mocked.assert_awaited_once()
    asyncio.run(run())


def test_disconnected_probe_cancels_and_waits_for_cleanup():
    from mcp_completion.runtime_probe import runtime_probe, RuntimeProbeRequest
    from fastapi import HTTPException
    from types import SimpleNamespace
    async def run():
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def probe(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()
        class Request:
            client = SimpleNamespace(host="127.0.0.1")
            async def body(self):
                return b'{}'
            async def receive(self):
                await started.wait()
                return {"type": "http.disconnect"}
        with patch("mcp_server_probe.run_isolated", probe):
            with pytest.raises(HTTPException) as exc:
                await runtime_probe(RuntimeProbeRequest(shared_url=config.MCP_SERVER_URL), Request())
            assert exc.value.status_code == 499
            assert cleaned.is_set()
    asyncio.run(run())


def test_git_initialization_failure_returns_structured_503_not_uncaught_500():
    from mcp_completion.runtime_probe import runtime_probe, RuntimeProbeRequest
    from mcp_completion.task_data import TaskDataError
    from fastapi import HTTPException
    from types import SimpleNamespace
    async def run():
        class Request:
            client = SimpleNamespace(host="127.0.0.1")
            async def body(self): return b'{}'
            async def receive(self): await asyncio.Future()
        with patch("mcp_server_probe.run_isolated", AsyncMock(side_effect=TaskDataError("Git operation failed: damaged cache"))):
            with pytest.raises(HTTPException) as exc:
                await runtime_probe(RuntimeProbeRequest(shared_url=config.MCP_SERVER_URL), Request())
            assert exc.value.status_code == 503
            assert exc.value.detail["code"] == "task_initialization_failed"
            assert "damaged cache" in exc.value.detail["detail"]
    asyncio.run(run())
