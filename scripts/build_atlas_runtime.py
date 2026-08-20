#!/usr/bin/env python3
"""Build the fixture-free, versioned MCP-Atlas tool runtime image."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "services/agent-environment"
RUNTIME = ROOT / "services/atlas-runtime"
DEFAULT_IMAGE = "mcp-atlas-runtime:latest"

SOURCE_FILES = (
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "dev_scripts/install_mcp_packages.sh",
    "src/agent_environment/__init__.py",
    "src/agent_environment/logger.py",
    "src/agent_environment/main.py",
    "src/agent_environment/mcp_client.py",
    "src/agent_environment/mcp_router.py",
    "src/agent_environment/osm_mcp_compat.py",
    "src/agent_environment/oxylabs_mcp_compat.py",
    "src/agent_environment/pubmed_mcp_compat.py",
    "src/agent_environment/twelvedata_mcp_compat.py",
    "src/agent_environment/run_node_mcp.cjs",
    "src/agent_environment/yibu_fetch_preload.cjs",
    "src/agent_environment/wikipedia_preload/sitecustomize.py",
    "src/agent_environment/weatherapi_preload/sitecustomize.py",
    "src/agent_environment/mcp_server_template.json",
)


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def stage_build_context(context: Path) -> None:
    """Stage only runtime code and patches; fixture data never enters context."""
    _copy_file(RUNTIME / "Dockerfile", context / "Dockerfile")
    _copy_file(RUNTIME / "entrypoint.sh", context / "entrypoint.sh")
    _copy_file(
        RUNTIME / "code-executor-requirements.txt",
        context / "code-executor-requirements.txt",
    )
    for relative in SOURCE_FILES:
        target = context / relative
        if relative == "dev_scripts/install_mcp_packages.sh":
            target = context / "install_mcp_packages.sh"
        _copy_file(SOURCE / relative, target)

    template = context / "src/agent_environment/mcp_server_template.json"
    text = template.read_text(encoding="utf-8")
    old = "/data/repos/mcp_code_executor_workspace/.venv"
    if text.count(old) != 1:
        raise RuntimeError(
            "expected exactly one code-executor venv path in source template"
        )
    template.write_text(
        text.replace(old, "/opt/mcp-code-venv"),
        encoding="utf-8",
    )
    template_config = json.loads(template.read_text(encoding="utf-8"))
    # Every in-image path the template names must actually be staged. A
    # server-specific check only guards the one server someone remembered to
    # add, so a template that starts routing through a new shim or preload
    # silently ships an image where that server cannot start. Checking the
    # template itself keeps SOURCE_FILES honest without another manual list.
    image_prefix = "/agent-environment/"
    unstaged: list[str] = []
    for name, server in template_config["mcpServers"].items():
        referenced: list[tuple[str, bool]] = []
        for arg in server.get("args") or []:
            if isinstance(arg, str) and arg.startswith(image_prefix):
                referenced.append((arg, False))
        env = server.get("env")
        env_items = env if isinstance(env, list) else [env or {}]
        for entry in env_items:
            pythonpath = (entry or {}).get("PYTHONPATH")
            if isinstance(pythonpath, str) and pythonpath.startswith(image_prefix):
                referenced.append((pythonpath, True))
        for path, is_preload_dir in referenced:
            staged = context / path.removeprefix(image_prefix)
            present = (
                (staged / "sitecustomize.py").is_file()
                if is_preload_dir
                else staged.is_file()
            )
            if not present:
                unstaged.append(f"{name}: {path}")
    if unstaged:
        raise RuntimeError(
            "template references paths that SOURCE_FILES does not stage: "
            + ", ".join(sorted(unstaged))
        )

    patches = SOURCE / "vendor/yibu-patched" / "oxylabs_mcp"
    shutil.copytree(patches, context / "vendor/yibu-patched/oxylabs_mcp")

    forbidden = [
        path for path in context.rglob("*")
        if path.name == "data" or path.name.startswith(".env")
    ]
    if forbidden:
        raise RuntimeError(f"forbidden fixture/credential paths staged: {forbidden}")


def build_image(image: str) -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-atlas-runtime-build-") as raw:
        context = Path(raw)
        stage_build_context(context)
        subprocess.run(
            ["docker", "build", "--tag", image, str(context)],
            check=True,
        )
    print(json.dumps({"image": image, "data_contract": "external-data-v1"}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    build_image(args.image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
