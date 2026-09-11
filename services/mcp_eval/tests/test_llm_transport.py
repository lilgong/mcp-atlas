import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import litellm

from mcp_completion.account_guard import FatalAccountError
from mcp_completion.config import config
from mcp_completion.llm import create_completion
from mcp_completion.schema import UserMessage


def _response():
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "tool_calls": None,
                }
            }
        ]
    }


class _ProviderError(RuntimeError):
    def __init__(self, status_code, message=None):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


class UnifiedModelTransportTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self):
        return await create_completion(
            model="bare-model-name",
            messages=[UserMessage(role="user", content="test")],
            tools=[],
            task_id="transport-test",
        )

    async def test_only_429_500_502_503_are_retried(self):
        original_attempts = config.LLM_MAX_ATTEMPTS
        original_delay = config.LLM_RETRY_DELAY
        config.LLM_MAX_ATTEMPTS = 3
        config.LLM_RETRY_DELAY = 0
        try:
            for status_code in (429, 500, 502, 503):
                with self.subTest(status_code=status_code):
                    completion = AsyncMock(
                        side_effect=[_ProviderError(status_code), _response()]
                    )
                    with (
                        patch("mcp_completion.llm.litellm.acompletion", completion),
                        patch("mcp_completion.llm.write_runtime_event"),
                    ):
                        result = await self._run()
                    self.assertEqual(result.message.content, "ok")
                    self.assertEqual(completion.await_count, 2)
        finally:
            config.LLM_MAX_ATTEMPTS = original_attempts
            config.LLM_RETRY_DELAY = original_delay

    async def test_504_and_timeout_are_not_retried(self):
        for error in (_ProviderError(504), asyncio.TimeoutError("slow upstream")):
            with self.subTest(error=type(error).__name__):
                completion = AsyncMock(side_effect=error)
                with (
                    patch("mcp_completion.llm.litellm.acompletion", completion),
                    patch("mcp_completion.llm.write_runtime_event"),
                ):
                    with self.assertRaises(type(error)):
                        await self._run()
                self.assertEqual(completion.await_count, 1)

    async def test_real_litellm_connection_errors_are_not_retried(self):
        errors = (
            litellm.APIConnectionError(
                message="connection refused",
                llm_provider="openai",
                model="bare-model-name",
            ),
            litellm.Timeout(
                message="read timed out",
                llm_provider="openai",
                model="bare-model-name",
            ),
        )
        self.assertEqual(errors[0].status_code, 500)

        for error in errors:
            with self.subTest(error=type(error).__name__):
                completion = AsyncMock(side_effect=error)
                with (
                    patch("mcp_completion.llm.litellm.acompletion", completion),
                    patch("mcp_completion.llm.write_runtime_event"),
                ):
                    with self.assertRaises(type(error)):
                        await self._run()
                self.assertEqual(completion.await_count, 1)

    async def test_retry_budget_is_bounded(self):
        original_attempts = config.LLM_MAX_ATTEMPTS
        original_delay = config.LLM_RETRY_DELAY
        config.LLM_MAX_ATTEMPTS = 3
        config.LLM_RETRY_DELAY = 0
        completion = AsyncMock(side_effect=_ProviderError(503))
        try:
            with (
                patch("mcp_completion.llm.litellm.acompletion", completion),
                patch("mcp_completion.llm.write_runtime_event"),
            ):
                with self.assertRaises(_ProviderError):
                    await self._run()
        finally:
            config.LLM_MAX_ATTEMPTS = original_attempts
            config.LLM_RETRY_DELAY = original_delay
        self.assertEqual(completion.await_count, 3)

    async def test_cancellation_reaches_the_active_gateway_request(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def completion(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with (
            patch("mcp_completion.llm.litellm.acompletion", completion),
            patch("mcp_completion.llm.write_runtime_event"),
        ):
            task = asyncio.create_task(self._run())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())

    async def test_no_process_wide_thirty_request_limit(self):
        active = 0
        peak = 0
        all_started = asyncio.Event()
        release = asyncio.Event()

        async def completion(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 40:
                all_started.set()
            try:
                await release.wait()
                return _response()
            finally:
                active -= 1

        with (
            patch("mcp_completion.llm.litellm.acompletion", completion),
            patch("mcp_completion.llm.write_runtime_event"),
        ):
            tasks = [asyncio.create_task(self._run()) for _ in range(40)]
            await asyncio.wait_for(all_started.wait(), timeout=1)
            self.assertEqual(peak, 40)
            release.set()
            await asyncio.gather(*tasks)

    async def test_billing_failure_names_the_unified_key(self):
        completion = AsyncMock(side_effect=_ProviderError(402, "insufficient balance"))
        with (
            patch("mcp_completion.llm.litellm.acompletion", completion),
            patch("mcp_completion.llm.write_runtime_event"),
        ):
            with self.assertRaises(FatalAccountError) as raised:
                await self._run()

        self.assertEqual(raised.exception.source_name, "bare-model-name")
        self.assertEqual(raised.exception.credential_envs, ("LLM_API_KEY",))
