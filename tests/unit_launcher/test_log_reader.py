"""Unit tests for Log Reader."""

import tempfile
from pathlib import Path
from launcher.log_reader import decode_log_bytes, read_log_tail


def test_decode_utf8():
    data = "Line 1\nLine 2\nLine 3".encode("utf-8")
    lines, enc = decode_log_bytes(data)
    assert lines == ["Line 1", "Line 2", "Line 3"]


def test_decode_utf8_bom():
    data = b"\xef\xbb\xbfLine 1\r\nLine 2"
    lines, enc = decode_log_bytes(data)
    assert enc == "utf-8-bom"
    assert lines == ["Line 1", "Line 2"]


def test_decode_utf16_le_bom():
    data = "Line 1\r\nLine 2".encode("utf-16-le")
    lines, enc = decode_log_bytes(b"\xff\xfe" + data)
    assert enc == "utf-16-le-bom"
    assert lines == ["Line 1", "Line 2"]


def test_decode_mixed_powershell_redirection():
    """Simulate PowerShell >> redirection mixed with UTF-8 Add-Content."""
    # Line 1: UTF-8 Add-Content
    p1 = "[2026-09-03] Starting Harness Harbor daemon\r\n".encode("utf-8")
    # Line 2: PowerShell >> UTF-16 LE redirection
    p2 = "Harness Harbor daemon started: C:\\Users\\Example\\HarnessHarbor\\.jobs\r\n".encode("utf-16-le")

    mixed = p1 + p2
    lines, enc = decode_log_bytes(mixed)
    assert len(lines) == 2
    assert "Starting Harness Harbor daemon" in lines[0]
    assert "Harness Harbor daemon started" in lines[1]
    # Verify no raw null bytes remain
    assert not any("\x00" in l for l in lines)


def test_read_log_tail_file(tmp_path):
    log_file = tmp_path / "test.log"
    content = "\n".join(f"Log message {i}" for i in range(100))
    log_file.write_text(content, encoding="utf-8")

    lines, enc = read_log_tail(log_file, max_lines=10)
    assert len(lines) == 10
    assert lines[-1] == "Log message 99"


def test_read_log_nonexistent():
    lines, enc = read_log_tail(Path("Z:\\nonexistent\\path\\file.log"))
    assert "Log file not found" in lines[0]
