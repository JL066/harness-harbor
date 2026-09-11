"""Focused contract tests for constrained host-side Git delivery."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import control_plane
import server_legacy

OLD = "a" * 40
NEW = "b" * 40
URL = "https://git.example.invalid/acme/repository.git"
SRC = "refs/heads/release"
DST = "refs/heads/main"


class GitDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("C:/disposable/repository")
        self.calls: list[list[str]] = []

    def runner(self, remote_heads: list[str], push_ok: bool = True):
        heads = iter(remote_heads)

        def fake_run(args: list[str], root: Path, timeout: int = 30) -> dict:
            self.assertEqual(self.root, root)
            self.calls.append(list(args))
            stdout = ""
            ok = True
            if args[:3] == ["remote", "get-url", "--push"]:
                stdout = URL + "\n"
            elif args[:3] == ["ls-remote", "--refs", "--exit-code"]:
                stdout = f"{next(heads)}\t{DST}\n"
            elif args[:2] == ["rev-parse", "--verify"]:
                stdout = NEW + "\n"
            elif args[:2] == ["push", "--dry-run"]:
                ok = push_ok
            elif args[:1] == ["push"]:
                self.assertTrue(push_ok)
            else:
                self.fail(f"unexpected Git delivery command: {args!r}")
            return {"ok": ok, "argv": ["git", "--no-pager", *args], "stdout": stdout,
                    "stderr": "rejected non-fast-forward" if not push_ok else "",
                    "exit_code": 0 if ok else 1, "truncated": False}

        return fake_run

    def call(self, runner, fn, *args):
        with mock.patch.object(control_plane, "resolve_repo", return_value=self.root), mock.patch.object(
            control_plane, "_run_git", side_effect=runner,
        ):
            return fn(*args)

    def test_dry_run_uses_exact_single_ref_and_hides_configured_url(self) -> None:
        result = self.call(self.runner([OLD]), control_plane.git_push_dry_run_result,
                           str(self.root), "origin", SRC, DST, OLD)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["pushed"])
        self.assertNotIn(URL, json.dumps(result))
        self.assertEqual(["push", "--dry-run", URL, f"{SRC}:{DST}"], self.calls[-1])

    def test_real_push_dry_runs_rechecks_and_verifies(self) -> None:
        result = self.call(self.runner([OLD, OLD, NEW]), control_plane.git_push_ref_result,
                           str(self.root), "origin", SRC, DST, OLD)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["dry_run"])
        self.assertTrue(result["pushed"])
        self.assertEqual(NEW, result["after_head"])
        self.assertEqual(2, sum(call[:1] == ["push"] for call in self.calls))
        push_commands = [call for call in self.calls if call[:1] == ["push"]]
        self.assertEqual(
            [["push", "--dry-run", URL, f"{SRC}:{DST}"],
             ["push", URL, f"{SRC}:{DST}"]],
            push_commands,
        )
        for command in push_commands:
            self.assertFalse(any(
                flag in command for flag in ("--force", "-f", "--all", "--mirror", "--tags", "--delete")
            ))
            self.assertEqual(1, sum(1 for arg in command if arg == f"{SRC}:{DST}"))

    def test_stale_expected_head_prevents_real_push(self) -> None:
        result = self.call(self.runner([NEW]), control_plane.git_push_ref_result,
                           str(self.root), "origin", SRC, DST, OLD)
        self.assertFalse(result["ok"])
        self.assertFalse(result["pushed"])
        self.assertIn("differs from expected_remote_head", result["error"])
        self.assertFalse(any(call[:1] == ["push"] for call in self.calls))

    def test_immediate_drift_prevents_real_push(self) -> None:
        # Initial lookup matches the expected head; the mandatory lookup just
        # before the real push observes a concurrent remote update.
        result = self.call(self.runner([OLD, NEW]), control_plane.git_push_ref_result,
                           str(self.root), "origin", SRC, DST, OLD)
        self.assertFalse(result["ok"])
        self.assertFalse(result["pushed"])
        self.assertIn("immediately before push", result["error"])
        self.assertEqual(1, sum(call[:2] == ["push", "--dry-run"] for call in self.calls))
        self.assertFalse(any(call[:1] == ["push"] and "--dry-run" not in call for call in self.calls))

    def test_invalid_inputs_never_push(self) -> None:
        invalid_refs = [
            "refs/tags/v1",
            ":refs/heads/main",
            "refs/heads/main:",
            "refs/heads/main:refs/heads/other",
            "refs/heads/main refs/heads/other",
            "refs/heads/main --force",
        ]
        for src in invalid_refs:
            self.calls.clear()
            result = self.call(self.runner([OLD]), control_plane.git_push_ref_result,
                               str(self.root), "origin", src, DST, OLD)
            self.assertFalse(result["ok"])
            self.assertFalse(any(call[:1] == ["push"] for call in self.calls))
        for dst in invalid_refs:
            self.calls.clear()
            result = self.call(self.runner([OLD]), control_plane.git_push_ref_result,
                               str(self.root), "origin", SRC, dst, OLD)
            self.assertFalse(result["ok"])
            self.assertFalse(any(call[:1] == ["push"] for call in self.calls))
        self.calls.clear()
        result = self.call(self.runner([OLD]), control_plane.git_push_ref_result,
                           str(self.root), "origin", SRC, DST, "not-a-sha")
        self.assertFalse(result["ok"])
        self.assertFalse(any(call[:1] == ["push"] for call in self.calls))
        self.calls.clear()
        result = self.call(self.runner([OLD]), control_plane.git_push_ref_result,
                           str(self.root), "https://example.invalid/repo.git", SRC, DST, OLD)
        self.assertFalse(result["ok"])
        self.assertFalse(any(call[:1] == ["push"] for call in self.calls))

    def test_rejects_untrusted_configured_urls_and_redacts_tokens(self) -> None:
        for url in ["file:///tmp/repo.git", "ssh://git@example.invalid/repo.git",
                    "https://user:password@example.invalid/repo.git", "https://127.0.0.1/repo.git"]:
            with self.subTest(url=url):
                self.assertRaises(ValueError, control_plane._validate_git_delivery_https_url, url)
        rendered = control_plane._redact_git_delivery_text("Authorization: Bearer secret ghp_" + "a" * 36)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("ghp_", rendered)

    def test_tool_registration(self) -> None:
        self.assertTrue({"git_ls_remote", "git_push_dry_run", "git_push_ref"}.issubset(
            server_legacy.mcp._tool_manager._tools))


if __name__ == "__main__":
    unittest.main()
