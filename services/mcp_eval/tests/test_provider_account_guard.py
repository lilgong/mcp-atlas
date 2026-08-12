import pytest

from mcp_completion.account_guard import (
    FatalAccountError,
    credential_envs_for_mcp_server,
    describe_fatal_account_error,
    is_fatal_account_error,
    is_fatal_tool_result,
)
from mcp_completion import pangu_completion


def test_invalid_model_token_stops_the_run():
    body = (
        '{"detail":{"error":"Unknown error during mcp_eval: '
        "LLM completion failed: litellm.AuthenticationError: "
        'OpenAIException - Invalid token"}}'
    )
    assert is_fatal_account_error(body)


def test_insufficient_model_balance_stops_the_run():
    body = "LLM completion failed: provider reports insufficient balance"
    assert is_fatal_account_error(body)


def test_normal_rate_limit_does_not_stop_the_run():
    body = "LLM completion failed: HTTP 429 rate limit exceeded"
    assert not is_fatal_account_error(body)


def test_mcp_tool_billing_error_stops_the_run():
    body = "MCP tool failed: insufficient balance"
    assert is_fatal_account_error(body)


def test_transient_gateway_error_is_not_fatal():
    assert not is_fatal_account_error("HTTP 504 upstream temporarily unavailable")


def test_mcp_error_text_envelope_is_fatal():
    result = {
        "content": [{"type": "text", "text": "Error: quota exceeded"}],
        "is_error": False,
    }
    assert is_fatal_tool_result(result)


def test_mcp_plain_invalid_key_result_is_fatal():
    result = {
        "content": [{"type": "text", "text": "Invalid API key"}],
        "is_error": False,
    }
    assert is_fatal_tool_result(result)


def test_normal_search_text_is_not_treated_as_account_failure():
    result = {
        "content": [{
            "type": "text",
            "text": "Article title: What does invalid token mean?",
        }],
        "is_error": False,
    }
    assert not is_fatal_tool_result(result)


def test_fatal_account_description_names_source_and_env_without_key_value():
    error = FatalAccountError(
        "MCP credential is invalid or out of funds",
        source_kind="mcp",
        source_name="brave-search",
        credential_envs=credential_envs_for_mcp_server("brave-search"),
    )

    description = describe_fatal_account_error(error)
    assert "source=mcp" in description
    assert "name=brave-search" in description
    assert "credential_env=BRAVE_API_KEY" in description


def test_pangu_billing_response_identifies_the_active_key(monkeypatch):
    class Response:
        status_code = 402
        text = "insufficient balance"

    monkeypatch.setenv("PANGU_API_KEY", "secret-test-value")
    monkeypatch.setattr(pangu_completion.requests, "post", lambda *a, **k: Response())

    with pytest.raises(FatalAccountError) as raised:
        pangu_completion.generate_pangu("pangu/test-model", [], [])

    assert raised.value.source_name == "pangu/test-model"
    assert raised.value.credential_envs == ("PANGU_API_KEY",)
    assert "secret-test-value" not in describe_fatal_account_error(raised.value)
