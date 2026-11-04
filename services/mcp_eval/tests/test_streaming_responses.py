import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from litellm.types.utils import ModelResponse

from mcp_completion.llm import create_completion
from mcp_completion.schema import ToolCallOutputMessage, UserMessage

from mcp_completion.streaming import (
    IncompleteModelStream,
    collect_litellm_response,
    llm_streaming_enabled,
)


@pytest.fixture(autouse=True)
def isolate_reasoning_provider_state():
    # The rollout fork has a shared credential governor absent in main.
    # Compatibility tests must never read real keys or their persisted state.
    with patch("mcp_completion.llm.choose_key", return_value="offline-test"), patch(
        "mcp_completion.llm.reserve_delay", return_value=0
    ):
        yield


class AsyncChunks:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def iterate():
            for chunk in self.chunks:
                await asyncio.sleep(0)
                yield chunk

        return iterate()


def chunk(delta, finish_reason=None, usage=None):
    response = ModelResponse(
        model="stream-test",
        stream=True,
        choices=[
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    )
    if usage is not None:
        response.usage = usage
    return response


def test_streaming_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LLM_STREAMING_ENABLED", raising=False)

    assert llm_streaming_enabled() is True


@pytest.mark.parametrize("arguments", ['{"command": "find", "arguments": ', '{"pipeline": ', '{"a": {"b": 1}'])
def test_invalid_streamed_arguments_are_preserved_without_repair(arguments):
    response = AsyncChunks([
        chunk({"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                               "function": {"name": "lookup"}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]},
              finish_reason="tool_calls"),
    ])
    completion = AsyncMock(return_value=response)
    with (
        patch("mcp_completion.llm.litellm.acompletion", completion),
        patch("mcp_completion.llm.write_runtime_event"),
        patch("mcp_completion.llm._write_token_usage"),
        patch.dict(os.environ, {"LLM_STREAMING_ENABLED": "true"}),
    ):
        result = asyncio.run(create_completion(model="test-model", messages=[], tools=[]))
    assert result.message.tool_calls[0].function["arguments"] == arguments
    assert completion.await_count == 1


def test_stream_reconstructs_reasoning_content_and_split_tool_arguments():
    streamed = AsyncChunks(
        [
            chunk({"role": "assistant", "reasoning_content": "check "}),
            chunk(
                {
                    "reasoning_content": "the source",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"query"',
                            },
                        },
                        {
                            "index": 1,
                            "id": "call-2",
                            "type": "function",
                            "function": {
                                "name": "calculate",
                                "arguments": '{"value"',
                            },
                        },
                    ],
                }
            ),
            chunk(
                {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": ':"x"}'}},
                        {"index": 1, "function": {"arguments": ":2}"}},
                    ]
                },
                finish_reason="tool_calls",
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            ),
        ]
    )

    response = asyncio.run(
        collect_litellm_response(
            streamed,
            messages=[{"role": "user", "content": "test"}],
        )
    )
    message = response.choices[0].message

    assert message.reasoning_content == "check the source"
    assert [call.function.name for call in message.tool_calls] == [
        "lookup",
        "calculate",
    ]
    assert [call.function.arguments for call in message.tool_calls] == [
        '{"query":"x"}',
        '{"value":2}',
    ]
    assert response.usage.total_tokens == 15


def test_empty_stream_is_rejected_instead_of_becoming_empty_answer():
    with pytest.raises(IncompleteModelStream, match="no chunks"):
        asyncio.run(collect_litellm_response(AsyncChunks([]), messages=[]))


def test_disconnected_partial_stream_is_rejected():
    streamed = AsyncChunks([chunk({"content": "partial"})])

    with pytest.raises(IncompleteModelStream, match="finish reason"):
        asyncio.run(collect_litellm_response(streamed, messages=[]))


def test_non_stream_provider_response_remains_compatible():
    response = ModelResponse(
        model="fallback",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "complete"},
                "finish_reason": "stop",
            }
        ],
    )

    assert asyncio.run(collect_litellm_response(response, messages=[])) is response


def test_reasoning_alias_is_combined_without_duplicating_canonical_deltas():
    streamed = AsyncChunks(
        [
            chunk({"reasoning": "check "}),
            chunk({"reasoning": "the ", "reasoning_content": "the "}),
            chunk({"reasoning": "source", "reasoning_content": ""}),
            chunk({"content": "answer"}, finish_reason="stop"),
        ]
    )
    response = asyncio.run(collect_litellm_response(streamed, messages=[]))

    assert response.choices[0].message.reasoning_content == "check the source"
    assert response.choices[0].message.content == "answer"


@pytest.mark.parametrize("as_dict", [False, True])
def test_non_stream_reasoning_alias_is_preserved(as_dict):
    response = ModelResponse(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning": "check the source",
                },
                "finish_reason": "stop",
            }
        ]
    )
    if as_dict:
        response = response.model_dump()
    complete = asyncio.run(collect_litellm_response(response, messages=[]))
    message = complete["choices"][0]["message"] if as_dict else complete.choices[0].message
    reasoning = message["reasoning_content"] if as_dict else message.reasoning_content

    assert complete is response
    assert reasoning == "check the source"


@pytest.mark.parametrize("streaming", [True, False])
def test_alias_reasoning_survives_tool_turn_and_next_model_request(streaming):
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"query":"x"}'},
    }
    if streaming:
        first_response = AsyncChunks(
            [
                chunk({"reasoning": "check the source"}),
                chunk(
                    {"tool_calls": [{"index": 0, **tool_call}]},
                    finish_reason="tool_calls",
                ),
            ]
        )
    else:
        first_response = ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "reasoning": "check the source",
                        "tool_calls": [tool_call],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        )
    final_response = ModelResponse(
        choices=[
            {
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": "stop",
            }
        ]
    )
    completion = AsyncMock(side_effect=[first_response, final_response])

    async def run():
        user = UserMessage(role="user", content="test")
        first = await create_completion(model="test-model", messages=[user], tools=[])
        assert first.message.reasoning_content == "check the source"
        assert first.message.model_dump()["reasoning_content"] == "check the source"
        await create_completion(
            model="test-model",
            messages=[
                user,
                first.message,
                ToolCallOutputMessage(
                    role="tool",
                    tool_call_id="call-1",
                    content=[{"type": "text", "text": "source result"}],
                ),
            ],
            tools=[],
        )

    with (
        patch("mcp_completion.llm.litellm.acompletion", completion),
        patch("mcp_completion.llm.write_runtime_event"),
        patch("mcp_completion.llm._write_token_usage"),
        patch.dict(os.environ, {"LLM_STREAMING_ENABLED": str(streaming).lower()}),
    ):
        asyncio.run(run())

    previous = completion.await_args_list[1].kwargs["messages"][1]
    assert previous["reasoning_content"] == "check the source"
    assert previous["tool_calls"][0]["function"]["arguments"] == '{"query":"x"}'
    assert "original_message" not in previous
