import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from build_atlas_runtime import stage_build_context  # noqa: E402


class RuntimeRouterStagingTests(unittest.TestCase):
    def test_runtime_context_contains_direct_router(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            context = Path(raw)
            stage_build_context(context)

            router = (
                context
                / "src"
                / "agent_environment"
                / "mcp_router.py"
            )
            dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertTrue(router.is_file())
            self.assertIn("mcp_router.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
