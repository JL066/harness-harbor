import shlex
from pathlib import PureWindowsPath
from unittest.mock import patch

import pytest

from harbor_platform import commands


pytestmark = pytest.mark.windows_ci


def _windows_expected(argv):
    return [
        PureWindowsPath(value).as_posix() if PureWindowsPath(value).is_absolute() else value
        for value in argv
    ]


def test_windows_tunnel_command_roundtrips_paths_and_mode_arguments():
    argv = [
        r"D:\tools\Harness Harbor\mcp's\harbor-runtime.exe",
        "mcp",
        "--mode",
        "中文 mode",
        r"D:\数据\配置 文件.json",
        r"relative\mode",
    ]
    with patch.object(commands.sys, "platform", "win32"):
        rendered = commands.serialize_command(argv)

    assert shlex.split(rendered) == _windows_expected(argv)
    assert shlex.quote(_windows_expected(argv)[0]) in rendered
    assert r"D:\tools" not in rendered
    assert "mcp" in rendered and "--mode" in rendered and "中文 mode" in rendered


def test_macos_keeps_existing_shlex_join_semantics():
    argv = ["/Applications/Harness Harbor/runtime", "mcp", "--mode", "中文 mode"]
    with patch.object(commands.sys, "platform", "darwin"):
        assert commands.serialize_command(argv) == shlex.join(argv)
