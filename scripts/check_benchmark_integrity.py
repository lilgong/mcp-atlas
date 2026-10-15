#!/usr/bin/env python3
"""Verify the tracked MCP-Atlas baseline, including reviewed claim corrections."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = "services/mcp_eval/MCP-Atlas-origin.csv"
# Reviewed correction: task 688a441c1a0d2e5c93853e93, 65540 - 39261 = 26279.
# Original upstream SHA256: 065f423ffd1425185d23ed01a1d1ad8ed8c6355749868521a07faaa13ec4c0ad
BASELINE_SHA256 = (
    "4850307c5597d22f2e3cc8e83b047500e519a1554a04f6ecaa96629daa8f9d22"
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

    if actual != BASELINE_SHA256:
        print(
            "MCP-Atlas-origin.csv baseline integrity check failed\n"
            f"source:   {source}\n"
            f"expected: {BASELINE_SHA256}\n"
            f"actual:   {actual}\n"
            "Keep local Slack date shifts in the ignored MCP-Atlas.csv; "
            "Reviewed claim corrections must update the baseline hash and regression tests.",
            file=sys.stderr,
        )
        return 1
    print(f"MCP-Atlas-origin.csv baseline verified: {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
