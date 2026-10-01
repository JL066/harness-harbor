"""Focused, local-only contract tests for constrained host-side Git delivery."""

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

    def _run_with(self, *, remote_heads: list[str], push_ok: bool = True, stderr: str = ""):
        heads = iter(remote_heads)

        def fake_run(args: list[str], root: Path, timeout: int = 30) -> dict:
            self.assertEqual(self.root, root)
            self.calls.append(list(args))
            stdout = ""
            ok = True
            exit_code = 0
            if args[:3] == ["remote", "get-url", "--push"]:
                stdout = URL + "\n"
            elif args[:3] == ["ls-remote", "--refs", "--exit-code"]:
                stdout = f"{next(heads)}\t{DST}\n"
            elif args[:2] == ["rev-parse", "--verify"]:
                stdout = NEW + "\n"
            elif args[:2] == ["push", "--dry-run"]:
                ok = push_ok
                exit_code = 0 if push_ok else 1
            elif args[:1] == ["push"]:
                self.assertTrue(push_ok, "real push must not follow a failed dry-run")
            else:
                self.fail(f"unexpected Git delivery command: {args!r}")
            return {
                "ok": ok,
                "argv": ["git", "--no-pager", *args],
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "truncated": False,
            }

        return fake_run

    def _patch_delivery(self, runner):
        return mock.patch.multiple(
            control_plane,
            resolve_repo=mock.DEFAULT,
            _run_git=mock.DEFAULT,
        ), runner

    def _call(self, runner, fn, *args):
        with mock.patch.object(control_plane, "resolve_repo", return_value=self.root), mock.patch.object(
            control_plane, "_run_git", side_effect=runner,
        ):
            return fn(*args)

    def test_exact_head_single_branch_dry_run_succeeds_and_hides_url(self) -> None:
        result = self._call(
            self._run_with(remote_heads=[OLD]),
            control_plane.git_push_dry_run_result,
            str(self.root), "origin", SRC, DST, OLD,
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["pushed"])
        self.assertEqual(OLD, result["before_head"])
        self.assertEqual(OLD, result["after_head"])
        self.assertIn("<configured-https-remote>", result["argv"])
        self.assertNotIn(URL, json.dumps(result))
        self.assertEqual(
            ["push", "--dry-run", URL, f"{SRC}:{DST}"], self.calls[-1],
        )

    def test_exact_head_fast_forward_push_succeeds_and_after_head_is_verified(self) -> None:
        result = self._call(
            self._run_with(remote_heads=[OLD, OLD, NEW]),
            control_plane.git_push_ref_result,
            str(self.root), "origin", SRC, DST, OLD,
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["pushed"])
        self.assertEqual(NEW, result["after_head"])
        pushes = [call for call in self.calls if call[:1] == ["push"]]
        self.assertEqual(2, len(pushes))
        self.assertEqual(["push", "--dry-run", URL, f"{SRC}:{DST}"], pushes[0])
        self.assertEqual(["push", URL, f"{SRC}:{DST}"], pushes[1])
        self.assertFalse(any(call[:1] == ["config"] for call in self.calls))

    def test_stale_expected_head_rejects_before_any_push(self) -> None:
        result = self._call(
            self._run_with(remote_heads=[OLD]),
            control_plane.git_push_dry_run_result,
            str(self.root), "origin", SRC, DST, "c" * 40,
        )
        self.assertFalse(result["ok"])
        self.assertIn("differs", result["error"])
        self.assertFalse(any(call[:1] == ["push"] for call in self.calls))

    def test_non_fast_forward_dry_run_failure_prevents_real_push(self) -> None:
        result = self._call(
            self._run_with(remote_heads=[OLD], push_ok=False, stderr="rejected non-fast-forward"),
            control_plane.git_push_ref_result,
            str(self.root), "origin", SRC, DST, OLD,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["pushed"])
        self.assertIn("non-fast-forward", result["stderr"])
        self.assertEqual(1, sum(call[:2] == ["push", "--dry-run"] for call in self.calls))
        self.assertFalse(any(call[:1] == ["push"] and call[:2] != ["push", "--dry-run"] for call in self.calls))

    def test_rejects_force_tags_delete_wildcards_and_malformed_heads_without_push(self) -> None:
        invalid = [
            ("--force", DST, OLD),
            ("+refs/heads/release", DST, OLD),
            ("refs/tags/v1", DST, OLD),
            ("refs/heads/*", DST, OLD),
            ("refs/heads/release:refs/heads/main", DST, OLD),
            (SRC, "refs/tags/v1", OLD),
            (SRC, "refs/heads/*", OLD),
            (SRC, "refs/heads/main", ""),
            (SRC, "refs/heads/main", "abc"),
        ]
        for src, dst, expected in invalid:
            with self.subTest(src=src, dst=dst, expected=expected):
                self.calls.clear()
                result = self._call(
                    self._run_with(remote_heads=[OLD]),
                    control_plane.git_push_dry_run_result,
                    str(self.root), "origin", src, dst, expected,
                )
                self.assertFalse(result["ok"])
                self.assertFalse(any(call[:1] == ["push"] for call in self.calls))

    def test_rejects_untrusted_or_credential_bearing_configured_urls(self) -> None:
        for url in [
            "file:///tmp/repo.git", "ssh://git@example.invalid/acme/repo.git",
            "git@example.invalid:acme/repo.git", "https://user:password@example.invalid/a.git",
            "https://127.0.0.1/a.git", "https://localhost/a.git", "https://example.invalid/a.git?token=secret",
        ]:
            with self.subTest(url=url):
                self.assertRaises(ValueError, control_plane._validate_git_delivery_https_url, url)

    def test_timeout_and_credential_shaped_stderr_are_sanitized(self) -> None:
        def timeout_runner(args: list[str], root: Path, timeout: int = 30) -> dict:
            self.calls.append(list(args))
            return {
                "ok": False,
                "error": "git command terminated after exceeding 30 seconds; token=top-secret",
                "argv": ["git", "--no-pager", *args],
                "stdout": "",
                "stderr": "Authorization: Bearer top-secret https://user:pass@example.invalid/a.git",
                "exit_code": -1,
                "truncated": False,
            }

        result = self._call(
            timeout_runner, control_plane.git_push_dry_run_result,
            str(self.root), "origin", SRC, DST, OLD,
        )
        rendered = json.dumps(result)
        self.assertFalse(result["ok"])
        self.assertEqual(-1, result["exit_code"])
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("user:pass", rendered)
        self.assertIn("<redacted>", rendered)
        self.assertFalse(any(call[:1] == ["push"] for call in self.calls))

    def test_unlabelled_common_token_shapes_are_redacted(self) -> None:
        token = "ghp_" + "a" * 36
        rendered = control_plane._redact_git_delivery_text(f"remote failed: {token}")
        self.assertNotIn(token, rendered)
        self.assertIn("<redacted>", rendered)

    def test_remote_lookup_is_configured_name_only_and_tool_is_registered(self) -> None:
        result = self._call(
            self._run_with(remote_heads=[OLD]), control_plane.git_ls_remote_result,
            str(self.root), "https://example.invalid/repo.git", DST,
        )
        self.assertFalse(result["ok"])
        self.assertIn("configured remote name", result["error"])
        self.assertTrue({"git_ls_remote", "git_push_dry_run", "git_push_ref"}.issubset(server_legacy.mcp._tool_manager._tools))


if __name__ == "__main__":
    unittest.main()
