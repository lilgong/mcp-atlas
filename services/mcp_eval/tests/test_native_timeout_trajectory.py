"""Exercise native HTTP cancellation and CSV persistence without a proxy."""
import asyncio
import csv
import json
import socket
from types import SimpleNamespace

import pytest
import uvicorn

from mcp_completion import main
from mcp_completion.partial_trajectory import capture_context, snapshot_path
from mcp_completion.streaming import collect_litellm_response
import mcp_completion_script as runner


def test_timeout_native_http_keeps_last_attempt_and_judge_contract(tmp_path, monkeypatch):
    from mcp_evals_scores import CoverageEvaluator, EvaluatorConfig, response_for_scoring, scored_input_fingerprint

    async def scenario():
        attempts = []

        async def fake_outputs(body):
            attempts.append(body.evaluation_id)
            label = f"attempt-{len(attempts)}"
            yield SimpleNamespace(type="message", data={"role": "tool", "tool_call_id": "previous", "content": label + " tool evidence"})

            class Stream:
                def __aiter__(self):
                    async def chunks():
                        yield {"choices": [{"index": 0, "delta": {"reasoning": label + " thinking ", "content": "partial "}}]}
                        yield {"choices": [{"index": 0, "delta": {"reasoning_content": "continues", "tool_calls": [
                            {"index": 0, "id": "unfinished", "function": {"name": "lookup", "arguments": '{"query":'}}]}}]}
                        await asyncio.Event().wait()
                    return chunks()
            await collect_litellm_response(Stream(), messages=[])

        monkeypatch.setattr(main, "handle_run_mcp_eval", fake_outputs)
        monkeypatch.setenv("MCP_RUNTIME_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setattr(runner, "MAX_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr(runner, "MAX_TIMEOUT_ATTEMPTS", 2)
        monkeypatch.setattr(runner, "REQUEST_TIMEOUT", .15)
        monkeypatch.setattr(runner, "get_retry_delay", lambda attempt: 0)
        monkeypatch.setattr(runner.random, "uniform", lambda *args: 0)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        monkeypatch.setattr(runner, "SERVER_URL", f"http://127.0.0.1:{sock.getsockname()[1]}")
        server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="error"))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                await asyncio.sleep(.01)
            row = {"TASK": "native-timeout", "PROMPT": "question", "ENABLED_TOOLS": "[]", "GTFA_CLAIMS": "[\"answer\"]"}
            output = tmp_path / "completion.csv"
            async with runner.AsyncMCPTrajectoryGenerator("offline-model") as generator:
                result = await generator.process_single_task(row, str(output), 0, 1)
            assert len(attempts) == 2 and attempts[0] != attempts[1]
            history = json.loads(result["raw_conversation_history"])
            assert "attempt-1" not in result["raw_conversation_history"]
            assert history[0]["content"] == "attempt-2 tool evidence"
            assert history[-1]["original_message"]["reasoning_content"] == "attempt-2 thinking continues"
            assert history[-1]["tool_calls"][0]["function"]["arguments"] == '{"query":'
            assert "finish_reason" not in history[-1]
            assert result["errors"][0]["evaluation_id"] == attempts[-1]
            with output.open() as handle:
                persisted = list(csv.DictReader(handle))[0]
            assert json.loads(persisted["raw_conversation_history"]) == history
            assert response_for_scoring(persisted).startswith("ERROR [task_timeout]")
            stripped = {**persisted, "raw_conversation_history": ""}
            assert scored_input_fingerprint(persisted, "judge") == scored_input_fingerprint(stripped, "judge")
            # The real native evaluator must not ask a judge to read partial history.
            evaluator = CoverageEvaluator(SimpleNamespace(), EvaluatorConfig(evaluator_model="offline-judge", semaphore_limit=1))
            scored = await evaluator.evaluate(["answer"], response_for_scoring(persisted), task_id="native-timeout")
            assert scored["coverage_score"] == 0.0
            assert scored["per_claim"][0]["score"] == 0.0
            assert not main._ACTIVE_EVALUATIONS
        finally:
            server.should_exit = True
            await serving
            sock.close()
    asyncio.run(scenario())


def test_normal_completion_has_no_extra_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RUNTIME_LOG_DIR", str(tmp_path))
    async def fake_outputs(body):
        yield SimpleNamespace(type="message", data={"role": "assistant", "content": "normal answer"})
    monkeypatch.setattr(main, "handle_run_mcp_eval", fake_outputs)
    body = SimpleNamespace(evaluation_id="a" * 32, task_id="normal")
    result = asyncio.run(main._collect_agent_outputs(body))
    assert result == [{"type": "message", "data": {"role": "assistant", "content": "normal answer"}}]
    assert not snapshot_path(body.evaluation_id).exists()
    assert capture_context.get() is None


def test_snapshot_failure_does_not_replace_cancellation(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_RUNTIME_LOG_DIR", str(tmp_path / "not-a-directory"))
    (tmp_path / "not-a-directory").write_text("blocked")
    async def fake_outputs(body):
        yield SimpleNamespace(type="message", data={"role": "tool", "content": "evidence"})
        raise asyncio.CancelledError()
    monkeypatch.setattr(main, "handle_run_mcp_eval", fake_outputs)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._collect_agent_outputs(SimpleNamespace(evaluation_id="a" * 32, task_id="task")))
    assert capture_context.get() is None
