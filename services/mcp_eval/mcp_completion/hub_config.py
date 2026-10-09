"""Optional Hub scheduling and relay delegation; direct mode never contacts Hub."""

from __future__ import annotations

import os
import httpx


def hub_manages_policy(server: str) -> bool:
    return (
        os.getenv("MCP_TOOL_TRANSPORT", "direct").strip().lower() == "hub"
        and os.getenv("MCPHUB_MANAGE_RATE_LIMITS", "true").lower()
        in {"true", "1", "yes"}
        and (
            server
            in {name.strip() for name in os.getenv("MCPHUB_SERVERS", "").split(",")}
            or (
                os.getenv("MCPHUB_TASK_ROUTING", "false").lower()
                in {"true", "1", "yes"}
                and server
                in {
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
        )
    )


async def managed_task_relay(servers: list[str]) -> dict[str, str] | None:
    """Return None for legacy mode, otherwise a bounded relay-only snapshot."""
    if os.getenv("MCP_TOOL_TRANSPORT", "direct").strip().lower() != "hub" or os.getenv(
        "MCPHUB_MANAGE_RELAY", "false"
    ).lower() not in {"true", "1", "yes"}:
        return None
    requested = set(servers) & {"arxiv", "pubmed"}
    if not requested:
        return None
    root = (os.getenv("MCPHUB_CONFIG_URL") or os.getenv("MCPHUB_URL") or "").rstrip("/")
    token = os.getenv("MCPHUB_TOKEN", "")
    if not root or not token or any(char.isspace() for char in token):
        raise RuntimeError("Managed relay requires a Hub URL and operator Bearer Key")
    from urllib.parse import urlsplit

    parsed = urlsplit(root)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Invalid Hub configuration URL")
    snapshot = None
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for server in sorted(requested):
            response = await client.get(
                f"{root}/api/servers/{server}/runtime",
                params={"target": os.getenv("MCPHUB_TARGET", "")},
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    "Hub relay configuration is unavailable or access was denied"
                )
            try:
                body = response.json()
                values = body["data"]["env"]
                allowed = {
                    "MCP_RELAY_URL",
                    "MCP_RELAY_TOKEN",
                    "MCP_RELAY_TIMEOUT_SECONDS",
                    "WIKIPEDIA_RELAY_URL",
                    "WIKIPEDIA_RELAY_TOKEN",
                    "WIKIPEDIA_RELAY_TIMEOUT_SECONDS",
                    "PUBMED_RELAY_URL",
                    "PUBMED_RELAY_TOKEN",
                    "PUBMED_RELAY_TIMEOUT_SECONDS",
                }
                if (
                    body.get("success") is not True
                    or not isinstance(values, dict)
                    or set(values) - allowed
                    or any(not isinstance(value, str) for value in values.values())
                ):
                    raise ValueError()
                if snapshot is not None and snapshot != values:
                    raise ValueError()
                snapshot = values
            except (ValueError, KeyError, TypeError):
                raise RuntimeError(
                    "Invalid or inconsistent Hub relay configuration"
                ) from None
    return snapshot


async def managed_task_relays(servers: list[str]) -> dict[str, dict[str, str]] | None:
    """Per-wrapper snapshots; arxiv and pubmed may select different relay modes."""
    snapshots = {}
    for server in sorted(set(servers) & {"arxiv", "pubmed"}):
        snapshot = await managed_task_relay([server])
        if snapshot is None:
            return None
        snapshots[server] = snapshot
    return snapshots or None
