import pytest

from mcp_completion.account_guard import (
    FatalAccountError,
    credential_envs_for_mcp_server,
    describe_fatal_account_error,
    is_fatal_account_error,
    is_fatal_mcp_account_error,
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


def test_context7_monthly_quota_text_is_fatal():
    result = {
        "content": [{
            "type": "text",
            "text": "Monthly quota reached. Upgrade to Context7 Pro.",
        }],
        "is_error": False,
    }

    assert is_fatal_mcp_account_error("context7", result)


@pytest.mark.parametrize(
    ("server", "text"),
    [
        (
            "pubmed",
            '{"error":"Search failed: IPWO_PROXY_AUTH_FAILED: '
            'proxy credential rejected"}',
        ),
        (
            "wikipedia",
            '{"title":"Example","summary":"Error generating summary: '
            'IPWO_PROXY_AUTH_FAILED: proxy credential rejected"}',
        ),
    ],
)
def test_ipwo_marker_inside_json_text_is_fatal(server, text):
    result = {
        "content": [{"type": "text", "text": text}],
        "is_error": False,
    }

    assert is_fatal_mcp_account_error(server, result)


def test_git_parser_token_error_cannot_stop_the_run():
    result = {
        "content": [{
            "type": "text",
            "text": '{"detail":"Tool git_git_diff execution failed: '
            'Invalid token: \'.\'"}',
        }],
        "is_error": True,
    }

    assert is_fatal_tool_result(result)
    assert not is_fatal_mcp_account_error("git", result)


@pytest.mark.parametrize(
    "server",
    [
        "arxiv",
        "calculator",
        "cli-mcp-server",
        "clinicaltrialsgov-mcp-server",
        "ddg-search",
        "desktop-commander",
        "fetch",
        "filesystem",
        "git",
        "mcp-code-executor",
        "mcp-server-code-runner",
        "memory",
        "met-museum",
        "mongodb",
        "open-library",
        "osm-mcp-server",
        "weather",
        "whois",
    ],
)
def test_credential_free_servers_cannot_raise_account_failure(server):
    result = {
        "content": [{"type": "text", "text": "Error: invalid token"}],
        "is_error": True,
    }

    assert credential_envs_for_mcp_server(server) == ()
    assert not is_fatal_mcp_account_error(server, result)


@pytest.mark.parametrize(
    ("server", "message", "credential_env"),
    [
        (
            "brave-search",
            "This token has no access to model brave-web-search",
            "BRAVE_API_KEY",
        ),
        (
            "exa",
            "This token has no access to model exa-search",
            "EXA_API_KEY",
        ),
        (
            "oxylabs",
            "This token has no access to model oxylabs-scraper",
            "OXYLABS_PASSWORD",
        ),
        (
            "pubmed",
            "IPWO_PROXY_AUTH_FAILED: proxy credential rejected",
            "IPWO_PROXY_PASSWORD",
        ),
        (
            "wikipedia",
            "IPWO_PROXY_AUTH_FAILED: proxy credential rejected",
            "IPWO_PROXY_PASSWORD",
        ),
        ("github", "Bad credentials", "GITHUB_TOKEN"),
    ],
)
def test_credentialed_mcp_account_failures_stop_the_run(
    server, message, credential_env
):
    result = {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }

    assert credential_env in credential_envs_for_mcp_server(server)
    assert is_fatal_mcp_account_error(server, result)


def test_normal_search_text_is_not_treated_as_account_failure():
    result = {
        "content": [{
            "type": "text",
            "text": "Article title: What does invalid token mean?",
        }],
        "is_error": False,
    }
    assert not is_fatal_tool_result(result)


def test_relay_servers_identify_ipwo_proxy_credentials():
    assert credential_envs_for_mcp_server("pubmed") == (
        "IPWO_PROXY_USERNAME",
        "IPWO_PROXY_PASSWORD",
    )
    assert credential_envs_for_mcp_server("wikipedia") == (
        "IPWO_PROXY_USERNAME",
        "IPWO_PROXY_PASSWORD",
    )


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
    monkeypatch.setenv("PANGU_API_URL", "https://example.invalid/v1")
    monkeypatch.setattr(pangu_completion.requests, "post", lambda *a, **k: Response())

    with pytest.raises(FatalAccountError) as raised:
        pangu_completion.generate_pangu("pangu/test-model", [], [])

    assert raised.value.source_name == "pangu/test-model"
    assert raised.value.credential_envs == ("PANGU_API_KEY",)
    assert "secret-test-value" not in describe_fatal_account_error(raised.value)
