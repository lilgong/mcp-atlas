import unittest
import os
from unittest.mock import AsyncMock, patch

from litellm.types.utils import ModelResponse

from mcp_completion.llm import ThinkingContractViolation, create_completion
from mcp_completion.schema import UserMessage


class ExtraBodyPassthroughTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, extra_body, *, streaming=False):
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
            patch.dict(
                os.environ,
                {"LLM_STREAMING_ENABLED": "true" if streaming else "false"},
            ),
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

    async def test_completion_requests_lossless_streaming(self):
        kwargs = await self._call({}, streaming=True)

        self.assertIs(kwargs["stream"], True)
        self.assertEqual(kwargs["stream_options"], {"include_usage": True})

    async def test_non_streaming_omits_stream_parameters(self):
        kwargs = await self._call({}, streaming=False)

        self.assertNotIn("stream", kwargs)
        self.assertNotIn("stream_options", kwargs)


class DisabledThinkingContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _response(*, content="ok", reasoning_content=None, tool_calls=None):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": reasoning_content,
                        "tool_calls": tool_calls,
                    }
                }
            ]
        }

    async def _call(
        self,
        responses,
        extra_body=None,
        retry_thinking_contract_violations=False,
    ):
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
                retry_thinking_contract_violations=(retry_thinking_contract_violations),
                task_id="thinking-contract-test",
            )
        return result, completion, runtime_event, token_usage

    async def test_empty_reasoning_does_not_create_empty_think_tags(self):
        result, completion, _, _ = await self._call(
            [
                self._response(reasoning_content="  \n"),
            ]
        )

        self.assertEqual(result.message.content, "ok")
        self.assertEqual(completion.await_count, 1)

    async def test_provider_content_is_preserved_verbatim(self):
        result, _, _, _ = await self._call(
            [
                self._response(content="<think> \n</think>ok"),
            ]
        )

        self.assertEqual(result.message.content, "<think> \n</think>ok")

    async def test_litellm_response_with_empty_reasoning_has_no_think_tag(self):
        response = ModelResponse(
            model="test-model",
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "reasoning_content": "",
                    },
                    "finish_reason": "stop",
                }
            ],
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
        result, completion, runtime_event, token_usage = await self._call(
            [
                self._response(reasoning_content="private reasoning"),
                self._response(reasoning_content=""),
            ],
            retry_thinking_contract_violations=True,
        )

        self.assertEqual(result.message.content, "ok")
        self.assertEqual(completion.await_count, 2)
        self.assertEqual(token_usage.call_count, 2)
        events = [call.args[1] for call in runtime_event.call_args_list]
        self.assertEqual(events.count("thinking_contract_violation"), 1)

    async def test_direct_nonempty_think_block_also_retries(self):
        result, completion, _, _ = await self._call(
            [
                self._response(content="<think>private reasoning</think>answer"),
                self._response(content="answer"),
            ],
            retry_thinking_contract_violations=True,
        )

        self.assertEqual(result.message.content, "answer")
        self.assertEqual(completion.await_count, 2)

    async def test_three_contract_violations_fail_the_turn(self):
        completion = AsyncMock(
            side_effect=[
                self._response(reasoning_content="one"),
                self._response(reasoning_content="two"),
                self._response(reasoning_content="three"),
            ]
        )
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
                    retry_thinking_contract_violations=True,
                    task_id="thinking-contract-test",
                )

        self.assertEqual(completion.await_count, 3)
        self.assertEqual(token_usage.call_count, 3)

    async def test_enabled_reasoning_is_not_copied_into_content(self):
        result, completion, _, _ = await self._call(
            [self._response(reasoning_content="visible reasoning")],
            extra_body={"thinking": {"type": "enabled"}},
        )

        self.assertEqual(result.message.content, "ok")
        self.assertEqual(result.message.reasoning_content, "visible reasoning")
        self.assertEqual(
            result.message.original_message.reasoning_content,
            "visible reasoning",
        )
        self.assertEqual(completion.await_count, 1)

    async def test_reasoning_is_forwarded_separately_on_the_next_turn(self):
        first, _, _, _ = await self._call(
            [
                self._response(
                    content="",
                    reasoning_content="keep this plan",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"query":"x"}',
                            },
                        }
                    ],
                )
            ],
            extra_body={"thinking": {"type": "enabled"}},
        )
        completion = AsyncMock(return_value=self._response(content="done"))
        with (
            patch("mcp_completion.llm.litellm.acompletion", completion),
            patch("mcp_completion.llm.write_runtime_event"),
            patch("mcp_completion.llm._write_token_usage"),
        ):
            await create_completion(
                model="openai/test-model",
                messages=[
                    UserMessage(role="user", content="test"),
                    first.message,
                ],
                tools=[],
                extra_body={"thinking": {"type": "enabled"}},
                task_id="reasoning-continuation-test",
                turn=2,
            )

        previous_assistant = completion.await_args.kwargs["messages"][1]
        self.assertEqual(
            previous_assistant,
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "keep this plan",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"query":"x"}',
                        },
                    }
                ],
            },
        )
        self.assertNotIn("<think>", previous_assistant["content"])
        self.assertNotIn("original_message", previous_assistant)

    async def test_retry_disabled_preserves_first_leaking_response(self):
        result, completion, runtime_event, token_usage = await self._call(
            [
                self._response(reasoning_content="provider reasoning"),
            ]
        )

        self.assertEqual(result.message.content, "ok")
        self.assertEqual(result.message.reasoning_content, "provider reasoning")
        self.assertEqual(
            result.message.original_message.reasoning_content,
            "provider reasoning",
        )
        self.assertEqual(completion.await_count, 1)
        self.assertEqual(token_usage.call_count, 1)
        violation = next(
            call
            for call in runtime_event.call_args_list
            if call.args[1] == "thinking_contract_violation"
        )
        self.assertFalse(violation.kwargs["retry_enabled"])
        self.assertFalse(violation.kwargs["will_retry"])


if __name__ == "__main__":
    unittest.main()
