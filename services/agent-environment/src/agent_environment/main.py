import contextlib
import mcp
from typing import Any, AsyncGenerator, Dict, cast

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import mcp.types
from .mcp_client import config, create_server_client
from .mcp_router import DirectMCPRouter, RouterTimeoutError
from .logger import create_logger
from cacheout import Cache
import json
import hashlib
import random

CACHE_TTL_HOURS = 48

logger = create_logger(__name__)
router = DirectMCPRouter(config, create_server_client)

# Create cache with appropriate settings for the use case
tool_cache = Cache(
    maxsize=10000,  # Max 10000 unique requests (fits about 2000 tasks)
    ttl=CACHE_TTL_HOURS
    * 60
    * 60,  # 48 hours TTL by default (but each item will have some slight variation)
    enable_stats=True,  # Track cache performance
)

# Tool name mappings - maps old invalid names to correct names
TOOL_NAME_MAPPINGS = {
    "brave_brave_web_search": "brave-search_brave_web_search",
    "MongoDB_aggregate": "mongodb_aggregate",
    "MongoDB_collection-schema": "mongodb_collection-schema",
    "MongoDB_count": "mongodb_count",
    "MongoDB_find": "mongodb_find",
    "MongoDB_list-collections": "mongodb_list-collections",
    "MongoDB_list-databases": "mongodb_list-databases",
}

# Cache whitelist - only these servers will have their responses cached
CACHEABLE_SERVERS = {
    "airtable",
    "alchemy",
    # "arxiv",
    "brave-search",
    "calculator",
    # "cli-mcp-server",
    "clinicaltrialsgov-mcp-server",
    "context7",
    "ddg-search",
    "desktop-commander",
    "e2b-server",
    "exa",
    "fetch",
    # "filesystem",
    # "git",
    "github",
    "google-maps",
    "google-workspace",
    "lara-translate",
    "mcp-code-executor",
    "mcp-server-code-runner",
    "memory",
    "met-museum",
    "mongodb",
    "national-parks",
    "notion",
    "open-library",
    "osm-mcp-server",
    "oxylabs",
    "pubmed",
    "slack",
    "twelvedata",
    "weather",
    "weather-data",
    "whois",
    "wikipedia",
}


class CallToolRequest(BaseModel):
    tool_name: str
    tool_args: Dict[str, Any]
    use_cache: bool = True


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    mcp_servers = config.get("mcpServers", {})
    logger.info(
        f"Starting agent environment with {len(mcp_servers)} MCP servers: {mcp_servers.keys()}"
    )
    await router.start()
    tools = router.list_tools()
    logger.info(f"{len(tools)} tools loaded in total")
    tool_names = [tool.name for tool in tools]
    if "desktop-commander_set_config_value" in tool_names:
        result = await router.call_tool(
            "desktop-commander_set_config_value",
            {"key": "allowedDirectories", "value": ["/data"]},
        )
        if result.isError:
            logger.warning("Failed to configure desktop-commander: %s", result.content)
    try:
        yield
    finally:
        await router.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    """Health check endpoint."""
    return {"message": "MCP Agent Environment API"}


@app.post("/list-tools")
async def list_tools() -> list[mcp.types.Tool]:
    """List all available tools from the MCP server."""
    return router.list_tools()


def should_cache_tool(tool_name: str) -> bool:
    """Check if tool should be cached based on server whitelist."""
    server_name = tool_name.split("_", 1)[0]
    return server_name in CACHEABLE_SERVERS


def generate_cache_key(tool_name: str, tool_args: dict) -> str:
    """Generate consistent cache key from tool call parameters."""
    cache_data = {"tool_name": tool_name, "tool_args": tool_args}
    cache_str = json.dumps(cache_data, sort_keys=True)
    return hashlib.md5(cache_str.encode()).hexdigest()


@app.post("/call-tool")
async def call_tool(
    request: CallToolRequest,
) -> list[mcp.types.ContentBlock]:
    """Call a specific tool with the provided arguments."""

    mapped_tool_name = TOOL_NAME_MAPPINGS.get(request.tool_name, request.tool_name)

    # Generate cache key
    cache_key = generate_cache_key(mapped_tool_name, request.tool_args)

    # Check cache first
    cached_result = cast(
        list[mcp.types.ContentBlock] | None,
        tool_cache.get(cache_key),
    )
    if (
        cached_result is not None
        and request.use_cache
        and should_cache_tool(mapped_tool_name)
    ):
        logger.info(f"Returning cached result for tool '{request.tool_name}'")
        return cached_result

    try:
        result = await router.call_tool(mapped_tool_name, request.tool_args)

        if result.isError:
            error_msg = "Unknown error"
            if result.content and isinstance(
                result.content[0], mcp.types.TextContent
            ):
                error_msg = result.content[0].text
            raise HTTPException(
                status_code=500,
                detail=f"Tool '{request.tool_name}' execution failed: {error_msg}",
            )

        content_blocks = result.content
        if should_cache_tool(mapped_tool_name) and cache_key is not None:
            random_ttl = int(CACHE_TTL_HOURS * 60 * 60 * random.uniform(0.7, 1.0))
            tool_cache.set(cache_key, content_blocks, ttl=random_ttl)

        return content_blocks

    except HTTPException:
        raise
    except RouterTimeoutError as e:
        raise HTTPException(
            status_code=504,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to call tool '{request.tool_name}': {str(e)}",
        )


@app.get("/cache-stats")
async def get_cache_stats() -> dict[str, Any]:
    """Get cache statistics for monitoring."""
    return {
        "cache_size": len(tool_cache),
        "max_size": tool_cache.maxsize,
        "ttl_seconds": tool_cache.ttl,
    }


@app.post("/cache-clear")
async def clear_cache() -> dict[str, Any]:
    """Clear the entire cache."""
    tool_cache.clear()
    return {"message": "Cache cleared successfully", "cache_size": len(tool_cache)}


@app.get("/enabled-servers")
async def get_enabled_servers() -> dict[str, Any]:
    """Get list of configured MCP servers with their status (OK or ERROR_NOT_ONLINE)."""
    servers = router.server_statuses()
    details = router.server_details()
    online = sum(1 for _, status in servers if status == "OK")
    return {
        "servers": servers,
        "total": len(servers),
        "online": online,
        "offline": len(servers) - online,
        "details": details,
        "errors": {
            detail["name"]: detail["last_error"]
            for detail in details
            if detail["last_error"] is not None
        },
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    """Report router readiness and last-observed backend health."""
    servers = router.server_statuses()
    details = router.server_details()
    online = sum(1 for _, status in servers if status == "OK")
    if not router.started:
        status = "health_and_client_connection_not_started"
    elif online < len(servers):
        status = "health_and_client_connection_degraded"
    else:
        status = "health_and_client_connection_ok"
    return {
        "status": status,
        "ready": router.started,
        "online": online,
        "total": len(servers),
        "offline": len(servers) - online,
        "in_flight": sum(detail["in_flight"] for detail in details),
        "errors": {
            detail["name"]: detail["last_error"]
            for detail in details
            if detail["last_error"] is not None
        },
    }
