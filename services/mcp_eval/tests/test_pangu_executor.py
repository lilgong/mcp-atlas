import asyncio

import httpx
import pytest

from mcp_completion import pangu_completion


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def _disable_logs(monkeypatch):
    monkeypatch.setenv("PANGU_API_KEY", "test-key")
    monkeypatch.setenv("PANGU_API_URL", "https://example.invalid/v1")
    monkeypatch.setattr(
        pangu_completion, "write_runtime_event", lambda *args, **kwargs: None
    )

    async def no_log(*args, **kwargs):
        return None

    monkeypatch.setattr(pangu_completion, "_append_pangu_log", no_log)


def test_more_than_thirty_model_requests_can_run_together(monkeypatch):
    """The completion service must not impose its old process-wide limit."""

    class Client:
        is_closed = False

        def __init__(self):
            self.active = 0
            self.peak = 0
            self.all_started = asyncio.Event()
            self.release = asyncio.Event()

        async def post(self, *args, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 40:
                self.all_started.set()
            try:
                await self.release.wait()
                return _Response()
            finally:
                self.active -= 1

    async def scenario():
        client = Client()
        monkeypatch.setattr(pangu_completion, "_get_pangu_client", lambda: client)
        tasks = [
            asyncio.create_task(
                pangu_completion.generate_pangu_async("pangu/dynamic-model", [], [])
            )
            for _ in range(40)
        ]
        await asyncio.wait_for(client.all_started.wait(), timeout=1)
        assert client.peak == 40
        client.release.set()
        await asyncio.gather(*tasks)

    _disable_logs(monkeypatch)
    asyncio.run(scenario())


def test_cancelling_model_call_cancels_async_gateway_wait(monkeypatch):
    class Client:
        is_closed = False

        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def post(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    async def scenario():
        client = Client()
        monkeypatch.setattr(pangu_completion, "_get_pangu_client", lambda: client)
        task = asyncio.create_task(
            pangu_completion.generate_pangu_async("pangu/any-model-name", [], [])
        )
        await client.started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled model call unexpectedly completed")
        assert client.cancelled.is_set()

    _disable_logs(monkeypatch)
    asyncio.run(scenario())


def test_provider_timeout_is_not_retried(monkeypatch):
    class Client:
        is_closed = False

        def __init__(self):
            self.calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            raise httpx.ReadTimeout("slow upstream")

    async def scenario():
        client = Client()
        monkeypatch.setattr(pangu_completion, "_get_pangu_client", lambda: client)
        with pytest.raises(Exception, match="after 1 attempt"):
            await pangu_completion.generate_pangu_async("pangu/model", [], [])
        assert client.calls == 1

    _disable_logs(monkeypatch)
    asyncio.run(scenario())


def test_provider_bad_request_is_not_retried_and_rid_is_forwarded(monkeypatch):
    class Response:
        status_code = 400
        text = "longer than the model's context length"

    class Client:
        is_closed = False

        def __init__(self):
            self.payloads = []

        async def post(self, *args, **kwargs):
            self.payloads.append(kwargs["json"])
            return Response()

    async def scenario():
        client = Client()
        monkeypatch.setattr(pangu_completion, "_get_pangu_client", lambda: client)
        with pytest.raises(Exception, match="after 1 attempt"):
            await pangu_completion.generate_pangu_async(
                "pangu/model", [], [], call_id="call-123"
            )
        assert len(client.payloads) == 1
        assert client.payloads[0]["rid"] == "call-123"

    _disable_logs(monkeypatch)
    asyncio.run(scenario())
