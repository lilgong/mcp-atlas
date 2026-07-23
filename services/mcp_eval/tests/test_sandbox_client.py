import unittest

from mcp_completion.errors import MCPClientToolExecutionError
from mcp_completion.mcp_client.sandbox_client import SandboxMCPClient


class SandboxClientAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_time_allowlist_blocks_before_http(self):
        client = SandboxMCPClient(
            "http://127.0.0.1:1",
            enabled_tools=["filesystem_read_text_file"],
        )
        with self.assertRaises(MCPClientToolExecutionError):
            await client.call_tool(
                "filesystem_write_file",
                {"path": "/data/x", "content": "x"},
            )


if __name__ == "__main__":
    unittest.main()
