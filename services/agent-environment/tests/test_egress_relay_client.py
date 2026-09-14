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


def test_arxiv_httpx_and_requests_use_the_same_relay(monkeypatch):
    calls = []

    def fake_fetch(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _response(url)

    monkeypatch.setattr(arxiv_compat, "enabled", lambda: True)
    monkeypatch.setattr(arxiv_compat, "fetch", fake_fetch)
    originals = (
        httpx.Client.request, httpx.AsyncClient.request, httpx.Client.stream,
        requests.Session.request,
    )
    try:
        arxiv_compat.install_httpx_relay()
        arxiv_compat.install_requests_relay()
        assert httpx.get("https://export.arxiv.org/api/query").text == "ok"
        assert requests.get("https://arxiv.org/pdf/1").content == b"ok"

        async def request_async():
            async with httpx.AsyncClient() as client:
                return await client.get("https://export.arxiv.org/api/query")

        assert asyncio.run(request_async()).text == "ok"
    finally:
        httpx.Client.request, httpx.AsyncClient.request, httpx.Client.stream = originals[:3]
        requests.Session.request = originals[3]

    assert len(calls) == 3
