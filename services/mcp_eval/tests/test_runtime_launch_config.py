import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from run_shared_mcp import configured_shared_port  # noqa: E402


class RuntimeLaunchConfigTests(unittest.TestCase):
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
