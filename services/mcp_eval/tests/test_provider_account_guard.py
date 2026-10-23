import pytest

from mcp_completion.account_guard import (
    _FATAL_ACCOUNT_MARKERS,
    FatalAccountError,
    credential_envs_for_mcp_server,
    describe_fatal_account_error,
    is_fatal_account_error,
    is_fatal_mcp_account_error,
    is_fatal_tool_result,
)


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


def test_explicit_token_quota_shortfall_stops_the_run():
    body = (
        "litellm.APIError: APIError: OpenAIException - token quota is not enough, "
        "token remain quota: ＄0.000874, need quota: ＄0.020000"
    )
    assert is_fatal_account_error(body)


def test_normal_rate_limit_does_not_stop_the_run():
    body = "LLM completion failed: HTTP 429 rate limit exceeded"
    assert not is_fatal_account_error(body)


def test_lara_account_failure_names_yibu_and_official_credentials():
    assert credential_envs_for_mcp_server("lara-translate") == (
        "LARA_YIBU_API_KEY",
        "LARA_ACCESS_KEY_ID",
        "LARA_ACCESS_KEY_SECRET",
    )


def test_weather_account_failure_names_yibu_and_official_credentials():
    assert credential_envs_for_mcp_server("weather-data") == (
        "WEATHER_YIBU_API_KEY",
        "WEATHER_API_KEY",
    )


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


def test_exa_credit_limit_text_is_fatal():
    result = {
        "content": [{
            "type": "text",
            "text": (
                "web_search_exa error (402): You have exceeded your credits "
                "limit. Please top up to keep using Exa."
            ),
        }],
        # exa-mcp-server returns this provider failure in a normal MCP content
        # envelope, so the text prefix must carry the error shape.
        "is_error": False,
    }

    assert is_fatal_mcp_account_error("exa", result)


def test_e2b_missing_payment_method_inside_json_text_is_fatal():
    result = {
        "content": [{
            "type": "text",
            "text": (
                '{"detail":"Failed to call tool '
                "'e2b-server_run_code': 403: team is blocked: "
                'missing payment method"}'
            ),
        }],
        # e2b-server returns the upstream account failure inside a normal MCP
        # content envelope, so this cannot depend on is_error being true.
        "is_error": False,
    }

    assert is_fatal_account_error(result)
    assert is_fatal_mcp_account_error("e2b-server", result)
    assert not is_fatal_mcp_account_error("github", result)
    assert credential_envs_for_mcp_server("e2b-server") == ("E2B_API_KEY",)


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
        (
            "twelvedata",
            "TWELVEDATA_DAILY_CREDITS_EXHAUSTED: daily limit reached",
            "TWELVE_DATA_API_KEY",
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


def test_twelvedata_minute_rate_limit_does_not_stop_the_run():
    result = {
        "content": [{
            "type": "text",
            "text": "Error: 429 Too Many Requests: current minute limit",
        }],
        "is_error": True,
    }

    assert not is_fatal_mcp_account_error("twelvedata", result)


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


@pytest.mark.parametrize("window", ["Units per minute per user", "Requests per second"])
def test_time_window_quota_does_not_stop_account(window):
    from mcp_completion.account_guard import is_time_based_quota_error
    text = f"Error fetching emails: Quota exceeded for quota metric 'Total Query Cost' and limit '{window}' of service 'gmail.googleapis.com'"
    result = {"isError": True, "content": [{"type": "text", "text": text}]}
    assert is_time_based_quota_error(text)
    assert not is_fatal_account_error(text)
    assert not is_fatal_mcp_account_error("google-workspace", result)


@pytest.mark.parametrize("text", ["Quota exceeded: monthly quota", "Quota exceeded: daily credits exhausted", "Quota exceeded per minute; invalid credentials", "Quota exceeded per minute; invalid token"])
def test_permanent_quota_or_credentials_remain_fatal(text):
    assert is_fatal_account_error(text)


def test_gmail_minute_quota_is_a_rate_limited_tool_result():
    from mcp_completion.mcp_client.isolated_client import _is_rate_limited_tool_result
    result = {"isError": True, "content": [{"type": "text", "text": "Error fetching emails: Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user' of service 'gmail.googleapis.com'"}]}
    assert _is_rate_limited_tool_result(result)
    assert not is_fatal_mcp_account_error("google-workspace", result)
    result["isError"] = False
    result["content"][0]["text"] = "An email discussing quota exceeded per minute"
    assert not _is_rate_limited_tool_result(result)


@pytest.mark.parametrize("marker", [
    marker for marker in _FATAL_ACCOUNT_MARKERS
    if marker not in {"quota exceeded", "quota has been exceeded", "exceeded quota"}
])
def test_all_permanent_markers_override_time_window_quota(marker):
    from mcp_completion.account_guard import is_time_based_quota_error
    text = f"Error fetching emails: Quota exceeded: Units per minute per user; {marker}"
    result = {"isError": True, "content": [{"type": "text", "text": text}]}
    assert not is_time_based_quota_error(text)
    assert is_fatal_account_error(text)
    assert is_fatal_mcp_account_error("google-workspace", result)
