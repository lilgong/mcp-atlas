"""Fake clients only; never call MCP or market APIs from tests."""
import asyncio
from copy import deepcopy
import json
import mcp.types
import pytest

from agent_environment.credential_pool import TwelveDataPool


def make(responses=None):
    keys = [f"fake-key-{i}" for i in range(4)]
    calls, configs = [], []
    config = {"command": "uv", "args": ["run", "compat.py", "-k", "original-key"], "env": {}}
    original = deepcopy(config)
    class Fake:
        def __init__(self, slot):
            self.slot = slot
        async def __aenter__(self): return self
        async def close(self): pass
        async def list_tools(self): return ["public-schema"]
        async def call_tool_mcp(self, name, arguments, timeout=None):
            calls.append((self.slot, name, deepcopy(arguments)))
            text = (responses or {}).get(self.slot, "price=123")
            if isinstance(text, Exception): raise text
            return mcp.types.CallToolResult(isError=text != "price=123",
                content=[mcp.types.TextContent(type="text", text=text)])
    def factory(name, bound):
        assert name == "twelvedata"
        slot = keys.index(bound["env"]["TWELVE_DATA_API_KEY"])
        assert bound["args"][-1] == keys[slot]
        configs.append(bound)
        return Fake(slot)
    pool = TwelveDataPool(config, keys, factory)
    assert config == original
    return pool, calls, configs


def test_four_keys_rotate_under_concurrency_and_bind_real_transport_arguments():
    async def run():
        pool, calls, configs = make()
        assert await pool.list_tools() == ["public-schema"] and calls == []
        await asyncio.gather(*(pool.call_tool_mcp("GetPrice", {"symbol": "AAPL"}) for _ in range(40)))
        assert [c[0] for c in calls] == list(range(4)) * 10
        assert all(c[1:] == ("GetPrice", {"symbol": "AAPL"}) for c in calls)
        assert len(configs) == 4
        await pool.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [
    "TWELVEDATA_DAILY_CREDITS_EXHAUSTED: TwelveData daily API credit limit reached",
    RuntimeError("TWELVEDATA_DAILY_CREDITS_EXHAUSTED"),
    "TwelveData upstream HTTP 401: Invalid API key",
])
def test_confirmed_exhaustion_or_auth_retries_same_request_without_permanent_ban(failure):
    async def run():
        responses = {0: failure}
        pool, calls, _ = make(responses)
        result = await pool.call_tool_mcp("GetTimeSeries", {"symbol": "MSFT", "interval": "1day"})
        assert not result.isError
        assert [c[0] for c in calls] == [0, 1] and calls[0][1:] == calls[1][1:]
        for _ in range(8): await pool.call_tool_mcp("GetPrice", {})
        assert [c[0] for c in calls].count(0) > 1
        # A quota reset/key recovery needs no restart or manual state reset.
        responses[0] = "price=123"
        pool.cursor = 0
        result = await pool.call_tool_mcp("GetPrice", {})
        assert not result.isError and calls[-1][0] == 0
        await pool.close()
    asyncio.run(run())


@pytest.mark.parametrize("failure", [
    "TwelveData upstream HTTP 429: You have run out of API credits for the current minute",
    "TwelveData upstream HTTP 403: This endpoint is not available on your subscription plan",
])
def test_minute_limits_or_endpoint_entitlements_do_not_disable_credentials(failure):
    async def run():
        pool, calls, _ = make({0: failure})
        result = await pool.call_tool_mcp("GetPrice", {})
        assert result.isError and len(calls) == 1
        await pool.close()
    asyncio.run(run())


def test_all_four_exhausted_emit_stable_account_stop_marker_and_no_extra_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_USAGE_LOG_DIR", str(tmp_path))
    async def run():
        pool, calls, _ = make({i: "TWELVEDATA_DAILY_CREDITS_EXHAUSTED" for i in range(4)})
        result = await pool.call_tool_mcp("GetPrice", {})
        assert result.isError and "TWELVEDATA_DAILY_CREDITS_EXHAUSTED" in result.content[0].text
        assert len(calls) == 4
        await pool.call_tool_mcp("GetPrice", {})
        assert len(calls) == 8
        await pool.close()
    asyncio.run(run())
    path = tmp_path / "twelvedata-pool.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["key_slot"] for r in rows] == [1, 2, 3, 4] * 2
    assert all(r["service"] == "twelvedata" and r["status"] == 429 for r in rows)
    assert "fake-key" not in path.read_text() and path.stat().st_mode & 0o777 == 0o644


def test_missing_key_argument_fails_closed():
    with pytest.raises(ValueError, match="transport binding"):
        TwelveDataPool({"args": []}, ["fake"], lambda *_: None)


def test_concurrent_all_key_failures_are_bounded_per_request_then_recover():
    async def run():
        responses = {i: "TWELVEDATA_DAILY_CREDITS_EXHAUSTED" for i in range(4)}
        pool, calls, _ = make(responses)
        results = await asyncio.gather(*(pool.call_tool_mcp("GetPrice", {"request": i})
                                        for i in range(8)))
        assert all(r.isError for r in results) and len(calls) == 32
        for i in range(8):
            assert sorted(slot for slot, _, args in calls if args["request"] == i) == list(range(4))
        responses.clear()
        result = await pool.call_tool_mcp("GetPrice", {"request": "after-reset"})
        assert not result.isError and len(calls) == 33
        await pool.close()
    asyncio.run(run())
