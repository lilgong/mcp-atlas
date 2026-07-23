#!/usr/bin/env python3
"""Remove orphaned MCP task containers owned by this completion service."""

import asyncio

from mcp_completion.task_sandbox import reap_owned_task_sandboxes


if __name__ == "__main__":
    asyncio.run(reap_owned_task_sandboxes())
