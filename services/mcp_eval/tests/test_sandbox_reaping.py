import unittest
from unittest.mock import AsyncMock, patch

from mcp_completion import task_sandbox
from mcp_completion.task_sandbox import (
    _parse_docker_timestamp,
    reap_orphan_task_sandboxes,
)


class DockerTimestampTests(unittest.TestCase):
    def test_nanosecond_precision_is_accepted(self):
        # Docker reports 9 fractional digits, which datetime rejects outright.
        self.assertIsNotNone(
            _parse_docker_timestamp("2026-08-03T11:59:54.40087854Z")
        )

    def test_offset_form_is_accepted(self):
        self.assertIsNotNone(
            _parse_docker_timestamp("2026-08-03T11:59:54+08:00")
        )

    def test_unparseable_timestamp_yields_none(self):
        self.assertIsNone(_parse_docker_timestamp("not-a-timestamp"))


class OrphanSweepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = set(task_sandbox._LIVE_SANDBOX_NAMES)
        task_sandbox._LIVE_SANDBOX_NAMES.clear()

    def tearDown(self):
        task_sandbox._LIVE_SANDBOX_NAMES.clear()
        task_sandbox._LIVE_SANDBOX_NAMES.update(self._saved)

    @staticmethod
    def _runner(container_lines, container_details):
        """Fake _run that answers ps/inspect and records rm calls."""
        removed = []

        async def fake_run(*args, **kwargs):
            if args[:2] == ("docker", "ps"):
                return "\n".join(container_lines), "", 0
            if args[:2] == ("docker", "inspect"):
                return "\n".join(container_details), "", 0
            if args[:3] == ("docker", "volume", "ls"):
                return "", "", 0
            if args[:3] == ("docker", "rm", "-f"):
                removed.append(args[-1])
                return "", "", 0
            return "", "", 0

        return fake_run, removed

    async def test_stale_untracked_container_is_removed(self):
        fake_run, removed = self._runner(
            ["mcp-atlas-local-syn-old-aaaaaaaaaa"],
            ["/mcp-atlas-local-syn-old-aaaaaaaaaa\t2020-01-01T00:00:00.000000000Z"],
        )
        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            result = await reap_orphan_task_sandboxes(min_age_seconds=1800)

        self.assertEqual(removed, ["mcp-atlas-local-syn-old-aaaaaaaaaa"])
        self.assertEqual(result["containers"], 1)

    async def test_tracked_container_is_never_removed(self):
        name = "mcp-atlas-local-syn-live-bbbbbbbbbb"
        task_sandbox._LIVE_SANDBOX_NAMES.add(name)
        fake_run, removed = self._runner(
            [name], [f"/{name}\t2020-01-01T00:00:00.000000000Z"]
        )
        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            result = await reap_orphan_task_sandboxes(min_age_seconds=1800)

        self.assertEqual(removed, [])
        self.assertEqual(result["containers"], 0)

    async def test_young_container_survives_the_startup_window(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        fake_run, removed = self._runner(
            ["mcp-atlas-local-syn-new-cccccccccc"],
            [f"/mcp-atlas-local-syn-new-cccccccccc\t{now}"],
        )
        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            result = await reap_orphan_task_sandboxes(min_age_seconds=1800)

        self.assertEqual(removed, [])
        self.assertEqual(result["containers"], 0)

    async def test_unreadable_creation_time_is_left_alone(self):
        fake_run, removed = self._runner(
            ["mcp-atlas-local-syn-weird-dddddddddd"],
            ["/mcp-atlas-local-syn-weird-dddddddddd\t???"],
        )
        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            result = await reap_orphan_task_sandboxes(min_age_seconds=1800)

        self.assertEqual(removed, [])
        self.assertEqual(result["containers"], 0)


class SandboxNameLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = set(task_sandbox._LIVE_SANDBOX_NAMES)
        task_sandbox._LIVE_SANDBOX_NAMES.clear()

    def tearDown(self):
        task_sandbox._LIVE_SANDBOX_NAMES.clear()
        task_sandbox._LIVE_SANDBOX_NAMES.update(self._saved)

    def _sandbox(self):
        return task_sandbox.TaskSandbox(
            task_id="syn-lifecycle",
            local_servers=set(),
            network_servers=set(),
            agent_image="img",
            mongo_image="",
            startup_timeout=1.0,
            memory_limit="1g",
            cpu_limit="1.0",
            task_data_source="/tmp",
        )

    def test_claim_registers_name_globally(self):
        sandbox = self._sandbox()
        sandbox._claim_name("mcp-atlas-local-syn-lifecycle-eeeeeeeeee")
        self.assertIn(
            "mcp-atlas-local-syn-lifecycle-eeeeeeeeee",
            task_sandbox._LIVE_SANDBOX_NAMES,
        )

    async def test_close_releases_names_even_when_teardown_fails(self):
        sandbox = self._sandbox()
        name = sandbox._claim_name("mcp-atlas-local-syn-lifecycle-ffffffffff")

        with patch.object(
            sandbox, "_close_resources", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            with self.assertRaises(RuntimeError):
                await sandbox.close()

        # Released regardless, so the sweeper can reclaim what teardown missed.
        self.assertNotIn(name, task_sandbox._LIVE_SANDBOX_NAMES)


if __name__ == "__main__":
    unittest.main()
