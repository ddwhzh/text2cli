"""Tests for the cache layer (L1 blob, L2 file, L3 state)."""
from __future__ import annotations

import tempfile
import threading
import os

import pytest

from text2cli.cache import LocalLRUBackend, WorkspaceCache, create_cache
from text2cli.db import WorkspaceDB


@pytest.fixture
def cache():
    return create_cache()


@pytest.fixture
def cached_db():
    with tempfile.TemporaryDirectory() as td:
        c = create_cache()
        db = WorkspaceDB(os.path.join(td, "test.db"), cache=c)
        db.init()
        yield db, c


# ── LocalLRUBackend ──


class TestLocalLRU:
    def test_get_set(self):
        b = LocalLRUBackend(max_size=10)
        assert b.get("k1") is None
        b.set("k1", "v1")
        assert b.get("k1") == "v1"

    def test_lru_eviction(self):
        b = LocalLRUBackend(max_size=3)
        b.set("a", "1")
        b.set("b", "2")
        b.set("c", "3")
        b.set("d", "4")
        assert b.get("a") is None
        assert b.get("b") == "2"

    def test_ttl_expiry(self):
        import time
        b = LocalLRUBackend(max_size=10)
        b.set("k", "v", ttl=1)
        assert b.get("k") == "v"
        time.sleep(1.05)
        assert b.get("k") is None

    def test_delete(self):
        b = LocalLRUBackend(max_size=10)
        b.set("k", "v")
        b.delete("k")
        assert b.get("k") is None

    def test_delete_prefix(self):
        b = LocalLRUBackend(max_size=10)
        b.set("pre:a", "1")
        b.set("pre:b", "2")
        b.set("other:c", "3")
        count = b.delete_prefix("pre:")
        assert count == 2
        assert b.get("pre:a") is None
        assert b.get("other:c") == "3"

    def test_stats(self):
        b = LocalLRUBackend(max_size=10)
        b.set("k", "v")
        b.get("k")
        b.get("missing")
        stats = b.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["backend"] == "local_lru"

    def test_thread_safety(self):
        b = LocalLRUBackend(max_size=1000)
        errors = []

        def writer(prefix: str):
            try:
                for i in range(200):
                    b.set(f"{prefix}:{i}", f"val{i}")
            except Exception as e:
                errors.append(e)

        def reader(prefix: str):
            try:
                for i in range(200):
                    b.get(f"{prefix}:{i}")
            except Exception as e:
                errors.append(e)

        threads = []
        for p in ["a", "b", "c", "d"]:
            threads.append(threading.Thread(target=writer, args=(p,)))
            threads.append(threading.Thread(target=reader, args=(p,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors


# ── WorkspaceCache ──


class TestWorkspaceCache:
    def test_blob_cache(self, cache: WorkspaceCache):
        assert cache.get_blob("hash1") is None
        cache.set_blob("hash1", "content1")
        assert cache.get_blob("hash1") == "content1"

    def test_file_cache(self, cache: WorkspaceCache):
        assert cache.get_file("ws", "a.txt") is None
        cache.set_file("ws", "a.txt", {"content": "hello"})
        assert cache.get_file("ws", "a.txt")["content"] == "hello"

    def test_state_cache(self, cache: WorkspaceCache):
        assert cache.get_state("ws", "list") is None
        cache.set_state("ws", "list", {"files": [1, 2]})
        assert cache.get_state("ws", "list")["files"] == [1, 2]

    def test_invalidate_file(self, cache: WorkspaceCache):
        cache.set_file("ws", "a.txt", {"content": "x"})
        cache.set_state("ws", "list", {"files": []})
        cache.invalidate_file("ws", "a.txt")
        assert cache.get_file("ws", "a.txt") is None
        assert cache.get_state("ws", "list") is None

    def test_invalidate_workspace(self, cache: WorkspaceCache):
        cache.set_file("ws", "a.txt", {"content": "x"})
        cache.set_file("ws", "b.txt", {"content": "y"})
        cache.set_state("ws", "tree", {"tree": {}})
        cache.set_blob("hash99", "blob_data")
        cache.invalidate_workspace("ws")
        assert cache.get_file("ws", "a.txt") is None
        assert cache.get_file("ws", "b.txt") is None
        assert cache.get_state("ws", "tree") is None
        assert cache.get_blob("hash99") == "blob_data"

    def test_blob_survives_workspace_invalidation(self, cache: WorkspaceCache):
        cache.set_blob("immutable", "forever")
        cache.invalidate_workspace("any_ws")
        assert cache.get_blob("immutable") == "forever"


# ── Integration with WorkspaceDB ──


class TestCachedDB:
    def test_read_file_cached(self, cached_db: tuple):
        db, cache = cached_db
        db.write_file("main", "test.txt", "hello")
        r1 = db.read_file("main", "test.txt")
        assert r1["content"] == "hello"
        r2 = db.read_file("main", "test.txt")
        assert r2["content"] == "hello"
        stats = cache.stats()
        assert stats["hits"] > 0

    def test_write_invalidates_cache(self, cached_db: tuple):
        db, cache = cached_db
        db.write_file("main", "f.txt", "v1")
        db.read_file("main", "f.txt")
        db.write_file("main", "f.txt", "v2")
        r = db.read_file("main", "f.txt")
        assert r["content"] == "v2"

    def test_delete_invalidates_cache(self, cached_db: tuple):
        db, cache = cached_db
        db.write_file("main", "del.txt", "content")
        db.read_file("main", "del.txt")
        db.delete_file("main", "del.txt")
        with pytest.raises(Exception):
            db.read_file("main", "del.txt")

    def test_commit_invalidates_workspace(self, cached_db: tuple):
        db, cache = cached_db
        db.write_file("main", "c.txt", "data")
        db.list_files("main")
        db.commit_workspace("main", "commit msg")
        result = db.list_files("main")
        assert any(f["path"] == "c.txt" for f in result["files"])

    def test_rollback_invalidates_workspace(self, cached_db: tuple):
        db, cache = cached_db
        db.write_file("main", "rb.txt", "data")
        db.list_files("main")
        db.rollback_staged("main")
        result = db.list_files("main")
        assert not any(f["path"] == "rb.txt" for f in result["files"])

    def test_tree_cached(self, cached_db: tuple):
        db, cache = cached_db
        db.write_file("main", "dir/file.txt", "nested")
        t1 = db.tree_workspace("main")
        t2 = db.tree_workspace("main")
        assert t1 == t2
        stats = cache.stats()
        assert stats["hits"] >= 1

    def test_concurrent_cached_reads(self, cached_db: tuple):
        db, cache = cached_db
        for i in range(10):
            db.write_file("main", f"f{i}.txt", f"content {i}")

        errors = []
        results = []

        def reader(idx: int):
            try:
                for _ in range(5):
                    r = db.read_file("main", f"f{idx}.txt")
                    results.append(r["content"])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(results) == 50
        stats = cache.stats()
        assert stats["hits"] > 0
