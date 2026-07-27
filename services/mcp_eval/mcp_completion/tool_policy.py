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

from enum import Enum
from typing import Iterable, Optional


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

# These servers may execute arbitrary local code or mutate files/database state.
# They are never run in the credentialed shared cloud container for an agent task.
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
UNSUPPORTED_TOOLS = frozenset(
    {
        # Task-local containers deliberately have no network access.
        "mcp-code-executor_install_dependencies",
    }
)


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
) -> list[str]:
    """Combine shared-cloud health with task-routed runtime availability.

    In isolation mode, a task-local/network server must never inherit its
    availability from the fixture-free shared container.  Those routes are
    available when task data is configured; Mongo additionally requires its
    fixture image.  With isolation disabled, preserve the legacy shared-only
    behavior.
    """

    shared = set(shared_enabled_servers)
    if not isolation_enabled:
        return sorted(shared)

    task_routed = set(TASK_LOCAL_SERVERS) | set(TASK_NETWORK_SERVERS)
    effective = shared - task_routed
    if task_data_configured:
        effective.update(TASK_NETWORK_SERVERS)
        effective.update(set(TASK_LOCAL_SERVERS) - {"mongodb"})
        if task_mongo_configured:
            effective.add("mongodb")
    return sorted(effective)
