"""Focused safety and MCP contract tests for native git_init."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import control_plane
import server_legacy


class GitInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.roots = mock.patch.object(control_plane, "write_roots", return_value=[self.root])
        self.roots.start()
        self.addCleanup(self.roots.stop)

    def test_default_branch_and_no_global_config_mutation(self) -> None:
        target = self.root / "new"
        target.mkdir()
        global_config = self.root / "global.gitconfig"
        global_config.write_text("[user]\n\tname = Existing User\n", encoding="utf-8")
        before = global_config.read_bytes()
        redirected_git_dir = self.root / "redirected.git"
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config),
                                       "GIT_DIR": str(redirected_git_dir)}):
            result = control_plane.git_init_result(str(target))
        self.assertTrue(result["ok"], result)
        self.assertEqual("git_init", result["operation"])
        self.assertEqual("initialized", result["state"])
        self.assertEqual(str(target), result["path"])
        self.assertEqual(str(target), result["repo_root"])
        self.assertEqual("main", result["initial_branch"])
        self.assertEqual(["git", "--no-pager", "init", "--initial-branch=main", "--template="], result["argv"])
        self.assertEqual(before, global_config.read_bytes())
        self.assertFalse(redirected_git_dir.exists())
        self.assertEqual("main", subprocess.check_output(
            ["git", "symbolic-ref", "--short", "HEAD"], cwd=target, text=True).strip())
        self.assertFalse((target / ".git" / "config").read_text(encoding="utf-8").find("[user]") >= 0)

    def test_custom_safe_branch(self) -> None:
        target = self.root / "custom"
        target.mkdir()
        result = control_plane.git_init_result(str(target), "feature/git_init-116")
        self.assertTrue(result["ok"], result)
        self.assertEqual("feature/git_init-116", subprocess.check_output(
            ["git", "symbolic-ref", "--short", "HEAD"], cwd=target, text=True).strip())

    def test_existing_and_nested_repositories_are_refused(self) -> None:
        target = self.root / "existing"
        target.mkdir()
        self.assertTrue(control_plane.git_init_result(str(target))["ok"])
        nested = target / "nested"
        nested.mkdir()
        for path in (target, nested):
            with self.subTest(path=path):
                result = control_plane.git_init_result(str(path))
                self.assertFalse(result["ok"], result)
                self.assertEqual("refused", result["state"])
                self.assertEqual([], result["argv"])
        self.assertFalse((nested / ".git").exists())

    def test_bare_repository_is_refused(self) -> None:
        target = self.root / "bare"
        target.mkdir()
        subprocess.run(["git", "init", "--bare", str(target)], check=True, capture_output=True)
        result = control_plane.git_init_result(str(target))
        self.assertFalse(result["ok"], result)
        self.assertEqual("refused", result["state"])
        self.assertEqual([], result["argv"])

    def test_rejects_out_of_root_missing_file_and_device_paths(self) -> None:
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        existing_file = self.root / "file.txt"
        existing_file.write_text("x", encoding="utf-8")
        for value in (str(outside), str(self.root / "missing"), str(existing_file),
                      r"\\server\share\repo", r"\\?\C:\repo", r"\\.\C:\repo"):
            with self.subTest(value=value):
                result = control_plane.git_init_result(value)
                self.assertFalse(result["ok"], result)
                self.assertEqual([], result["argv"])

    def test_rejects_branch_flags_refspecs_and_metacharacters(self) -> None:
        target = self.root / "new"
        target.mkdir()
        for branch in ("", "--bare", "-x", "../main", "feature/../main", "refs/heads/main:other",
                       "main;echo", "main$(echo bad)", "main name", "main\\other", "main..other", ".hidden"):
            with self.subTest(branch=branch):
                result = control_plane.git_init_result(str(target), branch)
                self.assertFalse(result["ok"], result)
                self.assertEqual([], result["argv"])
        self.assertFalse((target / ".git").exists())

    def test_mcp_discovery_and_schema(self) -> None:
        tool = server_legacy.mcp._tool_manager._tools["git_init"]
        self.assertIn("path", tool.parameters["properties"])
        self.assertEqual("main", tool.parameters["properties"]["initial_branch"]["default"])
        self.assertEqual(["path"], tool.parameters["required"])


if __name__ == "__main__":
    unittest.main()
