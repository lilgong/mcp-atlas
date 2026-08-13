import asyncio
import os
import threading

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
