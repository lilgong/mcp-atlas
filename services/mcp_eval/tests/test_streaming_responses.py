import asyncio

import pytest
from litellm.types.utils import ModelResponse

from mcp_completion.streaming import (
    IncompleteModelStream,
    collect_litellm_response,
    llm_streaming_enabled,
)


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
