"""Configuration for MCP eval."""

import os
from typing import Optional
from urllib.parse import urlsplit
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()


class Config:
    """Configuration class for MCP eval."""

    # Server configuration
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "3000"))

    # LLM configuration
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")

    # MCP Server configuration
    MCP_SERVER_URL: str = os.getenv("MCP_SERVER_URL", "http://localhost:1984")

    # Timeout configuration
    DEFAULT_TIMEOUT: float = float(os.getenv("DEFAULT_TIMEOUT", "300.0"))
    TOOL_CALL_TIMEOUT: float = float(os.getenv("TOOL_CALL_TIMEOUT", "180.0"))
    LIST_TOOLS_TIMEOUT: float = float(os.getenv("LIST_TOOLS_TIMEOUT", "180.0"))

    # Logging configuration
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    def validate_required_config(self) -> None:
        """Validate that all required configuration values are set."""
        required_configs = [("LLM_API_KEY", self.LLM_API_KEY)]

        missing_configs = []
        for name, value in required_configs:
            if not value or not value.strip():
                missing_configs.append(name)

        if missing_configs:
            raise ValueError(
                f"Missing required configuration: {', '.join(missing_configs)}. "
                f"Please set these environment variables or add them to your .env file."
            )

        isolation_enabled = (
            os.getenv("MCP_TASK_ISOLATION_ENABLED", "true").lower()
            not in {"0", "false", "no"}
        )
        if isolation_enabled:
            validate_isolated_control_plane(self.HOST, self.MCP_SERVER_URL)


def _is_loopback_host(host: Optional[str]) -> bool:
    return (host or "").strip().lower() in {"127.0.0.1", "::1", "localhost"}


def validate_isolated_control_plane(host: str, shared_mcp_url: str) -> None:
    """Keep host control APIs unreachable from networked task containers."""
    if not _is_loopback_host(host):
        raise ValueError(
            "HOST must be a loopback address when MCP task isolation is enabled; "
            "use 127.0.0.1 and an SSH tunnel for remote access"
        )
    shared_host = urlsplit(shared_mcp_url).hostname
    if not _is_loopback_host(shared_host):
        raise ValueError(
            "MCP_SERVER_URL must use a loopback host when MCP task isolation is "
            "enabled; networked task containers must not reach the shared runtime"
        )


config = Config()
