"""Small standalone atomic-I/O helpers for host hooks.

This module deliberately has no dependency on ``agent_memory``: host hook tests and
real hosts may launch the scripts with a minimal environment and without importing
the installed package.
"""

from __future__ import annotations

import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCAL = threading.local()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def interprocess_lock(path: Path):
    """Serialize hook state changes across threads and Windows processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    depths = getattr(_LOCAL, "depths", None)
    if depths is None:
        depths = _LOCAL.depths = {}
    with _thread_lock(path):
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        depths[key] = 1
        try:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as created:
                    created.write(b"0")
                    created.flush()
                    os.fsync(created.fileno())
            with path.open("r+b") as handle:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - deployed hooks run on Windows
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            depths.pop(key, None)


def atomic_write_text(path: Path, text: str) -> None:
    """Durably replace one hook state file in its destination directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
