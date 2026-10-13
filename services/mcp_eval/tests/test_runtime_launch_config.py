import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from run_shared_mcp import (  # noqa: E402
    configured_shared_port,
    configured_shared_hub_servers,
    validate_shared_bind_host,
)

from mcp_completion.config import validate_isolated_control_plane  # noqa: E402


class RuntimeLaunchConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {"MCP_TOOL_TRANSPORT": "direct"})
    def test_isolated_control_planes_require_loopback(self):
        validate_shared_bind_host("127.0.0.1")
        validate_isolated_control_plane("127.0.0.1", "http://localhost:2984")
        with self.assertRaisesRegex(ValueError, "MCP_SHARED_HOST"):
            validate_shared_bind_host("0.0.0.0")
        with self.assertRaisesRegex(ValueError, "HOST must be"):
            validate_isolated_control_plane("0.0.0.0", "http://localhost:2984")
        with self.assertRaisesRegex(ValueError, "MCP_SERVER_URL"):
            validate_isolated_control_plane("127.0.0.1", "http://192.168.0.10:2984")

    def test_hub_shared_selection_does_not_require_global_task_allowlist(self):
        with patch.dict(
            os.environ,
            {"MCP_TOOL_TRANSPORT": "hub", "MCPHUB_SERVERS": "slack, calculator"},
            clear=True,
        ):
            self.assertEqual("calculator,slack", configured_shared_hub_servers())
            self.assertNotIn("ENABLED_SERVERS", os.environ)
            os.environ["ENABLED_SERVERS"] = "slack,filesystem"
            self.assertEqual("slack", configured_shared_hub_servers())
            os.environ["ENABLED_SERVERS"] = "filesystem"
            with self.assertRaisesRegex(ValueError, "No Hub servers"):
                configured_shared_hub_servers()
            os.environ["MCP_TOOL_TRANSPORT"] = "direct"
            self.assertIsNone(configured_shared_hub_servers())

    def test_isolated_run_runtime_host_must_match_exactly(self):
        validate_isolated_control_plane(
            "127.0.0.1",
            "http://mcp-atlas-runtime-run-1:2984",
            "mcp-atlas-runtime-run-1",
        )
        with self.assertRaisesRegex(ValueError, "MCP_SERVER_URL"):
            validate_isolated_control_plane(
                "127.0.0.1",
                "http://other-runtime:2984",
                "mcp-atlas-runtime-run-1",
            )

    def test_shared_port_prefers_explicit_env(self):
        with patch.dict(
            os.environ,
            {
                "MCP_SHARED_PORT": "2984",
                "MCP_SERVER_URL": "http://localhost:1984",
            },
            clear=False,
        ):
            self.assertEqual(2984, configured_shared_port())

    def test_shared_port_falls_back_to_configured_url(self):
        with patch.dict(
            os.environ,
            {
                "MCP_SHARED_PORT": "",
                "MCP_SERVER_URL": "http://localhost:3984",
            },
            clear=False,
        ):
            self.assertEqual(3984, configured_shared_port())


if __name__ == "__main__":
    unittest.main()
