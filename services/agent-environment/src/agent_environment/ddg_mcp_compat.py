"""Run duckduckgo-mcp-server 0.1.1 with a valid httpx timeout type."""

from __future__ import annotations

from importlib.metadata import version

import httpx
from duckduckgo_mcp_server import server


EXPECTED_VERSION = "0.1.1"


def install_timeout_compat() -> None:
    installed = version("duckduckgo-mcp-server")
    if installed != EXPECTED_VERSION:
        raise RuntimeError(
            "DDG compatibility wrapper expected duckduckgo-mcp-server=="
            f"{EXPECTED_VERSION}, found {installed}"
        )
    if hasattr(httpx, "TimeoutError"):
        raise RuntimeError(
            "DDG compatibility wrapper is no longer needed: httpx now "
            "provides TimeoutError; inspect the pinned server before upgrading"
        )
    httpx.TimeoutError = httpx.TimeoutException  # type: ignore[attr-defined]


def main() -> None:
    install_timeout_compat()
    server.mcp.run()


if __name__ == "__main__":
    main()
