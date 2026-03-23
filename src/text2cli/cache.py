"""Multi-level cache: Redis when available, in-process LRU fallback.

L1  blob cache    hash → content       permanent (content-addressed, LRU eviction only)
L2  file cache    ws:path → result     invalidated on write/delete/commit/rollback
L3  state cache   ws → state dict      invalidated on any workspace mutation
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol

logger = logging.getLogger(__name__)

BLOB_PREFIX = "t2:blob:"
FILE_PREFIX = "t2:file:"
STATE_PREFIX = "t2:state:"

DEFAULT_LRU_MAX = 4096
DEFAULT_REDIS_TTL = 3600


class CacheBackend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def stats(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class LocalLRUBackend:
    """Thread-safe in-process LRU cache."""

    def __init__(self, max_size: int = DEFAULT_LRU_MAX) -> None:
        self._max = max_size
        self._store: OrderedDict[str, tuple[str, float | None]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            value, expires = entry
            if expires is not None and time.monotonic() > expires:
                del self._store[key]
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        expires = (time.monotonic() + ttl) if ttl else None
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expires)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]
            return len(keys)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "backend": "local_lru",
                "size": len(self._store),
                "max_size": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }

    def close(self) -> None:
        with self._lock:
            self._store.clear()


class RedisCacheBackend:
    """Redis cache backend — only constructed when redis-py is importable."""

    def __init__(self, url: str, ttl: int = DEFAULT_REDIS_TTL) -> None:
        import redis as _redis  # type: ignore[import-untyped]
        self._client: Any = _redis.from_url(url, decode_responses=True)
        self._client.ping()
        self._default_ttl = ttl
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        val = self._client.get(key)
        with self._lock:
            if val is None:
                self._misses += 1
            else:
                self._hits += 1
        return val

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        real_ttl = ttl if ttl is not None else self._default_ttl
        if real_ttl and real_ttl > 0:
            self._client.setex(key, real_ttl, value)
        else:
            self._client.set(key, value)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def delete_prefix(self, prefix: str) -> int:
        cursor, count = "0", 0
        while True:
            cursor, keys = self._client.scan(cursor=cursor, match=f"{prefix}*", count=200)
            if keys:
                self._client.delete(*keys)
                count += len(keys)
            if cursor == 0 or cursor == "0":
                break
        return count

    def stats(self) -> dict[str, Any]:
        info = self._client.info("memory")
        with self._lock:
            total = self._hits + self._misses
            return {
                "backend": "redis",
                "used_memory_human": info.get("used_memory_human", "?"),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }

    def close(self) -> None:
        self._client.close()


class WorkspaceCache:
    """Multi-level cache wrapping a CacheBackend.

    Thread-safe.  All public methods handle backend errors gracefully (log + miss).
    """

    def __init__(self, backend: CacheBackend) -> None:
        self._b = backend

    # ── L1: blob content (content-addressed, very long TTL) ──

    def get_blob(self, blob_hash: str) -> str | None:
        try:
            return self._b.get(f"{BLOB_PREFIX}{blob_hash}")
        except Exception:
            logger.debug("cache get_blob error", exc_info=True)
            return None

    def set_blob(self, blob_hash: str, content: str) -> None:
        try:
            self._b.set(f"{BLOB_PREFIX}{blob_hash}", content, ttl=86400)
        except Exception:
            logger.debug("cache set_blob error", exc_info=True)

    # ── L2: file read results ──

    def get_file(self, workspace: str, path: str) -> dict[str, Any] | None:
        try:
            raw = self._b.get(f"{FILE_PREFIX}{workspace}:{path}")
            return json.loads(raw) if raw else None
        except Exception:
            logger.debug("cache get_file error", exc_info=True)
            return None

    def set_file(self, workspace: str, path: str, result: dict[str, Any]) -> None:
        try:
            self._b.set(f"{FILE_PREFIX}{workspace}:{path}", json.dumps(result, ensure_ascii=False))
        except Exception:
            logger.debug("cache set_file error", exc_info=True)

    # ── L3: workspace-level state ──

    def get_state(self, workspace: str, kind: str) -> dict[str, Any] | None:
        try:
            raw = self._b.get(f"{STATE_PREFIX}{workspace}:{kind}")
            return json.loads(raw) if raw else None
        except Exception:
            logger.debug("cache get_state error", exc_info=True)
            return None

    def set_state(self, workspace: str, kind: str, result: dict[str, Any]) -> None:
        try:
            self._b.set(f"{STATE_PREFIX}{workspace}:{kind}", json.dumps(result, ensure_ascii=False))
        except Exception:
            logger.debug("cache set_state error", exc_info=True)

    # ── Invalidation ──

    def invalidate_file(self, workspace: str, path: str) -> None:
        try:
            self._b.delete(f"{FILE_PREFIX}{workspace}:{path}")
        except Exception:
            logger.debug("cache invalidate_file error", exc_info=True)
        self.invalidate_workspace_state(workspace)

    def invalidate_workspace_state(self, workspace: str) -> None:
        try:
            self._b.delete_prefix(f"{STATE_PREFIX}{workspace}:")
        except Exception:
            logger.debug("cache invalidate_state error", exc_info=True)

    def invalidate_workspace(self, workspace: str) -> None:
        """Full workspace invalidation: all file cache + state cache."""
        try:
            self._b.delete_prefix(f"{FILE_PREFIX}{workspace}:")
            self._b.delete_prefix(f"{STATE_PREFIX}{workspace}:")
        except Exception:
            logger.debug("cache invalidate_workspace error", exc_info=True)

    # ── Stats / lifecycle ──

    def stats(self) -> dict[str, Any]:
        try:
            return self._b.stats()
        except Exception:
            return {"backend": "error"}

    def close(self) -> None:
        try:
            self._b.close()
        except Exception:
            pass


def create_cache(
    redis_url: str | None = None,
    *,
    lru_max: int = DEFAULT_LRU_MAX,
    redis_ttl: int = DEFAULT_REDIS_TTL,
) -> WorkspaceCache:
    """Auto-detect: try Redis, fall back to in-process LRU."""
    url = redis_url or os.environ.get("T2_REDIS_URL")
    if url:
        try:
            backend = RedisCacheBackend(url, ttl=redis_ttl)
            logger.info("Cache backend: Redis (%s)", url)
            return WorkspaceCache(backend)
        except Exception as exc:
            logger.warning("Redis unavailable (%s), falling back to LRU: %s", url, exc)
    backend = LocalLRUBackend(max_size=lru_max)
    logger.info("Cache backend: local LRU (max=%d)", lru_max)
    return WorkspaceCache(backend)
