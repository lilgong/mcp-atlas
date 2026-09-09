import asyncio
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from mcp_completion.account_guard import FatalAccountError


# mcp_completion_script intentionally writes its operational log relative to
# the evaluator directory. Import it through that same working-directory
# contract so this test also works when pytest is launched from the repo root.
EVAL_DIR = Path(__file__).resolve().parents[1]
original_cwd = Path.cwd()
try:
    os.chdir(EVAL_DIR)
    from mcp_completion_script import (
        AsyncMCPTrajectoryGenerator,
        TerminalTaskError,
        extract_final_assistant_content,
        resolve_system_prompt,
        validate_completion_output,
    )
finally:
    os.chdir(original_cwd)


def test_custom_system_prompt_file_overrides_built_in(tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("custom system prompt\n", encoding="utf-8")

    assert resolve_system_prompt(True, str(prompt_file)) == "custom system prompt"


def test_disabled_system_prompt_does_not_read_configured_file():
    assert resolve_system_prompt(False, "/missing/prompt.txt") is None


def test_enabled_empty_system_prompt_file_is_rejected(tmp_path):
    prompt_file = tmp_path / "empty.txt"
    prompt_file.write_text(" \n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="system prompt file is empty"):
        resolve_system_prompt(True, str(prompt_file))


def test_final_response_matches_official_runner_when_tool_message_is_last():
    trajectory = json.dumps(
        [
            {
                "type": "message",
                "data": {
                    "role": "assistant",
                    "content": "latest assistant content",
                    "tool_calls": [{"id": "call-1"}],
                },
            },
            {
                "type": "message",
                "data": {
                    "role": "tool",
                    "content": "tool evidence must not become the answer",
                    "tool_call_id": "call-1",
                },
            },
            {
                "type": "error",
                "data": {"reason": "max_tool_calls_reached"},
            },
        ]
    )

    assert extract_final_assistant_content(trajectory) == "latest assistant content"


def test_final_response_is_empty_without_nonempty_assistant_content():
    trajectory = json.dumps(
        [
            {
                "type": "message",
                "data": {"role": "assistant", "content": "", "tool_calls": []},
            },
            {
                "type": "message",
                "data": {"role": "tool", "content": "tool evidence"},
            },
        ]
    )

    assert extract_final_assistant_content(trajectory) == ""


def test_final_response_does_not_stringify_none_content():
    trajectory = json.dumps(
        [
            {
                "type": "message",
                "data": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [],
                },
            }
        ]
    )

    assert extract_final_assistant_content(trajectory) == ""


def test_unexpected_task_exception_does_not_cancel_siblings(tmp_path):
    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        completed = []

        async def process(row, _output, _index, _total):
            task_id = row["TASK"]
            if task_id == "bad":
                raise RuntimeError("broken task-local parser")
            await asyncio.sleep(0.01)
            completed.append(task_id)
            return {"TASK": task_id}

        generator.process_single_task = process
        frame = pd.DataFrame([{"TASK": "bad"}, {"TASK": "good-1"}, {"TASK": "good-2"}])

        with pytest.raises(RuntimeError, match="output is incomplete"):
            await generator.evaluate_dataset_async(
                frame,
                str(tmp_path / "results.csv"),
                max_concurrent_requests=3,
            )

        assert sorted(completed) == ["good-1", "good-2"]

    asyncio.run(run())


def test_missing_agent_response_is_written_as_terminal_failure(tmp_path, monkeypatch):
    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        writes = []

        async def no_response(**_kwargs):
            return None, 2

        async def record_write(result, _output):
            writes.append(result)

        generator.run_live_task_async = no_response
        generator.write_result_to_csv = record_write
        monkeypatch.setattr("mcp_completion_script.random.uniform", lambda *_: 0)

        result = await generator.process_single_task(
            {
                "TASK": "timeout-task",
                "PROMPT": "test prompt",
                "TRAJECTORY": "[]",
                "GTFA_CLAIMS": "[]",
                "ENABLED_TOOLS": "[]",
            },
            str(tmp_path / "results.csv"),
            0,
            1,
        )

        assert result == writes[0]
        assert result["TASK"] == "timeout-task"
        assert result["script_model_response"].startswith(
            "ERROR [no_agent_response]:"
        )
        assert result["num_retry"] == 2

    asyncio.run(run())


def test_exhausted_live_request_is_terminal(monkeypatch):
    class FailedResponse:
        status = 500

        async def text(self):
            return "temporary upstream failure"

    class RequestContext:
        async def __aenter__(self):
            return FailedResponse()

        async def __aexit__(self, *_args):
            return False

    class Session:
        def post(self, *_args, **_kwargs):
            return RequestContext()

    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        generator.session = Session()
        monkeypatch.setattr("mcp_completion_script.MAX_RETRY_ATTEMPTS", 1)

        with pytest.raises(TerminalTaskError, match="after 1 attempts") as caught:
            await generator.run_live_task_async([], "prompt", "failed-task")
        assert caught.value.kind == "http_500"
        assert caught.value.attempts == 1

    asyncio.run(run())


def test_timeout_cancels_exact_attempt_before_retry(monkeypatch):
    events = []

    class RequestContext:
        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *_args):
            return False

    class Session:
        def post(self, *_args, **kwargs):
            events.append(("post", kwargs["json"]["evaluationId"]))
            return RequestContext()

    async def fake_cancel(evaluation_id):
        events.append(("cancel", evaluation_id))

    async def no_sleep(_delay):
        return None

    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        generator.session = Session()
        monkeypatch.setattr("mcp_completion_script.MAX_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr("mcp_completion_script.MAX_TIMEOUT_ATTEMPTS", 2)
        monkeypatch.setattr(
            "mcp_completion_script.cancel_remote_evaluation", fake_cancel
        )
        monkeypatch.setattr("mcp_completion_script.asyncio.sleep", no_sleep)

        with pytest.raises(TerminalTaskError) as caught:
            await generator.run_live_task_async([], "prompt", "timeout-task")

        assert caught.value.kind == "task_timeout"
        first_id = events[0][1]
        second_id = events[2][1]
        assert first_id != second_id
        assert events == [
            ("post", first_id),
            ("cancel", first_id),
            ("post", second_id),
            ("cancel", second_id),
        ]

    asyncio.run(run())


def test_run_cancellation_cleans_remote_attempt(monkeypatch):
    request_started = asyncio.Event()
    cancelled_ids = []

    class RequestContext:
        async def __aenter__(self):
            request_started.set()
            await asyncio.Event().wait()

        async def __aexit__(self, *_args):
            return False

    class Session:
        def post(self, *_args, **_kwargs):
            return RequestContext()

    async def fake_cancel(evaluation_id):
        cancelled_ids.append(evaluation_id)

    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        generator.session = Session()
        monkeypatch.setattr(
            "mcp_completion_script.cancel_remote_evaluation", fake_cancel
        )
        task = asyncio.create_task(
            generator.run_live_task_async([], "prompt", "cancelled-task")
        )
        await request_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(cancelled_ids) == 1

    asyncio.run(run())


def test_fatal_account_exception_still_cancels_siblings(tmp_path):
    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        sibling_cancelled = asyncio.Event()

        async def process(row, _output, _index, _total):
            if row["TASK"] == "fatal":
                await asyncio.sleep(0)
                raise FatalAccountError("out of funds")
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

        generator.process_single_task = process
        frame = pd.DataFrame([{"TASK": "fatal"}, {"TASK": "sibling"}])

        with pytest.raises(FatalAccountError, match="out of funds"):
            await generator.evaluate_dataset_async(
                frame,
                str(tmp_path / "results.csv"),
                max_concurrent_requests=2,
            )

        assert sibling_cancelled.is_set()

    asyncio.run(run())


def test_cancelling_dataset_waits_for_all_task_cleanup(tmp_path):
    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        started = 0
        all_started = asyncio.Event()
        cleaned = []

        async def process(row, _output, _index, _total):
            nonlocal started
            started += 1
            if started == 2:
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.append(row["TASK"])

        generator.process_single_task = process
        frame = pd.DataFrame([{"TASK": "one"}, {"TASK": "two"}])
        evaluation = asyncio.create_task(
            generator.evaluate_dataset_async(
                frame,
                str(tmp_path / "results.csv"),
                max_concurrent_requests=2,
            )
        )
        await all_started.wait()
        evaluation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await evaluation
        assert sorted(cleaned) == ["one", "two"]

    asyncio.run(run())


def test_completion_output_requires_selected_task_coverage(tmp_path):
    expected = pd.DataFrame([{"TASK": "a"}, {"TASK": "b"}])
    output = tmp_path / "results.csv"
    pd.DataFrame([{"TASK": "a"}]).to_csv(output, index=False)

    with pytest.raises(RuntimeError, match=r"expected=2 rows, actual=1 rows"):
        validate_completion_output(expected, str(output))

    pd.DataFrame([{"TASK": "a"}, {"TASK": "b"}]).to_csv(output, index=False)
    validate_completion_output(expected, str(output))

    # A resumed subset may share an output file with tasks completed by an
    # earlier full run. Those extra rows are valid and must remain available
    # for downstream scoring.
    pd.DataFrame([{"TASK": "a"}, {"TASK": "b"}, {"TASK": "c"}]).to_csv(
        output, index=False
    )
    validate_completion_output(expected, str(output))


def test_completion_output_rejects_duplicate_tasks(tmp_path):
    expected = pd.DataFrame([{"TASK": "a"}, {"TASK": "b"}])
    output = tmp_path / "results.csv"
    pd.DataFrame([{"TASK": "a"}, {"TASK": "b"}, {"TASK": "b"}]).to_csv(
        output, index=False
    )

    with pytest.raises(RuntimeError, match=r"duplicates=\['b'\]"):
        validate_completion_output(expected, str(output))
