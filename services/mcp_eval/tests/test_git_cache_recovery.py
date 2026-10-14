"""Local Git only: cache safety without network, models or MCP calls."""
import multiprocessing
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_completion.task_data import (RepoSpec, TaskDataError, git_cache_path,
                                     materialize_git_repositories, _run_git)


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True).stdout.strip()


def upstream(root, name="source"):
    repo = root / name
    git("init", "-q", repo)
    git("-C", repo, "config", "user.name", "Offline test")
    git("-C", repo, "config", "user.email", "offline@example.invalid")
    (repo / "file").write_text(name)
    git("-C", repo, "add", "file")
    git("-C", repo, "commit", "-qm", "test")
    return RepoSpec(str(repo), git("-C", repo, "rev-parse", "HEAD"), "same-name")


def prepare(root, spec):
    (root / "repos").mkdir(parents=True)
    (root / "repos/git_submodule_info.csv").write_text(f"{spec.url},{spec.sha},/data/repos/{spec.name}\n")


class GitCacheRecoveryTests(unittest.TestCase):
    def test_invalid_cache_is_preserved_and_rebuilt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); spec = upstream(root)
            data = root / "data"; prepare(data, spec)
            cache = git_cache_path(root / "cache", spec)
            cache.mkdir(parents=True); (cache / "partial").write_text("preserve")
            materialize_git_repositories(data, root / "cache")
            self.assertEqual(git("-C", data / "repos" / spec.name, "rev-parse", "HEAD"), spec.sha)
            [saved] = list((cache.parent / "quarantine").iterdir())
            self.assertEqual((saved / "partial").read_text(), "preserve")

    def test_same_name_different_sources_do_not_share_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); one = upstream(root, "one"); two = upstream(root, "two")
            self.assertNotEqual(git_cache_path(root / "cache", one), git_cache_path(root / "cache", two))
            for i, spec in enumerate((one, two)):
                data = root / str(i); prepare(data, spec)
                materialize_git_repositories(data, root / "cache")
                self.assertEqual((data / "repos" / spec.name / "file").read_text(), ("one", "two")[i])

    def test_failed_clone_does_not_publish_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); spec = RepoSpec(str(root / "absent"), "1" * 40, "missing")
            prepare(root / "data", spec)
            with self.assertRaises(TaskDataError):
                materialize_git_repositories(root / "data", root / "cache")
            cache = git_cache_path(root / "cache", spec)
            self.assertFalse(cache.exists())
            self.assertEqual(list(cache.parent.glob("clone-*")), [])

    def test_two_processes_share_one_complete_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); spec = upstream(root)
            data_dirs = [root / str(i) for i in range(2)]
            for data in data_dirs: prepare(data, spec)
            ctx = multiprocessing.get_context("fork")
            processes = [ctx.Process(target=materialize_git_repositories, args=(data, root / "cache")) for data in data_dirs]
            for process in processes: process.start()
            for process in processes:
                process.join(15)
                if process.is_alive(): process.terminate(); process.join(); self.fail("cache lock deadlocked")
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(len(list((root / "cache").glob("*/repo.git"))), 1)

    def test_git_timeout_is_actionable(self):
        with patch("mcp_completion.task_data.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 120)):
            with self.assertRaisesRegex(TaskDataError, "timed out"):
                _run_git("clone", "unused")

    def test_git_failure_detail_redacts_configured_secrets(self):
        failure = subprocess.CalledProcessError(128, ["git"], stderr="fatal: https://alice:password@host/repo token-secret-value")
        with patch("mcp_completion.runtime_log._redact_string", lambda value: value.replace("token-secret-value", "<redacted>")):
            with patch("mcp_completion.task_data.subprocess.run", side_effect=failure):
                with self.assertRaises(TaskDataError) as exc:
                    _run_git("fetch", "origin")
        self.assertNotIn("password", str(exc.exception))
        self.assertNotIn("token-secret-value", str(exc.exception))
        self.assertIn("exit 128", str(exc.exception))

    def test_wrong_origin_cache_is_quarantined(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); one = upstream(root, "one"); two = upstream(root, "two")
            cache = git_cache_path(root / "cache", one); cache.parent.mkdir(parents=True)
            git("clone", "--bare", two.url, cache)
            data = root / "data"; prepare(data, one)
            materialize_git_repositories(data, root / "cache")
            self.assertEqual((data / "repos" / one.name / "file").read_text(), "one")
            self.assertEqual(len(list((cache.parent / "quarantine").iterdir())), 1)

    def test_unavailable_pinned_commit_does_not_publish_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); real = upstream(root)
            spec = RepoSpec(real.url, "1" * 40, real.name)
            data = root / "data"; prepare(data, spec)
            with self.assertRaisesRegex(TaskDataError, "unavailable"):
                materialize_git_repositories(data, root / "cache")
            self.assertFalse(git_cache_path(root / "cache", spec).exists())
