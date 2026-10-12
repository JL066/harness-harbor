"""Focused contract tests for native Git fetch and worktree lifecycle tools."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import control_plane
import server_legacy


HTTPS_PLACEHOLDER = "https://git.example.invalid/acme/repository.git"


class GitControlPlaneTests:
    # Use lightweight assertion helpers so pytest can parameterize these tests
    # while keeping the existing unittest-style assertions readable.
    @staticmethod
    def assertTrue(value, message=None) -> None:
        assert value, message

    @staticmethod
    def assertFalse(value, message=None) -> None:
        assert not value, message

    @staticmethod
    def assertEqual(expected, actual, message=None) -> None:
        assert expected == actual, message or f"expected {expected!r}, got {actual!r}"

    @staticmethod
    def assertNotEqual(expected, actual, message=None) -> None:
        assert expected != actual, message or f"did not expect {expected!r}"

    @staticmethod
    def assertIn(member, container, message=None) -> None:
        assert member in container, message or f"{member!r} not found in {container!r}"

    @staticmethod
    def assertNotIn(member, container, message=None) -> None:
        assert member not in container, message or f"{member!r} unexpectedly found in {container!r}"

    def setup_method(self) -> None:
        # Keep all fixture writes inside the authorized workspace; Windows test
        # runners may deny cleanup of pytest's default AppData temp directory.
        self.temp = tempfile.TemporaryDirectory(prefix=".git-control-test-", dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temp.name).resolve()
        self.write_roots = mock.patch.object(control_plane, "write_roots", return_value=[self.root])
        self.write_roots.start()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git(self.repo, "init", "--initial-branch=main")
        self._git(self.repo, "config", "user.name", "Harbor Git Test")
        self._git(self.repo, "config", "user.email", "harbor-git-test@example.invalid")
        self.head = self._commit_file(self.repo, "tracked.txt", "base\n", "initial")

    def teardown_method(self) -> None:
        self.write_roots.stop()
        self.temp.cleanup()

    @staticmethod
    def _git(cwd: Path, *args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args], cwd=cwd, input=input_text, text=True,
            capture_output=True, check=False,
        )
        if check and result.returncode:
            raise AssertionError(f"git {args!r} failed in {cwd}: {result.stderr}")
        return result

    def _commit_file(self, repo: Path, relative: str, content: str, message: str) -> str:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._git(repo, "add", "--", relative)
        self._git(repo, "commit", "-m", message)
        return self._git(repo, "rev-parse", "HEAD").stdout.strip()

    @staticmethod
    def _path_token(value: str | Path) -> str:
        return str(value).replace("\\", "/").casefold()

    def _new_linked_worktree(self, name: str = "feature/remove-test") -> tuple[Path, str]:
        target = self.root / "linked"
        self._git(self.repo, "worktree", "add", "-b", name, str(target), self.head)
        return target, name


class TestGitFetch(GitControlPlaneTests):
    def test_all_mutations_refuse_a_repository_outside_authorized_write_roots(self) -> None:
        authorized = self.root / "authorized-only"
        authorized.mkdir()
        target = self.root / "must-not-be-created"
        commands: list[list[str]] = []
        original = control_plane._run_git

        def capture(args: list[str], root: Path, timeout: int = 30, **kwargs) -> dict:
            commands.append(list(args))
            return original(args, root, timeout=timeout, **kwargs)

        with mock.patch.object(control_plane, "write_roots", return_value=[authorized]), \
             mock.patch.object(control_plane, "_run_git", side_effect=capture):
            results = [
                control_plane.git_fetch_result(str(self.repo), "origin"),
                control_plane.git_worktree_create_result(str(self.repo), str(target), "feature/denied", self.head),
                control_plane.git_worktree_remove_result(str(self.repo), str(self.root / "linked")),
            ]

        for result in results:
            self.assertFalse(result["ok"], result)
            self.assertEqual("refused", result["state"])
            self.assertEqual([], result["argv"])
        self.assertFalse(target.exists())
        self.assertEqual(self.head, self._git(self.repo, "rev-parse", "HEAD").stdout.strip())
        self.assertFalse(any(args and args[0] in {"fetch", "worktree"} for args in commands), commands)

    def test_fetch_updates_tracking_refs_and_objects_without_checkout_or_prune(self) -> None:
        remote = self.root / "origin.git"
        self._git(self.root, "clone", "--bare", str(self.repo), str(remote))
        receiver = self.root / "receiver"
        self._git(self.root, "clone", str(remote), str(receiver))
        receiver_head = self._git(receiver, "rev-parse", "HEAD").stdout.strip()
        stale_head = receiver_head
        self._git(receiver, "update-ref", "refs/remotes/origin/stale", stale_head)

        newer = self._commit_file(self.repo, "tracked.txt", "remote update\n", "advance remote")
        self._git(self.repo, "push", str(remote), "main")
        calls: list[list[str]] = []
        original = control_plane._run_git

        def local_remote_runner(args: list[str], root: Path, timeout: int = 30, **kwargs) -> dict:
            calls.append(list(args))
            # The configured local bare remote is used for the actual fetch.
            # Mock only URL resolution so the production HTTPS policy can be
            # exercised without external network access.
            if args[:2] == ["remote", "get-url"]:
                return {
                    "ok": True,
                    "argv": ["git", "--no-pager", *args],
                    "stdout": HTTPS_PLACEHOLDER + "\n",
                    "stderr": "",
                    "exit_code": 0,
                    "truncated": False,
                }
            command = list(args)
            # If the implementation fetches its internally resolved URL,
            # redirect that URL to the local fixture. If it fetches by name,
            # Git uses receiver's configured local origin path directly.
            if command and command[0] == "fetch":
                command = [str(remote) if value == HTTPS_PLACEHOLDER else value for value in command]
            return original(command, root, timeout=timeout, **kwargs)

        with mock.patch.object(control_plane, "_run_git", side_effect=local_remote_runner):
            result = control_plane.git_fetch_result(str(receiver), "origin")

        self.assertTrue(result["ok"], result)
        self.assertEqual("git_fetch", result["operation"])
        self.assertEqual("fetched", result["state"])
        self.assertTrue(result["mutation"])
        self.assertEqual(str(receiver.resolve()), result["repo_root"])
        self.assertEqual("origin", result["remote"])
        self.assertIn("before_remote_tracking", result)
        self.assertIn("after_remote_tracking", result)
        self.assertEqual(newer, self._git(receiver, "rev-parse", "refs/remotes/origin/main").stdout.strip())
        self.assertEqual(stale_head, self._git(receiver, "rev-parse", "refs/remotes/origin/stale").stdout.strip())
        self.assertEqual(receiver_head, self._git(receiver, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual("base\n", (receiver / "tracked.txt").read_text(encoding="utf-8"))
        self.assertEqual("", self._git(receiver, "status", "--porcelain").stdout)
        fetches = [args for args in calls if args and args[0] == "fetch"]
        self.assertEqual(1, len(fetches), calls)
        self.assertIn("--no-recurse-submodules", fetches[0])
        self.assertIn("--no-prune", fetches[0])
        self.assertEqual("origin", fetches[0][-1])
        self.assertFalse(any(args and args[0] in {"checkout", "merge", "rebase", "reset"} for args in calls), calls)
        self.assertFalse(any(":" in arg and not arg.startswith("https:") for arg in fetches[0]), fetches[0])

    @pytest.mark.parametrize(
        "remote",
        ["", "--origin", "-upload-pack=evil", "https://example.invalid/repo.git",
         "origin:refs/heads/main", "origin;whoami", "origin name", "origin$(whoami)"],
    )
    def test_fetch_accepts_only_simple_configured_remote_names(self, remote: str) -> None:
        calls: list[list[str]] = []
        original = control_plane._run_git

        def capture(args: list[str], root: Path, timeout: int = 30, **kwargs) -> dict:
            calls.append(list(args))
            return original(args, root, timeout=timeout, **kwargs)

        with mock.patch.object(control_plane, "_run_git", side_effect=capture):
            result = control_plane.git_fetch_result(str(self.repo), remote)
        self.assertFalse(result["ok"], result)
        self.assertFalse(any(args and args[0] == "fetch" for args in calls), calls)

    def test_fetch_requires_existing_remote_and_redacts_credential_bearing_url(self) -> None:
        missing = control_plane.git_fetch_result(str(self.repo), "missing")
        self.assertFalse(missing["ok"], missing)

        self._git(self.repo, "remote", "add", "origin", "https://user:top-secret@example.invalid/repo.git")
        result = control_plane.git_fetch_result(str(self.repo), "origin")
        rendered = json.dumps(result)
        self.assertFalse(result["ok"], result)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("user:top-secret", rendered)
        self.assertNotIn("https://user:", rendered)
        self.assertNotIn("fetch", result.get("argv", []))


class TestGitWorktreeCreate(GitControlPlaneTests):
    def test_create_starts_exact_commit_in_one_new_linked_branch_without_upstream(self) -> None:
        target = self.root / "created"
        branch = "feature/worktree-create"
        calls: list[list[str]] = []
        original = control_plane._run_git

        def capture(args: list[str], root: Path, timeout: int = 30, **kwargs) -> dict:
            calls.append(list(args))
            return original(args, root, timeout=timeout, **kwargs)

        with mock.patch.object(control_plane, "_run_git", side_effect=capture):
            result = control_plane.git_worktree_create_result(
                str(self.repo), str(target), branch, self.head,
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual("git_worktree_create", result["operation"])
        self.assertEqual("created", result["state"])
        self.assertTrue(result["mutation"])
        self.assertEqual(str(self.repo.resolve()), result["repo_root"])
        self.assertEqual(str(target.resolve()), result["worktree_path"])
        self.assertEqual(branch, result["branch"])
        self.assertEqual(self.head, result["commit"])
        self.assertTrue(result["registered"])
        self.assertEqual(branch, self._git(target, "branch", "--show-current").stdout.strip())
        self.assertEqual(self.head, self._git(target, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(self.head, self._git(self.repo, "rev-parse", f"refs/heads/{branch}").stdout.strip())
        upstream = self._git(self.repo, "config", "--get", f"branch.{branch}.remote", check=False)
        self.assertNotEqual(0, upstream.returncode)
        self.assertIn(self._path_token(target.resolve()), self._path_token(self._git(self.repo, "worktree", "list", "--porcelain").stdout))
        argv = result.get("argv", [])
        self.assertIn("worktree", argv)
        self.assertIn("add", argv)
        self.assertNotIn("fetch", argv)
        self.assertNotIn("push", argv)
        self.assertFalse(any(args and args[0] in {"fetch", "push"} for args in calls), calls)

    @pytest.mark.parametrize("commit", ["HEAD", "abc123", "g" * 40, "a" * 40])
    def test_create_requires_an_existing_exact_full_commit_sha(self, commit: str) -> None:
        result = control_plane.git_worktree_create_result(
            str(self.repo), str(self.root / "invalid-commit"), "feature/invalid-commit", commit,
        )
        self.assertFalse(result["ok"], result)
        self.assertFalse((self.root / "invalid-commit").exists())
        self.assertNotEqual(
            0,
            self._git(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/feature/invalid-commit", check=False).returncode,
        )

    def test_create_rejects_a_full_sha_for_a_non_commit_object(self) -> None:
        blob_sha = self._git(self.repo, "hash-object", "-w", "--stdin", input_text="blob object\n").stdout.strip()
        self.assertEqual(40, len(blob_sha))
        result = control_plane.git_worktree_create_result(
            str(self.repo), str(self.root / "non-commit"), "feature/non-commit", blob_sha,
        )
        self.assertFalse(result["ok"], result)
        self.assertFalse((self.root / "non-commit").exists())

    def test_create_rejects_existing_branch_or_existing_target_path(self) -> None:
        for target, branch in ((self.root / "existing-branch", "main"), (self.root / "occupied", "feature/occupied")):
            if target.name == "occupied":
                target.mkdir()
                (target / "keep.txt").write_text("keep", encoding="utf-8")
            result = control_plane.git_worktree_create_result(str(self.repo), str(target), branch, self.head)
            self.assertFalse(result["ok"], result)
            if target.name == "occupied":
                self.assertEqual("keep", (target / "keep.txt").read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "branch",
        ["", "--bare", "-x", "../main", "feature/../main", "refs/heads/main:other",
         "main;echo", "main$(echo bad)", "main name", "main\\other", "main..other", ".hidden"],
    )
    def test_create_rejects_unsafe_branch_names(self, branch: str) -> None:
        target = self.root / "invalid-branch"
        result = control_plane.git_worktree_create_result(str(self.repo), str(target), branch, self.head)
        self.assertFalse(result["ok"], result)
        self.assertFalse(target.exists())

    def test_create_rejects_paths_outside_authorized_roots_and_windows_device_paths(self) -> None:
        outside = self.root.parent / (self.root.name + "-outside-worktree")
        values = [str(outside), r"\\server\share\worktree", r"\\?\C:\worktree", r"\\.\C:\worktree"]
        for path in values:
            result = control_plane.git_worktree_create_result(
                str(self.repo), path, "feature/outside", self.head,
            )
            self.assertFalse(result["ok"], result)
        self.assertFalse(outside.exists())


class TestGitWorktreeRemove(GitControlPlaneTests):
    def test_remove_clean_linked_worktree_and_preserve_branch(self) -> None:
        target, branch = self._new_linked_worktree()
        head = self._git(target, "rev-parse", "HEAD").stdout.strip()
        calls: list[list[str]] = []
        original = control_plane._run_git

        def capture(args: list[str], root: Path, timeout: int = 30, **kwargs) -> dict:
            calls.append(list(args))
            return original(args, root, timeout=timeout, **kwargs)

        with mock.patch.object(control_plane, "_run_git", side_effect=capture):
            result = control_plane.git_worktree_remove_result(str(self.repo), str(target))
        self.assertTrue(result["ok"], result)
        self.assertEqual("git_worktree_remove", result["operation"])
        self.assertEqual("removed", result["state"])
        self.assertTrue(result["mutation"])
        self.assertEqual(str(self.repo.resolve()), result["repo_root"])
        self.assertEqual(str(target.resolve()), result["worktree_path"])
        self.assertEqual(branch, result["branch"])
        self.assertEqual(head, result["head"])
        self.assertIn("before_registration", result)
        self.assertIn("after_registration", result)
        self.assertIn(self._path_token(target.resolve()), self._path_token(json.dumps(result["before_registration"])))
        self.assertNotIn(self._path_token(target.resolve()), self._path_token(json.dumps(result["after_registration"])))
        self.assertFalse(target.exists())
        self.assertEqual(head, self._git(self.repo, "rev-parse", f"refs/heads/{branch}").stdout.strip())
        self.assertNotIn("--force", result.get("argv", []))
        self.assertFalse(any("--force" in args or args[:2] == ["worktree", "prune"] for args in calls), calls)

    def test_remove_refuses_primary_worktree_and_unregistered_directory(self) -> None:
        primary = control_plane.git_worktree_remove_result(str(self.repo), str(self.repo))
        self.assertFalse(primary["ok"], primary)
        self.assertTrue(self.repo.exists())

        arbitrary = self.root / "arbitrary"
        arbitrary.mkdir()
        sentinel = arbitrary / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        result = control_plane.git_worktree_remove_result(str(self.repo), str(arbitrary))
        self.assertFalse(result["ok"], result)
        self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("dirty_kind", ["staged", "unstaged", "untracked"])
    def test_remove_refuses_all_kinds_of_dirty_worktree(self, dirty_kind: str) -> None:
        target, _ = self._new_linked_worktree(f"feature/dirty-{dirty_kind}")
        if dirty_kind == "untracked":
            (target / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        else:
            (target / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            if dirty_kind == "staged":
                self._git(target, "add", "tracked.txt")
        before_status = self._git(target, "status", "--porcelain").stdout
        calls: list[list[str]] = []
        original = control_plane._run_git

        def capture(args: list[str], root: Path, timeout: int = 30, **kwargs) -> dict:
            calls.append(list(args))
            return original(args, root, timeout=timeout, **kwargs)

        with mock.patch.object(control_plane, "_run_git", side_effect=capture):
            result = control_plane.git_worktree_remove_result(str(self.repo), str(target))
        self.assertFalse(result["ok"], result)
        self.assertTrue(target.exists())
        self.assertEqual(before_status, self._git(target, "status", "--porcelain").stdout)
        listing = self._git(self.repo, "worktree", "list", "--porcelain").stdout
        self.assertIn(self._path_token(target.resolve()), self._path_token(listing))
        self.assertNotIn("--force", result.get("argv", []))
        self.assertFalse(any(args[:2] == ["worktree", "remove"] or "--force" in args for args in calls), calls)

    def test_remove_refuses_locked_worktree(self) -> None:
        target, _ = self._new_linked_worktree("feature/locked")
        self._git(self.repo, "worktree", "lock", str(target))
        result = control_plane.git_worktree_remove_result(str(self.repo), str(target))
        self.assertFalse(result["ok"], result)
        self.assertTrue(target.exists())
        self.assertIn("locked", self._git(self.repo, "worktree", "list", "--porcelain").stdout)
        self.assertNotIn("--force", result.get("argv", []))

    def test_remove_fails_closed_for_missing_registered_directory_without_pruning(self) -> None:
        target, _ = self._new_linked_worktree("feature/missing-directory")
        moved = self.root / "moved-away"
        target.rename(moved)
        calls: list[list[str]] = []
        original = control_plane._run_git

        def capture(args: list[str], root: Path, timeout: int = 30, **kwargs) -> dict:
            calls.append(list(args))
            return original(args, root, timeout=timeout, **kwargs)

        with mock.patch.object(control_plane, "_run_git", side_effect=capture):
            result = control_plane.git_worktree_remove_result(str(self.repo), str(target))
        self.assertFalse(result["ok"], result)
        self.assertFalse(target.exists())
        self.assertTrue(moved.exists())
        listing = self._git(self.repo, "worktree", "list", "--porcelain").stdout
        self.assertIn(self._path_token(target.resolve()), self._path_token(listing))
        self.assertFalse(any("prune" in value for value in result.get("argv", [])))
        self.assertFalse(any(args[:2] == ["worktree", "remove"] or args[:2] == ["worktree", "prune"] for args in calls), calls)


def test_mcp_discovery_and_schema_for_all_native_git_mutations() -> None:
    tools = server_legacy.mcp._tool_manager._tools
    expected = {"git_init", "git_fetch", "git_worktree_create", "git_worktree_remove"}
    assert expected.issubset(tools)
    expected_schema = {
        "git_init": ({"path", "initial_branch"}, ["path"]),
        "git_fetch": ({"repo", "remote"}, ["repo"]),
        "git_worktree_create": ({"repo", "path", "branch", "commit"}, ["repo", "path", "branch", "commit"]),
        "git_worktree_remove": ({"repo", "path"}, ["repo", "path"]),
    }
    for name, (properties, required) in expected_schema.items():
        tool = tools[name]
        assert set(tool.parameters["properties"]) == properties
        assert tool.parameters["required"] == required
        assert not getattr(tool.annotations, "readOnlyHint", False), name
        assert not getattr(tool.annotations, "idempotentHint", False), name
    assert tools["git_init"].parameters["properties"]["initial_branch"]["default"] == "main"
    assert tools["git_fetch"].parameters["properties"]["remote"]["default"] == "origin"
