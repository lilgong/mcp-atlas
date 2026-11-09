"""Preserve explicitly empty reasoning during tool-call continuation."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from litellm.types.utils import ModelResponse

from mcp_completion.llm import _reasoning_and_content, create_completion
from mcp_completion.schema import ToolCallOutputMessage, UserMessage


@pytest.mark.parametrize("as_dict", [True, False])
@pytest.mark.parametrize(
    "canonical,alias,expected",
    [
        ("", None, ""),
        ("", "", ""),
        ("", "alias thought", "alias thought"),
        ("canonical thought", "alias thought", "canonical thought"),
        (None, "", ""),
        (None, None, None),
        (None, "alias thought", "alias thought"),
    ],
)
def test_reasoning_presence_survives_normalization(as_dict, canonical, alias, expected):
    message = {"role": "assistant", "content": "unchanged answer"}
    if canonical is not None:
        message["reasoning_content"] = canonical
    if alias is not None:
        message["reasoning"] = alias
    response = {"choices": [{"message": message}]}
    if not as_dict:
        response = ModelResponse(**response)
    assert _reasoning_and_content(response) == (expected, "unchanged answer")


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("as_dict", [True, False])
def test_empty_thinking_tool_turn_is_sent_back_on_next_request(streaming, as_dict):
    tool_call = {
        "id": "call-one",
        "type": "function",
        "function": {"name": "lookup", "arguments": "{}"},
    }
    first = {
        "choices": [{"message": {
            "role": "assistant",
            "content": None,
            "reasoning_content": "",
            "tool_calls": [tool_call],
        }}],
    }
    second = {"choices": [{"message": {
        "role": "assistant", "content": "done", "reasoning_content": "verified",
    }}]}
    if streaming:
        async def stream():
            yield ModelResponse(stream=True, model="offline-model", choices=[{
                "index": 0,
                "delta": {"role": "assistant", "reasoning_content": ""},
                "finish_reason": None,
            }])
            yield ModelResponse(stream=True, model="offline-model", choices=[{
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, **tool_call}]},
                "finish_reason": "tool_calls",
            }])
        first = stream()
    elif not as_dict:
        first = ModelResponse(**first)

    async def reply(**kwargs):
        if provider.await_count == 1:
            return first
        previous = kwargs["messages"][1]
        # A strict provider rejects an omitted reasoning_content on this tool turn.
        assert previous["reasoning_content"] == ""
        assert previous["content"] == ""
        assert previous["tool_calls"][0]["function"]["arguments"] == "{}"
        assert "original_message" not in previous
        return second if as_dict else ModelResponse(**second)

    provider = AsyncMock(side_effect=reply)

    async def scenario():
        messages = [UserMessage(role="user", content="Look it up.")]
        result = await create_completion(
            model="offline-model", messages=messages, tools=[],
            extra_body={"thinking": {"type": "enabled"}},
        )
        assert result.message.reasoning_content == ""
        messages += [result.message, ToolCallOutputMessage(
            role="tool", tool_call_id="call-one",
            content=[{"type": "text", "text": "found"}],
        )]
        final = await create_completion(
            model="offline-model", messages=messages, tools=[],
            extra_body={"thinking": {"type": "enabled"}},
        )
        assert final.message.content == "done"
        assert final.message.reasoning_content == "verified"

    with (
        patch("mcp_completion.llm.litellm.acompletion", provider),
        patch("mcp_completion.llm.write_runtime_event"),
        patch("mcp_completion.llm._write_token_usage"),
        patch.dict("os.environ", {"LLM_STREAMING_ENABLED": str(streaming).lower()}),
    ):
        asyncio.run(scenario())
    assert provider.await_count == 2
