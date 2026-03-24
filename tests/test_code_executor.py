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


class _DummyFuseManager:
    def __init__(self, work_dir: str) -> None:
        self._work_dir = work_dir

    def ensure_mounted(self, workspace: str) -> str:  # noqa: ARG002
        return self._work_dir


class TestExecutorPolicy(unittest.TestCase):
    def test_local_untrusted_disables_host_backend(self) -> None:
        from text2cli.code_executor import CodeExecutor, ExecutorPolicy, EXEC_BACKEND_HOST, EXEC_MODE_UNTRUSTED
        temp_dir = tempfile.TemporaryDirectory()
        try:
            policy = ExecutorPolicy(mode=EXEC_MODE_UNTRUSTED, backend=EXEC_BACKEND_HOST)
            exec_ = CodeExecutor(_DummyFuseManager(temp_dir.name), policy=policy)  # type: ignore[arg-type]
            result = exec_.execute("main", "python", "print('hi')", timeout=1)
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("local-untrusted", result.stderr)
            self.assertIn("T2_EXEC_BACKEND=docker", result.stderr)
        finally:
            temp_dir.cleanup()


class TestExecutorScheduler(unittest.TestCase):
    def test_auto_prefers_docker_over_remote_when_available(self) -> None:
        from text2cli.code_executor import CodeExecutor, ExecutorPolicy, EXEC_BACKEND_AUTO, EXEC_BACKEND_DOCKER, EXEC_MODE_UNTRUSTED
        from unittest.mock import patch
        policy = ExecutorPolicy(mode=EXEC_MODE_UNTRUSTED, backend=EXEC_BACKEND_AUTO, scheduler_enabled=True, remote_url="http://127.0.0.1:9770")
        exec_ = CodeExecutor(_DummyFuseManager("/tmp"), policy=policy)  # type: ignore[arg-type]
        with patch("text2cli.code_executor.shutil.which", return_value="/usr/bin/docker"):
            self.assertEqual(exec_._select_backend(), EXEC_BACKEND_DOCKER)  # noqa: SLF001

    def test_auto_uses_remote_when_docker_unavailable(self) -> None:
        from text2cli.code_executor import CodeExecutor, ExecutorPolicy, EXEC_BACKEND_AUTO, EXEC_BACKEND_REMOTE, EXEC_MODE_UNTRUSTED
        from unittest.mock import patch
        policy = ExecutorPolicy(mode=EXEC_MODE_UNTRUSTED, backend=EXEC_BACKEND_AUTO, scheduler_enabled=True, remote_url="http://127.0.0.1:9770")
        exec_ = CodeExecutor(_DummyFuseManager("/tmp"), policy=policy)  # type: ignore[arg-type]
        with patch("text2cli.code_executor.shutil.which", return_value=None):
            self.assertEqual(exec_._select_backend(), EXEC_BACKEND_REMOTE)  # noqa: SLF001


class TestExecutorSchedulerMicroVM(unittest.TestCase):
    def test_auto_can_select_microvm_when_configured(self) -> None:
        from text2cli.code_executor import (
            CodeExecutor,
            ExecutorPolicy,
            EXEC_BACKEND_AUTO,
            EXEC_BACKEND_MICROVM,
            EXEC_MODE_UNTRUSTED,
        )
        from unittest.mock import patch

        policy = ExecutorPolicy(
            mode=EXEC_MODE_UNTRUSTED,
            backend=EXEC_BACKEND_AUTO,
            scheduler_enabled=True,
            remote_url="http://127.0.0.1:9770",
            microvm_runtime="io.containerd.kata.v2",
            pool_weights={"security": 10, "latency": 1, "cost": 1},
        )
        exec_ = CodeExecutor(_DummyFuseManager("/tmp"), policy=policy)  # type: ignore[arg-type]
        with patch("text2cli.code_executor.shutil.which", return_value="/usr/bin/docker"):
            self.assertEqual(exec_._select_backend(), EXEC_BACKEND_MICROVM)  # noqa: SLF001

    def test_required_residency_filters_to_remote(self) -> None:
        from text2cli.code_executor import (
            CodeExecutor,
            ExecutorPolicy,
            EXEC_BACKEND_AUTO,
            EXEC_BACKEND_REMOTE,
            EXEC_MODE_UNTRUSTED,
        )
        from unittest.mock import patch

        policy = ExecutorPolicy(
            mode=EXEC_MODE_UNTRUSTED,
            backend=EXEC_BACKEND_AUTO,
            scheduler_enabled=True,
            remote_url="http://127.0.0.1:9770",
            required_residency="remote",
        )
        exec_ = CodeExecutor(_DummyFuseManager("/tmp"), policy=policy)  # type: ignore[arg-type]
        with patch("text2cli.code_executor.shutil.which", return_value="/usr/bin/docker"):
            self.assertEqual(exec_._select_backend(), EXEC_BACKEND_REMOTE)  # noqa: SLF001

    def test_required_permission_domain_filters_to_microvm(self) -> None:
        from text2cli.code_executor import (
            CodeExecutor,
            ExecutorPolicy,
            EXEC_BACKEND_AUTO,
            EXEC_BACKEND_MICROVM,
            EXEC_MODE_UNTRUSTED,
        )
        from unittest.mock import patch

        policy = ExecutorPolicy(
            mode=EXEC_MODE_UNTRUSTED,
            backend=EXEC_BACKEND_AUTO,
            scheduler_enabled=True,
            remote_url="http://127.0.0.1:9770",
            microvm_runtime="io.containerd.kata.v2",
            required_permission_domain="microvm",
        )
        exec_ = CodeExecutor(_DummyFuseManager("/tmp"), policy=policy)  # type: ignore[arg-type]
        with patch("text2cli.code_executor.shutil.which", return_value="/usr/bin/docker"):
            self.assertEqual(exec_._select_backend(), EXEC_BACKEND_MICROVM)  # noqa: SLF001


class TestRemoteBackend(unittest.TestCase):
    def test_remote_applies_changes_to_workspace_dir(self) -> None:
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from text2cli.code_executor import CodeExecutor, ExecutorPolicy, EXEC_BACKEND_REMOTE, EXEC_MODE_TRUSTED

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length)
                _ = json.loads(raw.decode("utf-8"))  # request payload
                resp = {
                    "stdout": "ok\n",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "changes": {"a.txt": "new", "b.txt": "created"},
                    "deletes": [],
                }
                data = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):  # noqa: A003
                return

        with tempfile.TemporaryDirectory() as td:
            Path(td, "a.txt").write_text("old", encoding="utf-8")

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            host, port = server.server_address
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                policy = ExecutorPolicy(
                    mode=EXEC_MODE_TRUSTED,
                    backend=EXEC_BACKEND_REMOTE,
                    remote_url=f"http://{host}:{port}",
                )
                exec_ = CodeExecutor(_DummyFuseManager(td), policy=policy)  # type: ignore[arg-type]
                result = exec_.execute("main", "python", "print('hi')", timeout=3)
                self.assertEqual(result.exit_code, 0)
                self.assertIn("ok", result.stdout)
                self.assertEqual(Path(td, "a.txt").read_text(encoding="utf-8"), "new")
                self.assertEqual(Path(td, "b.txt").read_text(encoding="utf-8"), "created")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
