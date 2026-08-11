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


if __name__ == "__main__":
    unittest.main()
