from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from text2cli.db import ConflictError, NotFoundError, PolicyRejection, ValidationError, WorkspaceDB


class ConcurrentAgentTest(unittest.TestCase):
    """Simulate N agents forking, editing, committing, and merging concurrently."""

    AGENT_COUNT = 8

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "concurrent.db"
        self.db = WorkspaceDB(self.db_path)
        self.db.init()
        self.db.write_file("main", "README.md", "base content")
        self.db.commit_workspace("main", "seed")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parallel_isolated_writes(self) -> None:
        """Each agent forks, writes its own file, commits -- no conflicts."""
        barrier = threading.Barrier(self.AGENT_COUNT)
        errors: list[Exception] = []

        def agent_work(agent_id: int) -> None:
            try:
                ws_name = f"agent-{agent_id}"
                self.db.create_workspace(ws_name, from_workspace="main")
                barrier.wait(timeout=10)
                self.db.write_file(ws_name, f"output/agent-{agent_id}.txt", f"result from agent {agent_id}")
                self.db.commit_workspace(ws_name, f"agent-{agent_id} output")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=agent_work, args=(i,)) for i in range(self.AGENT_COUNT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"Agent errors: {errors}")

        for i in range(self.AGENT_COUNT):
            result = self.db.read_file(f"agent-{i}", f"output/agent-{i}.txt")
            self.assertEqual(result["content"], f"result from agent {i}")

    def test_parallel_merges_to_main(self) -> None:
        """Agents write non-overlapping files and merge sequentially after parallel commits."""
        for i in range(self.AGENT_COUNT):
            self.db.create_workspace(f"worker-{i}", from_workspace="main")

        barrier = threading.Barrier(self.AGENT_COUNT)
        errors: list[Exception] = []

        def agent_commit(agent_id: int) -> None:
            try:
                ws = f"worker-{agent_id}"
                barrier.wait(timeout=10)
                self.db.write_file(ws, f"data/file-{agent_id}.txt", f"data-{agent_id}")
                self.db.commit_workspace(ws, f"worker-{agent_id} commit")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=agent_commit, args=(i,)) for i in range(self.AGENT_COUNT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"Commit errors: {errors}")

        merged_count = 0
        for i in range(self.AGENT_COUNT):
            ws = f"worker-{i}"
            try:
                self.db.merge_workspace(ws, "main", f"merge worker-{i}")
                merged_count += 1
            except ConflictError:
                pass

        self.assertGreater(merged_count, 0, "At least some merges should succeed")

        files = self.db.list_files("main")
        main_paths = {f["path"] for f in files["files"]}
        self.assertIn("README.md", main_paths)

    def test_snapshot_isolation_during_write(self) -> None:
        """A snapshot taken before another agent's commit should not see the new data."""
        self.db.create_workspace("writer", from_workspace="main")
        self.db.create_workspace("reader", from_workspace="main")

        snapshot_before = self.db.list_files("reader")
        paths_before = {f["path"] for f in snapshot_before["files"]}

        self.db.write_file("writer", "new-file.txt", "invisible to reader")
        self.db.commit_workspace("writer", "writer adds file")

        snapshot_after = self.db.list_files("reader")
        paths_after = {f["path"] for f in snapshot_after["files"]}

        self.assertEqual(paths_before, paths_after, "Reader workspace should not see writer's commit")

        writer_files = self.db.list_files("writer")
        writer_paths = {f["path"] for f in writer_files["files"]}
        self.assertIn("new-file.txt", writer_paths)


class MVCCFeatureTest(unittest.TestCase):
    """Test MVCC-specific features: time travel, snapshots, rollback, reset."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "mvcc.db"
        self.db = WorkspaceDB(self.db_path)
        self.db.init()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_time_travel_read(self) -> None:
        self.db.write_file("main", "doc.txt", "version-1")
        c1 = self.db.commit_workspace("main", "v1")

        self.db.write_file("main", "doc.txt", "version-2")
        c2 = self.db.commit_workspace("main", "v2")

        current = self.db.read_file("main", "doc.txt")
        self.assertEqual(current["content"], "version-2")

        historical = self.db.read_file_at("main", "doc.txt", c1["commit_id"])
        self.assertEqual(historical["content"], "version-1")
        self.assertEqual(historical["source"], "historical")

    def test_named_snapshot(self) -> None:
        self.db.write_file("main", "README.md", "snapshot test")
        commit = self.db.commit_workspace("main", "for snapshot")

        snap = self.db.create_snapshot("main", "release-v1")
        self.assertEqual(snap["commit_id"], commit["commit_id"])

        snaps = self.db.list_snapshots()
        self.assertEqual(len(snaps["snapshots"]), 1)
        self.assertEqual(snaps["snapshots"][0]["name"], "release-v1")

        with self.assertRaises(ValidationError):
            self.db.create_snapshot("main", "release-v1")

    def test_rollback_staged(self) -> None:
        self.db.write_file("main", "a.txt", "content-a")
        self.db.commit_workspace("main", "seed")

        self.db.write_file("main", "a.txt", "modified-a")
        self.db.write_file("main", "b.txt", "new-b")

        staged_files = self.db.list_files("main")
        staged_paths = {f["path"] for f in staged_files["files"] if f["staged"]}
        self.assertEqual(staged_paths, {"a.txt", "b.txt"})

        result = self.db.rollback_staged("main")
        self.assertEqual(result["discarded_count"], 2)

        after = self.db.list_files("main")
        after_paths = {f["path"] for f in after["files"]}
        self.assertEqual(after_paths, {"a.txt"})
        self.assertEqual(self.db.read_file("main", "a.txt")["content"], "content-a")

    def test_reset_workspace(self) -> None:
        self.db.write_file("main", "file.txt", "v1")
        c1 = self.db.commit_workspace("main", "commit-1")

        self.db.write_file("main", "file.txt", "v2")
        self.db.commit_workspace("main", "commit-2")

        self.assertEqual(self.db.read_file("main", "file.txt")["content"], "v2")

        self.db.reset_workspace("main", c1["commit_id"])
        self.assertEqual(self.db.read_file("main", "file.txt")["content"], "v1")

    def test_reset_rejects_non_ancestor(self) -> None:
        self.db.write_file("main", "file.txt", "data")
        self.db.commit_workspace("main", "seed")

        self.db.create_workspace("branch", from_workspace="main")
        self.db.write_file("branch", "other.txt", "branch-data")
        branch_commit = self.db.commit_workspace("branch", "branch commit")

        with self.assertRaises(ValidationError):
            self.db.reset_workspace("main", branch_commit["commit_id"])


class SearchOperationTest(unittest.TestCase):
    """Test fs.find and fs.grep operations."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "search.db"
        self.db = WorkspaceDB(self.db_path)
        self.db.init()
        self.db.write_file("main", "src/main.py", "import os\nprint('hello')\n")
        self.db.write_file("main", "src/utils.py", "def helper():\n    return 42\n")
        self.db.write_file("main", "docs/README.md", "# Project\nOverview of the project.\n")
        self.db.write_file("main", "tests/test_main.py", "import unittest\nclass TestMain(unittest.TestCase): pass\n")
        self.db.commit_workspace("main", "seed project")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_find_by_glob(self) -> None:
        result = self.db.find_files("main", "src/*.py")
        paths = [m["path"] for m in result["matches"]]
        self.assertEqual(sorted(paths), ["src/main.py", "src/utils.py"])

    def test_find_all_python(self) -> None:
        result = self.db.find_files("main", "*.py")
        paths = [m["path"] for m in result["matches"]]
        self.assertEqual(len(paths), 3)

    def test_find_no_match(self) -> None:
        result = self.db.find_files("main", "*.rs")
        self.assertEqual(result["matches"], [])

    def test_grep_content(self) -> None:
        result = self.db.grep_files("main", "import")
        paths = [r["path"] for r in result["results"]]
        self.assertIn("src/main.py", paths)
        self.assertIn("tests/test_main.py", paths)

    def test_grep_regex(self) -> None:
        result = self.db.grep_files("main", r"def \w+\(")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["path"], "src/utils.py")

    def test_grep_invalid_regex(self) -> None:
        with self.assertRaises(ValidationError):
            self.db.grep_files("main", "[invalid")


class PolicyHookTest(unittest.TestCase):
    """Test policy hook registration and enforcement."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "policy.db"
        self.db = WorkspaceDB(self.db_path)
        self.db.init()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_path_deny_rejects_commit(self) -> None:
        self.db.register_hook(
            event_type="pre_commit",
            hook_type="path_deny",
            config={"pattern": "*.secret"},
        )
        self.db.write_file("main", "config.secret", "password=hunter2")
        with self.assertRaises(PolicyRejection):
            self.db.commit_workspace("main", "add secret")

    def test_path_deny_allows_clean_commit(self) -> None:
        self.db.register_hook(
            event_type="pre_commit",
            hook_type="path_deny",
            config={"pattern": "*.secret"},
        )
        self.db.write_file("main", "config.yaml", "key: value")
        result = self.db.commit_workspace("main", "add config")
        self.assertEqual(result["status"], "ok")

    def test_require_message_prefix(self) -> None:
        self.db.register_hook(
            event_type="pre_commit",
            hook_type="require_message_prefix",
            config={"prefix": "feat: "},
        )
        self.db.write_file("main", "file.txt", "content")
        with self.assertRaises(PolicyRejection):
            self.db.commit_workspace("main", "added file")

        result = self.db.commit_workspace("main", "feat: add file")
        self.assertEqual(result["status"], "ok")

    def test_max_file_size(self) -> None:
        self.db.register_hook(
            event_type="pre_commit",
            hook_type="max_file_size",
            config={"max_bytes": 100},
        )
        self.db.write_file("main", "small.txt", "ok")
        result = self.db.commit_workspace("main", "small file")
        self.assertEqual(result["status"], "ok")

        self.db.write_file("main", "big.txt", "x" * 200)
        with self.assertRaises(PolicyRejection):
            self.db.commit_workspace("main", "big file")

    def test_list_hooks(self) -> None:
        self.db.register_hook("pre_commit", "path_deny", config={"pattern": "*.log"})
        self.db.register_hook("pre_merge", "path_deny", config={"pattern": "*.tmp"})
        hooks = self.db.list_hooks()
        self.assertEqual(len(hooks["hooks"]), 2)


if __name__ == "__main__":
    unittest.main()
