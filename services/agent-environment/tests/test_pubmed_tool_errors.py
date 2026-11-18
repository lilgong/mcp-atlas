"""Failures must cross the MCP boundary as errors in both direct and Hub mode."""

import asyncio
import json

import pytest
from mcp import types

from agent_environment import pubmed_mcp_compat as pubmed


CASES = [
    ("search_pubmed_key_words", "search_key_words", {"key_words": "vaccine"},
     [{"PMID": "33473101", "Title": "Vaccine study"}]),
    ("search_pubmed_advanced", "search_advanced", {"term": "vaccine"},
     [{"PMID": "35970920", "Title": "Trial results"}]),
    ("get_pubmed_article_metadata", "get_metadata", {"pmid": "33473101"},
     {"PMID": "33473101", "Title": "Vaccine study"}),
    ("download_pubmed_pdf", "download_pdf", {"pmid": "33473101"},
     "PDF has been downloaded as /data/article.pdf"),
]


def call(tool, arguments):
    handler = pubmed.mcp._mcp_server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call", params={"name": tool, "arguments": arguments}
    )
    return asyncio.run(handler(request)).root


@pytest.mark.parametrize("tool,implementation,arguments,payload", CASES)
@pytest.mark.parametrize("message", [
    "PubMed relay request failed: 504 Gateway Timeout",
    "NCBI returned HTTP 500 after relay retries",
    "IPWO_PROXY_AUTH_FAILED: proxy credential rejected",
])
def test_execution_failure_is_a_protocol_error(
    monkeypatch, tool, implementation, arguments, payload, message
):
    def fail(*args, **kwargs):
        raise pubmed.PubMedUpstreamError(message)

    monkeypatch.setattr(pubmed, implementation, fail)
    result = call(tool, arguments)
    assert result.isError is True
    assert message in "\n".join(part.text for part in result.content)


@pytest.mark.parametrize("tool,implementation,arguments,payload", CASES)
def test_successful_payload_is_preserved(
    monkeypatch, tool, implementation, arguments, payload
):
    monkeypatch.setattr(pubmed, implementation, lambda *args, **kwargs: payload)
    result = call(tool, arguments)
    assert result.isError is False
    text = "\n".join(part.text for part in result.content)
    if isinstance(payload, str):
        assert text == payload
    elif isinstance(payload, list):
        assert [json.loads(part.text) for part in result.content] == payload
    else:
        assert json.loads(text) == payload


def test_empty_search_is_not_a_protocol_error(monkeypatch):
    monkeypatch.setattr(pubmed, "search_key_words", lambda *args: [])
    assert call("search_pubmed_key_words", {"key_words": "no matches"}).isError is False
