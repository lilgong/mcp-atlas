"""The pinned MCP must propagate failures to FastMCP, not return success text."""
import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from agent_environment import oxylabs_mcp_compat as compat


@pytest.fixture
def patched_provider(monkeypatch):
    package = ModuleType('oxylabs_mcp')
    exceptions = ModuleType('oxylabs_mcp.exceptions')

    class ProviderError(Exception):
        async def process(self, ctx):
            return str(self)

    class Wrapper:
        async def scrape(self, payload):
            return payload

    exceptions.MCPServerError = ProviderError
    package.utils = SimpleNamespace(_OxylabsClientWrapper=Wrapper)
    monkeypatch.setitem(sys.modules, 'oxylabs_mcp', package)
    monkeypatch.setitem(sys.modules, 'oxylabs_mcp.exceptions', exceptions)
    monkeypatch.setattr(compat, 'version', lambda _: '0.4.1')
    compat.install_compatibility_patch()
    compat.install_compatibility_patch()
    return ProviderError, Wrapper


@pytest.mark.parametrize('message', [
    'HTTP error during POST request: 429 - Your current plan\'s quota has been reached.',
    'HTTP error during POST request: 429 - Rate limit exceeded',
    'HTTP error during POST request: 504 - Gateway timeout',
    'Request error during POST request: timed out',
])
def test_provider_errors_raise_tool_error_preserving_details(patched_provider, message):
    error, _ = patched_provider
    ctx = SimpleNamespace(error=AsyncMock())
    with pytest.raises(ToolError) as caught:
        asyncio.run(error(message).process(ctx))
    assert str(caught.value) == message
    ctx.error.assert_awaited_once_with(message)


def test_successful_scraping_stays_successful(patched_provider):
    _, wrapper = patched_provider
    assert asyncio.run(wrapper().scrape({'url': 'https://example.com'})) == {
        'url': 'https://example.com', 'source': 'universal',
    }
