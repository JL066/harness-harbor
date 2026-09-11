"""Host locks and bounded pipe reads; runtime orchestration stays portable."""
import os
import queue
import sys
import threading


def acquire_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = path.open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except OSError:
        stream.close()
        raise RuntimeError("A Harbor bridge already owns this state directory") from None


def read_line(stream, timeout=15, limit=262144):
    result = queue.Queue(maxsize=1)
    def read():
        try:
            result.put(stream.readline(limit + 1))
        except (OSError, ValueError):
            result.put(b"")
    threading.Thread(target=read, daemon=True).start()
    try:
        line = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("MCP handshake timed out") from None
    if len(line) > limit or not line.endswith(b"\n"):
        raise ValueError("Invalid MCP handshake response")
    return line
