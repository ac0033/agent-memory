"""跨文件写入使用的进程锁与原子文本替换。"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_local = threading.local()


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _locks_guard:
        return _thread_locks.setdefault(key, threading.RLock())


@contextmanager
def interprocess_lock(path: Path) -> Iterator[None]:
    """同一路径的线程/进程互斥锁；支持同线程嵌套。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    lock = _thread_lock(path)
    with lock:
        depths = getattr(_local, "depths", {})
        depth = depths.get(key, 0)
        depths[key] = depth + 1
        _local.depths = depths
        handle = None
        try:
            if depth == 0:
                handle = path.open("a+b")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:  # pragma: no cover - Windows 是本项目主要部署环境
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            depths[key] -= 1
            if depths[key] == 0:
                del depths[key]
                if handle is not None:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:  # pragma: no cover
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """同目录临时文件写完并 fsync 后原子替换目标。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
