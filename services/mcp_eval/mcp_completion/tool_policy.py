"""Tool routing and cloud-write policy for isolated MCP evaluations.

The important distinction is where mutable state lives:

* ``task_local`` servers can mutate local state and therefore run in a disposable,
  credential-free container for each task.
* ``task_network`` servers maintain a local download cache but also need Internet
  access.  They get a separate disposable container that does not expose a shell,
  code runner, or cloud credential.
* ``cloud`` servers are served by the long-running credentialed environment.
  Tools that mutate shared cloud account data are fail-closed here.

The cloud policy is an allowlist for stateful cloud servers.  A newly added tool
on one of those servers is blocked until it is classified, rather than silently
becoming writable.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Iterable, Mapping, Optional


KNOWN_SERVERS = (
    "clinicaltrialsgov-mcp-server",
    "mcp-server-code-runner",
    "mcp-code-executor",
    "desktop-commander",
    "google-workspace",
    "cli-mcp-server",
    "lara-translate",
    "brave-search",
    "osm-mcp-server",
    "open-library",
    "weather-data",
    "national-parks",
    "met-museum",
    "e2b-server",
    "ddg-search",
    "twelvedata",
    "calculator",
    "filesystem",
    "airtable",
    "alchemy",
    "context7",
    "github",
    "google-maps",
    "memory",
    "mongodb",
    "notion",
    "oxylabs",
    "pubmed",
    "slack",
    "weather",
    "wikipedia",
    "arxiv",
    "fetch",
    "git",
    "whois",
    "exa",
)

# The official dataset still names some services that the official runtime
# removed. The official harness naturally drops those names when it intersects
# ENABLED_TOOLS with list-tools. Keep the same behavior during our preflight:
# ignore only these explicitly retired names, while continuing to reject every
# other unknown tool as schema drift.
OFFICIAL_RETIRED_SERVERS = (
    "airbnb",
    "rijksmuseum-server",
    "f1-mcp-server",
    "anili",
    "balldontlie",
    "reddit",
    "yfmcp",
    "youtube",
    "youtube-transcript",
)

# These servers may execute arbitrary local code or mutate files/database state.
# They run in a disposable, credential-free container for each agent task.  The
# container has outbound networking, matching the official tool behavior, while
# the host control APIs remain bound to loopback.
TASK_LOCAL_SERVERS = frozenset(
    {
        "cli-mcp-server",
        "desktop-commander",
        "filesystem",
        "git",
        "mcp-code-executor",
        "mcp-server-code-runner",
        "memory",
        "mongodb",
    }
)

# These servers download and retain local artifacts.  They need outbound network
# access, so they are kept separate from the no-network arbitrary-code sandbox.
TASK_NETWORK_SERVERS = frozenset({"arxiv", "pubmed"})
TASK_MONGODB_DATABASE = "store"
UNSUPPORTED_TOOLS: frozenset[str] = frozenset()

# Generation safety is deliberately separate from routing.  A tool can be safe
# to execute in a disposable task container while still being inappropriate for
# read-only evidence synthesis (for example ``mongodb_drop-database``).
_LOCAL_READ_ACTIONS = {
    "desktop-commander": frozenset({
        "get_config", "get_file_info", "get_usage_stats", "list_directory",
        "list_processes", "list_sessions", "read_file", "read_multiple_files",
        "read_process_output", "search_code", "search_files",
    }),
    "filesystem": frozenset({
        "directory_tree", "get_file_info", "list_allowed_directories",
        "list_directory", "list_directory_with_sizes", "read_file",
        "read_media_file", "read_multiple_files", "read_text_file",
        "search_files",
    }),
    "git": frozenset({
        "git_diff", "git_diff_staged", "git_diff_unstaged", "git_log",
        "git_show", "git_status",
    }),
    "memory": frozenset({"open_nodes", "read_graph", "search_nodes"}),
    "mongodb": frozenset({
        "aggregate", "collection-indexes", "collection-schema",
        "collection-storage-size", "count", "db-stats", "explain", "find",
        "list-collections", "list-databases", "mongodb-logs",
    }),
}
_LOCAL_COMPUTE_SERVERS = frozenset({
    "cli-mcp-server", "mcp-code-executor", "mcp-server-code-runner",
})
_CLOUD_COMPUTE_SERVERS = frozenset({"calculator", "e2b-server", "lara-translate"})
_ENTRY_TOKENS = (
    "list", "search", "schema", "directory_tree", "resolve", "read_graph",
)
_SUPPORT_TOKENS = ("get_config", "get_usage", "logs", "status", "security_rules")


class ToolRoute(str, Enum):
    CLOUD = "cloud"
    TASK_LOCAL = "task_local"
    TASK_NETWORK = "task_network"
    BLOCKED_CLOUD_WRITE = "blocked_cloud_write"
    BLOCKED_UNSUPPORTED = "blocked_unsupported"


# Exact read-only surface for services backed by a shared cloud account.
CLOUD_DATA_READ_TOOLS = frozenset(
    {
        # Airtable
        "airtable_get_record",
        "airtable_list_bases",
        "airtable_list_records",
        "airtable_list_tables",
        "airtable_search_records",
        # GitHub
        "github_get_commit",
        "github_get_file_contents",
        "github_get_issue",
        "github_get_issue_comments",
        "github_get_pull_request",
        "github_get_pull_request_comments",
        "github_get_pull_request_files",
        "github_get_pull_request_review_comments",
        "github_get_pull_request_status",
        "github_get_repository",
        "github_get_tag",
        "github_list_branches",
        "github_list_commits",
        "github_list_issues",
        "github_list_pull_requests",
        "github_list_tags",
        "github_search_code",
        "github_search_issues",
        "github_search_repositories",
        "github_search_users",
        # Google Workspace
        "google-workspace_list_emails",
        "google-workspace_list_events",
        "google-workspace_search_emails",
        # Lara Translate (translation itself is stateless account usage)
        "lara-translate_check_import_status",
        "lara-translate_list_languages",
        "lara-translate_list_memories",
        "lara-translate_translate",
        # Notion: the two POST endpoints below are queries, not writes.
        "notion_API-get-block-children",
        "notion_API-get-self",
        "notion_API-get-user",
        "notion_API-get-users",
        "notion_API-post-database-query",
        "notion_API-post-search",
        "notion_API-retrieve-a-block",
        "notion_API-retrieve-a-comment",
        "notion_API-retrieve-a-database",
        "notion_API-retrieve-a-page",
        "notion_API-retrieve-a-page-property",
        # Slack
        "slack_channels_list",
        "slack_conversations_history",
        "slack_conversations_replies",
        "slack_conversations_search_messages",
    }
)

CLOUD_DATA_SERVERS = frozenset(
    {"airtable", "github", "google-workspace", "lara-translate", "notion", "slack"}
)


def server_for_tool(tool_name: str) -> Optional[str]:
    """Return the canonical server prefix for a live MCP tool name."""

    # Longest-first avoids treating ``mcp-server-code-runner`` as ``mcp``.
    for server in sorted(KNOWN_SERVERS, key=len, reverse=True):
        if tool_name.startswith(f"{server}_"):
            return server

    # Historical datasets used an uppercase MongoDB prefix.
    if tool_name.startswith("MongoDB_"):
        return "mongodb"
    return None


def _is_official_retired_tool(tool_name: str) -> bool:
    return any(
        tool_name.startswith(f"{server}_")
        for server in OFFICIAL_RETIRED_SERVERS
    )


def servers_for_enabled_tools(value: Any) -> list[str]:
    """Return canonical servers from a task's authoritative ENABLED_TOOLS."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("ENABLED_TOOLS is not valid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("ENABLED_TOOLS must be a JSON list")

    servers: set[str] = set()
    for item in value:
        if isinstance(item, str):
            tool_name = item
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            tool_name = item["name"]
        else:
            raise ValueError("ENABLED_TOOLS contains an invalid tool entry")
        server = server_for_tool(tool_name)
        if server is None and _is_official_retired_tool(tool_name):
            continue
        if server is None:
            raise ValueError(f"unknown tool in ENABLED_TOOLS: {tool_name}")
        servers.add(server)
    return sorted(servers)


def is_cloud_data_write(tool_name: str) -> bool:
    """Fail closed for unknown tools on shared-account cloud servers."""

    server = server_for_tool(tool_name)
    return bool(
        server in CLOUD_DATA_SERVERS and tool_name not in CLOUD_DATA_READ_TOOLS
    )


def route_for_tool(tool_name: str) -> ToolRoute:
    if tool_name in UNSUPPORTED_TOOLS:
        return ToolRoute.BLOCKED_UNSUPPORTED
    if is_cloud_data_write(tool_name):
        return ToolRoute.BLOCKED_CLOUD_WRITE

    server = server_for_tool(tool_name)
    if server in TASK_LOCAL_SERVERS:
        return ToolRoute.TASK_LOCAL
    if server in TASK_NETWORK_SERVERS:
        return ToolRoute.TASK_NETWORK
    return ToolRoute.CLOUD


def generation_policy_for_tool(tool_name: str) -> dict[str, object]:
    """Classify whether a live tool may be exposed to evidence generation.

    Routing answers *where* a call can execute.  This policy answers whether a
    normal read-only synthesis campaign should expose it at all.  Unknown
    task-local actions fail closed instead of inheriting safety from isolation.
    """

    route = route_for_tool(tool_name)
    server = server_for_tool(tool_name)
    if route in {ToolRoute.BLOCKED_CLOUD_WRITE, ToolRoute.BLOCKED_UNSUPPORTED}:
        effect = "cloud_mutation" if route is ToolRoute.BLOCKED_CLOUD_WRITE else "unknown"
        allowed = False
    elif server in _LOCAL_COMPUTE_SERVERS:
        effect, allowed = "compute", True
    elif server in _LOCAL_READ_ACTIONS:
        prefix = f"{server}_"
        action = tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name
        allowed = action in _LOCAL_READ_ACTIONS[server]
        effect = "read" if allowed else "local_mutation"
    elif route is ToolRoute.TASK_NETWORK:
        effect, allowed = "read", True
    elif server in _CLOUD_COMPUTE_SERVERS:
        effect, allowed = "compute", True
    elif server is None:
        effect, allowed = "unknown", False
    else:
        # Shared-account mutation tools were already rejected by route_for_tool.
        effect, allowed = "read", True

    lowered = tool_name.casefold()
    if not allowed:
        coverage_role = "excluded"
    elif effect == "compute":
        coverage_role = "compute"
    elif any(token in lowered for token in _SUPPORT_TOKENS):
        coverage_role = "support"
    elif any(token in lowered for token in _ENTRY_TOKENS):
        coverage_role = "entry"
    else:
        coverage_role = "evidence"
    return {
        "effect": effect,
        "generation_allowed": allowed,
        "coverage_role": coverage_role,
        "policy_version": "generation-safety-v1",
    }


def partition_tools(tool_names: Iterable[str]) -> dict[ToolRoute, list[str]]:
    result = {route: [] for route in ToolRoute}
    for tool_name in tool_names:
        result[route_for_tool(tool_name)].append(tool_name)
    return result


def effective_enabled_servers(
    shared_enabled_servers: Iterable[str],
    *,
    isolation_enabled: bool,
    task_data_configured: bool,
    task_mongo_configured: bool,
    allowed_servers: Iterable[str] | None = None,
) -> list[str]:
    """Combine shared-cloud health with task-routed runtime availability.

    In isolation mode, a task-local/network server must never inherit its
    availability from the fixture-free shared container.  Those routes are
    available when task data is configured; Mongo additionally requires its
    fixture image.  With isolation disabled, preserve the legacy shared-only
    behavior.
    """

    shared = set(shared_enabled_servers)
    allowed = set(allowed_servers) if allowed_servers is not None else None
    if not isolation_enabled:
        return sorted(shared if allowed is None else shared & allowed)

    task_routed = set(TASK_LOCAL_SERVERS) | set(TASK_NETWORK_SERVERS)
    effective = shared - task_routed
    if task_data_configured:
        effective.update(TASK_NETWORK_SERVERS)
        effective.update(set(TASK_LOCAL_SERVERS) - {"mongodb"})
        if task_mongo_configured:
            effective.add("mongodb")
    if allowed is not None:
        effective.intersection_update(allowed)
    return sorted(effective)


def shared_routable_servers(
    health: Mapping[str, Any],
) -> tuple[list[str], list[str], int]:
    """Resolve shared routes without dropping transiently degraded servers.

    A server with previously discovered tools remains routable even when its
    latest call marked the connection unhealthy: the router retains those
    routes and reconnects on the next call.  A server that has never exposed
    any tools still fails closed.

    Returns ``(routable, reconnectable, online_count)``.
    """

    if "servers" not in health:
        enabled = sorted(set(health.get("enabled_servers", [])))
        return enabled, [], len(enabled)

    statuses = {
        str(name): str(status)
        for name, status in health.get("servers", [])
    }
    details = {
        str(detail.get("name")): detail
        for detail in health.get("details", [])
        if isinstance(detail, Mapping) and detail.get("name")
    }
    online = {name for name, status in statuses.items() if status == "OK"}
    reconnectable = {
        name
        for name, status in statuses.items()
        if status != "OK"
        and int(details.get(name, {}).get("tool_count") or 0) > 0
    }
    return sorted(online | reconnectable), sorted(reconnectable), len(online)
