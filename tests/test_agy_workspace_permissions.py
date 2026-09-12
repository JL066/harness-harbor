"""The macOS mapping must not silently broaden other harness/platform policies."""
from pathlib import Path
from unittest.mock import patch

import pytest
import control_plane


def test_macos_workspace_write_uses_scoped_native_flags():
    state = {"cwd": "/tmp/task workspace", "prompt": "Create result.txt", "sandbox": "workspace-write", "agy_dangerously_skip_permissions": False}
    with patch.object(control_plane.sys, "platform", "darwin"):
        command = control_plane.build_agy_command(state, Path("result.txt"))
    assert command[command.index("--mode") + 1] == "accept-edits"
    assert command[command.index("--add-dir") + 1] == state["cwd"]
    assert command.count("--add-dir") == 1
    assert "--sandbox" in command
    assert "--dangerously-skip-permissions" not in command
    assert command[-1].startswith("--print=Harbor task workspace: /tmp/task workspace\n")
    assert command[-1].endswith(state["prompt"])
    with patch.object(control_plane.sys, "platform", "darwin"), pytest.raises(ValueError, match="read-only"):
        control_plane.build_agy_command({**state, "sandbox": "read-only"}, Path("result.txt"))


def test_windows_command_mapping_is_unchanged():
    with patch.object(control_plane.sys, "platform", "win32"):
        command = control_plane.build_agy_command({"prompt": "hello", "agy_dangerously_skip_permissions": False}, Path("result.txt"))
    assert "--mode" not in command
    assert "--add-dir" not in command
    assert command[-1] == "--print=hello"
