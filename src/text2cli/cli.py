from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .db import ConflictError, NotFoundError, PolicyRejection, ValidationError, WorkspaceDB, WorkspaceError


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=".text2cli/workspace.db",
        help=argparse.SUPPRESS,
    )
    parser = argparse.ArgumentParser(
        prog="t2",
        description="Agent-native transactional workspace database POC.",
    )
    parser.add_argument(
        "--db",
        default=".text2cli/workspace.db",
        help="SQLite database path.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Initialize the workspace database.", parents=[common])

    ws_create = subparsers.add_parser("ws.create", help="Create a new workspace.", parents=[common])
    ws_create.add_argument("name")
    ws_create.add_argument("--from", dest="from_workspace", default="main")

    subparsers.add_parser("ws.list", help="List workspaces.", parents=[common])

    fs_list = subparsers.add_parser("fs.list", help="List files in a workspace.", parents=[common])
    fs_list.add_argument("--workspace", default="main")

    fs_read = subparsers.add_parser("fs.read", help="Read a file from a workspace.", parents=[common])
    fs_read.add_argument("--workspace", default="main")
    fs_read.add_argument("--at", dest="at_commit", default=None, help="Read at a specific commit (time travel).")
    fs_read.add_argument("path")

    fs_write = subparsers.add_parser("fs.write", help="Stage a file write.", parents=[common])
    fs_write.add_argument("--workspace", default="main")
    fs_write.add_argument("path")
    source = fs_write.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--from-file", dest="from_file")

    fs_patch = subparsers.add_parser("fs.patch", help="Patch file content in-place.", parents=[common])
    fs_patch.add_argument("--workspace", default="main")
    fs_patch.add_argument("path")
    mode = fs_patch.add_mutually_exclusive_group(required=True)
    mode.add_argument("--append")
    mode.add_argument("--find")
    fs_patch.add_argument("--replace")

    fs_delete = subparsers.add_parser("fs.delete", help="Stage a file deletion.", parents=[common])
    fs_delete.add_argument("--workspace", default="main")
    fs_delete.add_argument("path")

    ws_diff = subparsers.add_parser("ws.diff", help="Show staged diffs for a workspace.", parents=[common])
    ws_diff.add_argument("--workspace", default="main")

    ws_commit = subparsers.add_parser("ws.commit", help="Commit staged changes.", parents=[common])
    ws_commit.add_argument("--workspace", default="main")
    ws_commit.add_argument("-m", "--message", required=True)

    ws_merge = subparsers.add_parser("ws.merge", help="Merge one workspace into another.", parents=[common])
    ws_merge.add_argument("--source", required=True)
    ws_merge.add_argument("--target", required=True)
    ws_merge.add_argument("-m", "--message", required=True)

    ws_log = subparsers.add_parser("ws.log", help="Show commit history for a workspace.", parents=[common])
    ws_log.add_argument("--workspace", default="main")
    ws_log.add_argument("--limit", type=int, default=20)

    ws_rollback = subparsers.add_parser("ws.rollback", help="Discard staged changes.", parents=[common])
    ws_rollback.add_argument("--workspace", default="main")

    ws_reset = subparsers.add_parser("ws.reset", help="Reset workspace head to a commit.", parents=[common])
    ws_reset.add_argument("--workspace", default="main")
    ws_reset.add_argument("--to", dest="target_commit", required=True)

    ws_snapshot = subparsers.add_parser("ws.snapshot", help="Create a named snapshot.", parents=[common])
    ws_snapshot.add_argument("name")
    ws_snapshot.add_argument("--workspace", default="main")

    ws_snapshots = subparsers.add_parser("ws.snapshots", help="List snapshots.", parents=[common])
    ws_snapshots.add_argument("--workspace", default=None)

    fs_tree = subparsers.add_parser("fs.tree", help="Show directory tree.", parents=[common])
    fs_tree.add_argument("--workspace", default="main")

    fs_find = subparsers.add_parser("fs.find", help="Find files by glob pattern.", parents=[common])
    fs_find.add_argument("--workspace", default="main")
    fs_find.add_argument("pattern")

    fs_grep = subparsers.add_parser("fs.grep", help="Search file contents by regex.", parents=[common])
    fs_grep.add_argument("--workspace", default="main")
    fs_grep.add_argument("--max-matches", type=int, default=200)
    fs_grep.add_argument("pattern")

    exec_run = subparsers.add_parser("exec.run", help="Run a whitelisted text command.", parents=[common])
    exec_run.add_argument("--workspace", default="main")
    exec_run.add_argument("exec_command", metavar="COMMAND")
    exec_run.add_argument("--path", default=None)
    exec_run.add_argument("exec_args", nargs="*", default=[])

    exec_script = subparsers.add_parser("exec.script", help="Execute a T2Script program.", parents=[common])
    exec_script.add_argument("--workspace", default="main")
    exec_script.add_argument("--code", default=None, help="Inline T2Script code.")
    exec_script.add_argument("--file", default=None, help="Path to .t2 script file in workspace.")

    tool_schemas = subparsers.add_parser("tool-schemas", help="Export tool schemas for LLM.", parents=[common])

    events = subparsers.add_parser("events", help="List recent events.", parents=[common])
    events.add_argument("--workspace")
    events.add_argument("--limit", type=int, default=50)

    return parser


def dispatch(args: argparse.Namespace) -> dict:
    db = WorkspaceDB(args.db)
    if args.command == "init":
        return db.init()
    if args.command == "ws.create":
        return db.create_workspace(args.name, from_workspace=args.from_workspace)
    if args.command == "ws.list":
        return db.list_workspaces()
    if args.command == "fs.list":
        return db.list_files(args.workspace)
    if args.command == "fs.read":
        if args.at_commit:
            return db.read_file_at(args.workspace, args.path, args.at_commit)
        return db.read_file(args.workspace, args.path)
    if args.command == "fs.write":
        content = args.text
        if args.from_file:
            content = Path(args.from_file).read_text(encoding="utf-8")
        return db.write_file(args.workspace, args.path, content)
    if args.command == "fs.patch":
        return db.patch_file(
            args.workspace,
            args.path,
            find=args.find,
            replace=args.replace,
            append=args.append,
        )
    if args.command == "fs.delete":
        return db.delete_file(args.workspace, args.path)
    if args.command == "ws.diff":
        return db.diff_workspace(args.workspace)
    if args.command == "ws.commit":
        return db.commit_workspace(args.workspace, args.message)
    if args.command == "ws.merge":
        return db.merge_workspace(args.source, args.target, args.message)
    if args.command == "ws.log":
        return db.log_workspace(args.workspace, limit=args.limit)
    if args.command == "ws.rollback":
        return db.rollback_staged(args.workspace)
    if args.command == "ws.reset":
        return db.reset_workspace(args.workspace, args.target_commit)
    if args.command == "ws.snapshot":
        return db.create_snapshot(args.workspace, args.name)
    if args.command == "ws.snapshots":
        return db.list_snapshots(workspace=args.workspace)
    if args.command == "fs.tree":
        return db.tree_workspace(args.workspace)
    if args.command == "fs.find":
        return db.find_files(args.workspace, args.pattern)
    if args.command == "fs.grep":
        return db.grep_files(args.workspace, args.pattern, max_matches=args.max_matches)
    if args.command == "exec.run":
        return db.exec_run(args.workspace, args.exec_command, path=args.path, args=args.exec_args)
    if args.command == "exec.script":
        code = args.code
        if args.file:
            file_data = db.read_file(args.workspace, args.file)
            code = file_data["content"]
        if not code:
            raise ValidationError("exec.script requires --code or --file")
        return db.exec_script(args.workspace, code)
    if args.command == "tool-schemas":
        return {"status": "ok", "tools": WorkspaceDB.tool_schemas()}
    if args.command == "events":
        return db.list_events(workspace=args.workspace, limit=args.limit)
    raise ValidationError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = dispatch(args)
        json.dump(payload, sys.stdout, ensure_ascii=True, indent=2)
        sys.stdout.write("\n")
        return 0
    except ConflictError as exc:
        json.dump(
            {
                "status": "conflict",
                "error": str(exc),
                "conflicts": exc.conflicts,
            },
            sys.stdout,
            ensure_ascii=True,
            indent=2,
        )
        sys.stdout.write("\n")
        return 2
    except PolicyRejection as exc:
        json.dump(
            {
                "status": "rejected",
                "error": str(exc),
                "hook_id": exc.hook_id,
            },
            sys.stdout,
            ensure_ascii=True,
            indent=2,
        )
        sys.stdout.write("\n")
        return 3
    except (ValidationError, NotFoundError, WorkspaceError, FileNotFoundError) as exc:
        json.dump(
            {
                "status": "error",
                "error": str(exc),
            },
            sys.stdout,
            ensure_ascii=True,
            indent=2,
        )
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
