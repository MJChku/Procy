"""Thread-safe terminal writes shared across procy modules."""
from __future__ import annotations

import os
import sys
import threading

TTY_WRITE_LOCK = threading.RLock()


def write_bytes(data: bytes) -> None:
    if not data:
        return
    with TTY_WRITE_LOCK:
        try:
            os.write(sys.stdout.fileno(), data)
        except OSError:
            pass


def write_stdout(text: str) -> None:
    write_bytes(text.encode("utf-8", errors="replace"))
