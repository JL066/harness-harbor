"""Robust log reader with multi-encoding detection and tail retrieval.

Handles UTF-8, UTF-16 LE/BE, BOM variations, mixed PowerShell redirection,
and shared Windows file locking without crashing.
"""

from __future__ import annotations

import re
from pathlib import Path


def decode_log_bytes(raw_bytes: bytes) -> tuple[list[str], str]:
    """Decodes raw log bytes gracefully, handling mixed UTF-8 and UTF-16 LE chunks."""
    if not raw_bytes:
        return [], "empty"

    # Check BOM first
    detected_encoding = "mixed"
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        detected_encoding = "utf-8-bom"
        raw_bytes = raw_bytes[3:]
    elif raw_bytes.startswith(b"\xff\xfe"):
        detected_encoding = "utf-16-le-bom"
        raw_bytes = raw_bytes[2:]
    elif raw_bytes.startswith(b"\xfe\xff"):
        detected_encoding = "utf-16-be-bom"
        raw_bytes = raw_bytes[2:]

    # Split lines across both UTF-16 LE newlines and UTF-8 / ASCII newlines
    # Pattern matches:
    # \r\x00\n\x00 (UTF-16 LE CRLF)
    # \n\x00 (UTF-16 LE LF)
    # \r\n (ASCII / UTF-8 CRLF)
    # \n (ASCII / UTF-8 LF)
    raw_lines = re.split(b"(?:\r\x00\n\x00|\n\x00|\r\n|\n)", raw_bytes)
    lines: list[str] = []

    for chunk in raw_lines:
        if not chunk:
            continue

        # If chunk contains null bytes, it's likely UTF-16 LE from PowerShell >> redirection
        if b"\x00" in chunk:
            try:
                # Ensure even byte length for UTF-16 LE
                padded = chunk if len(chunk) % 2 == 0 else chunk + b"\x00"
                decoded = padded.decode("utf-16-le", errors="replace")
                cleaned = decoded.replace("\x00", "").strip()
                if cleaned:
                    lines.append(cleaned)
                continue
            except Exception:
                pass

        # Attempt UTF-8
        try:
            decoded = chunk.decode("utf-8", errors="replace").replace("\x00", "").strip()
            if decoded:
                lines.append(decoded)
            continue
        except Exception:
            pass

        # Fallback to Latin-1 / CP1252
        try:
            decoded = chunk.decode("cp1252", errors="replace").replace("\x00", "").strip()
            if decoded:
                lines.append(decoded)
        except Exception:
            pass

    return lines, detected_encoding


def read_log_tail(
    file_path: Path | str,
    max_lines: int = 300,
    max_bytes: int = 512 * 1024,
) -> tuple[list[str], str]:
    """Read the last `max_lines` from a log file safely.

    Returns (lines, detected_encoding).
    Does not crash on file locking or encoding issues.
    """
    path = Path(file_path)
    if not path.exists():
        return [f"[Log file not found: {path}]"], "none"

    try:
        file_size = path.stat().st_size
    except Exception as e:
        return [f"[Error reading file status: {e}]"], "none"

    if file_size == 0:
        return ["[Log file is empty]"], "empty"

    bytes_to_read = min(file_size, max_bytes)

    try:
        # Open in binary mode with shared read access
        with open(path, "rb") as f:
            if file_size > bytes_to_read:
                f.seek(file_size - bytes_to_read)
            raw_bytes = f.read(bytes_to_read)
    except Exception as e:
        return [f"[Error reading log file: {e}]"], "none"

    lines, encoding = decode_log_bytes(raw_bytes)

    # If we started reading from mid-file, drop the first line as it may be truncated
    if file_size > bytes_to_read and len(lines) > 1:
        lines = lines[1:]

    tail_lines = lines[-max_lines:] if len(lines) > max_lines else lines
    return tail_lines, encoding
