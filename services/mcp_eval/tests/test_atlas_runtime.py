import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from build_atlas_runtime import stage_build_context  # noqa: E402
from prepare_task_data_fixture import prepare_fixture  # noqa: E402
from mcp_completion.task_data import (  # noqa: E402
    FIXTURE_CONTRACT,
    FIXTURE_MANIFEST,
    TaskDataError,
    content_digest,
    load_fixture,
    prepare_task_workspace,
    write_git_safe_directory_config,
)
from mcp_completion.task_sandbox import (  # noqa: E402
    TaskSandbox,
    TaskSandboxError,
    _release_sandbox_names,
    inspect_runtime_image,
)


class AtlasRuntimeTests(unittest.IsolatedAsyncioTestCase):
    FIXTURE_V2_VECTOR_SHA256 = (
        "857ee1508a17cca148ee326d72141926e9b2abfe0797a3f70a5a9d4efce51182"
    )

    @staticmethod
    def _write_fixture_v2_vector(root: Path) -> None:
        files = {
            "keep.txt": b"kept\n",
            "uv.lock": b"version = 1\n",
            "nested/data.bin": bytes([0, 1, 2, 255]),
            "repos/ordinary/code_reference.py": b'print("keep")\n',
            ".venv/secret.txt": b"ignored\n",
            "cache/__pycache__/x.pyc": b"ignored\n",
            "repos/mcp_code_executor_workspace/code_deadbeef.py": b"ignored\n",
            "repos/mcp_code_executor_workspace/check_packages_x.py": b"ignored\n",
            "repos/mcp_code_executor_workspace/"
            "mcp_code_executor_server_x.py": b"ignored\n",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

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

    async def test_task_network_is_private_and_disables_icc(self):
        sandbox = TaskSandbox(
            task_id="network-policy",
            local_servers={"filesystem"},
            network_servers=set(),
            agent_image="image",
            mongo_image="",
            startup_timeout=1.0,
            memory_limit="1g",
            cpu_limit="1.0",
            task_data_source="/tmp/data",
        )
        with patch(
            "mcp_completion.task_sandbox._run",
            new=AsyncMock(return_value=("", "", 0)),
        ) as run, patch(
            "mcp_completion.task_sandbox.write_runtime_event"
        ):
            await sandbox._create_task_network()
        command = run.await_args.args
        self.assertEqual(command[:3], ("docker", "network", "create"))
        self.assertIn("com.docker.network.bridge.enable_icc=false", command)
        self.assertIn("mcp-atlas.task-sandbox=true", command)
        self.assertTrue(sandbox.task_network.startswith("mcp-atlas-net-"))
        _release_sandbox_names(sandbox.owned_names)

    async def test_local_agent_activates_runtime_venv_for_child_tools(self):
        sandbox = TaskSandbox(
            task_id="venv-path",
            local_servers={"mcp-server-code-runner"},
            network_servers=set(),
            agent_image="image",
            mongo_image="",
            startup_timeout=1.0,
            memory_limit="1g",
            cpu_limit="1.0",
            task_data_source="/tmp/data",
        )
        sandbox.task_data_dir = Path("/tmp/data")
        with patch(
            "mcp_completion.task_sandbox._run",
            new=AsyncMock(return_value=("", "", 0)),
        ) as run, patch.object(
            sandbox,
            "_wait_for_agent",
            new=AsyncMock(),
        ):
            await sandbox._start_agent_container(
                kind="local",
                enabled_servers=sandbox.local_servers,
                network="private-task-network",
                extra_env={},
            )
        command = run.await_args.args
        image_index = command.index("image")
        self.assertEqual(
            ("uv", "run", "--no-sync", "python", "-m", "uvicorn"),
            command[image_index + 1:image_index + 7],
        )
        _release_sandbox_names(sandbox.owned_names)

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
            self.assertIn("package.json", paths)
            self.assertIn("package-lock.json", paths)
            self.assertIn(
                "src/agent_environment/yibu_fetch_preload.cjs", paths
            )
            self.assertIn(
                "src/agent_environment/wikipedia_preload/sitecustomize.py",
                paths,
            )
            self.assertFalse(
                any(path.startswith("vendor/yibu-patched/node_modules/") for path in paths)
            )
            self.assertNotIn("filesystem_server_compat.mjs", paths)
            self.assertIn(
                "src/agent_environment/oxylabs_mcp_compat.py",
                paths,
            )
            self.assertNotIn("src/agent_environment/ddg_mcp_compat.py", paths)
            self.assertIn(
                "src/agent_environment/osm_mcp_compat.py",
                paths,
            )
            self.assertIn(
                "src/agent_environment/pubmed_mcp_compat.py",
                paths,
            )
            self.assertNotIn("metmuseum_mcp_compat.mjs", paths)
            template = (
                context
                / "src/agent_environment/mcp_server_template.json"
            ).read_text(encoding="utf-8")
            template_config = json.loads(template)
            self.assertEqual(
                {
                    "command": "/usr/local/bin/arxiv-mcp-server",
                    "args": [],
                },
                template_config["mcpServers"]["arxiv"],
            )
            self.assertIn("/opt/mcp-code-venv", template)
            self.assertNotIn("filesystem_server_compat.mjs", template)
            self.assertIn("oxylabs_mcp_compat", template)
            self.assertNotIn("ddg_mcp_compat", template)
            self.assertIn("osm_mcp_compat", template)
            self.assertIn("pubmed_mcp_compat", template)
            self.assertNotIn("metmuseum_mcp_compat", template)
            self.assertNotIn(
                "/data/repos/mcp_code_executor_workspace/.venv",
                template,
            )
            dockerfile = (context / "Dockerfile").read_text(encoding="utf-8")
            self.assertIn("nodesource.com/setup_22.x", dockerfile)
            self.assertIn("src/agent_environment/pubmed_mcp_compat.py", dockerfile)
            self.assertIn(
                "src/agent_environment/wikipedia_preload/sitecustomize.py",
                dockerfile,
            )

    def test_git_trust_is_scoped_to_task_repositories(self):
        template = json.loads(
            (
                ROOT / "services/task-sandbox/local_mcp_server_template.json"
            ).read_text(encoding="utf-8")
        )
        env = template["mcpServers"]["git"]["env"]
        self.assertEqual(
            {"GIT_CONFIG_GLOBAL": "/data/.atlas-gitconfig"},
            env,
        )

    def test_git_safe_config_lists_only_exact_task_repositories(self):
        with tempfile.TemporaryDirectory() as raw:
            data = Path(raw) / "data"
            repository = data / "repos/sample"
            repository.mkdir(parents=True)
            config = write_git_safe_directory_config(
                data,
                [str(repository)],
            )
            self.assertEqual(
                "[safe]\n\tdirectory = /data/repos/sample\n",
                config.read_text(encoding="utf-8"),
            )

    def test_fixture_identity_uses_git_manifest_not_materialized_clone(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repos = root / "repos"
            repos.mkdir()
            (repos / "git_submodule_info.csv").write_text(
                "https://example.invalid/repo.git,"
                f"{'a' * 40},/data/repos/pinned-repo\n",
                encoding="utf-8",
            )
            before = content_digest(root)
            materialized = repos / "pinned-repo"
            materialized.mkdir()
            (materialized / "README.md").write_text(
                "materialized\n", encoding="utf-8",
            )
            (materialized / "linked").symlink_to("README.md")
            (root / ".atlas-gitconfig").write_text(
                "[safe]\n\tdirectory = /data/repos/pinned-repo\n",
                encoding="utf-8",
            )
            self.assertEqual(before, content_digest(root))

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

    def test_empty_workspace_and_git_cache_paths_use_temp_defaults(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            fixture_path = root / "fixture"
            temp_root = root / "system-temp"
            source.mkdir()
            (source / "sample.txt").write_text(
                "original\n", encoding="utf-8",
            )
            prepare_fixture(source, fixture_path, "unit-fixture-empty-paths")

            with (
                patch.dict(
                    "os.environ",
                    {
                        "MCP_TASK_WORKSPACE_ROOT": "",
                        "MCP_GIT_CACHE_DIR": "",
                    },
                    clear=False,
                ),
                patch(
                    "mcp_completion.task_data.tempfile.gettempdir",
                    return_value=str(temp_root),
                ),
                patch(
                    "mcp_completion.task_data.materialize_git_repositories",
                    return_value=[],
                ) as materialize,
            ):
                data_dir, _, _ = prepare_task_workspace(
                    source_dir=fixture_path,
                    task_id="task-empty-paths",
                    include_git=True,
                )

            self.assertEqual(temp_root.resolve(), data_dir.parent.parent)
            self.assertEqual(
                (temp_root / "mcp-atlas-git-cache").resolve(),
                materialize.call_args.args[1],
            )

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

    def test_fixture_v2_contract_vector_is_stable(self):
        self.assertEqual("mcp-atlas-task-data-v2", FIXTURE_CONTRACT)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            self._write_fixture_v2_vector(source)
            self.assertEqual(
                self.FIXTURE_V2_VECTOR_SHA256,
                content_digest(source),
            )
            output = root / "fixture"
            prepare_fixture(source, output, "contract-vector")
            self.assertEqual(
                self.FIXTURE_V2_VECTOR_SHA256,
                load_fixture(output).content_sha256,
            )
            self.assertFalse(
                (output / "repos/mcp_code_executor_workspace/code_deadbeef.py").exists()
            )
            self.assertTrue(
                (output / "repos/ordinary/code_reference.py").is_file()
            )
            (source / ".venv/secret.txt").write_text(
                "changed but ignored\n", encoding="utf-8",
            )
            (source / "repos/mcp_code_executor_workspace/code_new.py").write_text(
                "changed but ignored\n", encoding="utf-8",
            )
            self.assertEqual(
                self.FIXTURE_V2_VECTOR_SHA256,
                content_digest(source),
            )
            (source / "uv.lock").write_text("version = 2\n", encoding="utf-8")
            self.assertNotEqual(
                self.FIXTURE_V2_VECTOR_SHA256,
                content_digest(source),
            )

    def test_fixture_packager_rejects_symlinks_before_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            output = root / "fixture"
            source.mkdir()
            target = source / "target.txt"
            target.write_text("target\n", encoding="utf-8")
            ignored = source / ".venv"
            ignored.mkdir()
            (ignored / "linked.txt").symlink_to(target)
            with self.assertRaisesRegex(TaskDataError, "symlinks"):
                prepare_fixture(source, output, "symlink-fixture")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
