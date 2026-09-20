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


def _uses_httpx_relay(
    client: httpx.Client | httpx.AsyncClient,
    method: str,
    url: Any,
) -> bool:
    if not enabled():
        return False
    resolved_url = httpx.URL(str(url))
    if not resolved_url.host:
        resolved_url = client.build_request(method, url).url
    return resolved_url.host in ARXIV_HOSTS


def _httpx_response(request: httpx.Request, response: Any) -> httpx.Response:
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=response.body,
        request=request,
    )


def _prepare_httpx_request(
    client: httpx.Client | httpx.AsyncClient,
    method: str,
    url: Any,
    kwargs: dict[str, Any],
) -> httpx.Request:
    """Apply httpx's normal URL and payload encoding before using the relay."""
    request_kwargs = {
        key: kwargs[key]
        for key in (
            "content", "data", "files", "json", "params", "headers", "cookies",
            "timeout", "extensions",
        )
        if key in kwargs
    }
    return client.build_request(method, url, **request_kwargs)


def _relay_headers(prepared: Any, supplied: Any) -> dict[str, str]:
    headers = dict(prepared)
    supplied_names = {str(key).lower() for key in dict(supplied or {})}
    if "user-agent" not in supplied_names:
        headers.pop("user-agent", None)
        headers.pop("User-Agent", None)
    return headers


def install_httpx_relay() -> None:
    original_sync = httpx.Client.request
    original_async = httpx.AsyncClient.request
    original_stream = httpx.Client.stream

    def sync_request(self, method, url, **kwargs):
        if not _uses_httpx_relay(self, method, url):
            return original_sync(self, method, url, **kwargs)
        request = _prepare_httpx_request(self, method, url, kwargs)
        request.read()
        response = fetch(
            method, str(request.url), body=request.content,
            headers=_relay_headers(request.headers, kwargs.get("headers")),
            allow_redirects=bool(kwargs.get("follow_redirects", True)),
        )
        return _httpx_response(request, response)

    async def async_request(self, method, url, **kwargs):
        if not _uses_httpx_relay(self, method, url):
            return await original_async(self, method, url, **kwargs)
        import asyncio

        request = _prepare_httpx_request(self, method, url, kwargs)
        await request.aread()
        response = await asyncio.to_thread(
            fetch, method, str(request.url), body=request.content,
            headers=_relay_headers(request.headers, kwargs.get("headers")),
            allow_redirects=bool(kwargs.get("follow_redirects", True)),
        )
        return _httpx_response(request, response)

    @contextlib.contextmanager
    def stream(self, method, url, **kwargs):
        if not _uses_httpx_relay(self, method, url):
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
        request = self.prepare_request(requests.Request(
            method,
            url,
            params=kwargs.get("params"),
            data=kwargs.get("data"),
            json=kwargs.get("json"),
            files=kwargs.get("files"),
            headers=kwargs.get("headers"),
            cookies=kwargs.get("cookies"),
            auth=kwargs.get("auth"),
            hooks=kwargs.get("hooks"),
        ))
        response = fetch(
            method, request.url, body=request.body,
            headers=_relay_headers(request.headers, kwargs.get("headers")),
            allow_redirects=bool(kwargs.get("allow_redirects", True)),
        )
        result = requests.Response()
        result.status_code = response.status_code
        result.headers.update(response.headers)
        result._content = response.body
        result.url = response.url
        result.request = request
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
