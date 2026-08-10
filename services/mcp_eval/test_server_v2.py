#!/usr/bin/env python3
"""Validate tools and official data through task-isolated production routes.

Cloud tools use the configured shared service. Git, filesystem, Mongo and
other task-routed tools use disposable containers on this host.
"""

from mcp_server_probe import cli_isolated, run_isolated

main = run_isolated


if __name__ == "__main__":
    cli_isolated()
