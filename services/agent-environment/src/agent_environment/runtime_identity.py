"""Non-secret identity exposed by rollout health endpoints."""

from __future__ import annotations

import os


def runtime_identity() -> dict[str, str]:
    identity = {
        "credential_set": os.getenv("MCP_ATLAS_CREDENTIAL_SET", ""),
        "fixture_fingerprint": os.getenv("MCP_ATLAS_FIXTURE_FINGERPRINT", ""),
        "runtime_commit": os.getenv("MCP_ATLAS_RUNTIME_COMMIT", ""),
        "launch_id": os.getenv("MCP_ATLAS_LAUNCH_ID", ""),
    }
    if os.getenv("MCP_TOOL_TRANSPORT", "direct").strip().lower() == "hub":
        identity["transport_digest"] = os.getenv("MCP_ATLAS_TRANSPORT_DIGEST", "")
    return identity
