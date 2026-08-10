#!/usr/bin/env python3
"""Remove orphaned MCP task containers owned by this completion service."""

import asyncio

from mcp_completion.task_sandbox import reap_owned_task_sandboxes


if __name__ == "__main__":
    result = asyncio.run(reap_owned_task_sandboxes())
    print(
        "MCP-Atlas sandbox cleanup: "
        f"containers removed={result['containers_removed']} "
        f"remaining={result['containers_remaining']}; "
        f"volumes removed={result['volumes_removed']} "
        f"remaining={result['volumes_remaining']}; "
        f"removal failures={result['removal_failures']}; "
        f"listing failures={result['listing_failures']}"
    )
    if (
        result["containers_remaining"]
        or result["volumes_remaining"]
        or result["removal_failures"]
        or result["listing_failures"]
    ):
        raise SystemExit(1)
