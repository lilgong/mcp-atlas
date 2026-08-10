"""Sandbox MCP client implementation."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from .base_client import MCPClient
from ..docker_http import docker_post_json
from ..errors import MCPClientToolExecutionError
from ..schema import ToolDefinition, CallToolResponse, TextContent
from ..config import config

logger = logging.getLogger(__name__)


class SandboxMCPClient(MCPClient):
    """MCP client that connects to pre-running sandbox environments."""

    def __init__(
        self,
        sandbox_url: str,
        enabled_tools: Optional[List[str]] = None,  # if None, all tools are enabled
        container_name: Optional[str] = None,
    ):
        self.sandbox_url = sandbox_url
        self.enabled_tools = enabled_tools
        self.container_name = container_name
        self._enabled_tool_set = (
            set(enabled_tools) if enabled_tools is not None else None
        )
        self._canonical_to_backend: Dict[str, str] = {}
        self.tool_call_timeout = config.TOOL_CALL_TIMEOUT
        self.list_tools_timeout = config.LIST_TOOLS_TIMEOUT

    async def list_tools(self) -> List[ToolDefinition]:
        """List available tools from the sandbox."""
        try:
            if self.container_name:
                status_code, response_text = await docker_post_json(
                    self.container_name,
                    "/list-tools",
                    {},
                    timeout=self.list_tools_timeout,
                )
                if status_code >= 400:
                    raise RuntimeError(
                        f"HTTP {status_code}: {response_text[:500]}"
                    )
                tools_data = json.loads(response_text)
            else:
                async with httpx.AsyncClient(
                    timeout=self.list_tools_timeout
                ) as client:
                    response = await client.post(
                        f"{self.sandbox_url}/list-tools",
                        headers={"Content-Type": "application/json"},
                    )
                    response.raise_for_status()
                    tools_data = response.json()

            backend_tools = [ToolDefinition(**tool) for tool in tools_data]
            tools: List[ToolDefinition] = []

            # A FastMCP client with exactly one configured server may expose
            # raw names (``read_text_file``) instead of the multi-server
            # canonical name (``filesystem_read_text_file``). Resolve each
            # raw name back to the requested canonical name by unique suffix.
            for tool in backend_tools:
                canonical_name = tool.name
                if self._enabled_tool_set is not None:
                    if canonical_name not in self._enabled_tool_set:
                        candidates = [
                            name
                            for name in self._enabled_tool_set
                            if name.endswith(f"_{tool.name}")
                        ]
                        if len(candidates) != 1:
                            continue
                        canonical_name = candidates[0]
                self._canonical_to_backend[canonical_name] = tool.name
                if canonical_name != tool.name:
                    tool = tool.model_copy(update={"name": canonical_name})
                tools.append(tool)

            return tools

        except Exception as error:
            logger.error(f"Failed to list tools from sandbox: {error}")
            raise

    async def call_tool(self, tool_name: str, args: Any) -> CallToolResponse:
        """Call a tool in the sandbox."""
        if (
            self._enabled_tool_set is not None
            and tool_name not in self._enabled_tool_set
        ):
            raise MCPClientToolExecutionError(
                f"Tool is not enabled for this client: {tool_name}"
            )
        if (
            self._enabled_tool_set is not None
            and tool_name not in self._canonical_to_backend
        ):
            await self.list_tools()
        try:
            backend_tool_name = self._canonical_to_backend.get(
                tool_name, tool_name
            )
            body = {
                "tool_name": backend_tool_name,
                "tool_args": args,
            }

            if self.container_name:
                status_code, response_text = await docker_post_json(
                    self.container_name,
                    "/call-tool",
                    body,
                    timeout=self.tool_call_timeout,
                )
                if status_code != 200:
                    return CallToolResponse(
                        content=[TextContent(type="text", text=response_text)],
                        is_error=True,
                    )
                response_data = json.loads(response_text)
            else:
                async with httpx.AsyncClient(
                    timeout=self.tool_call_timeout
                ) as client:
                    response = await client.post(
                        f"{self.sandbox_url}/call-tool",
                        json=body,
                        headers={"Content-Type": "application/json"},
                    )

                    if response.status_code != 200:
                        error_text = response.text
                        return CallToolResponse(
                            content=[TextContent(type="text", text=error_text)],
                            is_error=True,
                        )
                    response_data = response.json()
            return CallToolResponse(
                content=response_data,
                is_error=False,
            )

        except httpx.ReadTimeout:
            logger.error(f"Tool {tool_name} timed out after {self.tool_call_timeout}s")
            raise MCPClientToolExecutionError(
                f"Tool {tool_name} timed out after {self.tool_call_timeout}s"
            )
        except Exception as error:
            logger.error(f"Failed to call tool {tool_name} in sandbox: {error}")
            raise MCPClientToolExecutionError(
                f"Failed to call tool {tool_name}: {error}"
            )

    @property
    def sandbox_info(self) -> Dict[str, Any]:
        """Get sandbox information."""
        return {
            "sandbox_url": self.sandbox_url,
            "container_name": self.container_name,
        }
