import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import mcp_evals_scores
from mcp_evals_scores import (
    AsyncLiteLLMClient,
    CoverageEvaluator,
    EvaluatorConfig,
    get_single_claim_evaluation_schema,
)
from mcp_completion.account_guard import FatalAccountError


def _fake_response(content='{"coverage": "fulfilled"}'):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        ),
    )


class LiteLLMRequestOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_and_single_retry_layer_are_passed_through(self):
        captured = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            return _fake_response()

        client = AsyncLiteLLMClient(
            EvaluatorConfig(evaluator_model="openai/gpt-5.4", semaphore_limit=2)
        )
        with patch.object(
            mcp_evals_scores.litellm, "acompletion", fake_acompletion
        ), patch.object(mcp_evals_scores, "TOKEN_LOG_PATH", self._log_path()):
            schema = get_single_claim_evaluation_schema()
            await client.generate_structured_content("prompt", schema)

        # Without an explicit timeout this inherits litellm's 6000s default.
        self.assertEqual(captured["timeout"], mcp_evals_scores.EVAL_REQUEST_TIMEOUT)
        # The SDK's own retries would multiply with the tenacity decorator.
        self.assertEqual(captured["max_retries"], 0)
        self.assertEqual(captured["response_format"]["type"], "json_schema")
        structured = captured["response_format"]["json_schema"]
        self.assertEqual(structured["name"], "single_claim_evaluation")
        self.assertIs(structured["strict"], True)
        self.assertEqual(structured["schema"], schema)
        self.assertIs(structured["schema"]["additionalProperties"], False)

    def _log_path(self):
        import tempfile, os

        return os.path.join(tempfile.mkdtemp(), "token_log.jsonl")

    async def test_account_failure_is_not_retried(self):
        calls = 0

        async def fake_acompletion(**kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("Invalid API key")

        client = AsyncLiteLLMClient(
            EvaluatorConfig(evaluator_model="openai/gpt-5.4", semaphore_limit=2)
        )
        with patch.object(
            mcp_evals_scores.litellm, "acompletion", fake_acompletion
        ), patch.dict(
            mcp_evals_scores.os.environ, {"EVAL_LLM_API_KEY": ""}
        ):
            with self.assertRaises(FatalAccountError) as raised:
                await client.generate_structured_content("prompt", {})

        self.assertEqual(calls, 1)
        self.assertEqual(raised.exception.source_kind, "evaluator_model")
        self.assertEqual(raised.exception.source_name, "openai/gpt-5.4")
        self.assertEqual(raised.exception.credential_envs, ("LLM_API_KEY",))


class SemaphoreScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_sleep_does_not_hold_a_concurrency_slot(self):
        """The slot must be released once the HTTP call returns.

        Previously the semaphore wrapped the rate-limit sleep and JSON parsing
        too, so with limit=1 three requests serialised their delays.
        """
        import os
        import tempfile

        delay = 0.3
        config = EvaluatorConfig(
            evaluator_model="openai/gpt-5.4",
            semaphore_limit=1,
            request_delay=delay,
        )
        client = AsyncLiteLLMClient(config)

        async def fake_acompletion(**kwargs):
            return _fake_response()

        log_path = os.path.join(tempfile.mkdtemp(), "token_log.jsonl")
        started = time.monotonic()
        with patch.object(
            mcp_evals_scores.litellm, "acompletion", fake_acompletion
        ), patch.object(mcp_evals_scores, "TOKEN_LOG_PATH", log_path):
            await asyncio.gather(
                *(
                    client.generate_structured_content("prompt", {})
                    for _ in range(3)
                )
            )
        elapsed = time.monotonic() - started

        # Serialised delays would take ~0.9s; overlapping them takes ~0.3s.
        self.assertLess(elapsed, delay * 2)

    async def test_a_stalled_request_still_frees_its_slot_on_timeout(self):
        import os
        import tempfile

        config = EvaluatorConfig(
            evaluator_model="openai/gpt-5.4",
            semaphore_limit=1,
            request_delay=0.0,
        )
        client = AsyncLiteLLMClient(config)

        async def fake_acompletion(**kwargs):
            raise TimeoutError("upstream stalled")

        log_path = os.path.join(tempfile.mkdtemp(), "token_log.jsonl")
        with patch.object(
            mcp_evals_scores.litellm, "acompletion", fake_acompletion
        ), patch.object(mcp_evals_scores, "TOKEN_LOG_PATH", log_path):
            with self.assertRaises(TimeoutError):
                # reraise=True on the tenacity decorator surfaces the last error.
                await client.generate_structured_content.retry_with(
                    stop=mcp_evals_scores.stop_after_attempt(1)
                )(client, "prompt", {})

        self.assertEqual(client.semaphore._value, 1, "slot was not released")


class CoverageEvaluatorOutputTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_example_is_valid_json(self):
        evaluator = CoverageEvaluator(
            client=None,
            config=EvaluatorConfig(
                evaluator_model="openai/gpt-5.4", semaphore_limit=1
            ),
        )
        prompt = evaluator._get_single_claim_evaluation_prompt(
            "The claim", "The response"
        )
        marker = "- Final output must follow this format:\n"
        example = prompt.split(marker, 1)[1].split("\n\nIMPORTANT:", 1)[0]

        parsed = json.loads(example)
        self.assertEqual(parsed["justification"], "str type")

    async def test_evaluator_failure_is_not_converted_to_not_fulfilled(self):
        class FailingClient:
            async def generate_structured_content(self, *args, **kwargs):
                raise RuntimeError("judge unavailable")

        evaluator = CoverageEvaluator(
            client=FailingClient(),
            config=EvaluatorConfig(
                evaluator_model="openai/gpt-5.4", semaphore_limit=1
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "judge unavailable"):
            await evaluator.evaluate_single_claim("claim", "response")

    async def test_terminal_completion_failure_scores_zero_without_judge(self):
        class UnexpectedClient:
            async def generate_structured_content(self, *args, **kwargs):
                raise AssertionError("terminal failures must not call the judge")

        evaluator = CoverageEvaluator(
            client=UnexpectedClient(),
            config=EvaluatorConfig(
                evaluator_model="openai/gpt-5.4", semaphore_limit=1
            ),
        )

        result = await evaluator.evaluate(
            ["claim one", "claim two"],
            "ERROR [task_timeout]: task timed out twice",
            task_id="failed-task",
        )

        self.assertEqual(0.0, result["coverage_score"])
        self.assertEqual(2, result["total_claims"])
        self.assertEqual([0.0, 0.0], [x["score"] for x in result["per_claim"]])

    def test_terminal_failure_counts_are_grouped_by_kind(self):
        frame = mcp_evals_scores.pd.DataFrame(
            [
                {"script_model_response": "ERROR [task_timeout]: first"},
                {"script_model_response": "ERROR [task_timeout]: second"},
                {"script_model_response": "ERROR [http_500]: upstream"},
                {"script_model_response": "normal answer"},
            ]
        )

        self.assertEqual(
            {"task_timeout": 2, "http_500": 1},
            dict(mcp_evals_scores.terminal_failure_counts(frame)),
        )


if __name__ == "__main__":
    unittest.main()


class ResumeScoringTests(unittest.TestCase):
    """A rerun after one more completion lands must only pay for that completion."""

    def _previous(self, rows):
        import pandas as pd

        return pd.DataFrame(rows)

    def _write(self, rows):
        import tempfile, os, pandas as pd

        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        pd.DataFrame(rows).to_csv(path, index=False)
        self.addCleanup(os.unlink, path)
        return path

    def _scored_row(self, task, response, score=0.5):
        row = {
            "TASK": task,
            "GTFA_CLAIMS": '["c"]',
            "script_model_response": response,
            "coverage_score": score,
            "fully_covered_claims": 1,
            "partially_covered_claims": 0,
            "total_claims": 1,
            "coverage_details_json": "{}",
            "evaluation_confidence": 0.9,
        }
        row[mcp_evals_scores.SCORING_FINGERPRINT_COLUMN] = (
            mcp_evals_scores.scored_input_fingerprint(row, "judge-model")
        )
        row[mcp_evals_scores.SCORING_MODEL_COLUMN] = "judge-model"
        row[mcp_evals_scores.SCORING_POLICY_COLUMN] = (
            mcp_evals_scores.SCORING_POLICY_VERSION
        )
        return row

    def _input_row(self, task, response):
        return {
            "TASK": task,
            "GTFA_CLAIMS": '["c"]',
            "script_model_response": response,
        }

    def _reusable(self, previous_rows, input_rows):
        import pandas as pd
        import logging

        path = self._write(previous_rows)
        return mcp_evals_scores.load_reusable_scores(
            path,
            pd.DataFrame(input_rows),
            logging.getLogger("t"),
            "judge-model",
        )

    def test_unchanged_task_is_reused(self):
        reusable = self._reusable(
            [self._scored_row("t1", "answer")], [self._input_row("t1", "answer")]
        )
        self.assertEqual(list(reusable), [0])
        self.assertEqual(reusable[0]["coverage_score"], 0.5)

    def test_rerun_completion_is_not_given_the_stale_score(self):
        reusable = self._reusable(
            [self._scored_row("t1", "old answer")],
            [self._input_row("t1", "new answer")],
        )
        self.assertEqual(reusable, {}, "a changed response must be rescored")

    def test_changed_claims_are_not_given_the_stale_score(self):
        previous = self._scored_row("t1", "answer")
        row = self._input_row("t1", "answer")
        row["GTFA_CLAIMS"] = '["different claim"]'
        self.assertEqual(self._reusable([previous], [row]), {})

    def test_previously_unscored_task_is_retried(self):
        previous = self._scored_row("t1", "answer", score=float("nan"))
        self.assertEqual(
            self._reusable([previous], [self._input_row("t1", "answer")]),
            {},
            "an unscored row is a judge failure, not a result",
        )

    def test_new_task_is_scored(self):
        reusable = self._reusable(
            [self._scored_row("t1", "answer")],
            [self._input_row("t1", "answer"), self._input_row("t2", "answer")],
        )
        self.assertEqual(list(reusable), [0])

    def test_file_without_result_columns_is_ignored(self):
        self.assertEqual(
            self._reusable(
                [{"TASK": "t1", "GTFA_CLAIMS": '["c"]'}],
                [self._input_row("t1", "answer")],
            ),
            {},
        )

    def test_old_file_without_scoring_identity_is_not_reused(self):
        previous = self._scored_row("t1", "answer")
        previous.pop(mcp_evals_scores.SCORING_FINGERPRINT_COLUMN)
        self.assertEqual(
            self._reusable([previous], [self._input_row("t1", "answer")]),
            {},
        )

    def test_changed_evaluator_model_is_not_reused(self):
        import pandas as pd
        import logging

        previous = self._scored_row("t1", "answer")
        path = self._write([previous])
        reusable = mcp_evals_scores.load_reusable_scores(
            path,
            pd.DataFrame([self._input_row("t1", "answer")]),
            logging.getLogger("t"),
            "different-judge-model",
        )
        self.assertEqual(reusable, {})

    def test_fingerprint_reads_the_column_the_judge_reads(self):
        import pandas as pd

        row = pd.Series(
            {
                "GTFA_CLAIMS": '["c"]',
                "script_model_response": "picked",
                "response": "ignored",
            }
        )
        self.assertEqual(mcp_evals_scores.response_for_scoring(row), "picked")
        fallback = pd.Series(
            {"GTFA_CLAIMS": '["c"]', "script_model_response": None, "response": "used"}
        )
        self.assertEqual(mcp_evals_scores.response_for_scoring(fallback), "used")
