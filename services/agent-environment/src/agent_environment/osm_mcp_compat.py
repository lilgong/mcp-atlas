"""Run osm-mcp-server with a configurable, schema-preserving Overpass URL."""

from __future__ import annotations

import asyncio
import inspect
import os
from importlib.metadata import version
from typing import Any

import aiohttp
from osm_mcp_server import server


EXPECTED_VERSION = "0.1.1"
UPSTREAM_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_OVERPASS_URL = "https://overpass.kumi.systems/api/interpreter"
FALLBACK_OVERPASS_URL = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
OVERPASS_ATTEMPT_TIMEOUT_SECONDS = 45


class _FallbackRequestContext:
    def __init__(self, session, request, urls, args, kwargs):
        self.session = session
        self.request = request
        self.urls = urls
        self.args = args
        self.kwargs = kwargs
        self.active = None

    async def __aenter__(self):
        last_error = None
        for index, url in enumerate(self.urls):
            kwargs = dict(self.kwargs)
            kwargs.setdefault(
                "timeout",
                aiohttp.ClientTimeout(total=OVERPASS_ATTEMPT_TIMEOUT_SECONDS),
            )
            context = self.request(self.session, url, *self.args, **kwargs)
            try:
                response = await context.__aenter__()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                continue
            if (
                response.status in RETRYABLE_STATUSES
                and index + 1 < len(self.urls)
            ):
                await context.__aexit__(None, None, None)
                continue
            self.active = context
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("no Overpass endpoint was attempted")

    async def __aexit__(self, exc_type, exc, traceback):
        if self.active is None:
            return False
        return await self.active.__aexit__(exc_type, exc, traceback)


def install_overpass_redirect() -> None:
    installed = version("osm-mcp-server")
    if installed != EXPECTED_VERSION:
        raise RuntimeError(
            "OSM compatibility wrapper expected osm-mcp-server=="
            f"{EXPECTED_VERSION}, found {installed}"
        )
    if UPSTREAM_OVERPASS_URL not in inspect.getsource(server):
        raise RuntimeError(
            "OSM compatibility wrapper no longer matches the pinned upstream "
            "implementation; inspect osm_mcp_server.server before upgrading"
        )
    configured = (
        os.getenv("SYN_OSM_OVERPASS_URLS")
        or os.getenv("SYN_OSM_OVERPASS_URL")
        or f"{DEFAULT_OVERPASS_URL},{FALLBACK_OVERPASS_URL}"
    )
    targets = tuple(url.strip() for url in configured.split(",") if url.strip())
    if not targets or any(
        not target.startswith(("https://", "http://")) for target in targets
    ):
        raise RuntimeError(
            "SYN_OSM_OVERPASS_URLS must contain comma-separated HTTP(S) URLs"
        )

    original_get = aiohttp.ClientSession.get
    original_post = aiohttp.ClientSession.post

    def redirect(url: str) -> str:
        return targets[0] if url == UPSTREAM_OVERPASS_URL else url

    def redirected_get(self: aiohttp.ClientSession, url: str, *args: Any, **kwargs: Any):
        return original_get(self, redirect(url), *args, **kwargs)

    def redirected_post(self: aiohttp.ClientSession, url: str, *args: Any, **kwargs: Any):
        if url != UPSTREAM_OVERPASS_URL:
            return original_post(self, url, *args, **kwargs)
        return _FallbackRequestContext(
            self, original_post, targets, args, kwargs,
        )

    aiohttp.ClientSession.get = redirected_get
    aiohttp.ClientSession.post = redirected_post


def main() -> None:
    install_overpass_redirect()
    server.mcp.run()


if __name__ == "__main__":
    main()
