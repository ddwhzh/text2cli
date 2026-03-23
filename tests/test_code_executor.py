"""Tests for code executor and FUSE-related components.

FUSE mount requires libfuse + /dev/fuse, which is typically unavailable in
CI / test environments.  Tests that require FUSE are skipped when unavailable.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from text2cli.code_executor import ExecutionResult
from text2cli.db import WorkspaceDB
from text2cli.workspace_fuse import _MetaCache


class TestMetaCache(unittest.TestCase):
    def test_expired_initially(self) -> None:
        cache = _MetaCache()
        self.assertTrue(cache.expired())

    def test_refresh_clears_expired(self) -> None:
        cache = _MetaCache()
        cache.refresh([{"path": "a.txt", "size_bytes": 5}])
        self.assertFalse(cache.expired())
        self.assertIn("a.txt", cache.entries)

    def test_ttl_expiry(self) -> None:
        cache = _MetaCache(ttl=0.0)
        cache.refresh([])
        self.assertTrue(cache.expired())


class TestExecutionResult(unittest.TestCase):
    def test_display_stdout_only(self) -> None:
        r = ExecutionResult(stdout="hello\n", stderr="", exit_code=0)
        self.assertEqual(r.to_display(), "hello\n")

    def test_display_with_stderr(self) -> None:
        r = ExecutionResult(stdout="out", stderr="err", exit_code=0)
        display = r.to_display()
        self.assertIn("out", display)
        self.assertIn("[stderr]", display)
        self.assertIn("err", display)

    def test_display_timeout(self) -> None:
        r = ExecutionResult(stdout="", stderr="", exit_code=-1, timed_out=True)
        self.assertIn("[timeout]", r.to_display())

    def test_display_nonzero_exit(self) -> None:
        r = ExecutionResult(stdout="", stderr="", exit_code=1)
        self.assertIn("[exit_code=1]", r.to_display())

    def test_display_empty(self) -> None:
        r = ExecutionResult(stdout="", stderr="", exit_code=0)
        self.assertEqual(r.to_display(), "(no output)")


class TestFuseManagerConstruction(unittest.TestCase):
    """Test that FuseManager raises when FUSE is not available."""

    def test_raises_without_fuse(self) -> None:
        from text2cli.workspace_fuse import FuseManager, _fuse_mod
        if _fuse_mod is not None:
            self.skipTest("FUSE is available, skipping negative test")
        temp_dir = tempfile.TemporaryDirectory()
        try:
            db = WorkspaceDB(Path(temp_dir.name) / "test.db")
            db.init()
            with self.assertRaises(RuntimeError):
                FuseManager(db)
        finally:
            temp_dir.cleanup()


class TestWorkspaceToolsWithExecutor(unittest.TestCase):
    """Test tool counts and exec tool registration."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "test.db")
        self.db.init()
        self.db.write_file("main", "data.txt", "aaa\nbbb\nccc\n")
        self.db.commit_workspace("main", "seed")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_tools_exclude_exec_without_executor(self) -> None:
        from text2cli.workspace_tools import create_workspace_tools
        tools = create_workspace_tools(self.db, "main")
        names = {t.name for t in tools}
        self.assertNotIn("python_exec", names)
        self.assertNotIn("shell_exec", names)
        self.assertEqual(len(tools), 21)

    def test_web_search_requires_search_client(self) -> None:
        from text2cli.workspace_tools import create_workspace_tools
        tools_no_search = create_workspace_tools(self.db, "main")
        names = {t.name for t in tools_no_search}
        self.assertNotIn("web_search", names)

        from unittest.mock import MagicMock
        mock_client = MagicMock()
        tools_with_search = create_workspace_tools(self.db, "main", search_client=mock_client)
        names2 = {t.name for t in tools_with_search}
        self.assertIn("web_search", names2)
        self.assertEqual(len(tools_with_search), 22)


if __name__ == "__main__":
    unittest.main()
