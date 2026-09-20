import asyncio

import pytest
from pydantic import ValidationError

from mcp_completion.main import capabilities
from mcp_completion.schema import RunAgentAPIRequestBody


def test_synthesis_capabilities_are_explicit_and_non_secret():
    result = asyncio.run(capabilities())

    assert result["schema_version"] == 1
    assert result["service"] == "mcp-atlas-rollout"
    assert result["run_agent"]["endpoint"] == "/v2/mcp_eval/run_agent"
    assert result["run_agent"]["request_schema_version"] == 1
    assert result["run_agent"]["response_event_schema_version"] == 1
    assert set(result["run_agent"]["features"]) >= {
        "cancellable_evaluation",
        "enabled_tools",
        "execution_limits",
        "extra_body",
        "prompt_cache_key",
        "structured_failure",
        "usage_telemetry",
    }
    assert set(result["runtime"]["features"]) >= {
        "fixture_identity",
        "generation_safety_policy",
        "routed_tool_catalog",
        "tool_policy",
    }
    assert result["runtime"]["tool_catalog_endpoint"] == "/v2/mcp_eval/tool-catalog"
    assert "api_key" not in str(result).lower()
    assert "credential_env" not in str(result).lower()


def test_run_agent_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RunAgentAPIRequestBody.model_validate({
            "model": "teacher",
            "messages": [{"role": "user", "content": "question"}],
            "enabledTools": [],
            "enableThinkingTokens": True,
        })
