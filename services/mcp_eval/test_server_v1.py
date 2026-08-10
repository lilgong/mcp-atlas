#!/usr/bin/env python3
"""Validate tools and official data through the legacy shared MCP runtime.

Every representative tool call is sent directly to ``/call-tool`` on the
configured shared service. Use this entry point for the original
``agent-environment`` deployment.
"""

from mcp_server_probe import (
    ApiError,
    DataMismatch,
    cli_legacy,
    load_target_servers,
    make_caller,
    probe_airtable,
    run_legacy,
)

main = run_legacy


if __name__ == "__main__":
    cli_legacy()
