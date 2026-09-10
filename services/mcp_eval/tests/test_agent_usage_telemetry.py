import unittest
from unittest.mock import patch

from litellm.types.utils import Message as LiteLLMMessage

from mcp_completion import agent_eval
from mcp_completion.llm import LLMResponse, _normalized_usage
from mcp_completion.schema import AssistantMessage


class UsageNormalizationTests(unittest.TestCase):
    def test_cached_tokens_are_preserved(self):
        self.assertEqual(
            _normalized_usage({
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 625},
            }),
            {
                "input_tokens": 1000,
                "output_tokens": 50,
                "cached_tokens": 625,
                "cache_reported": True,
            },
        )

    def test_absent_cache_detail_remains_unknown(self):
        self.assertEqual(
            _normalized_usage({"prompt_tokens": 100, "completion_tokens": 5}),
            {
                "input_tokens": 100,
                "output_tokens": 5,
                "cached_tokens": 0,
                "cache_reported": False,
            },
        )


class AgentUsageAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_telemetry_is_returned_after_messages(self):
        class FakeClient:
            async def list_tools(self):
                return []

        async def fake_completion(**kwargs):
            return LLMResponse(
                message=AssistantMessage(
                    role="assistant",
                    content="done",
                    original_message=LiteLLMMessage(role="assistant", content="done"),
                    tool_calls=None,
                ),
                usage={
                    "input_tokens": 120,
                    "output_tokens": 8,
                    "cached_tokens": 60,
                    "cache_reported": True,
                },
            )

        with (
            patch.object(agent_eval, "create_completion", fake_completion),
            patch.object(agent_eval, "_transform_tool_calls", lambda tools: []),
        ):
            outputs = [
                item async for item in agent_eval.run_mcp_eval(
                    mcp_client=FakeClient(), model="model", messages=[],
                    max_turns=2, max_tool_calls=2, include_telemetry=True,
                )
            ]
        self.assertEqual([item.type for item in outputs], ["message", "telemetry"])
        self.assertEqual(outputs[-1].data["usage"]["cached_tokens"], 60)

    async def test_default_response_shape_has_no_telemetry_event(self):
        class FakeClient:
            async def list_tools(self):
                return []

        async def fake_completion(**kwargs):
            return LLMResponse(
                message=AssistantMessage(
                    role="assistant", content="done",
                    original_message=LiteLLMMessage(role="assistant", content="done"),
                    tool_calls=None,
                ),
                usage={"input_tokens": 1, "output_tokens": 1, "cached_tokens": 0,
                       "cache_reported": True},
            )

        with (
            patch.object(agent_eval, "create_completion", fake_completion),
            patch.object(agent_eval, "_transform_tool_calls", lambda tools: []),
        ):
            outputs = [
                item async for item in agent_eval.run_mcp_eval(
                    mcp_client=FakeClient(), model="model", messages=[],
                    max_turns=2, max_tool_calls=2,
                )
            ]
        self.assertEqual([item.type for item in outputs], ["message"])
