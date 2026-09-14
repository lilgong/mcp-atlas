import asyncio
import tempfile
import time
import unittest
from unittest.mock import patch

from mcp_completion.shared_rate_gate import SharedRateGate


class FakeRedis:
    values: dict[str, str] = {}
    hashes: dict[str, dict[str, str]] = {}

    async def set(self, key, value, *, nx=False, px=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def time(self):
        now = time.time()
        seconds = int(now)
        return seconds, int((now - seconds) * 1_000_000)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def eval(self, _script, numkeys, *args):
        keys = args[:numkeys]
        values = args[numkeys:]
        lock_key = keys[0]
        if self.values.get(lock_key) != values[0]:
            return 0
        if numkeys == 2:
            self.hashes[keys[1]] = {
                "last_started": str(values[1]),
                "cooldown_until": str(values[2]),
                "consecutive_rate_limits": str(values[3]),
            }
            self.values.pop(lock_key, None)
        elif len(values) == 1:
            self.values.pop(lock_key, None)
        return 1

    async def aclose(self):
        return None


class SharedRateGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_gate_instances_share_one_lock_and_schedule(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"MCP_SHARED_RATE_LIMIT_DIR": tmp, "REDIS_URL": ""},
            clear=False,
        ):
            first = SharedRateGate("arxiv-test", 0.03)
            second = SharedRateGate("arxiv-test", 0.03)
            starts = []

            async def call(gate):
                async with gate.slot() as lease:
                    starts.append(time.monotonic())
                    await asyncio.sleep(0.01)
                    lease.observe_rate_limit(False)

            await asyncio.gather(call(first), call(second))

        self.assertEqual(2, len(starts))
        self.assertGreaterEqual(starts[1] - starts[0], 0.025)

    async def test_rate_limit_cooldown_is_visible_to_another_instance(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"MCP_SHARED_RATE_LIMIT_DIR": tmp, "REDIS_URL": ""},
            clear=False,
        ):
            first = SharedRateGate("arxiv-test", 0, 0.03, 0.03)
            second = SharedRateGate("arxiv-test", 0, 0.03, 0.03)
            async with first.slot() as lease:
                self.assertEqual(0.03, lease.observe_rate_limit(True))
            started = time.monotonic()
            async with second.slot() as lease:
                waited = time.monotonic() - started
                lease.observe_rate_limit(False)

        self.assertGreaterEqual(waited, 0.025)

    async def test_completion_spacing_is_visible_to_another_instance(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"MCP_SHARED_RATE_LIMIT_DIR": tmp, "REDIS_URL": ""},
            clear=False,
        ):
            first = SharedRateGate(
                "wikipedia-test", 0, completion_spacing=0.03,
            )
            second = SharedRateGate(
                "wikipedia-test", 0, completion_spacing=0.03,
            )
            async with first.slot() as lease:
                lease.observe_rate_limit(False)
            started = time.monotonic()
            async with second.slot() as lease:
                waited = time.monotonic() - started
                lease.observe_rate_limit(False)

        self.assertGreaterEqual(waited, 0.025)

    async def test_redis_instances_share_lock_and_schedule(self):
        FakeRedis.values = {}
        FakeRedis.hashes = {}
        with (
            patch.dict(
                "os.environ", {"REDIS_URL": "redis://test"}, clear=False,
            ),
            patch(
                "mcp_completion.shared_rate_gate._redis_client",
                side_effect=lambda _url: FakeRedis(),
            ),
        ):
            first = SharedRateGate("arxiv-test", 0.03)
            second = SharedRateGate("arxiv-test", 0.03)
            starts = []

            async def call(gate):
                async with gate.slot() as lease:
                    starts.append(time.monotonic())
                    await asyncio.sleep(0.01)
                    lease.observe_rate_limit(False)

            await asyncio.gather(call(first), call(second))

        self.assertEqual(2, len(starts))
        self.assertGreaterEqual(starts[1] - starts[0], 0.025)

    async def test_redis_failure_is_fail_closed(self):
        class BrokenRedis(FakeRedis):
            async def set(self, *args, **kwargs):
                raise ConnectionError("unavailable")

        with (
            patch.dict(
                "os.environ", {"REDIS_URL": "redis://test"}, clear=False,
            ),
            patch(
                "mcp_completion.shared_rate_gate._redis_client",
                side_effect=lambda _url: BrokenRedis(),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Redis rate limiter unavailable"
            ):
                async with SharedRateGate("arxiv-test", 0).slot():
                    pass
