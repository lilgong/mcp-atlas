"""Reasoning compatibility through HTTP, OpenAI SDK and LiteLLM filtering."""

import asyncio
import json
import os
from unittest.mock import patch

import httpx
import litellm
import pytest
from openai import AsyncOpenAI

from mcp_completion.llm import create_completion
from mcp_completion.schema import ToolCallOutputMessage, UserMessage


@pytest.fixture(autouse=True)
def isolate_reasoning_provider_state():
    with patch("mcp_completion.llm.choose_key", return_value="offline-test"), patch(
        "mcp_completion.llm.reserve_delay", return_value=0
    ):
        yield


def sse(deltas, finish_reason="stop"):
    chunks = []
    for i, delta in enumerate(deltas):
        chunks.append(
            {
                "id": "http-reasoning-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason if i == len(deltas) - 1 else None,
                    }
                ],
            }
        )
    return (
        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
@pytest.mark.parametrize("kind", ["answer", "tool", "reasoning_only"])
def test_http_reasoning_is_saved_and_returned_without_changing_content(field, kind):
    requests = []
    deltas = [
        {"role": "assistant", field: "check "},
        {field: "the source"},
    ]
    finish = "stop"
    if kind == "answer":
        deltas.extend([{"content": "final "}, {"content": "answer"}])
    elif kind == "tool":
        finish = "tool_calls"
        deltas.extend(
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"query"'},
                        }
                    ]
                },
                {"tool_calls": [{"index": 0, "function": {"arguments": ':"x"}'}}]},
            ]
        )
    # A reasoning-only terminal response must remain without visible content.
    # This compatibility fix must never promote reasoning into the answer.
    else:
        deltas.append({})

    async def run():
        async def handler(request):
            requests.append(json.loads(request.content))
            body = (
                sse(deltas, finish)
                if len(requests) == 1
                else sse([{"role": "assistant", "content": "done"}])
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=body
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = AsyncOpenAI(
                api_key="offline-test",
                base_url="http://offline.invalid/v1",
                http_client=http,
            )
            real_completion = litellm.acompletion

            async def sdk_completion(**kwargs):
                return await real_completion(**{**kwargs, "client": client})

            with (
                patch("mcp_completion.llm.litellm.acompletion", sdk_completion),
                patch("mcp_completion.llm.write_runtime_event"),
                patch("mcp_completion.llm._write_token_usage"),
                patch.dict(os.environ, {"LLM_STREAMING_ENABLED": "true"}),
            ):
                user = UserMessage(role="user", content="test")
                first = await create_completion(
                    model="test-model", messages=[user], tools=[]
                )
                assert first.message.reasoning_content == "check the source"
                assert first.message.model_dump()["reasoning_content"] == "check the source"
                assert first.message.content == ("final answer" if kind == "answer" else "")
                history = [user, first.message]
                if kind == "tool":
                    call = first.message.tool_calls[0]
                    assert call.function["name"] == "lookup"
                    assert call.function["arguments"] == '{"query":"x"}'
                    history.append(
                        ToolCallOutputMessage(
                            role="tool",
                            tool_call_id="call-1",
                            content=[{"type": "text", "text": "source result"}],
                        )
                    )
                await create_completion(model="test-model", messages=history, tools=[])

    asyncio.run(run())
    assert len(requests) == 2
    previous = requests[1]["messages"][1]
    assert previous["reasoning_content"] == "check the source"
    assert previous["content"] == ("final answer" if kind == "answer" else "")
    assert "original_message" not in previous
