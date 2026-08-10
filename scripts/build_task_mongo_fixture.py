#!/usr/bin/env python3
"""Build a disposable task-Mongo image from an arbitrary mongodump database."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "services/task-sandbox/mongo/Dockerfile"


def _source_database_dir(dump_dir: Path, database: str) -> Path:
    nested = dump_dir / database
    if nested.is_dir():
        return nested
    if dump_dir.is_dir() and dump_dir.name == database:
        return dump_dir
    raise ValueError(
        f"mongodump database {database!r} was not found below {dump_dir}"
    )


def _fixture_files(source: Path) -> list[Path]:
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not any(path.suffix == ".bson" for path in files):
        raise ValueError(f"no BSON collection files found below {source}")
    return files


def _content_digest(source: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(source).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def build_fixture(
    *,
    dump_dir: Path,
    source_database: str,
    fixture_id: str,
    image: str,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", fixture_id):
        raise ValueError("fixture_id must be a safe 1-128 character identifier")
    source = _source_database_dir(dump_dir.resolve(), source_database)
    files = _fixture_files(source)
    content_sha256 = _content_digest(source, files)
    manifest = {
        "fixture_id": fixture_id,
        "source_database": source_database,
        "logical_database": "store",
        "content_sha256": content_sha256,
        "file_count": len(files),
    }
    with tempfile.TemporaryDirectory(prefix="mcp-task-mongo-build-") as raw:
        context = Path(raw)
        shutil.copytree(source, context / "fixture/store")
        (context / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "docker", "build",
                "--file", str(DOCKERFILE),
                "--label", f"mcp-atlas.fixture-id={fixture_id}",
                "--label", "mcp-atlas.logical-database=store",
                "--label", f"mcp-atlas.fixture-sha256={content_sha256}",
                "--tag", image,
                str(context),
            ],
            check=True,
        )
    print(json.dumps(
        {
            "image": image,
            "fixture_id": fixture_id,
            "source_database": source_database,
            "logical_database": "store",
            "content_sha256": content_sha256,
        },
        ensure_ascii=False,
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump-dir",
        required=True,
        type=Path,
        help="mongodump root, or the selected database directory",
    )
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument(
        "--image",
        help="output image tag; default mcp-task-mongo:<fixture-id>",
    )
    args = parser.parse_args()
    image = args.image or f"mcp-task-mongo:{args.fixture_id}"
    build_fixture(
        dump_dir=args.dump_dir,
        source_database=args.source_database,
        fixture_id=args.fixture_id,
        image=image,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
