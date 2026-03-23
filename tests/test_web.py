from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from text2cli.web import Text2CLIApp


class WebAppPOCTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "web.db"
        self.app = Text2CLIApp(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_file_crud_and_state(self) -> None:
        response = self.app.handle_api(
            "PUT",
            "/api/file",
            {},
            {"workspace": "main", "path": "docs/hello.md", "content": "hello app"},
        )
        self.assertEqual(response.status, 200)

        commit = self.app.handle_api(
            "POST",
            "/api/commit",
            {},
            {"workspace": "main", "message": "seed docs"},
        )
        self.assertEqual(commit.status, 200)

        file_response = self.app.handle_api(
            "GET",
            "/api/file",
            {"workspace": ["main"], "path": ["docs/hello.md"]},
        )
        self.assertEqual(file_response.payload["content"], "hello app")

        state = self.app.handle_api(
            "GET",
            "/api/state",
            {"workspace": ["main"]},
        )
        self.assertEqual(state.status, 200)
        self.assertEqual(state.payload["workspace"], "main")
        self.assertEqual(len(state.payload["files"]), 1)
        self.assertGreaterEqual(len(state.payload["commits"]), 1)

    def test_rule_agent_executes_workspace_commands(self) -> None:
        """Test the rule-based command agent (WorkspaceAgent) via direct call."""
        self.app.handle_api(
            "PUT", "/api/file", {},
            {"workspace": "main", "path": "README.md", "content": "agent visible"},
        )
        self.app.handle_api(
            "POST", "/api/commit", {},
            {"workspace": "main", "message": "seed readme"},
        )

        result = self.app.rule_agent.handle_message("main", "cat README.md")
        self.assertIn("agent visible", result["reply"])
        self.assertEqual(result["actions"][0]["tool"], "cat")

        result = self.app.rule_agent.handle_message("main", "ls")
        self.assertIn("README.md", result["reply"])

        result = self.app.rule_agent.handle_message("main", "echo 'finish poc' > notes/todo.md")
        self.assertIn("notes/todo.md", result["reply"])


if __name__ == "__main__":
    unittest.main()
