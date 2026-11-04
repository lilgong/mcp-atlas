"""Exercise the baked completion entry point without starting model/MCP services."""
import ast
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failure", [False, True])
def test_completion_entrypoint_preserves_fatal_exit_and_checkpoint(tmp_path, failure):
    source = PROJECT / "mcp_completion_script.py"
    entry = ast.parse(source.read_text()).body[-1]
    checkpoint = tmp_path / "checkpoint.csv"
    prefix = f'''
import asyncio
import logging
from pathlib import Path
import sys
sys.path.insert(0, {str(PROJECT)!r})
from mcp_completion.account_guard import FatalAccountError
async def main():
    Path({str(checkpoint)!r}).write_text("TASK\\nt1\\n")
    if {failure!r}:
        raise FatalAccountError("credential expired", source_kind="mcp", source_name="brave-search")
'''
    result = subprocess.run(
        [sys.executable, "-c", prefix + ast.unparse(entry)],
        capture_output=True, text=True, timeout=10,
    )
    assert checkpoint.read_text() == "TASK\nt1\n"
    if failure:
        assert result.returncode != 0
        assert "FatalAccountError: credential expired" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
