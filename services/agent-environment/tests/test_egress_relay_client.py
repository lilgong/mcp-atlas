from __future__ import annotations

import asyncio
import json

import httpx
import requests

from agent_environment import arxiv_mcp_compat as arxiv_compat
from agent_environment import egress_relay_client as relay_client


def _response(url: str, body: bytes = b"ok") -> relay_client.RelayResponse:
    return relay_client.RelayResponse(200, {"Content-Type": "text/plain"}, body, url)


def test_client_normalizes_http_and_encodes_query_before_relay(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "status_code": 200, "headers": {}, "body_base64": "",
                "url": "https://arxiv.org/pdf/1",
            }).encode()

    def urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(relay_client, "RELAY_URL", "http://relay:3985")
    monkeypatch.setattr(relay_client, "RELAY_TOKEN", "secret")
    monkeypatch.setattr(relay_client.urllib.request, "urlopen", urlopen)
    relay_client.fetch("GET", "http://arxiv.org/pdf/1", params={"download": "1"})

    assert captured["payload"]["url"] == "https://arxiv.org/pdf/1?download=1"
    assert captured["payload"]["params"] == {}

    raw_query = (
        'http://export.arxiv.org/api/query?search_query=("multi+agent+systems")'
        '+AND+submittedDate:[202401010000+TO+202412312359]'
    )
    expected_query = (
        "https://export.arxiv.org/api/query?"
        "search_query=(%22multi+agent+systems%22)"
        "+AND+submittedDate:[202401010000+TO+202412312359]"
    )
    relay_client.fetch("GET", raw_query)
    assert captured["payload"]["url"] == expected_query
    relay_client.fetch("GET", expected_query)
    assert captured["payload"]["url"] == expected_query


def test_arxiv_httpx_and_requests_use_the_same_relay(monkeypatch):
    calls = []

    def fake_fetch(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _response(url)

    monkeypatch.setattr(arxiv_compat, "enabled", lambda: True)
    monkeypatch.setattr(arxiv_compat, "fetch", fake_fetch)
    async def fake_fetch_async(*args, **kwargs):
        return fake_fetch(*args, **kwargs)
    monkeypatch.setattr(arxiv_compat, "fetch_async", fake_fetch_async)
    originals = (
        httpx.Client.request, httpx.AsyncClient.request, httpx.Client.stream,
        requests.Session.request,
    )
    try:
        arxiv_compat.install_httpx_relay()
        arxiv_compat.install_requests_relay()
        assert httpx.get("https://export.arxiv.org/api/query").text == "ok"
        assert requests.get("https://arxiv.org/pdf/1").content == b"ok"
        assert requests.post(
            "https://arxiv.org/upload",
            files={"paper": ("paper.txt", b"contents")},
        ).content == b"ok"
        with httpx.Client(base_url="https://arxiv.org") as client:
            with client.stream(
                "GET", "/pdf/1", headers={"User-Agent": "arxiv-test/1.0"},
            ) as response:
                assert response.read() == b"ok"

        async def request_async():
            query = (
                '/api/query?search_query=("multi+agent+systems")'
                '+AND+submittedDate:[202401010000+TO+202412312359]'
                '&max_results=10&sortBy=relevance&sortOrder=descending'
            )
            async with httpx.AsyncClient(base_url="http://export.arxiv.org") as client:
                return await client.get(query)

        assert asyncio.run(request_async()).text == "ok"
    finally:
        httpx.Client.request, httpx.AsyncClient.request, httpx.Client.stream = originals[:3]
        requests.Session.request = originals[3]

    assert len(calls) == 5
    assert b'filename="paper.txt"' in calls[2][2]["body"]
    assert calls[2][2]["headers"]["Content-Type"].startswith("multipart/form-data;")
    assert calls[3][2]["headers"]["user-agent"] == "arxiv-test/1.0"
    assert calls[4][1] == (
        "http://export.arxiv.org/api/query?"
        "search_query=(%22multi+agent+systems%22)"
        "+AND+submittedDate:[202401010000+TO+202412312359]"
        "&max_results=10&sortBy=relevance&sortOrder=descending"
    )
    assert calls[4][2]["body"] == b""
    assert "params" not in calls[4][2]
    assert all(
        "user-agent" not in {key.lower() for key in kwargs["headers"]}
        for index, (_method, _url, kwargs) in enumerate(calls)
        if index != 3
    )


def test_httpx_relay_resolves_only_relative_urls(monkeypatch):
    class Client:
        build_count = 0

        def build_request(self, method, url):
            self.build_count += 1
            return httpx.Request(method, f"https://export.arxiv.org/{url.lstrip('/')}")

    client = Client()
    monkeypatch.setattr(arxiv_compat, "enabled", lambda: True)

    assert not arxiv_compat._uses_httpx_relay(
        client, "GET", "https://example.com/api/query",
    )
    assert client.build_count == 0
    assert arxiv_compat._uses_httpx_relay(client, "GET", "/api/query")
    assert client.build_count == 1

    monkeypatch.setattr(arxiv_compat, "enabled", lambda: False)
    assert not arxiv_compat._uses_httpx_relay(client, "GET", "/not-resolved")
    assert client.build_count == 1


def test_generic_relay_names_and_empty_values_disable(monkeypatch):
    import importlib
    with monkeypatch.context() as env:
        env.setenv("MCP_RELAY_URL", "http://generic-relay")
        env.setenv("MCP_RELAY_TOKEN", "generic-token")
        importlib.reload(relay_client)
        assert relay_client.RELAY_URL == "http://generic-relay"
        assert relay_client.RELAY_TOKEN == "generic-token"
        env.setenv("MCP_RELAY_URL", "")
        env.setenv("MCP_RELAY_TOKEN", "")
        importlib.reload(relay_client)
        assert not relay_client.enabled()
    importlib.reload(relay_client)


def test_request_budget_leaves_delivery_margin_and_error_is_classified(monkeypatch):
    import io
    import urllib.error
    import pytest
    monkeypatch.setattr(relay_client, 'RELAY_TIMEOUT', 90)
    assert relay_client.request_budget_seconds() == 85
    monkeypatch.setattr(relay_client, 'RELAY_URL', 'http://relay:3985')
    monkeypatch.setattr(relay_client, 'RELAY_TOKEN', 'test-only')
    calls = []
    def urlopen(request, timeout):
        calls.append(json.loads(request.data))
        raise urllib.error.HTTPError(request.full_url, 504, 'timeout', {},
                                     io.BytesIO(b'{"code":"queue_timeout","error":"Queue deadline exceeded"}'))
    monkeypatch.setattr(relay_client.urllib.request, 'urlopen', urlopen)
    with pytest.raises(relay_client.RelayRequestError) as caught:
        relay_client.fetch('GET', 'https://export.arxiv.org/api/query')
    assert caught.value.code == 'queue_timeout' and caught.value.status == 504
    assert len(calls) == 1 and calls[0]['timeout_seconds'] == 85
    monkeypatch.setattr(relay_client, 'RELAY_TIMEOUT', float('nan'))
    with pytest.raises(ValueError):
        relay_client.request_budget_seconds()


def test_async_relay_call_cancellation_closes_client_operation(monkeypatch):
    started, cancelled = asyncio.Event(), []
    calls = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            cancelled.append('closed')
        async def post(self, url, **kwargs):
            calls.append(json.loads(kwargs['content']))
            started.set()
            await asyncio.Future()
    monkeypatch.setattr(relay_client, 'RELAY_URL', 'http://relay:3985')
    monkeypatch.setattr(relay_client, 'RELAY_TOKEN', 'test-only')
    monkeypatch.setattr(relay_client.httpx, 'AsyncClient', Client)
    async def run():
        import pytest
        task = asyncio.create_task(relay_client.fetch_async('GET', 'https://export.arxiv.org/api/query'))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert len(calls) == 1 and cancelled == ['closed']
