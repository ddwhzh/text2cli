"""Tests for the ReAct Agent architecture.

Covers:
1. workspace_tools.py -- each @tool function independently
2. graph_agent.py -- LangGraphAgent with mock ChatModel
3. agent.py -- LLMWorkspaceAgent adapter
4. LLM client env var configuration
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from text2cli.db import WorkspaceDB
from text2cli.workspace_tools import create_workspace_tools


class TestWorkspaceTools(unittest.TestCase):
    """Test each @tool function directly (no LLM involved)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "test.db")
        self.db.init()
        self.db.write_file("main", "hello.txt", "hello world")
        self.db.write_file("main", "data.txt", "line1\nline2\nline3")
        self.db.commit_workspace("main", "seed files")
        self.tools = create_workspace_tools(self.db, "main")
        self._tool_map = {t.name: t for t in self.tools}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _invoke(self, name: str, args: dict[str, Any]) -> str:
        return self._tool_map[name].invoke(args)

    def test_cat(self) -> None:
        result = self._invoke("cat", {"path": "hello.txt"})
        self.assertEqual(result, "hello world")

    def test_cat_missing_file(self) -> None:
        result = self._invoke("cat", {"path": "nope.txt"})
        self.assertIn("Error", result)

    def test_write_file(self) -> None:
        result = self._invoke("write_file", {"path": "new.txt", "content": "brand new"})
        self.assertIn("new.txt", result)
        self.assertEqual(self.db.read_file("main", "new.txt")["content"], "brand new")

    def test_append_file(self) -> None:
        self._invoke("append_file", {"path": "hello.txt", "content": "\nmore"})
        self.assertIn("more", self.db.read_file("main", "hello.txt")["content"])

    def test_sed(self) -> None:
        self._invoke("sed", {"path": "hello.txt", "find": "world", "replace": "earth"})
        self.assertEqual(self.db.read_file("main", "hello.txt")["content"], "hello earth")

    def test_rm(self) -> None:
        self._invoke("rm", {"path": "hello.txt"})
        files = [f["path"] for f in self.db.list_files("main")["files"]]
        self.assertNotIn("hello.txt", files)

    def test_cp(self) -> None:
        self._invoke("cp", {"src": "hello.txt", "dst": "backup.txt"})
        self.assertEqual(self.db.read_file("main", "backup.txt")["content"], "hello world")
        self.db.read_file("main", "hello.txt")

    def test_mv(self) -> None:
        self._invoke("mv", {"src": "hello.txt", "dst": "renamed.txt"})
        self.assertEqual(self.db.read_file("main", "renamed.txt")["content"], "hello world")
        files = [f["path"] for f in self.db.list_files("main")["files"]]
        self.assertNotIn("hello.txt", files)

    def test_touch_new(self) -> None:
        result = self._invoke("touch", {"path": "empty.txt"})
        self.assertIn("Created", result)
        self.assertEqual(self.db.read_file("main", "empty.txt")["content"], "")

    def test_touch_existing(self) -> None:
        result = self._invoke("touch", {"path": "hello.txt"})
        self.assertIn("already exists", result)

    def test_ls(self) -> None:
        result = self._invoke("ls", {})
        self.assertIn("hello.txt", result)
        self.assertIn("data.txt", result)

    def test_find(self) -> None:
        result = self._invoke("find", {"pattern": "*.txt"})
        self.assertIn("hello.txt", result)

    def test_grep_all(self) -> None:
        result = self._invoke("grep", {"pattern": "line"})
        self.assertIn("data.txt", result)

    def test_grep_specific_file(self) -> None:
        result = self._invoke("grep", {"pattern": "line2", "path": "data.txt"})
        self.assertIn("line2", result)

    def test_diff(self) -> None:
        self.db.write_file("main", "a.txt", "same\n")
        self.db.write_file("main", "b.txt", "different\n")
        result = self._invoke("diff", {"file1": "a.txt", "file2": "b.txt"})
        self.assertIn("-same", result)

    def test_head(self) -> None:
        result = self._invoke("head", {"path": "data.txt", "n": 2})
        self.assertIn("line1", result)

    def test_tail(self) -> None:
        result = self._invoke("tail", {"path": "data.txt", "n": 1})
        self.assertIn("line3", result)

    def test_wc(self) -> None:
        result = self._invoke("wc", {"path": "data.txt"})
        self.assertIn("3", result)

    def test_sort_file(self) -> None:
        result = self._invoke("sort_file", {"path": "data.txt"})
        self.assertTrue(len(result) > 0)

    def test_commit(self) -> None:
        self.db.write_file("main", "staged.txt", "will commit")
        result = self._invoke("commit", {"message": "test commit"})
        self.assertIn("test commit", result)

    def test_rollback(self) -> None:
        self.db.write_file("main", "temp.txt", "will discard")
        result = self._invoke("rollback", {})
        self.assertIn("Discarded", result)

    def test_log(self) -> None:
        result = self._invoke("log", {"limit": 5})
        self.assertIn("seed files", result)

    def test_snapshot(self) -> None:
        result = self._invoke("snapshot", {"name": "v1"})
        self.assertIn("v1", result)

    def test_script(self) -> None:
        result = self._invoke("script", {"code": 'echo "hello from script"'})
        self.assertIn("hello from script", result)

    def test_tool_count(self) -> None:
        self.assertEqual(len(self.tools), 21)

    def test_all_tools_are_named(self) -> None:
        names = {t.name for t in self.tools}
        expected = {
            "cat", "write_file", "append_file", "sed", "rm", "cp", "mv", "touch",
            "ls", "find", "grep", "diff", "head", "tail", "wc", "sort_file",
            "commit", "rollback", "log", "snapshot", "script",
        }
        self.assertEqual(names, expected)


class TestLangGraphAgentUnit(unittest.TestCase):
    """Test graph_agent.py with a mock ChatModel."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "test.db")
        self.db.init()
        self.db.write_file("main", "readme.txt", "hello from readme")
        self.db.commit_workspace("main", "seed")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_result_extracts_reply(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage
        from text2cli.graph_agent import LangGraphAgent

        agent = LangGraphAgent.__new__(LangGraphAgent)
        result = agent._parse_result({
            "messages": [
                HumanMessage(content="hi"),
                AIMessage(content="你好! 有什么可以帮你的?"),
            ]
        })
        self.assertEqual(result["status"], "ok")
        self.assertIn("你好", result["reply"])
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["llm_rounds"], 1)

    def test_parse_result_extracts_tool_calls(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from text2cli.graph_agent import LangGraphAgent

        agent = LangGraphAgent.__new__(LangGraphAgent)
        result = agent._parse_result({
            "messages": [
                HumanMessage(content="read file"),
                AIMessage(content="", tool_calls=[{"id": "t1", "name": "cat", "args": {"path": "a.txt"}}]),
                ToolMessage(content="file content here", tool_call_id="t1"),
                AIMessage(content="文件内容是 file content here"),
            ]
        })
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["tool"], "cat")
        self.assertIn("file content here", result["reply"])
        self.assertEqual(result["llm_rounds"], 2)

    def test_is_configured(self) -> None:
        import os
        from text2cli.graph_agent import LangGraphAgent
        old = os.environ.pop("LLM_API_KEY", None)
        old_glm = os.environ.pop("GLM_API_KEY", None)
        old_zhipu = os.environ.pop("ZHIPUAI_API_KEY", None)
        try:
            self.assertFalse(LangGraphAgent.is_configured())
            os.environ["LLM_API_KEY"] = "test"
            self.assertTrue(LangGraphAgent.is_configured())
        finally:
            os.environ.pop("LLM_API_KEY", None)
            if old:
                os.environ["LLM_API_KEY"] = old
            if old_glm:
                os.environ["GLM_API_KEY"] = old_glm
            if old_zhipu:
                os.environ["ZHIPUAI_API_KEY"] = old_zhipu


class TestLLMWorkspaceAgentAdapter(unittest.TestCase):
    """Test the LLMWorkspaceAgent adapter delegates to LangGraphAgent."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "test.db")
        self.db.init()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_uses_langgraph_impl(self) -> None:
        from unittest.mock import MagicMock
        from text2cli.agent import LLMWorkspaceAgent
        from text2cli.graph_agent import LangGraphAgent

        mock_model = MagicMock()
        agent = LLMWorkspaceAgent(self.db, chat_model=mock_model)
        self.assertIsInstance(agent._impl, LangGraphAgent)

    def test_llm_property_returns_model(self) -> None:
        from unittest.mock import MagicMock
        from text2cli.agent import LLMWorkspaceAgent

        mock_model = MagicMock()
        agent = LLMWorkspaceAgent(self.db, chat_model=mock_model)
        self.assertIs(agent.llm, mock_model)


class TestCommandAgentFallback(unittest.TestCase):
    """Test that the Rule Agent (WorkspaceAgent) still works as command-line fallback."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "test.db")
        self.db.init()
        self.db.write_file("main", "test.txt", "hello")
        self.db.commit_workspace("main", "seed")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_direct_cat_command(self) -> None:
        from text2cli.agent import WorkspaceAgent
        agent = WorkspaceAgent(self.db)
        result = agent.handle_message("main", "cat test.txt")
        self.assertEqual(result["status"], "ok")
        self.assertIn("hello", result["reply"])

    def test_unrecognized_natural_language_suggests_llm(self) -> None:
        from text2cli.agent import WorkspaceAgent
        agent = WorkspaceAgent(self.db)
        result = agent.handle_message("main", "把所有文件备份一下")
        self.assertIn("LLM", result["reply"])


class TestLangfuseFactory(unittest.TestCase):
    """Test Langfuse handler creation."""

    def test_returns_none_when_not_configured(self) -> None:
        import os
        from text2cli.graph_agent import create_langfuse_handler
        old = os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
        try:
            handler = create_langfuse_handler()
            self.assertIsNone(handler)
        finally:
            if old:
                os.environ["LANGFUSE_PUBLIC_KEY"] = old


if __name__ == "__main__":
    unittest.main()
