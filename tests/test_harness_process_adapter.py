import json
import subprocess
import unittest
from unittest import mock

import harness_process_adapter
from harness_process_adapter import (
    MacOSProcessActivityAdapter,
    UnavailableProcessActivityAdapter,
    WindowsProcessActivityAdapter,
    default_process_activity_adapter,
)


class HarnessProcessAdapterTests(unittest.TestCase):
    harnesses = ("codex", "agy", "minimax")

    def test_macos_discovery_identifies_all_harnesses_without_exposing_command_lines(self) -> None:
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                ["ps"],
                0,
                stdout="\n".join(
                    (
                        "/usr/local/bin/codex codex exec --api-key secret-value",
                        "/usr/local/bin/codex codex app-server",
                        "/usr/local/bin/agy agy run",
                        "/opt/homebrew/bin/node node /Users/test/.minimax-code/node_modules/cli.js",
                        "/opt/homebrew/bin/mcode mcode exec",
                    )
                ),
                stderr="",
            )
        )
        with mock.patch.object(harness_process_adapter.sys, "platform", "darwin"):
            observed = MacOSProcessActivityAdapter(runner).observe(self.harnesses)

        self.assertEqual({name: observed[name]["process_count"] for name in self.harnesses}, {
            "codex": 1,
            "agy": 1,
            "minimax": 2,
        })
        self.assertNotIn("secret-value", repr(observed))
        runner.assert_called_once_with(["ps", "-axo", "comm=,args="], timeout=5)

    def test_macos_default_adapter_is_selected(self) -> None:
        with mock.patch.object(harness_process_adapter.sys, "platform", "darwin"):
            adapter = default_process_activity_adapter(mock.Mock())
        self.assertIsInstance(adapter, MacOSProcessActivityAdapter)

    def test_macos_discovery_failure_is_fail_soft(self) -> None:
        runner = mock.Mock(side_effect=OSError("ps unavailable"))
        with mock.patch.object(harness_process_adapter.sys, "platform", "darwin"):
            observed = MacOSProcessActivityAdapter(runner).observe(self.harnesses)
        self.assertEqual({name: observed[name]["process_count"] for name in self.harnesses}, {
            "codex": 0,
            "agy": 0,
            "minimax": 0,
        })
        self.assertEqual(observed["codex"]["error"], "macOS process discovery failed")

    def test_windows_adapter_contract_remains_unchanged(self) -> None:
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess(
                ["powershell.exe"],
                0,
                stdout=json.dumps([
                    {"Name": "codex.exe", "CommandLine": "codex.exe exec"},
                    {"Name": "codex.exe", "CommandLine": "codex.exe app-server"},
                    {"Name": "agy.exe", "CommandLine": "agy.exe run"},
                    {"Name": "node.exe", "CommandLine": r"node C:\Users\test\.minimax-code\node_modules\cli.js"},
                ]),
                stderr="",
            )
        )
        with mock.patch.object(harness_process_adapter.sys, "platform", "win32"):
            observed = WindowsProcessActivityAdapter(runner).observe(self.harnesses)
        self.assertEqual(observed["codex"]["process_count"], 1)
        self.assertEqual(observed["agy"]["process_count"], 1)
        self.assertEqual(observed["minimax"]["process_count"], 1)
        self.assertEqual(runner.call_args.args[0][0], "powershell.exe")

    def test_unsupported_platform_uses_unavailable_adapter(self) -> None:
        with mock.patch.object(harness_process_adapter.sys, "platform", "linux"):
            adapter = default_process_activity_adapter(mock.Mock())
        self.assertIsInstance(adapter, UnavailableProcessActivityAdapter)


if __name__ == "__main__":
    unittest.main()
