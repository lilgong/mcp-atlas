"""Compatibility entrypoint for the pinned Oxylabs MCP server.

oxylabs-mcp 0.4.1 exposes ``universal_scraper(url=...)`` but forwards only the
URL to the configured Scraper API. The Realtime/Yibu-compatible endpoint also
requires ``source="universal"``. Inject it at the HTTP boundary without
changing the public MCP tool schema.
"""

from __future__ import annotations

import inspect
from importlib.metadata import version
from typing import Any


EXPECTED_OXYLABS_MCP_VERSION = "0.4.1"


def normalize_scraper_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add the source omitted by universal_scraper without touching searches."""
    if "url" in payload and "source" not in payload and "query" not in payload:
        return {**payload, "source": "universal"}
    return payload


def install_compatibility_patch() -> None:
    """Patch the pinned package once, failing clearly if upstream drifted."""
    from oxylabs_mcp import utils

    actual_version = version("oxylabs-mcp")
    if actual_version != EXPECTED_OXYLABS_MCP_VERSION:
        raise RuntimeError(
            "Oxylabs compatibility patch requires "
            f"oxylabs-mcp=={EXPECTED_OXYLABS_MCP_VERSION}, "
            f"found {actual_version}"
        )
    wrapper = getattr(utils, "_OxylabsClientWrapper", None)
    if wrapper is None or not hasattr(wrapper, "scrape"):
        raise RuntimeError(
            "oxylabs-mcp compatibility target "
            "utils._OxylabsClientWrapper.scrape is missing"
        )
    if getattr(wrapper.scrape, "_atlas_universal_source_compat", False):
        return

    original_scrape = wrapper.scrape
    if list(inspect.signature(original_scrape).parameters) != ["self", "payload"]:
        raise RuntimeError(
            "unexpected _OxylabsClientWrapper.scrape signature: "
            f"{inspect.signature(original_scrape)}"
        )

    async def scrape(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await original_scrape(self, normalize_scraper_payload(payload))

    scrape._atlas_universal_source_compat = True  # type: ignore[attr-defined]
    wrapper.scrape = scrape


def main() -> None:
    install_compatibility_patch()
    from oxylabs_mcp import main as upstream_main

    upstream_main()


if __name__ == "__main__":
    main()
