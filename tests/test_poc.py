from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from text2cli.db import ConflictError, NotFoundError, WorkspaceDB


class WorkspacePOCTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "workspace.db"
        self.db = WorkspaceDB(self.db_path)
        self.db.init()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_commit_read_and_events(self) -> None:
        self.db.write_file("main", "notes/todo.txt", "hello\nworld\n")
        diff = self.db.diff_workspace("main")
        self.assertEqual(len(diff["changes"]), 1)
        commit = self.db.commit_workspace("main", "seed todo")
        self.assertEqual(commit["status"], "ok")

        readback = self.db.read_file("main", "notes/todo.txt")
        self.assertEqual(readback["content"], "hello\nworld\n")
        self.assertEqual(readback["source"], "committed")

        events = self.db.list_events(workspace="main", limit=10)
        event_types = [event["event_type"] for event in events["events"]]
        self.assertIn("pre_commit", event_types)
        self.assertIn("post_commit", event_types)

    def test_patch_and_delete_are_visible_before_commit(self) -> None:
        self.db.write_file("main", "README.md", "hello")
        self.db.commit_workspace("main", "seed")

        self.db.patch_file("main", "README.md", append="\nworld")
        staged = self.db.read_file("main", "README.md")
        self.assertEqual(staged["source"], "staged")
        self.assertEqual(staged["content"], "hello\nworld")

        self.db.delete_file("main", "README.md")
        with self.assertRaises(NotFoundError):
            self.db.read_file("main", "README.md")

    def test_branch_merge_conflict(self) -> None:
        self.db.write_file("main", "README.md", "hello workspace")
        self.db.commit_workspace("main", "seed main")

        self.db.create_workspace("agent-a", from_workspace="main")
        self.db.create_workspace("agent-b", from_workspace="main")

        self.db.patch_file("agent-a", "README.md", find="hello", replace="hello agent-a")
        self.db.commit_workspace("agent-a", "agent-a change")
        merged = self.db.merge_workspace("agent-a", "main", "merge agent-a")
        self.assertEqual(merged["status"], "ok")

        self.db.patch_file("agent-b", "README.md", find="hello", replace="hello agent-b")
        self.db.commit_workspace("agent-b", "agent-b change")

        with self.assertRaises(ConflictError) as ctx:
            self.db.merge_workspace("agent-b", "main", "merge agent-b")

        self.assertEqual(len(ctx.exception.conflicts), 1)
        self.assertEqual(ctx.exception.conflicts[0]["path"], "README.md")

        events = self.db.list_events(workspace="main", limit=20)
        event_types = [event["event_type"] for event in events["events"]]
        self.assertIn("merge_conflict", event_types)


if __name__ == "__main__":
    unittest.main()
