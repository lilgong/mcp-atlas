#!/usr/bin/env python3
"""End-to-end check for per-task MCP container isolation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import uuid

from mcp_completion.mcp_client import IsolatedMCPClient


CLOUD_SECRET_NAMES = {
    "AIRTABLE_API_KEY",
    "BRAVE_API_KEY",
    "EXA_API_KEY",
    "GITHUB_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REFRESH_TOKEN",
    "LARA_ACCESS_KEY_ID",
    "LARA_ACCESS_KEY_SECRET",
    "NOTION_TOKEN",
    "SLACK_MCP_XOXC_TOKEN",
    "SLACK_MCP_XOXD_TOKEN",
}

LOCAL_SERVER_PROBES = {
    "cli-mcp-server_show_security_rules",
    "desktop-commander_get_config",
    "filesystem_list_allowed_directories",
    "git_git_status",
    "mcp-code-executor_get_environment_config",
    "mcp-server-code-runner_run-code",
    "memory_read_graph",
    "mongodb_list-databases",
}

NETWORK_SERVER_PROBES = {
    "arxiv_search_papers",
    "pubmed_search_pubmed_key_words",
}


def _mongo_count(result) -> int | None:
    text = "\n".join(
        item.text for item in result.content if getattr(item, "type", None) == "text"
    )
    match = re.search(r"Found\s+(\d+)\s+documents?", text)
    return int(match.group(1)) if match else None


def _container_env_names(container_name: str) -> set[str]:
    result = subprocess.run(
        [
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{json .Config.Env}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(result.stdout)
    return {item.split("=", 1)[0] for item in values}


def _container_network_mode(container_name: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{.HostConfig.NetworkMode}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


async def _write_then_destroy(shared_url: str, marker: str) -> None:
    path = f"/data/{marker}.txt"
    tools = ["filesystem_write_file", "filesystem_read_text_file"]
    async with IsolatedMCPClient(
        task_id=f"isolation-write-{marker}",
        shared_url=shared_url,
        enabled_tools=tools,
    ) as client:
        listed = {tool.name for tool in await client.list_tools()}
        if set(tools) - listed:
            raise RuntimeError(f"Missing local tools: {sorted(set(tools) - listed)}")
        assert client._sandbox is not None
        local = next(
            item
            for item in client._sandbox.containers
            if item.kind == "local"
        )
        leaked = _container_env_names(local.name) & CLOUD_SECRET_NAMES
        if leaked:
            raise RuntimeError(
                f"Task-local container received cloud credentials: {sorted(leaked)}"
            )
        if _container_network_mode(local.name) != "none":
            raise RuntimeError("Task-local arbitrary-code container has network access")
        write_result = await client.call_tool(
            "filesystem_write_file",
            {"path": path, "content": marker},
        )
        if write_result.is_error:
            raise RuntimeError(f"Task-local write failed: {write_result}")
        read_result = await client.call_tool(
            "filesystem_read_text_file", {"path": path}
        )
        if read_result.is_error or marker not in str(read_result):
            raise RuntimeError("Task-local read-after-write did not see its own state")


async def _verify_fresh_copy(shared_url: str, marker: str) -> None:
    path = f"/data/{marker}.txt"
    async with IsolatedMCPClient(
        task_id=f"isolation-fresh-{marker}",
        shared_url=shared_url,
        enabled_tools=["filesystem_read_text_file"],
    ) as client:
        result = await client.call_tool(
            "filesystem_read_text_file", {"path": path}
        )
        if not result.is_error and marker in str(result):
            raise RuntimeError("State leaked from the previous task container")


async def _verify_cloud_write_gate(shared_url: str) -> None:
    async with IsolatedMCPClient(
        task_id="isolation-cloud-write-gate",
        shared_url=shared_url,
        enabled_tools=[
            "airtable_list_bases",
            "airtable_create_record",
            "notion_API-post-search",
            "notion_API-post-page",
        ],
    ) as client:
        listed = {tool.name for tool in await client.list_tools()}
        if "airtable_create_record" in listed or "notion_API-post-page" in listed:
            raise RuntimeError("Cloud write tool was advertised")
        if "airtable_list_bases" not in listed or "notion_API-post-search" not in listed:
            raise RuntimeError("Cloud read tool was unexpectedly removed")
        try:
            await client.call_tool("airtable_create_record", {})
        except Exception:
            pass
        else:
            raise RuntimeError("Cloud write tool reached the execution backend")


async def _verify_isolated_server_catalogs(shared_url: str) -> None:
    for kind, probes in (
        ("local", LOCAL_SERVER_PROBES),
        ("network", NETWORK_SERVER_PROBES),
    ):
        async with IsolatedMCPClient(
            task_id=f"isolation-{kind}-server-catalog",
            shared_url=shared_url,
            enabled_tools=sorted(probes),
        ) as client:
            listed = {tool.name for tool in await client.list_tools()}
            missing = probes - listed
            if missing:
                raise RuntimeError(
                    f"Missing {kind} isolated-server probes: {sorted(missing)}"
                )


async def _mongo_write_then_destroy(shared_url: str, marker: str) -> None:
    tools = ["mongodb_count", "mongodb_insert-many"]
    query = {"_mcp_atlas_isolation_marker": marker}
    async with IsolatedMCPClient(
        task_id=f"isolation-mongo-write-{marker}",
        shared_url=shared_url,
        enabled_tools=tools,
    ) as client:
        before = await client.call_tool(
            "mongodb_count",
            {
                "database": "video_game_store",
                "collection": "Inventory",
                "query": query,
            },
        )
        if before.is_error or _mongo_count(before) != 0:
            raise RuntimeError(f"Unexpected Mongo baseline marker count: {before}")
        inserted = await client.call_tool(
            "mongodb_insert-many",
            {
                "database": "video_game_store",
                "collection": "Inventory",
                "documents": [{"_mcp_atlas_isolation_marker": marker}],
            },
        )
        if inserted.is_error:
            raise RuntimeError(f"Mongo insert failed: {inserted}")
        after = await client.call_tool(
            "mongodb_count",
            {
                "database": "video_game_store",
                "collection": "Inventory",
                "query": query,
            },
        )
        if after.is_error or _mongo_count(after) != 1:
            raise RuntimeError(f"Mongo write was not visible in-task: {after}")


async def _verify_fresh_mongo(shared_url: str, marker: str) -> None:
    async with IsolatedMCPClient(
        task_id=f"isolation-mongo-fresh-{marker}",
        shared_url=shared_url,
        enabled_tools=["mongodb_count"],
    ) as client:
        result = await client.call_tool(
            "mongodb_count",
            {
                "database": "video_game_store",
                "collection": "Inventory",
                "query": {"_mcp_atlas_isolation_marker": marker},
            },
        )
        if result.is_error or _mongo_count(result) != 0:
            raise RuntimeError(f"Mongo state leaked between tasks: {result}")


async def _one_concurrency_probe(shared_url: str, index: int) -> None:
    async with IsolatedMCPClient(
        task_id=f"isolation-concurrency-{index}",
        shared_url=shared_url,
        enabled_tools=["filesystem_list_directory"],
    ) as client:
        listed = await client.list_tools()
        if not any(tool.name == "filesystem_list_directory" for tool in listed):
            raise RuntimeError(f"Concurrency probe {index} did not load filesystem")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--shared-url",
        default=os.getenv("MCP_SERVER_URL", "http://127.0.0.1:1984"),
    )
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()

    marker = f"mcp_atlas_isolation_{uuid.uuid4().hex}"
    await _verify_cloud_write_gate(args.shared_url)
    await _verify_isolated_server_catalogs(args.shared_url)
    await _write_then_destroy(args.shared_url, marker)
    await _verify_fresh_copy(args.shared_url, marker)
    await _mongo_write_then_destroy(args.shared_url, marker)
    await _verify_fresh_mongo(args.shared_url, marker)
    await asyncio.gather(
        *(
            _one_concurrency_probe(args.shared_url, index)
            for index in range(args.concurrency)
        )
    )
    print(
        "PASS: all isolated servers loaded; cloud writes blocked; "
        "filesystem and Mongo writes destroyed; "
        f"{args.concurrency} task sandboxes started concurrently"
    )


if __name__ == "__main__":
    asyncio.run(main())
