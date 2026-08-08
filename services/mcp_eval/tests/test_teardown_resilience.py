import asyncio
import unittest
from unittest.mock import patch

from mcp_completion import task_sandbox
from mcp_completion.llm import _sanitize_tool_calls
from mcp_completion.task_sandbox import ManagedContainer, TaskSandboxError


def _sandbox(**kwargs):
    return task_sandbox.TaskSandbox(
        task_id="syn-teardown",
        local_servers=set(),
        network_servers=set(),
        agent_image="img",
        mongo_image="",
        startup_timeout=1.0,
        memory_limit="1g",
        cpu_limit="1.0",
        task_data_source="/tmp",
        **kwargs,
    )


class TeardownFailureIsolationTests(unittest.IsolatedAsyncioTestCase):
    """A finished task must not be failed by a slow `docker rm`."""

    def setUp(self):
        self._saved = set(task_sandbox._LIVE_SANDBOX_NAMES)
        task_sandbox._LIVE_SANDBOX_NAMES.clear()

    def tearDown(self):
        task_sandbox._LIVE_SANDBOX_NAMES.clear()
        task_sandbox._LIVE_SANDBOX_NAMES.update(self._saved)

    async def test_docker_rm_timeout_does_not_escape_close(self):
        sandbox = _sandbox()
        sandbox.containers.append(
            ManagedContainer(kind="local", name="c1", task_id="syn-teardown")
        )
        sandbox._claim_name("c1")

        async def fake_run(*args, **kwargs):
            if args[:3] == ("docker", "rm", "-f"):
                raise TaskSandboxError("Command timed out after 90s: docker rm -f")
            return "", "", 0

        events = []
        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event", lambda *a, **k: events.append(a[1])
        ):
            await sandbox.close()  # must not raise

        self.assertIn("task_container_stopped", events)
        # The name is released so the sweeper can reclaim the container later.
        self.assertNotIn("c1", task_sandbox._LIVE_SANDBOX_NAMES)

    async def test_one_failed_removal_does_not_skip_the_rest(self):
        sandbox = _sandbox()
        for name in ("c1", "c2"):
            sandbox.containers.append(
                ManagedContainer(kind="local", name=name, task_id="syn-teardown")
            )
            sandbox._claim_name(name)
        sandbox.mongo_socket_volume = sandbox._claim_name("vol1")

        attempted = []

        async def fake_run(*args, **kwargs):
            if args[:3] == ("docker", "rm", "-f"):
                attempted.append(args[-1])
                if args[-1] == "c2":
                    raise TaskSandboxError("timed out")
                return "", "", 0
            if args[:4] == ("docker", "volume", "rm", "-f"):
                attempted.append(args[-1])
                return "", "", 0
            return "", "", 0

        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            await sandbox.close()

        # c2 blew up, yet c1 and the volume were still cleaned up.
        self.assertEqual(attempted, ["c2", "c1", "vol1"])

    async def test_log_capture_failure_does_not_block_removal(self):
        sandbox = _sandbox()
        sandbox.containers.append(
            ManagedContainer(kind="local", name="c1", task_id="syn-teardown")
        )
        removed = []

        async def fake_run(*args, **kwargs):
            if args[:2] == ("docker", "logs"):
                raise TaskSandboxError("logs timed out")
            if args[:3] == ("docker", "rm", "-f"):
                removed.append(args[-1])
            return "", "", 0

        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            await sandbox.close()

        self.assertEqual(removed, ["c1"])

    async def test_cancellation_still_propagates(self):
        sandbox = _sandbox()
        sandbox.containers.append(
            ManagedContainer(kind="local", name="c1", task_id="syn-teardown")
        )

        async def fake_run(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            with self.assertRaises(asyncio.CancelledError):
                await sandbox.close()

    def test_teardown_timeout_is_configurable_and_longer_than_20s(self):
        self.assertGreater(task_sandbox._teardown_timeout(), 20)
        with patch.dict("os.environ", {"MCP_SANDBOX_TEARDOWN_TIMEOUT": "45"}):
            self.assertEqual(task_sandbox._teardown_timeout(), 45.0)


class MalformedToolCallTests(unittest.TestCase):
    """A null function.name used to fail the whole task."""

    def test_null_name_is_dropped_not_raised(self):
        kept, dropped, _ = _sanitize_tool_calls(
            [
                {"id": "1", "type": "function",
                 "function": {"name": None, "arguments": "{}"}},
                {"id": "2", "type": "function",
                 "function": {"name": "search", "arguments": "{}"}},
            ]
        )
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["function"]["name"], "search")

    def test_all_malformed_yields_none_and_a_count(self):
        kept, dropped, _ = _sanitize_tool_calls(
            [
                {"id": "1", "type": "function",
                 "function": {"name": None, "arguments": "{}"}},
                {"id": "2", "type": "function",
                 "function": {"name": "", "arguments": "{}"}},
            ]
        )
        self.assertIsNone(kept)
        self.assertEqual(dropped, 2)

    def test_well_formed_calls_are_untouched(self):
        calls = [
            {"id": "1", "type": "function",
             "function": {"name": "search", "arguments": "{}"}}
        ]
        kept, dropped, _ = _sanitize_tool_calls(calls)
        self.assertEqual(dropped, 0)
        self.assertEqual(kept, calls)

    def test_missing_function_key_is_treated_as_malformed(self):
        kept, dropped, _ = _sanitize_tool_calls([{"id": "1", "type": "function"}])
        self.assertIsNone(kept)
        self.assertEqual(dropped, 1)


class MalformedTurnLoopTests(unittest.IsolatedAsyncioTestCase):
    """The agent loop must retry a malformed turn, not end the task."""

    @staticmethod
    def _response(dropped):
        from litellm.types.utils import Message as LiteLLMMessage
        from mcp_completion.llm import LLMResponse
        from mcp_completion.schema import AssistantMessage

        return LLMResponse(
            message=AssistantMessage(
                role="assistant",
                content="thinking",
                original_message=LiteLLMMessage(role="assistant", content="x"),
                tool_calls=None,
            ),
            dropped_tool_calls=dropped,
        )

    async def _drive(self, dropped_per_turn, max_turns=6):
        from mcp_completion import agent_eval

        class FakeClient:
            async def list_tools(self):
                return []

        calls = {"n": 0}

        async def fake_completion(**kwargs):
            i = calls["n"]
            calls["n"] += 1
            dropped = (
                dropped_per_turn[i] if i < len(dropped_per_turn) else 0
            )
            return self._response(dropped)

        with patch.object(agent_eval, "create_completion", fake_completion), \
             patch.object(agent_eval, "_transform_tool_calls", lambda t: []), \
             patch.object(agent_eval, "write_runtime_event"):
            outputs = [
                o
                async for o in agent_eval.run_mcp_eval(
                    mcp_client=FakeClient(),
                    model="m",
                    messages=[],
                    max_turns=max_turns,
                    task_id="syn-loop",
                )
            ]
        return calls["n"], outputs

    async def test_malformed_turn_triggers_a_corrective_retry(self):
        # Turn 1 malformed, turn 2 clean -> the loop must reach turn 2.
        turns, outputs = await self._drive([1, 0])
        self.assertEqual(turns, 2)
        corrective = [
            o for o in outputs if o.data.get("role") == "user"
        ]
        self.assertEqual(len(corrective), 1)
        self.assertIn("function name was missing", corrective[0].data["content"])

    async def test_persistent_malformed_calls_stop_instead_of_looping(self):
        # Bounded by _MAX_MALFORMED_TURNS (2), not by max_turns.
        turns, _ = await self._drive([1] * 6, max_turns=6)
        self.assertEqual(turns, 3)

    async def test_clean_turn_with_no_tool_calls_still_ends_immediately(self):
        turns, _ = await self._drive([0])
        self.assertEqual(turns, 1)


if __name__ == "__main__":
    unittest.main()


class MalformedArgumentsTests(unittest.TestCase):
    """Unbalanced arguments JSON poisons the history and 400s every later turn."""

    @staticmethod
    def _call(name="search", arguments='{"q": "x"}'):
        return {"id": "1", "type": "function",
                "function": {"name": name, "arguments": arguments}}

    def test_missing_outer_brace_is_repaired_not_dropped(self):
        # Exactly the provider defect: nested final value, outer '}' lost.
        broken = '{"query": "x", "filter": {"property": "object"}'
        kept, dropped, repaired = _sanitize_tool_calls(
            [self._call(arguments=broken)]
        )
        self.assertEqual((dropped, repaired), (0, 1))
        import json as _json
        self.assertEqual(
            _json.loads(kept[0]["function"]["arguments"]),
            {"query": "x", "filter": {"property": "object"}},
        )

    def test_valid_arguments_are_left_byte_identical(self):
        good = '{"q": "x", "n": 10}'
        kept, dropped, repaired = _sanitize_tool_calls(
            [self._call(arguments=good)]
        )
        self.assertEqual((dropped, repaired), (0, 0))
        self.assertEqual(kept[0]["function"]["arguments"], good)

    def test_unrepairable_arguments_are_dropped(self):
        kept, dropped, repaired = _sanitize_tool_calls(
            [self._call(arguments='{"q": "unterminated')]
        )
        self.assertIsNone(kept)
        self.assertEqual((dropped, repaired), (1, 0))

    def test_non_string_arguments_are_dropped(self):
        kept, dropped, repaired = _sanitize_tool_calls(
            [self._call(arguments=None)]
        )
        self.assertIsNone(kept)
        self.assertEqual((dropped, repaired), (1, 0))

    def test_repair_does_not_mutate_the_original_call(self):
        original = self._call(arguments='{"a": {"b": 1}')
        kept, _, repaired = _sanitize_tool_calls([original])
        self.assertEqual(repaired, 1)
        self.assertEqual(original["function"]["arguments"], '{"a": {"b": 1}')

    def test_name_and_argument_defects_are_counted_together(self):
        kept, dropped, repaired = _sanitize_tool_calls(
            [
                self._call(name=None),
                self._call(arguments='{"a": {"b": 1}'),
                self._call(),
            ]
        )
        self.assertEqual((dropped, repaired), (1, 1))
        self.assertEqual(len(kept), 2)


class StartupReapResilienceTests(unittest.IsolatedAsyncioTestCase):
    """Startup cleanup must never stop the service from coming up."""

    async def test_docker_rm_timeout_does_not_fail_startup(self):
        async def fake_run(*args, **kwargs):
            if args[:2] == ("docker", "ps"):
                return "abc123\ndef456", "", 0
            if args[:3] == ("docker", "rm", "-f"):
                raise TaskSandboxError("Command timed out after 30s: docker rm -f")
            return "", "", 0

        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            await task_sandbox.reap_owned_task_sandboxes()  # must not raise

    async def test_one_stuck_container_does_not_skip_the_others(self):
        attempted = []

        async def fake_run(*args, **kwargs):
            if args[:2] == ("docker", "ps"):
                return "c1\nc2\nc3", "", 0
            if args[:3] == ("docker", "volume", "ls"):
                return "v1", "", 0
            if args[:3] == ("docker", "rm", "-f"):
                attempted.append(args[-1])
                if args[-1] == "c2":
                    raise TaskSandboxError("timed out")
                return "", "", 0
            if args[:4] == ("docker", "volume", "rm", "-f"):
                attempted.append(args[-1])
                return "", "", 0
            return "", "", 0

        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            await task_sandbox.reap_owned_task_sandboxes()

        # c2 blew up, yet c3 and the volume were still reclaimed.
        self.assertEqual(attempted, ["c1", "c2", "c3", "v1"])

    async def test_listing_failure_is_survivable(self):
        async def fake_run(*args, **kwargs):
            raise TaskSandboxError("docker ps timed out")

        with patch.object(task_sandbox, "_run", side_effect=fake_run), patch.object(
            task_sandbox, "write_runtime_event"
        ):
            await task_sandbox.reap_owned_task_sandboxes()  # must not raise

    async def test_reap_uses_the_configurable_teardown_timeout(self):
        seen = []

        async def fake_run(*args, **kwargs):
            seen.append(kwargs.get("timeout"))
            return "", "", 0

        with patch.dict("os.environ", {"MCP_SANDBOX_TEARDOWN_TIMEOUT": "77"}), \
             patch.object(task_sandbox, "_run", side_effect=fake_run), \
             patch.object(task_sandbox, "write_runtime_event"):
            await task_sandbox.reap_owned_task_sandboxes()

        self.assertTrue(seen and all(t == 77.0 for t in seen), seen)
