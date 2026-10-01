from pathlib import Path

import pytest

from harbor_runtime.lifecycle import Runtime


pytestmark = pytest.mark.windows_ci


class _LogPaths:
    def __init__(self, root: Path):
        self.root = root

    def logs_dir(self):
        return self.root


class _Stream:
    def __init__(self, lines):
        self.lines = list(lines)
        self.calls = 0

    def readline(self, _limit):
        self.calls += 1
        return self.lines.pop(0)


def _runtime_for_logs(root: Path):
    runtime = object.__new__(Runtime)
    runtime.paths = _LogPaths(root)
    return runtime


def test_log_writes_utf8_when_default_is_gbk(tmp_path, monkeypatch):
    original_open = Path.open

    def gbk_default(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if encoding is None:
            encoding = "gbk"
        return original_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", gbk_default)
    runtime = _runtime_for_logs(tmp_path)

    runtime.log("tunnel", "runtime microsecond µ\n")

    assert (tmp_path / "tunnel.log").read_text(encoding="utf-8") == "runtime microsecond µ\n"


def test_drain_continues_after_one_log_oserror_and_reaches_eof():
    runtime = object.__new__(Runtime)
    logged = []

    def flaky_log(component, text):
        logged.append((component, text))
        if len(logged) == 1:
            raise OSError("simulated log sink failure")

    runtime.log = flaky_log
    stream = _Stream([b"first safe line\n", b"second safe line\n", b""])

    runtime._drain(stream, "tunnel")

    assert stream.calls == 3
    assert logged == [("tunnel", "first safe line\n"), ("tunnel", "second safe line\n")]
