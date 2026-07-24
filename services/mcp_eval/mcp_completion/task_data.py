"""External task-data fixture validation, copy-in, and Git materialization."""

from __future__ import annotations

import csv
import fnmatch
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


FIXTURE_MANIFEST = ".atlas-fixture.json"
FIXTURE_CONTRACT = "mcp-atlas-task-data-v1"
IGNORED_PATTERNS = (
    ".venv",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
    ".channels_cache_v2.json",
    ".users_cache.json",
)
_git_cache_lock = threading.Lock()


class TaskDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskDataFixture:
    source_dir: Path
    fixture_id: str
    content_sha256: str


@dataclass(frozen=True)
class RepoSpec:
    url: str
    sha: str
    name: str


def fixture_ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if any(fnmatch.fnmatch(name, pattern) for pattern in IGNORED_PATTERNS)
    }


def content_digest(root: str | Path) -> str:
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == FIXTURE_MANIFEST:
            continue
        if any(
            fnmatch.fnmatch(part, pattern)
            for part in path.relative_to(root).parts
            for pattern in IGNORED_PATTERNS
        ):
            continue
        if path.is_symlink():
            raise TaskDataError(f"fixture symlinks are not allowed: {path}")
        if not path.is_file():
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def make_task_copy_writable(root: Path) -> None:
    """Grant writes only on the disposable copy mounted into a task container."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            raise TaskDataError(f"task data symlinks are not allowed: {path}")
        if not path.exists():
            continue
        mode = path.stat().st_mode
        path.chmod(mode | (0o333 if path.is_dir() else 0o222))


def load_fixture(source_dir: str | Path) -> TaskDataFixture:
    source = Path(source_dir).resolve()
    manifest_path = source / FIXTURE_MANIFEST
    if not source.is_dir() or not manifest_path.is_file():
        raise TaskDataError(
            f"task data fixture must contain {FIXTURE_MANIFEST}: {source}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskDataError(f"invalid task data fixture manifest: {source}") from exc
    fixture_id = str(manifest.get("fixture_id") or "")
    expected = str(manifest.get("content_sha256") or "")
    if (
        manifest.get("contract") != FIXTURE_CONTRACT
        or not fixture_id
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", fixture_id)
        or not re.fullmatch(r"[0-9a-f]{64}", expected)
    ):
        raise TaskDataError(f"invalid task data fixture contract: {source}")
    actual = content_digest(source)
    if actual != expected:
        raise TaskDataError(
            f"task data fixture digest mismatch: expected {expected}, got {actual}"
        )
    return TaskDataFixture(source, fixture_id, actual)


@functools.lru_cache(maxsize=8)
def load_fixture_cached(source_dir: str) -> TaskDataFixture:
    """Validate an immutable content-addressed source once per process."""
    return load_fixture(source_dir)


def _load_repo_specs(manifest: Path) -> list[RepoSpec]:
    if not manifest.is_file():
        return []
    specs = []
    with manifest.open(encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle):
            if not row:
                continue
            if len(row) != 3:
                raise TaskDataError(f"invalid Git fixture row: {row!r}")
            url, sha, evaluation_path = (value.strip() for value in row)
            specs.append(RepoSpec(url, sha, Path(evaluation_path).name))
    return specs


def _run_git(*args: str) -> None:
    subprocess.run(
        ["git", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _commit_exists(bare_repo: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", f"--git-dir={bare_repo}", "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def materialize_git_repositories(data_dir: Path, cache_root: Path) -> list[str]:
    manifest = data_dir / "repos/git_submodule_info.csv"
    if not manifest.is_file():
        raise TaskDataError(
            "Git task fixture lacks repos/git_submodule_info.csv"
        )
    specs = _load_repo_specs(manifest)
    if not specs:
        raise TaskDataError(
            "Git task fixture has no pinned repositories"
        )
    cache_root.mkdir(parents=True, exist_ok=True)
    created = []
    with _git_cache_lock:
        for spec in specs:
            destination = data_dir / "repos" / spec.name
            if destination.exists():
                result = subprocess.run(
                    ["git", "-C", str(destination), "cat-file", "-e", f"{spec.sha}^{{commit}}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    raise TaskDataError(
                        f"fixture Git repository lacks {spec.sha}: {destination}"
                    )
                created.append(os.fspath(destination))
                continue
            bare_repo = cache_root / f"{spec.name}.git"
            if not bare_repo.exists():
                _run_git("clone", "--bare", spec.url, str(bare_repo))
            if not _commit_exists(bare_repo, spec.sha):
                _run_git(f"--git-dir={bare_repo}", "fetch", "--prune", "origin")
            if not _commit_exists(bare_repo, spec.sha):
                raise TaskDataError(
                    f"pinned commit {spec.sha} is unavailable for {spec.url}"
                )
            _run_git(
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                str(bare_repo),
                str(destination),
            )
            _run_git("-C", str(destination), "checkout", "--detach", spec.sha)
            created.append(os.fspath(destination))
    return created


def prepare_task_workspace(
    *,
    source_dir: str | Path,
    task_id: str,
    include_git: bool,
) -> tuple[Path, TaskDataFixture, list[str]]:
    fixture = load_fixture_cached(os.fspath(Path(source_dir).resolve()))
    workspace_root = Path(
        os.getenv("MCP_TASK_WORKSPACE_ROOT", tempfile.gettempdir())
    ).resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(
        prefix=f"mcp-atlas-{task_id[:24]}-",
        dir=workspace_root,
    ))
    data_dir = workspace / "data"
    try:
        shutil.copytree(
            fixture.source_dir,
            data_dir,
            ignore=fixture_ignore,
        )
        copied_digest = content_digest(data_dir)
        if copied_digest != fixture.content_sha256:
            raise TaskDataError(
                "copied task data digest mismatch: "
                f"expected {fixture.content_sha256}, got {copied_digest}"
            )
        make_task_copy_writable(data_dir)
        repos = []
        if include_git:
            repos = materialize_git_repositories(
                data_dir,
                Path(os.getenv(
                    "MCP_GIT_CACHE_DIR",
                    workspace_root / "mcp-atlas-git-cache",
                )).resolve(),
            )
            make_task_copy_writable(data_dir)
        return data_dir, fixture, repos
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
