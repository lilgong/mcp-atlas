import asyncio
import os
import threading
import json
import tempfile

import pytest

from mcp_completion import pangu_completion


def test_pangu_executor_matches_completion_concurrency():
    expected = int(os.getenv("MCP_COMPLETION_CONCURRENCY", "30"))

    assert pangu_completion.MCP_COMPLETION_CONCURRENCY == expected
    assert pangu_completion._PANGU_EXECUTOR._max_workers == expected


def test_generate_pangu_async_uses_dedicated_executor(monkeypatch):
    def fake_generate_pangu(*args, **kwargs):
        return threading.current_thread().name

    monkeypatch.setattr(pangu_completion, "generate_pangu", fake_generate_pangu)

    thread_name = asyncio.run(
        pangu_completion.generate_pangu_async("pangu/test", [], [])
    )

    assert thread_name.startswith("pangu-request")


class FakeStreamResponse:
    def __init__(self, payloads):
        self.payloads = payloads

    def iter_lines(self, decode_unicode=True):
        assert decode_unicode
        for payload in self.payloads:
            if payload == "[DONE]":
                yield "data: [DONE]"
            else:
                yield "data: " + json.dumps(payload)


def test_pangu_stream_reconstructs_reasoning_and_tool_calls():
    response = FakeStreamResponse(
        [
            {
                "id": "response-1",
                "model": "pangu-test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "reasoning_content": "find ",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q"',
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "reasoning_content": "evidence",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ':"x"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                },
            },
            "[DONE]",
        ]
    )

    result = pangu_completion._collect_pangu_stream(response)
    message = result["choices"][0]["message"]

    assert message["reasoning_content"] == "find evidence"
    assert message["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"q":"x"}'},
        }
    ]
    assert result["usage"]["total_tokens"] == 13


def test_pangu_stream_rejects_disconnect_before_finish():
    response = FakeStreamResponse(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "partial"},
                        "finish_reason": None,
                    }
                ]
            }
        ]
    )

    with pytest.raises(RuntimeError, match="before a finish marker"):
        pangu_completion._collect_pangu_stream(response)


class FakeJSONResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "tool_calls": None,
                    },
                    "finish_reason": "stop",
                }
            ]
        }


@pytest.mark.parametrize("enabled", [False, True])
def test_pangu_stream_switch_preserves_original_non_stream_request(
    monkeypatch, enabled
):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeJSONResponse()

    monkeypatch.setenv("LLM_STREAMING_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(pangu_completion.requests, "post", fake_post)
    monkeypatch.setattr(
        pangu_completion,
        "get_pangu_log_path",
        lambda: tempfile.mktemp(suffix=".jsonl"),
    )

    pangu_completion.generate_pangu("pangu/test", [], [])

    if enabled:
        assert captured["stream"] is True
        assert captured["json"]["stream"] is True
        assert captured["json"]["stream_options"] == {"include_usage": True}
    else:
        assert "stream" not in captured
        assert "stream" not in captured["json"]
        assert "stream_options" not in captured["json"]


def test_pangu_non_stream_json_decode_failure_keeps_original_retry_boundary(
    monkeypatch,
):
    calls = 0

    class MalformedJSONResponse(FakeJSONResponse):
        def json(self):
            raise ValueError("malformed JSON")

    def fake_post(url, **kwargs):
        nonlocal calls
        calls += 1
        return MalformedJSONResponse()

    monkeypatch.setenv("LLM_STREAMING_ENABLED", "false")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setattr(pangu_completion.requests, "post", fake_post)

    with pytest.raises(ValueError, match="malformed JSON"):
        pangu_completion.generate_pangu("pangu/test", [], [])

    assert calls == 1
