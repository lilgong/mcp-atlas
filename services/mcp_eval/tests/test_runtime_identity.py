from unittest.mock import patch

from mcp_completion.runtime_identity import runtime_identity


def test_runtime_identity_contains_only_non_secret_provenance():
    with patch.dict("os.environ", {
        "MCP_ATLAS_CREDENTIAL_SET": "fixture2",
        "MCP_ATLAS_FIXTURE_FINGERPRINT": "fixture2-v1",
        "MCP_ATLAS_RUNTIME_COMMIT": "abc123",
        "MCP_ATLAS_LAUNCH_ID": "launch-1",
        "LLM_API_KEY": "must-not-leak",
    }, clear=False):
        identity = runtime_identity()
    assert identity == {
        "credential_set": "fixture2",
        "fixture_fingerprint": "fixture2-v1",
        "runtime_commit": "abc123",
        "launch_id": "launch-1",
    }
    assert "must-not-leak" not in str(identity)
