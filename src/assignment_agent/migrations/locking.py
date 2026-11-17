"""业务持有共享维护锁；迁移和备份持有独占锁，允许正常状态查询。"""

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from assignment_agent.migrations.inspection import MigrationError

_local = threading.local()


def _lock_file(stream, shared):
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        lock = kernel.LockFileEx
        lock.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        lock.restype = wintypes.BOOL
        # 立即失败，不能让维护命令无限等待仍在执行的业务操作。
        flags = 1 | (0 if shared else 2)
        if not lock(
            msvcrt.get_osfhandle(stream.fileno()), flags, 0, 1, 0, ctypes.byref(Overlapped())
        ):
            raise OSError(ctypes.get_last_error(), "数据库维护锁被占用")
    else:
        import fcntl

        fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)


@contextmanager
def database_lock(database, *, shared=False):
    path = Path(database).resolve()
    key = str(path)
    held = getattr(_local, "held", {})
    _local.held = held
    if key in held:
        if held[key] and not shared:
            raise MigrationError("业务访问期间不能执行维护操作")
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".maintenance.lock").open("a+b") as stream:
        try:
            _lock_file(stream, shared)
        except OSError as exc:
            raise MigrationError("数据库被其他操作占用，请停止业务服务后重试维护操作") from exc
        held[key] = shared
        try:
            yield
        finally:
            del held[key]
