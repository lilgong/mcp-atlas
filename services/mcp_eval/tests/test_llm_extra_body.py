import unittest
from unittest.mock import AsyncMock, patch

from litellm.types.utils import ModelResponse

from mcp_completion.llm import ThinkingContractViolation, create_completion
from mcp_completion.schema import UserMessage


class ExtraBodyPassthroughTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, extra_body):
        provider_response = {
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
        completion = AsyncMock(return_value=provider_response)
        with (
            patch("mcp_completion.llm.litellm.acompletion", completion),
            patch("mcp_completion.llm.write_runtime_event"),
        ):
            await create_completion(
                model="openai/test-model",
                messages=[UserMessage(role="user", content="test")],
                tools=[],
                extra_body=extra_body,
                task_id="extra-body-test",
            )
        return completion.await_args.kwargs

    async def test_disabled_thinking_is_not_overridden(self):
        supplied = {"thinking": {"type": "disabled"}}

        kwargs = await self._call(supplied)

        self.assertEqual(kwargs["extra_body"], supplied)
        self.assertEqual(supplied, {"thinking": {"type": "disabled"}})

    async def test_enabled_max_reasoning_is_passed_unchanged(self):
        supplied = {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        }

        kwargs = await self._call(supplied)

        self.assertEqual(kwargs["extra_body"], supplied)

    async def test_empty_extra_body_is_omitted(self):
        kwargs = await self._call({})

        self.assertNotIn("extra_body", kwargs)


class DisabledThinkingContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _response(*, content="ok", reasoning_content=None):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": reasoning_content,
                        "tool_calls": None,
                    }
                }
            ]
        }

    async def _call(self, responses, extra_body=None):
        completion = AsyncMock(side_effect=responses)
        with (
            patch("mcp_completion.llm.litellm.acompletion", completion),
            patch("mcp_completion.llm.write_runtime_event") as runtime_event,
            patch("mcp_completion.llm._write_token_usage") as token_usage,
        ):
            result = await create_completion(
                model="openai/test-model",
                messages=[UserMessage(role="user", content="test")],
                tools=[],
                extra_body=extra_body or {"thinking": {"type": "disabled"}},
                task_id="thinking-contract-test",
            )
        return result, completion, runtime_event, token_usage

    async def test_empty_reasoning_does_not_create_empty_think_tags(self):
        result, completion, _, _ = await self._call([
            self._response(reasoning_content="  \n"),
        ])

        self.assertEqual(result.message.content, "ok")
        self.assertEqual(completion.await_count, 1)

    async def test_provider_empty_think_tag_is_removed(self):
        result, _, _, _ = await self._call([
            self._response(content="<think> \n</think>ok"),
        ])

        self.assertEqual(result.message.content, "ok")

    async def test_litellm_response_with_empty_reasoning_has_no_think_tag(self):
        response = ModelResponse(
            model="test-model",
            choices=[{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_content": "",
                },
                "finish_reason": "stop",
            }],
            usage={
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        )

        result, completion, _, token_usage = await self._call([response])

        self.assertEqual(result.message.content, "ok")
        self.assertEqual(completion.await_count, 1)
        self.assertEqual(token_usage.call_count, 1)

    async def test_disabled_reasoning_retries_only_the_model_call(self):
        result, completion, runtime_event, token_usage = await self._call([
            self._response(reasoning_content="private reasoning"),
            self._response(reasoning_content=""),
        ])

        self.assertEqual(result.message.content, "ok")
        self.assertEqual(completion.await_count, 2)
        self.assertEqual(token_usage.call_count, 2)
        events = [call.args[1] for call in runtime_event.call_args_list]
        self.assertEqual(events.count("thinking_contract_violation"), 1)

    async def test_direct_nonempty_think_block_also_retries(self):
        result, completion, _, _ = await self._call([
            self._response(content="<think>private reasoning</think>answer"),
            self._response(content="answer"),
        ])

        self.assertEqual(result.message.content, "answer")
        self.assertEqual(completion.await_count, 2)

    async def test_three_contract_violations_fail_the_turn(self):
        completion = AsyncMock(side_effect=[
            self._response(reasoning_content="one"),
            self._response(reasoning_content="two"),
            self._response(reasoning_content="three"),
        ])
        with (
            patch("mcp_completion.llm.litellm.acompletion", completion),
            patch("mcp_completion.llm.write_runtime_event"),
            patch("mcp_completion.llm._write_token_usage") as token_usage,
        ):
            with self.assertRaises(ThinkingContractViolation):
                await create_completion(
                    model="openai/test-model",
                    messages=[UserMessage(role="user", content="test")],
                    tools=[],
                    extra_body={"thinking": {"type": "disabled"}},
                    task_id="thinking-contract-test",
                )

        self.assertEqual(completion.await_count, 3)
        self.assertEqual(token_usage.call_count, 3)

    async def test_enabled_reasoning_is_preserved_without_retry(self):
        result, completion, _, _ = await self._call(
            [self._response(reasoning_content="visible reasoning")],
            extra_body={"thinking": {"type": "enabled"}},
        )

        self.assertEqual(
            result.message.content,
            "<think>visible reasoning</think>ok",
        )
        self.assertEqual(completion.await_count, 1)


if __name__ == "__main__":
    unittest.main()
