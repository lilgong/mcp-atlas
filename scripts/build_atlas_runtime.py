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
    "dev_scripts/install_mcp_packages.sh",
    "src/agent_environment/__init__.py",
    "src/agent_environment/logger.py",
    "src/agent_environment/main.py",
    "src/agent_environment/mcp_client.py",
    "src/agent_environment/mcp_router.py",
    "src/agent_environment/ddg_mcp_compat.py",
    "src/agent_environment/osm_mcp_compat.py",
    "src/agent_environment/oxylabs_mcp_compat.py",
    "src/agent_environment/mcp_server_template.json",
    "filesystem_server_compat.mjs",
    "metmuseum_mcp_compat.mjs",
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

    patches = SOURCE / "vendor/yibu-patched"
    shutil.copytree(patches, context / "vendor/yibu-patched")

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
