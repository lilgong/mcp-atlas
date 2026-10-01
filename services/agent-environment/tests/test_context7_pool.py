import asyncio
import mcp.types
from agent_environment.credential_pool import Context7Pool, record_attempt


def test_usage_log_is_host_readable_and_contains_no_credentials(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('MCP_USAGE_LOG_DIR', str(tmp_path))
    record_attempt(0, 'resolve-library-id', 200)
    path = tmp_path / 'context7-pool.jsonl'
    assert path.stat().st_mode & 0o777 == 0o644
    assert json.loads(path.read_text())['service'] == 'context7'


def run_async(function):
    def run():
        asyncio.run(function())
    return run


class FakeClient:
    def __init__(self, slot, calls, exhausted):
        self.slot, self.calls, self.exhausted = slot, calls, exhausted
    async def __aenter__(self):
        return self
    async def list_tools(self):
        return []
    async def close(self):
        pass
    async def call_tool_mcp(self, name, arguments, timeout=None):
        self.calls.append(self.slot)
        return mcp.types.CallToolResult(isError=False, content=[mcp.types.TextContent(type="text",
            text="Monthly quota reached. Upgrade to Context7 Pro." if self.slot in self.exhausted else "docs")])


def make(exhausted=()):
    calls = []
    keys = [f"fake-{i}" for i in range(9)]
    def factory(name, config):
        return FakeClient(keys.index(config['env']['CONTEXT7_API_KEY']), calls, exhausted)
    return Context7Pool({'env': {}}, keys, factory), calls


@run_async
async def test_nine_keys_rotate_and_keep_public_arguments():
    pool, calls = make()
    await asyncio.gather(*(pool.call_tool_mcp('resolve-library-id', {'libraryName':'test'}) for _ in range(18)))
    assert calls == list(range(9))*2
    await pool.close()


@run_async
async def test_quota_retries_next_and_exhausted_key_is_not_reused():
    pool, calls = make({0,1})
    result = await pool.call_tool_mcp('get-library-docs', {})
    assert not result.isError and calls == [0,1,2]
    for _ in range(10):
        await pool.call_tool_mcp('get-library-docs', {})
    assert calls.count(0) == 1 and calls.count(1) == 1


@run_async
async def test_all_exhausted_stops_after_nine_attempts():
    pool, calls = make(set(range(9)))
    result = await pool.call_tool_mcp('get-library-docs', {})
    assert result.isError and len(calls) == 9
    await pool.call_tool_mcp('get-library-docs', {})
    assert len(calls) == 9
