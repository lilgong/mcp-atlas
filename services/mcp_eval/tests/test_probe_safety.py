import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch

from mcp_server_probe import (
    FAIL,
    OK,
    Result,
    _result_is_empty,
    _tool_errored,
    _write_results,
    run_smoke,
)
from test_servers import TEST_CALLS


def test_fixture_safe_representative_calls_are_pinned():
    assert TEST_CALLS["notion"] == (
        "notion_API-post-search", {"query": "", "page_size": 1},
    )
    assert TEST_CALLS["memory"] == ("memory_read_graph", {})
    assert TEST_CALLS["notion"][0] not in {
        "notion_API-get-user", "notion_API-get-users",
    }


def test_structured_errors_do_not_pass_success_envelopes():
    assert _tool_errored(json.dumps([{
        "type": "text", "text": json.dumps({"statusCode": 403, "message": "denied"}),
    }]))
    assert _tool_errored(json.dumps([{"type": "text", "text": "Error: denied"}]))
    assert not _tool_errored(json.dumps([{
        "type": "text", "text": "The measured error rate is 0.01%.",
    }]))


def test_empty_search_envelope_is_not_usable():
    assert _result_is_empty(json.dumps([{
        "type": "text", "text": json.dumps({"query": "x", "results": []}),
    }]))
    assert not _result_is_empty(json.dumps([{
        "type": "text", "text": json.dumps({"results": [{"id": "1"}]}),
    }]))


def test_sensitive_probe_is_refused_before_call():
    called = False

    async def call(_tool, _args):
        nonlocal called
        called = True
        return "[]"

    result = asyncio.run(run_smoke(call, "notion", "notion_API-get-users", {}))
    assert result.status == FAIL
    assert called is False


def test_json_report_is_restricted_and_redacted(tmp_path: Path):
    output = tmp_path / "probe.json"
    with patch.dict(os.environ, {"NOTION_TOKEN": "secret-value"}):
        _write_results(
            output,
            [Result("notion", "smoke", OK, 0.1, "token=secret-value", "tool")],
            mode="isolated",
        )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["summary"] == {"api_fail": 0, "data_bad": 0, "ok": 1, "total": 1}
    assert payload["results"][0]["detail"] == "token=<redacted>"
    assert output.stat().st_mode & 0o777 == 0o600
