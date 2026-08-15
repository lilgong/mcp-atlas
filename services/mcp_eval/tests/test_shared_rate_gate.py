import asyncio
import tempfile
import time
import unittest
from unittest.mock import patch

from mcp_completion.shared_rate_gate import SharedRateGate


class SharedRateGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_gate_instances_share_one_lock_and_schedule(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"MCP_SHARED_RATE_LIMIT_DIR": tmp}, clear=False,
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
            "os.environ", {"MCP_SHARED_RATE_LIMIT_DIR": tmp}, clear=False,
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
            "os.environ", {"MCP_SHARED_RATE_LIMIT_DIR": tmp}, clear=False,
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
