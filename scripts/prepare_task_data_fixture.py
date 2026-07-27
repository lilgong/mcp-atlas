#!/usr/bin/env python3
"""Create a content-addressed external /data fixture directory."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/mcp_eval"))

from mcp_completion.task_data import (  # noqa: E402
    FIXTURE_CONTRACT,
    FIXTURE_MANIFEST,
    content_digest,
    fixture_copy_ignore,
)


def prepare_fixture(source: Path, output: Path, fixture_id: str) -> dict[str, str]:
    source = source.resolve()
    output = output.resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", fixture_id):
        raise ValueError("fixture_id must be a safe 1-128 character identifier")
    if not source.is_dir():
        raise ValueError(f"source fixture directory does not exist: {source}")
    if output == source or output.is_relative_to(source):
        raise ValueError("output fixture directory must be outside source")
    if output.exists():
        raise ValueError(f"output fixture directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Validate the source itself before copytree can follow a symlink.
        content_digest(source)
        shutil.copytree(source, output, ignore=fixture_copy_ignore(source))
        digest = content_digest(output)
        manifest = {
            "contract": FIXTURE_CONTRACT,
            "fixture_id": fixture_id,
            "content_sha256": digest,
        }
        (output / FIXTURE_MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return {
        "fixture_dir": str(output),
        "fixture_id": fixture_id,
        "content_sha256": digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fixture-id", required=True)
    args = parser.parse_args()
    result = prepare_fixture(args.source, args.output, args.fixture_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
