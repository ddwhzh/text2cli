"""FUSE filesystem that exposes a WorkspaceDB workspace as a POSIX mount.

Uses ``fusepy`` (synchronous API). Each workspace is mounted at
``<mount_base>/<workspace>/`` via a daemon thread.  All file I/O is
transparently forwarded to :class:`WorkspaceDB`.

Requires ``fusepy`` and a working ``libfuse`` / macFUSE installation.
If FUSE is unavailable, :class:`FuseManager` will raise at construction
time -- callers should handle this and disable code execution.
"""
from __future__ import annotations

import errno
import logging
import os
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .db import NotFoundError, WorkspaceDB, WorkspaceError

logger = logging.getLogger(__name__)

try:
    import fuse as _fuse_mod
except (ImportError, OSError):
    _fuse_mod = None  # type: ignore[assignment]


@dataclass
class _MetaCache:
    """Lightweight TTL cache for file metadata to avoid per-syscall DB hits."""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Ensure a freshly created cache is treated as expired so the first getattr/readdir
    # refreshes metadata instead of returning an empty listing for up to ttl seconds.
    ts: float = -1e9
    ttl: float = 2.0

    def expired(self) -> bool:
        return (time.monotonic() - self.ts) > self.ttl

    def refresh(self, files: list[dict[str, Any]]) -> None:
        self.entries = {f["path"]: f for f in files}
        self.ts = time.monotonic()


class WorkspaceFUSE:
    """FUSE Operations implementation backed by :class:`WorkspaceDB`."""

    def __init__(self, db: WorkspaceDB, workspace: str) -> None:
        self.db = db
        self.workspace = workspace
        self._meta = _MetaCache()
        self._write_buf: dict[str, bytearray] = {}
        self._fh = 0
        self._lock = threading.Lock()

    def _refresh_meta(self) -> dict[str, dict[str, Any]]:
        if self._meta.expired():
            try:
                files = self.db.list_files(self.workspace)["files"]
            except WorkspaceError:
                files = []
            self._meta.refresh(files)
        return self._meta.entries

    def _invalidate(self) -> None:
        self._meta.ts = 0.0

    def getattr(self, path: str, fh: Any = None) -> dict[str, Any]:
        rel = path.lstrip("/")
        now = time.time()
        base = {
            "st_atime": now,
            "st_mtime": now,
            "st_ctime": now,
            "st_uid": os.getuid(),
            "st_gid": os.getgid(),
        }
        if rel == "":
            return {**base, "st_mode": stat.S_IFDIR | 0o755, "st_nlink": 2, "st_size": 0}

        entries = self._refresh_meta()

        if rel in entries:
            size = entries[rel].get("size_bytes", 0) or 0
            return {**base, "st_mode": stat.S_IFREG | 0o644, "st_nlink": 1, "st_size": size}

        prefix = rel + "/"
        for p in entries:
            if p.startswith(prefix):
                return {**base, "st_mode": stat.S_IFDIR | 0o755, "st_nlink": 2, "st_size": 0}

        raise _fuse_error(errno.ENOENT)

    def readdir(self, path: str, fh: Any) -> list[str]:
        rel = path.lstrip("/")
        prefix = (rel + "/") if rel else ""
        entries = self._refresh_meta()
        result: list[str] = [".", ".."]
        seen_dirs: set[str] = set()
        for p in entries:
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if not rest:
                continue
            if "/" in rest:
                dirname = rest.split("/", 1)[0]
                if dirname not in seen_dirs:
                    seen_dirs.add(dirname)
                    result.append(dirname)
            else:
                result.append(rest)
        return result

    def open(self, path: str, flags: int) -> int:
        with self._lock:
            self._fh += 1
            return self._fh

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        rel = path.lstrip("/")
        with self._lock:
            if rel in self._write_buf:
                data = bytes(self._write_buf[rel])
                return data[offset : offset + size]
        try:
            content = self.db.read_file(self.workspace, rel)["content"]
        except (NotFoundError, WorkspaceError):
            raise _fuse_error(errno.ENOENT)
        data = content.encode("utf-8")
        return data[offset : offset + size]

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        rel = path.lstrip("/")
        with self._lock:
            if rel not in self._write_buf:
                try:
                    existing = self.db.read_file(self.workspace, rel)["content"]
                    self._write_buf[rel] = bytearray(existing.encode("utf-8"))
                except (NotFoundError, WorkspaceError):
                    self._write_buf[rel] = bytearray()
            buf = self._write_buf[rel]
            end = offset + len(data)
            if end > len(buf):
                buf.extend(b"\x00" * (end - len(buf)))
            buf[offset:end] = data
        return len(data)

    def create(self, path: str, mode: int, fi: Any = None) -> int:
        rel = path.lstrip("/")
        with self._lock:
            self._write_buf[rel] = bytearray()
            self._fh += 1
            return self._fh

    def truncate(self, path: str, length: int, fh: Any = None) -> None:
        rel = path.lstrip("/")
        with self._lock:
            if rel not in self._write_buf:
                try:
                    existing = self.db.read_file(self.workspace, rel)["content"]
                    self._write_buf[rel] = bytearray(existing.encode("utf-8"))
                except (NotFoundError, WorkspaceError):
                    self._write_buf[rel] = bytearray()
            self._write_buf[rel] = self._write_buf[rel][:length]

    def flush(self, path: str, fh: int) -> None:
        self._flush_path(path.lstrip("/"))

    def release(self, path: str, fh: int) -> None:
        self._flush_path(path.lstrip("/"))

    def _flush_path(self, rel: str) -> None:
        with self._lock:
            buf = self._write_buf.pop(rel, None)
        if buf is not None:
            content = buf.decode("utf-8", errors="replace")
            try:
                self.db.write_file(self.workspace, rel, content)
            except WorkspaceError:
                logger.exception("FUSE flush failed for %s/%s", self.workspace, rel)
            self._invalidate()

    def unlink(self, path: str) -> None:
        rel = path.lstrip("/")
        try:
            self.db.delete_file(self.workspace, rel)
        except WorkspaceError:
            raise _fuse_error(errno.ENOENT)
        self._invalidate()

    def rename(self, old: str, new: str) -> None:
        old_rel = old.lstrip("/")
        new_rel = new.lstrip("/")
        try:
            content = self.db.read_file(self.workspace, old_rel)["content"]
            self.db.write_file(self.workspace, new_rel, content)
            self.db.delete_file(self.workspace, old_rel)
        except WorkspaceError:
            raise _fuse_error(errno.ENOENT)
        self._invalidate()

    def mkdir(self, path: str, mode: int) -> None:
        pass

    def rmdir(self, path: str) -> None:
        pass

    def chmod(self, path: str, mode: int) -> None:
        pass

    def chown(self, path: str, uid: int, gid: int) -> None:
        pass

    def utimens(self, path: str, times: Any = None) -> None:
        pass


def _fuse_error(err: int) -> OSError:
    if _fuse_mod is not None and hasattr(_fuse_mod, "FuseOSError"):
        return _fuse_mod.FuseOSError(err)
    return OSError(err, os.strerror(err))


class FuseManager:
    """Manage per-workspace FUSE mounts; create on demand in daemon threads.

    Raises :class:`RuntimeError` if FUSE is not available.
    """

    def __init__(self, db: WorkspaceDB, mount_base: str | None = None) -> None:
        if _fuse_mod is None:
            raise RuntimeError(
                "FUSE is not available: install fusepy and libfuse/macFUSE. "
                "Code execution via FUSE will be disabled."
            )
        self.db = db
        self.mount_base = mount_base or os.path.join(tempfile.gettempdir(), "t2-fuse")
        self._mounts: dict[str, threading.Thread] = {}

    def ensure_mounted(self, workspace: str) -> str:
        """Return the mount point path for *workspace*, mounting if needed."""
        mount_point = os.path.join(self.mount_base, workspace)
        if workspace in self._mounts and self._mounts[workspace].is_alive():
            return mount_point

        os.makedirs(mount_point, exist_ok=True)
        fs = WorkspaceFUSE(self.db, workspace)

        def _run() -> None:
            try:
                _fuse_mod.FUSE(fs, mount_point, foreground=True, nothreads=True, allow_other=False)
            except Exception:
                logger.exception("FUSE mount failed for workspace=%s", workspace)

        t = threading.Thread(target=_run, daemon=True, name=f"fuse-{workspace}")
        t.start()
        time.sleep(0.3)
        self._mounts[workspace] = t
        logger.info("FUSE mounted: %s -> %s", workspace, mount_point)
        return mount_point

    def shutdown(self) -> None:
        for ws in list(self._mounts):
            self._unmount(ws)

    def _unmount(self, workspace: str) -> None:
        mount_point = os.path.join(self.mount_base, workspace)
        thread = self._mounts.pop(workspace, None)
        try:
            import subprocess
            subprocess.run(["fusermount", "-u", mount_point], capture_output=True, timeout=5)
        except Exception:
            try:
                import subprocess
                subprocess.run(["umount", mount_point], capture_output=True, timeout=5)
            except Exception:
                pass
        if thread and thread.is_alive():
            thread.join(timeout=2)
