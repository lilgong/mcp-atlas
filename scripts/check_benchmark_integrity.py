#!/usr/bin/env python3
"""Reject commits that replace the official MCP-Atlas benchmark CSV."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = "services/mcp_eval/MCP-Atlas.csv"
OFFICIAL_SHA256 = (
    "065f423ffd1425185d23ed01a1d1ad8ed8c6355749868521a07faaa13ec4c0ad"
)


def hash_stream(stream) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def hash_git_revision(revision: str) -> str:
    process = subprocess.Popen(
        ["git", "show", f"{revision}:{BENCHMARK_PATH}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    actual = hash_stream(process.stdout)
    stderr = process.stderr.read().decode("utf-8", errors="replace")
    return_code = process.wait()
    if return_code:
        raise RuntimeError(
            f"cannot read {BENCHMARK_PATH} from {revision}: {stderr.strip()}"
        )
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--git-ref",
        help="check the file stored in this commit instead of the working tree",
    )
    args = parser.parse_args()

    if args.git_ref:
        actual = hash_git_revision(args.git_ref)
        source = f"{args.git_ref}:{BENCHMARK_PATH}"
    else:
        path = ROOT / BENCHMARK_PATH
        with path.open("rb") as handle:
            actual = hash_stream(handle)
        source = str(path)

    if actual != OFFICIAL_SHA256:
        print(
            "official MCP-Atlas.csv integrity check failed\n"
            f"source:   {source}\n"
            f"expected: {OFFICIAL_SHA256}\n"
            f"actual:   {actual}\n"
            "Keep local Slack date shifts in MCP-Atlas.slack-aligned.csv; "
            "never commit them over MCP-Atlas.csv.",
            file=sys.stderr,
        )
        return 1
    print(f"official MCP-Atlas.csv verified: {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
