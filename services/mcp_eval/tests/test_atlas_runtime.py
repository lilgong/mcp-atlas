import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from build_atlas_runtime import stage_build_context  # noqa: E402
from prepare_task_data_fixture import prepare_fixture  # noqa: E402
from mcp_completion.task_data import (  # noqa: E402
    FIXTURE_MANIFEST,
    load_fixture,
    prepare_task_workspace,
)
from mcp_completion.task_sandbox import (  # noqa: E402
    TaskSandboxError,
    inspect_runtime_image,
)


class AtlasRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_image_contract_is_fixture_free(self):
        metadata = (
            f"sha256:{'a' * 64}\n"
            '{"mcp-atlas.runtime":"true",'
            '"mcp-atlas.runtime-version":"20260724",'
            '"mcp-atlas.data-contract":"external-data-v1",'
            '"mcp-atlas.contains-fixture":"false"}\n'
            '{"/data":{}}'
        )
        with patch(
            "mcp_completion.task_sandbox._run",
            new=AsyncMock(return_value=(metadata, "", 0)),
        ):
            runtime = await inspect_runtime_image(
                "mcp-atlas-runtime:20260724"
            )
        self.assertEqual("20260724", runtime["runtime_version"])

        old_image = (
            f"sha256:{'b' * 64}\n"
            '{"mcp-atlas.runtime":"false"}\n{}'
        )
        with patch(
            "mcp_completion.task_sandbox._run",
            new=AsyncMock(return_value=(old_image, "", 0)),
        ):
            with self.assertRaisesRegex(TaskSandboxError, "fixture-free"):
                await inspect_runtime_image("agent-environment:latest")

    def test_build_context_contains_runtime_but_no_fixture_data(self):
        with tempfile.TemporaryDirectory() as raw:
            context = Path(raw)
            stage_build_context(context)
            paths = {
                path.relative_to(context).as_posix()
                for path in context.rglob("*")
            }
            self.assertNotIn("data", paths)
            self.assertFalse(any(path.startswith("data/") for path in paths))
            self.assertIn(
                "vendor/yibu-patched/"
                "node_modules/@modelcontextprotocol/"
                "server-brave-search/dist/index.js",
                paths,
            )
            template = (
                context
                / "src/agent_environment/mcp_server_template.json"
            ).read_text(encoding="utf-8")
            self.assertIn("/opt/mcp-code-venv", template)
            self.assertNotIn(
                "/data/repos/mcp_code_executor_workspace/.venv",
                template,
            )

    def test_fixture_is_content_addressed_and_copied_per_task(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "fixture"
            source.mkdir()
            (source / "sample.txt").write_text("original\n", encoding="utf-8")
            ignored = source / "repos/workspace/.venv"
            ignored.mkdir(parents=True)
            (ignored / "secret.bin").write_bytes(b"not copied")
            prepare_fixture(source, output, "unit-fixture")

            fixture = load_fixture(output)
            self.assertEqual("unit-fixture", fixture.fixture_id)
            self.assertFalse((output / "repos/workspace/.venv").exists())
            self.assertTrue((output / FIXTURE_MANIFEST).is_file())
            source_mode = (output / "sample.txt").stat().st_mode

            with patch.dict(
                "os.environ",
                {"MCP_TASK_WORKSPACE_ROOT": str(root / "workspaces")},
                clear=False,
            ):
                data_dir, copied, repos = prepare_task_workspace(
                    source_dir=output,
                    task_id="task-one",
                    include_git=False,
                )
            self.assertEqual([], repos)
            self.assertEqual(
                fixture.content_sha256,
                copied.content_sha256,
            )
            (data_dir / "sample.txt").write_text("mutated\n", encoding="utf-8")
            self.assertEqual(
                "original\n",
                (output / "sample.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                source_mode,
                (output / "sample.txt").stat().st_mode,
            )
            self.assertTrue(data_dir.stat().st_mode & 0o002)
            self.assertTrue((data_dir / "sample.txt").stat().st_mode & 0o002)

    def test_fixture_digest_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "fixture"
            source.mkdir()
            (source / "sample.txt").write_text("original\n", encoding="utf-8")
            prepare_fixture(source, output, "unit-fixture")
            (output / "sample.txt").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                load_fixture(output)


if __name__ == "__main__":
    unittest.main()
