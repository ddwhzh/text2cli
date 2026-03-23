"""LangChain @tool definitions for the transactional workspace.

Each tool wraps a WorkspaceDB operation behind a Linux-like command name.
Tools are created per-request via ``create_workspace_tools(db, workspace)``
so that the workspace binding is captured in a closure.

When a :class:`CodeExecutor` is provided, ``python_exec`` and ``shell_exec``
tools are also registered, enabling the agent to write and run code whose
file I/O is transparently backed by the workspace (via FUSE or materialize).
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool as _lc_tool

from .db import NotFoundError, WorkspaceDB, WorkspaceError
from .search import BraveSearchClient

if TYPE_CHECKING:
    from .code_executor import CodeExecutor

logger = logging.getLogger(__name__)


def create_workspace_tools(
    db: WorkspaceDB,
    workspace: str,
    *,
    executor: CodeExecutor | None = None,
    search_client: BraveSearchClient | None = None,
) -> list:
    """Build a list of LangChain Tool objects bound to *db* and *workspace*."""

    @_lc_tool
    def cat(path: str) -> str:
        """Read and display file contents (like Linux cat)."""
        try:
            return db.read_file(workspace, path)["content"]
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def write_file(path: str, content: str) -> str:
        """Write content to a file, creating or overwriting it. Like 'echo content > file'. Changes go to staging area."""
        try:
            result = db.write_file(workspace, path, content)
            return f"Written to {result['path']} ({result['op']})"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def append_file(path: str, content: str) -> str:
        """Append content to an existing file. Like 'echo content >> file'."""
        try:
            db.patch_file(workspace, path, append=content)
            return f"Appended to {path}"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def sed(path: str, find: str, replace: str) -> str:
        """Find and replace text in a file. Like 'sed s/find/replace/g file'."""
        try:
            db.patch_file(workspace, path, find=find, replace=replace)
            return f"Replaced '{find}' with '{replace}' in {path}"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def rm(path: str) -> str:
        """Delete a file. Like 'rm file'."""
        try:
            db.delete_file(workspace, path)
            return f"Deleted {path}"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def cp(src: str, dst: str) -> str:
        """Copy a file. Like 'cp src dst'."""
        try:
            content = db.read_file(workspace, src)["content"]
            db.write_file(workspace, dst, content)
            return f"Copied {src} -> {dst}"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def mv(src: str, dst: str) -> str:
        """Move/rename a file. Like 'mv src dst'."""
        try:
            content = db.read_file(workspace, src)["content"]
            db.write_file(workspace, dst, content)
            db.delete_file(workspace, src)
            return f"Moved {src} -> {dst}"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def touch(path: str) -> str:
        """Create an empty file if it doesn't exist. Like 'touch file'."""
        try:
            db.read_file(workspace, path)
            return f"{path} already exists"
        except NotFoundError:
            db.write_file(workspace, path, "")
            return f"Created {path}"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def ls() -> str:
        """List all files in the workspace with sizes. Like 'ls -la'."""
        try:
            result = db.list_files(workspace)
            if not result["files"]:
                return "(empty workspace)"
            lines = [f"{f['path']}  ({f.get('size_bytes', '?')} bytes)" for f in result["files"]]
            return "\n".join(lines)
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def find(pattern: str) -> str:
        """Find files matching a glob pattern. Like 'find . -name pattern'."""
        try:
            result = db.find_files(workspace, pattern)
            matches = result.get("matches", [])
            if not matches:
                return f"No files matching '{pattern}'"
            return "\n".join(m["path"] for m in matches)
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def grep(pattern: str, path: str = "") -> str:
        """Search file contents by regex. Like 'grep pattern [file]'. Returns matching lines with paths and line numbers."""
        try:
            if path:
                content = db.read_file(workspace, path)["content"]
                compiled = re.compile(pattern)
                matches = [
                    f"{path}:{i}: {ln}"
                    for i, ln in enumerate(content.splitlines(), 1)
                    if compiled.search(ln)
                ]
                return "\n".join(matches) if matches else f"No matches in {path}"
            result = db.grep_files(workspace, pattern)
            lines: list[str] = []
            for entry in result.get("results", []):
                for m in entry.get("matches", []):
                    lines.append(f"{entry['path']}:{m['line']}: {m['text']}")
            return "\n".join(lines) if lines else "No matches"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def diff(file1: str, file2: str) -> str:
        """Compare two files in unified diff format. Like 'diff file1 file2'."""
        try:
            c1 = db.read_file(workspace, file1)["content"].splitlines(keepends=True)
            c2 = db.read_file(workspace, file2)["content"].splitlines(keepends=True)
            d = list(difflib.unified_diff(c1, c2, fromfile=file1, tofile=file2))
            return "".join(d) if d else f"{file1} and {file2} are identical"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def head(path: str, n: int = 10) -> str:
        """Show first N lines of a file. Like 'head -n N file'."""
        try:
            result = db.exec_run(workspace, "head", path=path, args=[str(n)])
            return result.get("output", "")
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def tail(path: str, n: int = 10) -> str:
        """Show last N lines of a file. Like 'tail -n N file'."""
        try:
            result = db.exec_run(workspace, "tail", path=path, args=[str(n)])
            return result.get("output", "")
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def wc(path: str) -> str:
        """Count lines, words, and characters in a file. Like 'wc file'."""
        try:
            result = db.exec_run(workspace, "wc", path=path)
            return result.get("output", "")
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def sort_file(path: str) -> str:
        """Sort lines in a file alphabetically. Like 'sort file'."""
        try:
            result = db.exec_run(workspace, "sort", path=path)
            return result.get("output", "")
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def commit(message: str) -> str:
        """Commit all staged changes with a message. Like 'git commit -m msg'."""
        try:
            result = db.commit_workspace(workspace, message)
            if result["status"] == "noop":
                return "Nothing to commit"
            return f"[{result['commit_id'][:10]}] {message} ({len(result['paths'])} files)"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def rollback() -> str:
        """Discard all staged (uncommitted) changes. Like 'git checkout -- .'."""
        try:
            result = db.rollback_staged(workspace)
            return f"Discarded {result['discarded_count']} staged change(s)"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def log(limit: int = 10) -> str:
        """Show commit history. Like 'git log'."""
        try:
            result = db.log_workspace(workspace, limit=limit)
            if not result["commits"]:
                return "No commits yet"
            lines = [
                f"[{c['id'][:10]}] {c['message']} ({c.get('created_at', '')})"
                for c in result["commits"]
            ]
            return "\n".join(lines)
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def snapshot(name: str) -> str:
        """Create a named immutable snapshot of the current state. Like 'git tag name'."""
        try:
            result = db.create_snapshot(workspace, name)
            return f"Snapshot '{result['snapshot']}' -> {result['commit_id'][:10]}"
        except WorkspaceError as e:
            return f"Error: {e}"

    @_lc_tool
    def script(code: str) -> str:
        """Execute a T2Script program for batch operations. T2Script is a shell-like DSL supporting pipes, variables, loops."""
        try:
            result = db.exec_script(workspace, code)
            output = result.get("output", "")
            return output if output else f"(script completed in {result['steps']} steps)"
        except WorkspaceError as e:
            return f"Error: {e}"

    tools = [
        cat, write_file, append_file, sed, rm, cp, mv, touch,
        ls, find, grep, diff,
        head, tail, wc, sort_file,
        commit, rollback, log, snapshot, script,
    ]

    if executor is not None:
        @_lc_tool
        def python_exec(code: str) -> str:
            """Execute Python code in the workspace directory.

            Files in the workspace are accessible via standard open() / os.listdir()
            etc.  Use print() to output results.  The working directory is the
            workspace root.

            Example:
                python_exec(code="with open('data.csv') as f:\\n    print(f.read())")
            """
            result = executor.execute(workspace, "python", code)
            return result.to_display()

        @_lc_tool
        def shell_exec(command: str) -> str:
            """Execute a shell (bash) command in the workspace directory.

            Standard Unix commands are available (ls, cat, grep, awk, sed,
            sort, wc, etc.).  The working directory is the workspace root.

            Example:
                shell_exec(command="wc -l *.txt")
            """
            result = executor.execute(workspace, "shell", command)
            return result.to_display()

        tools.extend([python_exec, shell_exec])

    # ── Web search tool (Brave Search API) ────
    if search_client is not None:
        _brave = search_client

        @_lc_tool
        def web_search(query: str, count: int = 5) -> str:
            """Search the web for information using Brave Search.

            Returns titles, URLs, and descriptions. Use this when the user
            asks to search online, look up facts, find documentation, news,
            current events, or gather any information from the internet.

            Args:
                query: search query string.
                count: number of results to return (1-10, default 5).
            """
            try:
                count = max(1, min(count, 10))
                data = _brave.search(query, count=count)
                results = data.get("results", [])
                if not results:
                    return f"No results found for '{query}'"
                lines = []
                for i, r in enumerate(results, 1):
                    lines.append(f"{i}. {r.get('title', '(no title)')}")
                    lines.append(f"   {r.get('url', '')}")
                    desc = r.get("description", "")
                    if desc:
                        lines.append(f"   {desc[:300]}{'...' if len(desc) > 300 else ''}")
                    age = r.get("age", "")
                    if age:
                        lines.append(f"   ({age})")
                    lines.append("")
                return "\n".join(lines)
            except Exception as exc:
                logger.exception("web_search failed for query=%r", query)
                return f"Error: search failed - {exc}"

        tools.append(web_search)

    return tools
