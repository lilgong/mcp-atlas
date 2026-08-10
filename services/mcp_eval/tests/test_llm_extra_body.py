import unittest
from unittest.mock import AsyncMock, patch

from mcp_completion.llm import create_completion
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


if __name__ == "__main__":
    unittest.main()
