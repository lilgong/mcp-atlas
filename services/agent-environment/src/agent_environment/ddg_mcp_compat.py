"""Run the pinned DuckDuckGo MCP server with browser-fingerprint fallback.

DuckDuckGo's HTML endpoint returns an HTTP 202 bot challenge to ordinary HTTP
clients on this network.  Version 0.6.0 can detect that response and retry via
``curl_cffi`` with Chrome impersonation; the ``browser`` extra supplies it.
"""

from __future__ import annotations

from importlib.metadata import version

from duckduckgo_mcp_server import server


EXPECTED_VERSION = "0.6.0"


def validate_runtime() -> None:
    installed = version("duckduckgo-mcp-server")
    if installed != EXPECTED_VERSION:
        raise RuntimeError(
            "DDG compatibility entrypoint expected duckduckgo-mcp-server=="
            f"{EXPECTED_VERSION}, found {installed}"
        )
    try:
        import curl_cffi  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "DDG search requires the duckduckgo-mcp-server[browser] extra"
        ) from exc
    if getattr(server.searcher, "backend", None) != "auto":
        raise RuntimeError(
            "DDG server is not configured for automatic browser fallback"
        )


def main() -> None:
    validate_runtime()
    server.mcp.run()


if __name__ == "__main__":
    main()
