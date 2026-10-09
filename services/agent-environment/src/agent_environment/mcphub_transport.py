"""Opt selected shared servers into MCPHub; task isolation stays Atlas-owned."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote, urlsplit

TASK_SERVERS = frozenset(
    {
        "filesystem",
        "git",
        "memory",
        "mongodb",
        "cli-mcp-server",
        "desktop-commander",
        "mcp-code-executor",
        "mcp-server-code-runner",
        "arxiv",
        "pubmed",
    }
)


def hub_server_configs(
    environment: Mapping[str, str], known_servers: set[str]
) -> dict[str, dict[str, Any]]:
    mode = (environment.get("MCP_TOOL_TRANSPORT") or "direct").strip()
    if mode == "direct":
        return {}
    if mode != "hub":
        raise ValueError("MCP_TOOL_TRANSPORT must be direct or hub")
    base = (environment.get("MCPHUB_URL") or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "MCPHUB_URL must be an HTTP(S) Hub root without credentials/query/fragment"
        )
    token = (environment.get("MCPHUB_TOKEN") or "").strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("MCPHUB_TOKEN must contain an existing Hub Bearer Key")
    names = {
        n.strip()
        for n in (environment.get("MCPHUB_SERVERS") or "").split(",")
        if n.strip()
    }
    if not names or names - known_servers or names & TASK_SERVERS:
        raise ValueError(
            "MCPHUB_SERVERS must select known shared servers, excluding task-isolated servers"
        )
    separator = environment.get("MCPHUB_NAME_SEPARATOR") or "-"
    headers = {"Authorization": f"Bearer {token}"}
    target = (environment.get("MCPHUB_TARGET") or "").strip()
    if target:
        headers["X-MCPHub-target"] = target[:160]
    return {
        name: {
            "url": f"{base}/mcp/{quote(name, safe='')}",
            "transport": "streamable-http",
            "headers": dict(headers),
            "atlas_hub_prefix": f"{name}{separator}",
        }
        for name in names
    }
