from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from .db import NotFoundError, ValidationError, WorkspaceDB, WorkspaceError

logger = logging.getLogger(__name__)


class WorkspaceAgent:
    """Regex-based command parser (fallback when no LLM is available).

    Directly parses Linux-like commands typed by users. For natural language
    input, use LLMWorkspaceAgent instead (ReAct agent with autonomous
    planning and execution).
    """

    def __init__(self, db: WorkspaceDB) -> None:
        self.db = db

    def handle_message(self, workspace: str, message: str) -> dict[str, Any]:
        text = message.strip()
        if not text:
            raise ValidationError("Chat message must not be empty.")
        try:
            return self._dispatch(workspace, text)
        except WorkspaceError as exc:
            return {
                "status": "error",
                "reply": f"执行失败: {exc}",
                "actions": [],
            }

    def _dispatch(self, workspace: str, text: str) -> dict[str, Any]:
        lower = text.lower()
        if lower in {"help", "man", "/help"}:
            return {"status": "ok", "reply": self._help_text(), "actions": []}

        matchers = [
            self._match_cat,
            self._match_echo,
            self._match_grep,
            self._match_find,
            self._match_ls,
            self._match_cp,
            self._match_mv,
            self._match_rm,
            self._match_touch,
            self._match_diff,
            self._match_exec,
            self._match_commit,
            self._match_rollback,
            self._match_snapshot,
            self._match_script,
            self._match_overview,
        ]
        for matcher in matchers:
            matched = matcher(text, lower)
            if matched:
                return self._execute(workspace, matched)

        return {
            "status": "ok",
            "reply": (
                "未识别的命令.\n"
                "- 直接输入 Linux 命令: cat, grep, cp, mv, rm 等 (输入 help 查看全部)\n"
                "- 自然语言任务需要配置 LLM (设置 LLM_API_KEY 环境变量启用 AI Agent)"
            ),
            "actions": [],
        }

    # ── Linux-like matchers ──────────────────────────────────

    def _match_cat(self, text: str, lower: str) -> dict[str, Any] | None:
        """cat file1 [file2 ...] [> target | >> target]"""
        m = re.match(
            r"^cat\s+(.+?)\s+(>>?)\s+(\S+)\s*$", text, re.IGNORECASE,
        )
        if m:
            paths = m.group(1).strip().split()
            redirect = m.group(2)
            target = m.group(3).strip()
            return {"tool": "cat", "args": {"paths": paths, "redirect": redirect, "target": target}}

        m = re.match(r"^cat\s+(.+)$", text, re.IGNORECASE)
        if m:
            paths = m.group(1).strip().split()
            return {"tool": "cat", "args": {"paths": paths, "redirect": None, "target": None}}

        for pat in [
            r"^/(?:read|cat|open)\s+(.+)$",
            r"^(?:读取|查看|打开)\s+(\S+)$",
            r"^(?:read|open)\s+(\S+)$",
        ]:
            m = re.match(pat, text, re.IGNORECASE)
            if m:
                return {"tool": "cat", "args": {"paths": [m.group(1).strip()], "redirect": None, "target": None}}
        return None

    def _match_echo(self, text: str, lower: str) -> dict[str, Any] | None:
        """echo 'content' [> file | >> file]"""
        m = re.match(
            r'^echo\s+(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|(\S+))\s*(>>?)\s*(\S+)\s*$',
            text, re.IGNORECASE,
        )
        if m:
            content = m.group(1) or m.group(2) or m.group(3) or ""
            redirect = m.group(4)
            target = m.group(5).strip()
            return {"tool": "echo", "args": {"text": content, "redirect": redirect, "target": target}}

        m = re.match(
            r'^echo\s+(?:"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|([\s\S]+))\s*$',
            text, re.IGNORECASE,
        )
        if m:
            content = m.group(1) or m.group(2) or (m.group(3) or "").strip()
            return {"tool": "echo", "args": {"text": content, "redirect": None, "target": None}}

        m = re.match(r"^/(?:write|save)\s+(\S+)\s+([\s\S]+)$", text, re.IGNORECASE)
        if m:
            return {"tool": "echo", "args": {"text": m.group(2), "redirect": ">", "target": m.group(1).strip()}}
        m = re.match(r"^/append\s+(\S+)\s+([\s\S]+)$", text, re.IGNORECASE)
        if m:
            return {"tool": "echo", "args": {"text": m.group(2), "redirect": ">>", "target": m.group(1).strip()}}
        m = re.match(r"^(?:写入|保存)\s+(\S+)\s+(?:内容[:：])?\s*([\s\S]+)$", text)
        if m:
            return {"tool": "echo", "args": {"text": m.group(2).strip(), "redirect": ">", "target": m.group(1).strip()}}
        m = re.match(r"^(?:追加)\s+(\S+)\s+(?:内容[:：])?\s*([\s\S]+)$", text)
        if m:
            return {"tool": "echo", "args": {"text": m.group(2).strip(), "redirect": ">>", "target": m.group(1).strip()}}
        return None

    def _match_grep(self, text: str, lower: str) -> dict[str, Any] | None:
        """grep [-ivnc] pattern [file ...]"""
        m = re.match(
            r'^grep\s+(-[ivncl]+)\s+(?:"([^"]+)"|(\S+))(?:\s+(.+))?\s*$',
            text, re.IGNORECASE,
        )
        if m:
            flags = m.group(1)
            pattern = m.group(2) or m.group(3)
            files = m.group(4).split() if m.group(4) else None
            return {"tool": "grep", "args": {"pattern": pattern, "flags": flags, "files": files}}

        m = re.match(
            r'^grep\s+(?:"([^"]+)"|(\S+))(?:\s+(.+))?\s*$',
            text, re.IGNORECASE,
        )
        if m:
            pattern = m.group(1) or m.group(2)
            rest = m.group(3)
            files = rest.split() if rest else None
            if files and all(f.startswith("-") for f in files):
                files = None
            return {"tool": "grep", "args": {"pattern": pattern, "flags": "", "files": files}}

        m = re.match(r"^(?:搜索内容|搜索)\s+(\S+)$", text)
        if m:
            return {"tool": "grep", "args": {"pattern": m.group(1).strip(), "flags": "", "files": None}}
        return None

    def _match_find(self, text: str, lower: str) -> dict[str, Any] | None:
        """find <glob>, find . -name 'pattern'"""
        m = re.match(r'^find\s+\.\s+-name\s+["\']?([^"\']+)["\']?\s*$', text, re.IGNORECASE)
        if m:
            return {"tool": "find", "args": {"pattern": m.group(1).strip()}}
        m = re.match(r"^find\s+(.+)$", text, re.IGNORECASE)
        if m:
            return {"tool": "find", "args": {"pattern": m.group(1).strip()}}
        m = re.match(r"^(?:查找|搜索文件)\s+(\S+)$", text)
        if m:
            return {"tool": "find", "args": {"pattern": m.group(1).strip()}}
        return None

    def _match_ls(self, text: str, lower: str) -> dict[str, Any] | None:
        """ls [-la] [path]"""
        if re.match(r"^ls(\s+-\w+)*\s*$", lower):
            return {"tool": "ls", "args": {}}
        if ("列出" in text or "看看" in text or "展示" in text) and ("文件" in text or "工作区" in text):
            return {"tool": "ls", "args": {}}
        if "list files" in lower or "show files" in lower:
            return {"tool": "ls", "args": {}}
        return None

    def _match_cp(self, text: str, lower: str) -> dict[str, Any] | None:
        """cp source dest"""
        m = re.match(r"^cp\s+(\S+)\s+(\S+)\s*$", text, re.IGNORECASE)
        if m:
            return {"tool": "cp", "args": {"source": m.group(1).strip(), "dest": m.group(2).strip()}}
        m = re.match(r"^(?:复制|拷贝)\s+(\S+)\s+(?:到|to)\s+(\S+)$", text)
        if m:
            return {"tool": "cp", "args": {"source": m.group(1).strip(), "dest": m.group(2).strip()}}
        return None

    def _match_mv(self, text: str, lower: str) -> dict[str, Any] | None:
        """mv source dest"""
        m = re.match(r"^mv\s+(\S+)\s+(\S+)\s*$", text, re.IGNORECASE)
        if m:
            return {"tool": "mv", "args": {"source": m.group(1).strip(), "dest": m.group(2).strip()}}
        m = re.match(r"^(?:移动|重命名|rename)\s+(\S+)\s+(?:到|to)\s+(\S+)$", text)
        if m:
            return {"tool": "mv", "args": {"source": m.group(1).strip(), "dest": m.group(2).strip()}}
        return None

    def _match_rm(self, text: str, lower: str) -> dict[str, Any] | None:
        """rm [-rf] file"""
        m = re.match(r"^rm\s+(?:-\w+\s+)?(.+)$", text, re.IGNORECASE)
        if m:
            return {"tool": "rm", "args": {"path": m.group(1).strip()}}
        m = re.match(r"^(?:删除|移除|delete|remove)\s+(\S+)$", text, re.IGNORECASE)
        if m:
            return {"tool": "rm", "args": {"path": m.group(1).strip()}}
        return None

    def _match_touch(self, text: str, lower: str) -> dict[str, Any] | None:
        """touch file"""
        m = re.match(r"^touch\s+(\S+)\s*$", text, re.IGNORECASE)
        if m:
            return {"tool": "touch", "args": {"path": m.group(1).strip()}}
        m = re.match(r"^(?:创建文件|新建)\s+(\S+)$", text)
        if m:
            return {"tool": "touch", "args": {"path": m.group(1).strip()}}
        return None

    def _match_diff(self, text: str, lower: str) -> dict[str, Any] | None:
        """diff file1 file2"""
        m = re.match(r"^diff\s+(\S+)\s+(\S+)\s*$", text, re.IGNORECASE)
        if m:
            return {"tool": "diff", "args": {"file1": m.group(1).strip(), "file2": m.group(2).strip()}}
        return None

    def _match_exec(self, text: str, lower: str) -> dict[str, Any] | None:
        """Direct text processing: wc, sort, head, tail, uniq, tr, toc, wordfreq, linecount"""
        for cmd in ["wc", "sort", "head", "tail", "uniq", "tr", "toc", "wordfreq"]:
            m = re.match(rf"^{cmd}\s+(.+)$", text, re.IGNORECASE)
            if m:
                rest = m.group(1).strip().split()
                path = rest[0]
                extra = rest[1:] if len(rest) > 1 else []
                return {"tool": "exec", "args": {"command": cmd, "path": path, "args": extra}}
        if lower == "linecount":
            return {"tool": "exec", "args": {"command": "linecount", "path": None, "args": []}}
        m = re.match(r"^(?:统计|字数统计)\s+(\S+)$", text)
        if m:
            return {"tool": "exec", "args": {"command": "wc", "path": m.group(1).strip(), "args": []}}
        return None

    def _match_commit(self, text: str, lower: str) -> dict[str, Any] | None:
        m = re.match(r"^(?:commit|提交)\s+([\s\S]+)$", text, re.IGNORECASE)
        if m:
            return {"tool": "commit", "args": {"message": m.group(1).strip()}}
        return None

    def _match_rollback(self, text: str, lower: str) -> dict[str, Any] | None:
        if lower in {"rollback", "回滚"}:
            return {"tool": "rollback", "args": {}}
        if "丢弃" in text and ("staged" in lower or "暂存" in text or "变更" in text):
            return {"tool": "rollback", "args": {}}
        return None

    def _match_snapshot(self, text: str, lower: str) -> dict[str, Any] | None:
        m = re.match(r"^(?:snapshot|快照|创建快照)\s+(\S+)\s*$", text, re.IGNORECASE)
        if m:
            return {"tool": "snapshot", "args": {"name": m.group(1).strip()}}
        return None

    def _match_script(self, text: str, lower: str) -> dict[str, Any] | None:
        m = re.match(r"^(?:script|/script)\s+([\s\S]+)$", text, re.IGNORECASE)
        if m:
            return {"tool": "script", "args": {"code": m.group(1).strip()}}
        return None

    def _match_overview(self, text: str, lower: str) -> dict[str, Any] | None:
        if lower in {"pwd", "overview", "status"}:
            return {"tool": "overview", "args": {}}
        if "概览" in text or "工作区状态" in text or "workspace status" in lower:
            return {"tool": "overview", "args": {}}
        return None

    # ── Execution ──────────────────────────────────────────

    def _execute(self, workspace: str, instruction: dict[str, Any]) -> dict[str, Any]:
        tool = instruction["tool"]
        args = instruction["args"]
        handler = getattr(self, f"_exec_{tool}", None)
        if handler:
            return handler(workspace, args)
        raise NotFoundError(f"Unknown command: {tool}")

    def _exec_ls(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.list_files(workspace)
        files = result["files"]
        if not files:
            reply = "total 0"
        else:
            lines = [f"total {len(files)}"]
            for f in files:
                staged = " [staged]" if f["staged"] else ""
                lines.append(f"  {f['size_bytes']:>8}  {f['path']}{staged}")
            reply = "\n".join(lines)
        return self._response(reply, "ls", args, {"file_count": len(files)})

    def _exec_cat(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        paths: list[str] = args["paths"]
        redirect: str | None = args["redirect"]
        target: str | None = args["target"]

        contents: list[str] = []
        actions: list[dict[str, Any]] = []
        for p in paths:
            result = self.db.read_file(workspace, p)
            contents.append(result["content"])
            actions.append({"tool": "cat", "args": {"path": p}, "summary": {"path": p, "source": result["source"]}})

        merged = "\n".join(contents)

        if redirect == ">" and target:
            write_result = self.db.write_file(workspace, target, merged)
            actions.append({"tool": "cat", "args": {"redirect": ">", "target": target}, "summary": {"path": target, "op": write_result["op"]}})
            return {"status": "ok", "reply": f"# cat {' '.join(paths)} > {target}", "actions": actions}
        elif redirect == ">>" and target:
            append_result = self.db.patch_file(workspace, target, append=merged)
            actions.append({"tool": "cat", "args": {"redirect": ">>", "target": target}, "summary": {"path": target, "op": append_result["op"]}})
            return {"status": "ok", "reply": f"# cat {' '.join(paths)} >> {target}", "actions": actions}
        else:
            return {"status": "ok", "reply": merged, "actions": actions}

    def _exec_echo(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        text = args["text"]
        redirect: str | None = args["redirect"]
        target: str | None = args["target"]

        if redirect == ">" and target:
            result = self.db.write_file(workspace, target, text)
            return self._response(f"# echo ... > {target}", "echo", args, {"path": target, "op": result["op"]})
        elif redirect == ">>" and target:
            result = self.db.patch_file(workspace, target, append=text)
            return self._response(f"# echo ... >> {target}", "echo", args, {"path": target, "op": result["op"]})
        else:
            return self._response(text, "echo", args, {})

    def _exec_grep(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        pattern = args["pattern"]
        flags_str = args.get("flags", "")
        files: list[str] | None = args.get("files")
        ignore_case = "i" in flags_str
        invert = "v" in flags_str
        count_only = "c" in flags_str

        if files:
            return self._grep_in_files(workspace, pattern, files, ignore_case=ignore_case, invert=invert, count_only=count_only)

        re_flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(pattern, re_flags)
        except re.error as exc:
            raise ValidationError(f"grep: invalid regex: {exc}") from exc

        result = self.db.grep_files(workspace, pattern)
        results = result["results"]
        if not results:
            return self._response("(no matches)", "grep", args, {"total_matches": 0})

        lines: list[str] = []
        total = 0
        for r in results:
            for m in r["matches"]:
                if invert:
                    continue
                total += 1
                if not count_only:
                    lines.append(f"{r['path']}:{m['line']}: {m['text']}")
        reply = str(total) if count_only else "\n".join(lines) if lines else "(no matches)"
        return self._response(reply, "grep", args, {"total_matches": total})

    def _grep_in_files(
        self,
        workspace: str,
        pattern: str,
        files: list[str],
        *,
        ignore_case: bool = False,
        invert: bool = False,
        count_only: bool = False,
    ) -> dict[str, Any]:
        re_flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(pattern, re_flags)
        except re.error as exc:
            raise ValidationError(f"grep: invalid regex: {exc}") from exc

        lines: list[str] = []
        total = 0
        for fpath in files:
            content = self.db.read_file(workspace, fpath)["content"]
            for line_no, line in enumerate(content.splitlines(), 1):
                matched = bool(compiled.search(line))
                if invert:
                    matched = not matched
                if matched:
                    total += 1
                    if not count_only:
                        prefix = f"{fpath}:" if len(files) > 1 else ""
                        lines.append(f"{prefix}{line_no}: {line}")
        reply = str(total) if count_only else "\n".join(lines) if lines else "(no matches)"
        return self._response(reply, "grep", {"pattern": pattern, "files": files}, {"total_matches": total})

    def _exec_find(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.find_files(workspace, args["pattern"])
        matches = result["matches"]
        if not matches:
            reply = f"find: no match for '{args['pattern']}'"
        else:
            reply = "\n".join(m["path"] for m in matches)
        return self._response(reply, "find", args, {"match_count": len(matches)})

    def _exec_cp(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        source, dest = args["source"], args["dest"]
        content = self.db.read_file(workspace, source)["content"]
        write_result = self.db.write_file(workspace, dest, content)
        return {
            "status": "ok",
            "reply": f"# cp {source} {dest}",
            "actions": [
                {"tool": "cp", "args": {"source": source, "dest": dest}, "summary": {"path": dest, "op": write_result["op"]}},
            ],
        }

    def _exec_mv(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        source, dest = args["source"], args["dest"]
        content = self.db.read_file(workspace, source)["content"]
        self.db.write_file(workspace, dest, content)
        self.db.delete_file(workspace, source)
        return {
            "status": "ok",
            "reply": f"# mv {source} {dest}",
            "actions": [
                {"tool": "mv", "args": {"source": source, "dest": dest}, "summary": {"source": source, "dest": dest}},
            ],
        }

    def _exec_rm(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.delete_file(workspace, args["path"])
        return self._response(f"# rm {args['path']}", "rm", args, {"path": result["path"], "op": result["op"]})

    def _exec_touch(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        path = args["path"]
        try:
            self.db.read_file(workspace, path)
        except NotFoundError:
            self.db.write_file(workspace, path, "")
        return self._response(f"# touch {path}", "touch", args, {"path": path})

    def _exec_diff(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        f1, f2 = args["file1"], args["file2"]
        c1 = self.db.read_file(workspace, f1)["content"].splitlines(keepends=True)
        c2 = self.db.read_file(workspace, f2)["content"].splitlines(keepends=True)
        diff_lines = list(difflib.unified_diff(c1, c2, fromfile=f1, tofile=f2))
        reply = "".join(diff_lines) if diff_lines else f"# {f1} and {f2} are identical"
        return self._response(reply, "diff", args, {"file1": f1, "file2": f2, "has_diff": bool(diff_lines)})

    def _exec_exec(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.exec_run(
            workspace, args["command"], path=args.get("path"), args=args.get("args"),
        )
        return self._response(result["output"], args["command"], args, {"command": args["command"]})

    def _exec_commit(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.commit_workspace(workspace, args["message"])
        if result["status"] == "noop":
            return self._response("nothing to commit", "commit", args, {"status": "noop"})
        reply = f"[{result['commit_id'][:10]}] {args['message']} ({len(result['paths'])} files)"
        return self._response(reply, "commit", args, {"commit_id": result["commit_id"], "paths": result["paths"]})

    def _exec_rollback(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.rollback_staged(workspace)
        reply = f"discarded {result['discarded_count']} staged change(s)"
        return self._response(reply, "rollback", args, {"discarded_count": result["discarded_count"]})

    def _exec_snapshot(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.create_snapshot(workspace, args["name"])
        reply = f"snapshot '{result['snapshot']}' -> {result['commit_id'][:10]}"
        return self._response(reply, "snapshot", args, {"snapshot": result["snapshot"]})

    def _exec_script(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.exec_script(workspace, args["code"])
        reply = result["output"] if result["output"] else f"(script completed in {result['steps']} steps)"
        return self._response(reply, "script", args, {"steps": result["steps"], "elapsed": result["elapsed"]})

    def _exec_overview(self, workspace: str, args: dict[str, Any]) -> dict[str, Any]:
        files = self.db.list_files(workspace)["files"]
        commits = self.db.log_workspace(workspace, limit=5)["commits"]
        head = commits[0]["id"][:10] if commits else "n/a"
        lines = [f"workspace: {workspace}", f"HEAD: {head}", f"files: {len(files)}"]
        if files:
            for item in files[:8]:
                lines.append(f"  {item['path']}")
        return self._response("\n".join(lines), "overview", args, {"file_count": len(files), "head": head})

    # ── Helpers ────────────────────────────────────────────

    def _response(
        self,
        reply: str,
        tool: str,
        args: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            "reply": reply,
            "actions": [{"tool": tool, "args": args, "summary": summary}],
        }

    def _help_text(self) -> str:
        return (
            "FILE COMMANDS\n"
            "  cat <file> [file2 ...]           read / concatenate files\n"
            "  cat <file1> <file2> > <target>   merge files into target\n"
            "  cat <file1> <file2> >> <target>  append files to target\n"
            "  echo 'text' > <file>             write text to file\n"
            "  echo 'text' >> <file>            append text to file\n"
            "  cp <src> <dst>                   copy file\n"
            "  mv <src> <dst>                   move / rename file\n"
            "  rm <file>                        delete file\n"
            "  touch <file>                     create empty file\n"
            "  diff <file1> <file2>             compare two files\n"
            "\n"
            "SEARCH\n"
            "  grep <pattern>                   search all files\n"
            "  grep <pattern> <file>            search in specific file\n"
            "  grep -i <pattern>                case-insensitive search\n"
            "  grep -c <pattern>                count matches\n"
            "  grep -v <pattern> <file>         invert match\n"
            "  find <glob>                      find files by pattern\n"
            "  find . -name '*.py'              find files (linux style)\n"
            "\n"
            "TEXT PROCESSING\n"
            "  wc <file>                        line / word / char count\n"
            "  head <file> [N]                  first N lines (default 10)\n"
            "  tail <file> [N]                  last N lines (default 10)\n"
            "  sort <file>                      sort lines\n"
            "  uniq <file>                      deduplicate adjacent lines\n"
            "  tr <file> <from> <to>            character translation\n"
            "  toc <file>                       generate markdown TOC\n"
            "  wordfreq <file>                  word frequency analysis\n"
            "  linecount                        count lines in all files\n"
            "\n"
            "LISTING\n"
            "  ls                               list files\n"
            "  overview / pwd / status           workspace overview\n"
            "\n"
            "VERSION CONTROL\n"
            "  commit <message>                 commit staged changes\n"
            "  rollback                         discard staged changes\n"
            "  snapshot <name>                   create named snapshot\n"
            "\n"
            "SCRIPTING\n"
            "  script <T2Script code>           execute T2Script\n"
        )


class LLMWorkspaceAgent:
    """Adapter that wraps :class:`LangGraphAgent` as the primary AI agent."""

    def __init__(
        self,
        db: WorkspaceDB,
        *,
        chat_model: Any | None = None,
        langfuse_handler: Any | None = None,
        code_executor: Any | None = None,
        search_client: Any | None = None,
    ) -> None:
        self.db = db
        from .graph_agent import LangGraphAgent
        self._impl = LangGraphAgent(
            db,
            chat_model=chat_model,
            langfuse_handler=langfuse_handler,
            code_executor=code_executor,
            search_client=search_client,
        )

    @property
    def llm(self) -> Any:
        return self._impl.model

    def handle_message(self, workspace: str, message: str) -> dict[str, Any]:
        return self._impl.handle_message(workspace, message)

    def handle_message_stream(self, workspace: str, message: str):
        yield from self._impl.handle_message_stream(workspace, message)
