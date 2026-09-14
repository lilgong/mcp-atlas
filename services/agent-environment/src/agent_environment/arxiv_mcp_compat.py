"""Run arxiv-mcp-server with all arXiv HTTP traffic routed through the relay."""

from __future__ import annotations

import contextlib
from typing import Any

import httpx
import requests

try:
    from .egress_relay_client import enabled, fetch
except ImportError:  # Executed as the template's standalone script.
    from egress_relay_client import enabled, fetch


ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})


def _uses_relay(url: Any) -> bool:
    return enabled() and httpx.URL(str(url)).host in ARXIV_HOSTS


def _httpx_response(method: str, url: Any, response: Any) -> httpx.Response:
    request = httpx.Request(method, str(url))
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=response.body,
        request=request,
    )


def _body(kwargs: dict[str, Any]) -> bytes | str | dict[str, Any] | None:
    content = kwargs.get("content")
    if content is not None:
        return content
    data = kwargs.get("data")
    if data is not None:
        return data
    if kwargs.get("json") is not None:
        return __import__("json").dumps(kwargs["json"], separators=(",", ":"))
    return None


def install_httpx_relay() -> None:
    original_sync = httpx.Client.request
    original_async = httpx.AsyncClient.request
    original_stream = httpx.Client.stream

    def sync_request(self, method, url, **kwargs):
        if not _uses_relay(url):
            return original_sync(self, method, url, **kwargs)
        response = fetch(
            method, str(url), params=kwargs.get("params"), body=_body(kwargs),
            headers=dict(kwargs.get("headers") or {}),
            allow_redirects=bool(kwargs.get("follow_redirects", True)),
        )
        return _httpx_response(method, url, response)

    async def async_request(self, method, url, **kwargs):
        if not _uses_relay(url):
            return await original_async(self, method, url, **kwargs)
        import asyncio

        response = await asyncio.to_thread(
            fetch, method, str(url), params=kwargs.get("params"),
            body=_body(kwargs), headers=dict(kwargs.get("headers") or {}),
            allow_redirects=bool(kwargs.get("follow_redirects", True)),
        )
        return _httpx_response(method, url, response)

    @contextlib.contextmanager
    def stream(self, method, url, **kwargs):
        if not _uses_relay(url):
            with original_stream(self, method, url, **kwargs) as response:
                yield response
            return
        yield sync_request(self, method, url, **kwargs)

    httpx.Client.request = sync_request
    httpx.AsyncClient.request = async_request
    httpx.Client.stream = stream


def install_requests_relay() -> None:
    original = requests.Session.request

    def request(self, method, url, **kwargs):
        if not _uses_relay(url):
            return original(self, method, url, **kwargs)
        response = fetch(
            method, url, params=kwargs.get("params"), body=kwargs.get("data"),
            headers=dict(kwargs.get("headers") or {}),
            allow_redirects=bool(kwargs.get("allow_redirects", True)),
        )
        result = requests.Response()
        result.status_code = response.status_code
        result.headers.update(response.headers)
        result._content = response.body
        result.url = response.url
        result.request = requests.Request(method, url).prepare()
        return result

    requests.Session.request = request


def main() -> None:
    if enabled():
        install_httpx_relay()
        install_requests_relay()
    from arxiv_mcp_server import main as upstream_main

    upstream_main()


if __name__ == "__main__":
    main()
