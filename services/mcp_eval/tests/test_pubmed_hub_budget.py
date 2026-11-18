import asyncio
import time
from unittest.mock import AsyncMock

from mcp_completion.hub_task_transport import HubTaskClient


def test_pubmed_hub_tool_request_has_room_for_internal_retries():
    async def check():
        client = object.__new__(HubTaskClient)
        client.failed = False
        client.failed_servers = set()
        client.target = 'official'
        client.connections = {'pubmed': {'path': '/tasks/p', 'token': 'opaque'}, 'arxiv': {'path': '/tasks/a', 'token': 'opaque'}}
        client.lease_deadlines = {'pubmed': time.monotonic() + 300, 'arxiv': time.monotonic() + 300}
        client.root = 'http://hub'
        response = AsyncMock()
        response.status_code = 200
        response.json = lambda: {'content': [], 'isError': False}
        client.http = AsyncMock()
        client.http.post.return_value = response
        await client._request('pubmed', 'call-tool', {})
        assert client.http.post.await_args.kwargs['timeout'] >= 500
        await client._request('pubmed', 'list-tools', {})
        assert 'timeout' not in client.http.post.await_args.kwargs
        await client._request('arxiv', 'call-tool', {})
        assert 'timeout' not in client.http.post.await_args.kwargs
    asyncio.run(check())
