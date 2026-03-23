from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import posixpath
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WorkspaceError(Exception):
    """Base error for workspace operations."""


class ValidationError(WorkspaceError):
    """Raised when input is invalid."""


class NotFoundError(WorkspaceError):
    """Raised when a workspace or path cannot be found."""


class ConflictError(WorkspaceError):
    """Raised when a merge hits a write conflict."""

    def __init__(self, message: str, conflicts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.conflicts = conflicts or []


class PolicyRejection(WorkspaceError):
    """Raised when a policy hook rejects an operation."""

    def __init__(self, message: str, hook_id: int | None = None) -> None:
        super().__init__(message)
        self.hook_id = hook_id


@dataclass(frozen=True)
class WorkspaceRecord:
    name: str
    head_commit_id: str
    base_commit_id: str
    tracking_workspace: str | None


class WorkspaceDB:
    def __init__(
        self,
        db_path: str | Path = ".text2cli/workspace.db",
        cache: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.cache = cache

    def init(self) -> dict[str, Any]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS blobs (
                    hash TEXT PRIMARY KEY,
                    content BLOB NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS commits (
                    id TEXT PRIMARY KEY,
                    parent_commit_id TEXT REFERENCES commits(id),
                    workspace_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS commit_changes (
                    commit_id TEXT NOT NULL REFERENCES commits(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    op TEXT NOT NULL CHECK(op IN ('upsert', 'delete')),
                    blob_hash TEXT REFERENCES blobs(hash),
                    PRIMARY KEY (commit_id, path)
                );

                CREATE TABLE IF NOT EXISTS workspaces (
                    name TEXT PRIMARY KEY,
                    head_commit_id TEXT NOT NULL REFERENCES commits(id),
                    base_commit_id TEXT NOT NULL REFERENCES commits(id),
                    tracking_workspace TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS staged_changes (
                    workspace_name TEXT NOT NULL REFERENCES workspaces(name) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    op TEXT NOT NULL CHECK(op IN ('upsert', 'delete')),
                    blob_hash TEXT REFERENCES blobs(hash),
                    staged_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_name, path)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_name TEXT NOT NULL,
                    commit_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    name TEXT PRIMARY KEY,
                    workspace_name TEXT NOT NULL,
                    commit_id TEXT NOT NULL REFERENCES commits(id),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS policy_hooks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_pattern TEXT NOT NULL DEFAULT '*',
                    event_type TEXT NOT NULL,
                    hook_type TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                """
            )
            existing = conn.execute("SELECT COUNT(*) AS count FROM workspaces").fetchone()[0]
            if existing == 0:
                root_commit = self._new_commit_id()
                now = self._now()
                conn.execute(
                    """
                    INSERT INTO commits (id, parent_commit_id, workspace_name, kind, message, metadata_json, created_at)
                    VALUES (?, NULL, 'main', 'root', 'Initialize workspace database', '{}', ?)
                    """,
                    (root_commit, now),
                )
                conn.execute(
                    """
                    INSERT INTO workspaces (name, head_commit_id, base_commit_id, tracking_workspace, created_at, updated_at)
                    VALUES ('main', ?, ?, NULL, ?, ?)
                    """,
                    (root_commit, root_commit, now, now),
                )
                self._emit_event(
                    conn,
                    workspace_name="main",
                    commit_id=root_commit,
                    event_type="workspace_initialized",
                    payload={"head_commit_id": root_commit},
                )
            row = self._get_workspace(conn, "main")
            return {
                "status": "ok",
                "db_path": str(self.db_path),
                "main_head_commit_id": row.head_commit_id,
            }

    def create_workspace(self, name: str, from_workspace: str = "main") -> dict[str, Any]:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValidationError("Workspace name must not be empty.")
        if normalized_name == from_workspace:
            raise ValidationError("New workspace name must differ from source workspace.")
        now = self._now()
        with self._connect() as conn:
            source = self._get_workspace(conn, from_workspace)
            exists = conn.execute(
                "SELECT 1 FROM workspaces WHERE name = ?",
                (normalized_name,),
            ).fetchone()
            if exists:
                raise ValidationError(f"Workspace '{normalized_name}' already exists.")
            conn.execute(
                """
                INSERT INTO workspaces (name, head_commit_id, base_commit_id, tracking_workspace, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_name,
                    source.head_commit_id,
                    source.head_commit_id,
                    source.name,
                    now,
                    now,
                ),
            )
            self._emit_event(
                conn,
                workspace_name=normalized_name,
                commit_id=source.head_commit_id,
                event_type="workspace_created",
                payload={"from_workspace": source.name, "base_commit_id": source.head_commit_id},
            )
            return {
                "status": "ok",
                "workspace": normalized_name,
                "from_workspace": source.name,
                "base_commit_id": source.head_commit_id,
            }

    def list_workspaces(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT name, head_commit_id, base_commit_id, tracking_workspace, updated_at
                FROM workspaces
                ORDER BY name
                """
            ).fetchall()
            return {
                "status": "ok",
                "workspaces": [
                    {
                        "name": row["name"],
                        "head_commit_id": row["head_commit_id"],
                        "base_commit_id": row["base_commit_id"],
                        "tracking_workspace": row["tracking_workspace"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ],
            }

    def write_file(self, workspace: str, path: str, content: str) -> dict[str, Any]:
        normalized_path = self._normalize_path(path)
        blob_hash = self._hash_content(content)
        now = self._now()
        with self._connect() as conn:
            self._get_workspace(conn, workspace)
            self._store_blob(conn, blob_hash, content)
            conn.execute(
                """
                INSERT INTO staged_changes (workspace_name, path, op, blob_hash, staged_at)
                VALUES (?, ?, 'upsert', ?, ?)
                ON CONFLICT(workspace_name, path)
                DO UPDATE SET op = 'upsert', blob_hash = excluded.blob_hash, staged_at = excluded.staged_at
                """,
                (workspace, normalized_path, blob_hash, now),
            )
            self._touch_workspace(conn, workspace)
            if self.cache:
                self.cache.invalidate_file(workspace, normalized_path)
            return {
                "status": "ok",
                "workspace": workspace,
                "path": normalized_path,
                "op": "upsert",
                "blob_hash": blob_hash,
                "size_bytes": len(content.encode("utf-8")),
            }

    def patch_file(
        self,
        workspace: str,
        path: str,
        *,
        find: str | None = None,
        replace: str | None = None,
        append: str | None = None,
    ) -> dict[str, Any]:
        if append is None and find is None:
            raise ValidationError("Patch requires either --append or --find/--replace.")
        if append is not None and find is not None:
            raise ValidationError("Choose either append mode or find/replace mode, not both.")
        current = self.read_file(workspace, path)
        original = current["content"]
        if append is not None:
            updated = f"{original}{append}"
        else:
            if replace is None:
                raise ValidationError("Find/replace patch requires --replace.")
            if find not in original:
                raise ValidationError(f"Patch target '{find}' was not found in {path}.")
            updated = original.replace(find, replace)
        result = self.write_file(workspace, path, updated)
        result["previous_blob_hash"] = current["blob_hash"]
        return result

    def delete_file(self, workspace: str, path: str) -> dict[str, Any]:
        normalized_path = self._normalize_path(path)
        now = self._now()
        with self._connect() as conn:
            self._get_workspace(conn, workspace)
            conn.execute(
                """
                INSERT INTO staged_changes (workspace_name, path, op, blob_hash, staged_at)
                VALUES (?, ?, 'delete', NULL, ?)
                ON CONFLICT(workspace_name, path)
                DO UPDATE SET op = 'delete', blob_hash = NULL, staged_at = excluded.staged_at
                """,
                (workspace, normalized_path, now),
            )
            self._touch_workspace(conn, workspace)
            if self.cache:
                self.cache.invalidate_file(workspace, normalized_path)
            return {
                "status": "ok",
                "workspace": workspace,
                "path": normalized_path,
                "op": "delete",
            }

    def read_file(self, workspace: str, path: str) -> dict[str, Any]:
        normalized_path = self._normalize_path(path)
        if self.cache:
            cached = self.cache.get_file(workspace, normalized_path)
            if cached is not None:
                return cached
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            staged = conn.execute(
                """
                SELECT op, blob_hash
                FROM staged_changes
                WHERE workspace_name = ? AND path = ?
                """,
                (workspace, normalized_path),
            ).fetchone()
            if staged:
                if staged["op"] == "delete":
                    raise NotFoundError(f"Path '{normalized_path}' is staged for deletion.")
                content = self._load_blob_text(conn, staged["blob_hash"])
                result = {
                    "status": "ok",
                    "workspace": workspace,
                    "path": normalized_path,
                    "content": content,
                    "blob_hash": staged["blob_hash"],
                    "source": "staged",
                    "head_commit_id": ws.head_commit_id,
                }
                if self.cache:
                    self.cache.set_file(workspace, normalized_path, result)
                return result
            resolved = self._resolve_path(conn, ws.head_commit_id, normalized_path)
            if resolved is None or resolved["op"] == "delete":
                raise NotFoundError(f"Path '{normalized_path}' was not found.")
            content = self._load_blob_text(conn, resolved["blob_hash"])
            result = {
                "status": "ok",
                "workspace": workspace,
                "path": normalized_path,
                "content": content,
                "blob_hash": resolved["blob_hash"],
                "source": "committed",
                "head_commit_id": ws.head_commit_id,
            }
            if self.cache:
                self.cache.set_file(workspace, normalized_path, result)
            return result

    def list_files(self, workspace: str) -> dict[str, Any]:
        if self.cache:
            cached = self.cache.get_state(workspace, "list_files")
            if cached is not None:
                return cached
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            snapshot = self._snapshot_for_workspace(conn, ws)
            result = {
                "status": "ok",
                "workspace": workspace,
                "head_commit_id": ws.head_commit_id,
                "files": [
                    {
                        "path": path,
                        "blob_hash": item["blob_hash"],
                        "size_bytes": item["size_bytes"],
                        "staged": item["staged"],
                    }
                    for path, item in sorted(snapshot.items())
                ],
            }
            if self.cache:
                self.cache.set_state(workspace, "list_files", result)
            return result

    def diff_workspace(self, workspace: str) -> dict[str, Any]:
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            effective = self._effective_staged_changes(conn, ws)
            diffs: list[dict[str, Any]] = []
            for change in effective:
                base_change = self._resolve_path(conn, ws.head_commit_id, change["path"])
                before = (
                    self._load_blob_text(conn, base_change["blob_hash"])
                    if base_change and base_change["op"] == "upsert"
                    else ""
                )
                after = (
                    self._load_blob_text(conn, change["blob_hash"])
                    if change["op"] == "upsert"
                    else ""
                )
                unified = "\n".join(
                    difflib.unified_diff(
                        before.splitlines(),
                        after.splitlines(),
                        fromfile=f"{change['path']}@head",
                        tofile=f"{change['path']}@staged",
                        lineterm="",
                    )
                )
                diffs.append(
                    {
                        "path": change["path"],
                        "op": change["op"],
                        "before_blob_hash": base_change["blob_hash"] if base_change else None,
                        "after_blob_hash": change["blob_hash"],
                        "diff": unified,
                    }
                )
            return {"status": "ok", "workspace": workspace, "changes": diffs}

    def commit_workspace(self, workspace: str, message: str) -> dict[str, Any]:
        if not message.strip():
            raise ValidationError("Commit message must not be empty.")
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            effective = self._effective_staged_changes(conn, ws)
            if not effective:
                return {
                    "status": "noop",
                    "workspace": workspace,
                    "message": "No effective staged changes to commit.",
                    "head_commit_id": ws.head_commit_id,
                }
            self._evaluate_hooks(
                conn,
                workspace=workspace,
                event_type="pre_commit",
                context={"message": message, "paths": [row["path"] for row in effective]},
            )
            self._emit_event(
                conn,
                workspace_name=workspace,
                commit_id=ws.head_commit_id,
                event_type="pre_commit",
                payload={"message": message, "paths": [row["path"] for row in effective]},
            )
            commit_id = self._new_commit_id()
            now = self._now()
            metadata = {
                "base_commit_id": ws.base_commit_id,
                "paths": [row["path"] for row in effective],
            }
            conn.execute(
                """
                INSERT INTO commits (id, parent_commit_id, workspace_name, kind, message, metadata_json, created_at)
                VALUES (?, ?, ?, 'commit', ?, ?, ?)
                """,
                (commit_id, ws.head_commit_id, workspace, message, self._json(metadata), now),
            )
            conn.executemany(
                """
                INSERT INTO commit_changes (commit_id, path, op, blob_hash)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (commit_id, row["path"], row["op"], row["blob_hash"])
                    for row in effective
                ],
            )
            conn.execute("DELETE FROM staged_changes WHERE workspace_name = ?", (workspace,))
            if ws.tracking_workspace is None:
                base_commit_id = commit_id
            else:
                base_commit_id = ws.base_commit_id
            conn.execute(
                """
                UPDATE workspaces
                SET head_commit_id = ?, base_commit_id = ?, updated_at = ?
                WHERE name = ?
                """,
                (commit_id, base_commit_id, now, workspace),
            )
            self._emit_event(
                conn,
                workspace_name=workspace,
                commit_id=commit_id,
                event_type="post_commit",
                payload={"message": message, "paths": [row["path"] for row in effective]},
            )
            if self.cache:
                self.cache.invalidate_workspace(workspace)
            return {
                "status": "ok",
                "workspace": workspace,
                "commit_id": commit_id,
                "parent_commit_id": ws.head_commit_id,
                "paths": [row["path"] for row in effective],
            }

    def merge_workspace(self, source: str, target: str, message: str) -> dict[str, Any]:
        if source == target:
            raise ValidationError("Source and target workspaces must differ.")
        if not message.strip():
            raise ValidationError("Merge message must not be empty.")
        with self._connect() as conn:
            source_ws = self._get_workspace(conn, source)
            target_ws = self._get_workspace(conn, target)
            staged = conn.execute(
                "SELECT COUNT(*) FROM staged_changes WHERE workspace_name = ?",
                (source,),
            ).fetchone()[0]
            if staged:
                raise ValidationError(f"Workspace '{source}' has staged changes. Commit or discard them first.")
            source_delta = self._collect_changes_since(conn, source_ws.head_commit_id, source_ws.base_commit_id)
            if not source_delta:
                return {
                    "status": "noop",
                    "source": source,
                    "target": target,
                    "message": "No source changes to merge.",
                    "target_head_commit_id": target_ws.head_commit_id,
                }
            target_delta = self._collect_changes_since(conn, target_ws.head_commit_id, source_ws.base_commit_id)
            conflicts = self._find_conflicts(source_delta, target_delta)
            if conflicts:
                self._emit_event(
                    conn,
                    workspace_name=target,
                    commit_id=target_ws.head_commit_id,
                    event_type="merge_conflict",
                    payload={
                        "source": source,
                        "target": target,
                        "base_commit_id": source_ws.base_commit_id,
                        "conflicts": conflicts,
                    },
                )
                conn.commit()
                raise ConflictError("Merge conflict detected.", conflicts=conflicts)
            self._evaluate_hooks(
                conn,
                workspace=target,
                event_type="pre_merge",
                context={
                    "source": source,
                    "target": target,
                    "paths": sorted(source_delta),
                },
            )
            self._emit_event(
                conn,
                workspace_name=target,
                commit_id=target_ws.head_commit_id,
                event_type="pre_merge",
                payload={
                    "source": source,
                    "target": target,
                    "base_commit_id": source_ws.base_commit_id,
                    "paths": sorted(source_delta),
                },
            )
            merge_commit_id = self._new_commit_id()
            now = self._now()
            metadata = {
                "source_workspace": source,
                "source_head_commit_id": source_ws.head_commit_id,
                "base_commit_id": source_ws.base_commit_id,
            }
            conn.execute(
                """
                INSERT INTO commits (id, parent_commit_id, workspace_name, kind, message, metadata_json, created_at)
                VALUES (?, ?, ?, 'merge', ?, ?, ?)
                """,
                (merge_commit_id, target_ws.head_commit_id, target, message, self._json(metadata), now),
            )
            conn.executemany(
                """
                INSERT INTO commit_changes (commit_id, path, op, blob_hash)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        merge_commit_id,
                        path,
                        change["op"],
                        change["blob_hash"],
                    )
                    for path, change in sorted(source_delta.items())
                ],
            )
            conn.execute(
                """
                UPDATE workspaces
                SET head_commit_id = ?, base_commit_id = ?, updated_at = ?
                WHERE name = ?
                """,
                (merge_commit_id, merge_commit_id, now, target),
            )
            conn.execute(
                """
                UPDATE workspaces
                SET head_commit_id = ?, base_commit_id = ?, updated_at = ?
                WHERE name = ?
                """,
                (merge_commit_id, merge_commit_id, now, source),
            )
            self._emit_event(
                conn,
                workspace_name=target,
                commit_id=merge_commit_id,
                event_type="post_merge",
                payload={
                    "source": source,
                    "target": target,
                    "paths": sorted(source_delta),
                },
            )
            if self.cache:
                self.cache.invalidate_workspace(source)
                self.cache.invalidate_workspace(target)
            return {
                "status": "ok",
                "source": source,
                "target": target,
                "commit_id": merge_commit_id,
                "paths": sorted(source_delta),
            }

    def log_workspace(self, workspace: str, limit: int = 20) -> dict[str, Any]:
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            commits: list[dict[str, Any]] = []
            current = ws.head_commit_id
            while current and len(commits) < limit:
                row = conn.execute(
                    """
                    SELECT id, parent_commit_id, workspace_name, kind, message, metadata_json, created_at
                    FROM commits
                    WHERE id = ?
                    """,
                    (current,),
                ).fetchone()
                if row is None:
                    break
                commits.append(
                    {
                        "id": row["id"],
                        "parent_commit_id": row["parent_commit_id"],
                        "workspace_name": row["workspace_name"],
                        "kind": row["kind"],
                        "message": row["message"],
                        "metadata": json.loads(row["metadata_json"]),
                        "created_at": row["created_at"],
                    }
                )
                current = row["parent_commit_id"]
            return {"status": "ok", "workspace": workspace, "commits": commits}

    # ── MVCC snapshot & time-travel ────────────────────────────────────

    def create_snapshot(self, workspace: str, snapshot_name: str) -> dict[str, Any]:
        normalized = snapshot_name.strip()
        if not normalized:
            raise ValidationError("Snapshot name must not be empty.")
        now = self._now()
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            exists = conn.execute(
                "SELECT 1 FROM snapshots WHERE name = ?", (normalized,)
            ).fetchone()
            if exists:
                raise ValidationError(f"Snapshot '{normalized}' already exists.")
            conn.execute(
                """
                INSERT INTO snapshots (name, workspace_name, commit_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (normalized, workspace, ws.head_commit_id, now),
            )
            self._emit_event(
                conn,
                workspace_name=workspace,
                commit_id=ws.head_commit_id,
                event_type="snapshot_created",
                payload={"snapshot_name": normalized, "commit_id": ws.head_commit_id},
            )
            return {
                "status": "ok",
                "snapshot": normalized,
                "workspace": workspace,
                "commit_id": ws.head_commit_id,
            }

    def list_snapshots(self, workspace: str | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            if workspace:
                rows = conn.execute(
                    "SELECT name, workspace_name, commit_id, created_at FROM snapshots WHERE workspace_name = ? ORDER BY created_at DESC",
                    (workspace,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT name, workspace_name, commit_id, created_at FROM snapshots ORDER BY created_at DESC"
                ).fetchall()
            return {
                "status": "ok",
                "snapshots": [
                    {
                        "name": row["name"],
                        "workspace": row["workspace_name"],
                        "commit_id": row["commit_id"],
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ],
            }

    def read_file_at(self, workspace: str, path: str, commit_id: str) -> dict[str, Any]:
        """Time-travel read: resolve a path at a specific commit."""
        normalized_path = self._normalize_path(path)
        with self._connect() as conn:
            self._get_workspace(conn, workspace)
            commit_row = conn.execute(
                "SELECT id FROM commits WHERE id = ?", (commit_id,)
            ).fetchone()
            if commit_row is None:
                raise NotFoundError(f"Commit '{commit_id}' was not found.")
            resolved = self._resolve_path(conn, commit_id, normalized_path)
            if resolved is None or resolved["op"] == "delete":
                raise NotFoundError(f"Path '{normalized_path}' was not found at commit '{commit_id}'.")
            content = self._load_blob_text(conn, resolved["blob_hash"])
            return {
                "status": "ok",
                "workspace": workspace,
                "path": normalized_path,
                "content": content,
                "blob_hash": resolved["blob_hash"],
                "source": "historical",
                "at_commit_id": commit_id,
            }

    # ── Rollback & reset ────────────────────────────────────────────

    def rollback_staged(self, workspace: str) -> dict[str, Any]:
        """Discard all staged changes in a workspace."""
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            count = conn.execute(
                "SELECT COUNT(*) FROM staged_changes WHERE workspace_name = ?",
                (workspace,),
            ).fetchone()[0]
            conn.execute(
                "DELETE FROM staged_changes WHERE workspace_name = ?", (workspace,)
            )
            self._touch_workspace(conn, workspace)
            self._emit_event(
                conn,
                workspace_name=workspace,
                commit_id=ws.head_commit_id,
                event_type="staged_rollback",
                payload={"discarded_count": count},
            )
            if self.cache:
                self.cache.invalidate_workspace(workspace)
            return {
                "status": "ok",
                "workspace": workspace,
                "discarded_count": count,
                "head_commit_id": ws.head_commit_id,
            }

    def reset_workspace(self, workspace: str, target_commit_id: str) -> dict[str, Any]:
        """Reset workspace head to a specific ancestor commit, discarding staged changes."""
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            commit_row = conn.execute(
                "SELECT id FROM commits WHERE id = ?", (target_commit_id,)
            ).fetchone()
            if commit_row is None:
                raise NotFoundError(f"Commit '{target_commit_id}' was not found.")
            chain = self._commit_chain(conn, ws.head_commit_id)
            if target_commit_id not in chain:
                raise ValidationError(
                    f"Commit '{target_commit_id}' is not an ancestor of workspace '{workspace}' head."
                )
            now = self._now()
            conn.execute(
                "DELETE FROM staged_changes WHERE workspace_name = ?", (workspace,)
            )
            conn.execute(
                """
                UPDATE workspaces
                SET head_commit_id = ?, base_commit_id = ?, updated_at = ?
                WHERE name = ?
                """,
                (target_commit_id, target_commit_id, now, workspace),
            )
            self._emit_event(
                conn,
                workspace_name=workspace,
                commit_id=target_commit_id,
                event_type="workspace_reset",
                payload={
                    "previous_head": ws.head_commit_id,
                    "new_head": target_commit_id,
                },
            )
            if self.cache:
                self.cache.invalidate_workspace(workspace)
            return {
                "status": "ok",
                "workspace": workspace,
                "previous_head": ws.head_commit_id,
                "new_head": target_commit_id,
            }

    # ── Search operations ───────────────────────────────────────────

    def tree_workspace(self, workspace: str) -> dict[str, Any]:
        """Return hierarchical directory tree of the workspace."""
        if self.cache:
            cached = self.cache.get_state(workspace, "tree")
            if cached is not None:
                return cached
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            snapshot = self._snapshot_for_workspace(conn, ws)

            root: dict[str, Any] = {"name": "/", "type": "dir", "children": []}
            dir_map: dict[str, dict[str, Any]] = {"": root}

            for path, item in sorted(snapshot.items()):
                parts = path.split("/")
                current_dir = ""
                for i, part in enumerate(parts[:-1]):
                    parent_dir = current_dir
                    current_dir = f"{current_dir}/{part}" if current_dir else part
                    if current_dir not in dir_map:
                        new_dir: dict[str, Any] = {"name": part, "type": "dir", "children": []}
                        dir_map[current_dir] = new_dir
                        dir_map[parent_dir]["children"].append(new_dir)

                parent = dir_map.get(current_dir if len(parts) > 1 else "", root)
                parent["children"].append({
                    "name": parts[-1],
                    "type": "file",
                    "path": path,
                    "size_bytes": item["size_bytes"],
                    "staged": item["staged"],
                })

            result = {
                "status": "ok",
                "workspace": workspace,
                "head_commit_id": ws.head_commit_id,
                "tree": root,
                "text": self._render_tree_text(root, ""),
            }
            if self.cache:
                self.cache.set_state(workspace, "tree", result)
            return result

    def _render_tree_text(self, node: dict[str, Any], prefix: str) -> str:
        """Render tree node as CLI-style text."""
        lines: list[str] = []
        if node["type"] == "dir" and node["name"] != "/":
            lines.append(f"{node['name']}/")
        children = node.get("children", [])
        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            connector = "└── " if is_last else "├── "
            extension = "    " if is_last else "│   "
            if child["type"] == "dir":
                sub_text = self._render_tree_text(child, prefix + extension)
                lines.append(f"{prefix}{connector}{sub_text}")
            else:
                tag = " [staged]" if child.get("staged") else ""
                lines.append(f"{prefix}{connector}{child['name']} ({child['size_bytes']}B){tag}")
        return "\n".join(lines)

    def find_files(self, workspace: str, pattern: str) -> dict[str, Any]:
        """Glob-style path search within a workspace snapshot."""
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            snapshot = self._snapshot_for_workspace(conn, ws)
            matched = [
                {
                    "path": path,
                    "blob_hash": item["blob_hash"],
                    "size_bytes": item["size_bytes"],
                    "staged": item["staged"],
                }
                for path, item in sorted(snapshot.items())
                if fnmatch.fnmatch(path, pattern)
            ]
            return {
                "status": "ok",
                "workspace": workspace,
                "pattern": pattern,
                "matches": matched,
            }

    def grep_files(
        self, workspace: str, pattern: str, *, max_matches: int = 200
    ) -> dict[str, Any]:
        """Content search across all files in a workspace snapshot."""
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValidationError(f"Invalid regex pattern: {exc}") from exc
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            snapshot = self._snapshot_for_workspace(conn, ws)
            results: list[dict[str, Any]] = []
            total_matches = 0
            for path, item in sorted(snapshot.items()):
                if total_matches >= max_matches:
                    break
                content = self._load_blob_text(conn, item["blob_hash"])
                lines = content.splitlines()
                file_matches: list[dict[str, Any]] = []
                for line_no, line in enumerate(lines, start=1):
                    if total_matches >= max_matches:
                        break
                    if compiled.search(line):
                        file_matches.append({"line": line_no, "text": line})
                        total_matches += 1
                if file_matches:
                    results.append({"path": path, "matches": file_matches})
            return {
                "status": "ok",
                "workspace": workspace,
                "pattern": pattern,
                "results": results,
                "total_matches": total_matches,
                "truncated": total_matches >= max_matches,
            }

    # ── Policy hooks ────────────────────────────────────────────────

    def register_hook(
        self,
        event_type: str,
        hook_type: str,
        *,
        workspace_pattern: str = "*",
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register a policy hook.
        hook_type: 'path_deny' (deny paths matching config.pattern),
                   'max_file_size' (reject files larger than config.max_bytes),
                   'require_message_prefix' (commit message must start with config.prefix).
        """
        valid_types = {"path_deny", "max_file_size", "require_message_prefix"}
        if hook_type not in valid_types:
            raise ValidationError(f"hook_type must be one of {valid_types}")
        valid_events = {"pre_commit", "pre_merge"}
        if event_type not in valid_events:
            raise ValidationError(f"event_type must be one of {valid_events}")
        config = config or {}
        now = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO policy_hooks (workspace_pattern, event_type, hook_type, config_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (workspace_pattern, event_type, hook_type, self._json(config), now),
            )
            return {
                "status": "ok",
                "hook_id": cursor.lastrowid,
                "event_type": event_type,
                "hook_type": hook_type,
                "workspace_pattern": workspace_pattern,
            }

    def list_hooks(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, workspace_pattern, event_type, hook_type, config_json, enabled, created_at FROM policy_hooks ORDER BY id"
            ).fetchall()
            return {
                "status": "ok",
                "hooks": [
                    {
                        "id": row["id"],
                        "workspace_pattern": row["workspace_pattern"],
                        "event_type": row["event_type"],
                        "hook_type": row["hook_type"],
                        "config": json.loads(row["config_json"]),
                        "enabled": bool(row["enabled"]),
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ],
            }

    # ── Controlled execution ──────────────────────────────────────────

    EXEC_WHITELIST = {
        "wc": "Count lines/words/chars in file content",
        "sort": "Sort lines of file content",
        "head": "Show first N lines of file content",
        "tail": "Show last N lines of file content",
        "uniq": "Remove duplicate adjacent lines",
        "tr": "Transliterate characters",
        "toc": "Generate markdown table of contents",
        "wordfreq": "Word frequency analysis",
        "linecount": "Count lines per file in workspace",
    }

    def exec_run(
        self, workspace: str, command: str, *, path: str | None = None, args: list[str] | None = None
    ) -> dict[str, Any]:
        """Execute a whitelisted text-processing command within the workspace."""
        if command not in self.EXEC_WHITELIST:
            raise ValidationError(
                f"Command '{command}' is not in the whitelist. Allowed: {sorted(self.EXEC_WHITELIST)}"
            )
        args = args or []

        if command == "linecount":
            return self._exec_linecount(workspace)
        if command == "toc":
            return self._exec_toc(workspace, path)
        if command == "wordfreq":
            return self._exec_wordfreq(workspace, path)

        if path is None:
            raise ValidationError(f"Command '{command}' requires a --path argument.")
        file_data = self.read_file(workspace, path)
        content = file_data["content"]

        if command == "wc":
            lines = content.count("\n")
            words = len(content.split())
            chars = len(content)
            return {"status": "ok", "command": command, "path": path, "output": f"{lines} {words} {chars}"}
        if command == "sort":
            sorted_lines = sorted(content.splitlines())
            return {"status": "ok", "command": command, "path": path, "output": "\n".join(sorted_lines)}
        if command == "head":
            n = int(args[0]) if args else 10
            head_lines = content.splitlines()[:n]
            return {"status": "ok", "command": command, "path": path, "output": "\n".join(head_lines)}
        if command == "tail":
            n = int(args[0]) if args else 10
            tail_lines = content.splitlines()[-n:]
            return {"status": "ok", "command": command, "path": path, "output": "\n".join(tail_lines)}
        if command == "uniq":
            lines = content.splitlines()
            result = [lines[0]] if lines else []
            for line in lines[1:]:
                if line != result[-1]:
                    result.append(line)
            return {"status": "ok", "command": command, "path": path, "output": "\n".join(result)}
        if command == "tr":
            if len(args) < 2:
                raise ValidationError("tr requires two arguments: <from> <to>")
            table = str.maketrans(args[0], args[1])
            return {"status": "ok", "command": command, "path": path, "output": content.translate(table)}
        raise ValidationError(f"Unimplemented command: {command}")

    def _exec_linecount(self, workspace: str) -> dict[str, Any]:
        with self._connect() as conn:
            ws = self._get_workspace(conn, workspace)
            snapshot = self._snapshot_for_workspace(conn, ws)
            counts: list[dict[str, Any]] = []
            total = 0
            for path, item in sorted(snapshot.items()):
                content = self._load_blob_text(conn, item["blob_hash"])
                n = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
                counts.append({"path": path, "lines": n})
                total += n
            output_lines = [f"{c['lines']:>6}  {c['path']}" for c in counts]
            output_lines.append(f"{total:>6}  total")
            return {"status": "ok", "command": "linecount", "output": "\n".join(output_lines), "total": total}

    def _exec_toc(self, workspace: str, path: str | None) -> dict[str, Any]:
        if path is None:
            raise ValidationError("toc requires a --path argument.")
        file_data = self.read_file(workspace, path)
        content = file_data["content"]
        toc_lines: list[str] = []
        for line in content.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                level = 0
                for ch in stripped:
                    if ch == "#":
                        level += 1
                    else:
                        break
                title = stripped[level:].strip()
                indent = "  " * (level - 1)
                anchor = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
                toc_lines.append(f"{indent}- [{title}](#{anchor})")
        return {"status": "ok", "command": "toc", "path": path, "output": "\n".join(toc_lines)}

    def _exec_wordfreq(self, workspace: str, path: str | None) -> dict[str, Any]:
        if path is None:
            raise ValidationError("wordfreq requires a --path argument.")
        file_data = self.read_file(workspace, path)
        words = re.findall(r"\b\w+\b", file_data["content"].lower())
        freq: dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:30]
        output = "\n".join(f"{count:>6}  {word}" for word, count in top)
        return {"status": "ok", "command": "wordfreq", "path": path, "output": output}

    # ── Tool schema export (for LLM function calling) ───

    @staticmethod
    def tool_schemas() -> list[dict[str, Any]]:
        """Return tool schemas compatible with LLM function-calling interfaces."""
        return [
            {"name": "fs.read", "description": "Read a file from the workspace", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "path": {"type": "string"}, "at_commit": {"type": "string", "description": "Optional commit ID for time-travel read"}}, "required": ["path"]}},
            {"name": "fs.write", "description": "Write/create a file in the workspace (staged)", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "fs.patch", "description": "Patch a file in-place (find/replace or append)", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "path": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"}, "append": {"type": "string"}}, "required": ["path"]}},
            {"name": "fs.delete", "description": "Delete a file from the workspace (staged)", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "path": {"type": "string"}}, "required": ["path"]}},
            {"name": "fs.list", "description": "List all files in the workspace", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}}}},
            {"name": "fs.tree", "description": "Show hierarchical directory tree of the workspace", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}}}},
            {"name": "fs.find", "description": "Find files matching a glob pattern", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "pattern": {"type": "string"}}, "required": ["pattern"]}},
            {"name": "fs.grep", "description": "Search file contents by regex", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "pattern": {"type": "string"}, "max_matches": {"type": "integer", "default": 200}}, "required": ["pattern"]}},
            {"name": "ws.commit", "description": "Commit all staged changes", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "message": {"type": "string"}}, "required": ["message"]}},
            {"name": "ws.diff", "description": "Show staged diffs", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}}}},
            {"name": "ws.rollback", "description": "Discard all staged changes", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}}}},
            {"name": "ws.snapshot", "description": "Create a named immutable snapshot", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "name": {"type": "string"}}, "required": ["name"]}},
            {"name": "ws.log", "description": "Show commit history", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "limit": {"type": "integer", "default": 20}}}},
            {"name": "exec.run", "description": "Run a whitelisted text-processing command", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "command": {"type": "string", "enum": ["wc", "sort", "head", "tail", "uniq", "tr", "toc", "wordfreq", "linecount"]}, "path": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}}, "required": ["command"]}},
            {"name": "exec.script", "description": "Execute a T2Script program. T2Script is a shell-like DSL with pipes, variables (let), control flow (if/for), functions (fn), try/catch, and built-in commands for text processing, FS/workspace operations, and HTTP. Example: 'fs.read \"data.csv\" | sort | head 5 | fs.write \"top.csv\"'", "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}, "code": {"type": "string", "description": "T2Script source code to execute"}}, "required": ["code"]}},
        ]

    def exec_script(
        self,
        workspace: str,
        code: str,
        *,
        search: Any = None,
        max_steps: int = 10000,
        max_time: float = 30.0,
    ) -> dict[str, Any]:
        """Execute a T2Script program within the workspace."""
        from .lang import execute_script, ScriptError, LexError, ParseError
        try:
            result = execute_script(
                code,
                workspace=workspace,
                db=self,
                search=search,
                max_steps=max_steps,
                max_time=max_time,
            )
            return {
                "status": "ok",
                "output": result.output,
                "variables": {k: v for k, v in result.variables.items() if isinstance(v, (str, int, float, bool, list, dict, type(None)))},
                "steps": result.steps,
                "elapsed": result.elapsed,
            }
        except (LexError, ParseError) as exc:
            raise ValidationError(f"Script parse error: {exc}") from exc
        except ScriptError as exc:
            raise WorkspaceError(str(exc)) from exc

    def list_events(self, workspace: str | None = None, limit: int = 50) -> dict[str, Any]:
        with self._connect() as conn:
            query = """
                SELECT id, workspace_name, commit_id, event_type, payload_json, created_at
                FROM events
            """
            params: tuple[Any, ...]
            if workspace:
                query += " WHERE workspace_name = ?"
                params = (workspace, limit)
                query += " ORDER BY id DESC LIMIT ?"
            else:
                params = (limit,)
                query += " ORDER BY id DESC LIMIT ?"
            rows = conn.execute(query, params).fetchall()
            return {
                "status": "ok",
                "events": [
                    {
                        "id": row["id"],
                        "workspace_name": row["workspace_name"],
                        "commit_id": row["commit_id"],
                        "event_type": row["event_type"],
                        "payload": json.loads(row["payload_json"]),
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ],
            }

    @contextmanager
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _get_workspace(self, conn: sqlite3.Connection, name: str) -> WorkspaceRecord:
        row = conn.execute(
            """
            SELECT name, head_commit_id, base_commit_id, tracking_workspace
            FROM workspaces
            WHERE name = ?
            """,
            (name,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Workspace '{name}' was not found.")
        return WorkspaceRecord(
            name=row["name"],
            head_commit_id=row["head_commit_id"],
            base_commit_id=row["base_commit_id"],
            tracking_workspace=row["tracking_workspace"],
        )

    def _store_blob(self, conn: sqlite3.Connection, blob_hash: str, content: str) -> None:
        now = self._now()
        conn.execute(
            """
            INSERT INTO blobs (hash, content, size_bytes, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(hash) DO NOTHING
            """,
            (blob_hash, content.encode("utf-8"), len(content.encode("utf-8")), now),
        )

    def _load_blob_text(self, conn: sqlite3.Connection, blob_hash: str) -> str:
        if self.cache:
            cached = self.cache.get_blob(blob_hash)
            if cached is not None:
                return cached
        row = conn.execute("SELECT content FROM blobs WHERE hash = ?", (blob_hash,)).fetchone()
        if row is None:
            raise NotFoundError(f"Blob '{blob_hash}' was not found.")
        text = bytes(row["content"]).decode("utf-8")
        if self.cache:
            self.cache.set_blob(blob_hash, text)
        return text

    def _snapshot_for_workspace(
        self,
        conn: sqlite3.Connection,
        ws: WorkspaceRecord,
    ) -> dict[str, dict[str, Any]]:
        snapshot = self._snapshot_at_commit(conn, ws.head_commit_id)
        staged_rows = conn.execute(
            """
            SELECT path, op, blob_hash
            FROM staged_changes
            WHERE workspace_name = ?
            ORDER BY path
            """,
            (ws.name,),
        ).fetchall()
        for row in staged_rows:
            if row["op"] == "delete":
                snapshot.pop(row["path"], None)
                continue
            blob_row = conn.execute(
                "SELECT size_bytes FROM blobs WHERE hash = ?",
                (row["blob_hash"],),
            ).fetchone()
            snapshot[row["path"]] = {
                "blob_hash": row["blob_hash"],
                "size_bytes": blob_row["size_bytes"],
                "staged": True,
            }
        return snapshot

    def _snapshot_at_commit(
        self,
        conn: sqlite3.Connection,
        head_commit_id: str,
    ) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for commit_id in self._commit_chain(conn, head_commit_id):
            rows = conn.execute(
                """
                SELECT path, op, blob_hash
                FROM commit_changes
                WHERE commit_id = ?
                ORDER BY path
                """,
                (commit_id,),
            ).fetchall()
            for row in rows:
                path = row["path"]
                if path in seen:
                    continue
                seen.add(path)
                if row["op"] == "delete":
                    continue
                blob_row = conn.execute(
                    "SELECT size_bytes FROM blobs WHERE hash = ?",
                    (row["blob_hash"],),
                ).fetchone()
                snapshot[path] = {
                    "blob_hash": row["blob_hash"],
                    "size_bytes": blob_row["size_bytes"],
                    "staged": False,
                }
        return snapshot

    def _resolve_path(
        self,
        conn: sqlite3.Connection,
        head_commit_id: str,
        path: str,
    ) -> dict[str, Any] | None:
        for commit_id in self._commit_chain(conn, head_commit_id):
            row = conn.execute(
                """
                SELECT path, op, blob_hash
                FROM commit_changes
                WHERE commit_id = ? AND path = ?
                """,
                (commit_id, path),
            ).fetchone()
            if row is not None:
                return {"path": row["path"], "op": row["op"], "blob_hash": row["blob_hash"]}
        return None

    def _effective_staged_changes(
        self,
        conn: sqlite3.Connection,
        ws: WorkspaceRecord,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT path, op, blob_hash
            FROM staged_changes
            WHERE workspace_name = ?
            ORDER BY path
            """,
            (ws.name,),
        ).fetchall()
        effective: list[dict[str, Any]] = []
        for row in rows:
            current = self._resolve_path(conn, ws.head_commit_id, row["path"])
            if row["op"] == "delete":
                if current is None or current["op"] == "delete":
                    continue
                effective.append({"path": row["path"], "op": "delete", "blob_hash": None})
                continue
            if current and current["op"] == "upsert" and current["blob_hash"] == row["blob_hash"]:
                continue
            effective.append({"path": row["path"], "op": "upsert", "blob_hash": row["blob_hash"]})
        return effective

    def _collect_changes_since(
        self,
        conn: sqlite3.Connection,
        head_commit_id: str,
        base_commit_id: str,
    ) -> dict[str, dict[str, Any]]:
        changes: dict[str, dict[str, Any]] = {}
        for commit_id in self._commit_chain(conn, head_commit_id, stop_at=base_commit_id):
            rows = conn.execute(
                """
                SELECT path, op, blob_hash
                FROM commit_changes
                WHERE commit_id = ?
                ORDER BY path
                """,
                (commit_id,),
            ).fetchall()
            for row in rows:
                changes.setdefault(
                    row["path"],
                    {
                        "op": row["op"],
                        "blob_hash": row["blob_hash"],
                        "commit_id": commit_id,
                    },
                )
        return changes

    def _find_conflicts(
        self,
        source_delta: dict[str, dict[str, Any]],
        target_delta: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for path, source_change in source_delta.items():
            target_change = target_delta.get(path)
            if target_change is None:
                continue
            same_op = source_change["op"] == target_change["op"]
            same_blob = source_change["blob_hash"] == target_change["blob_hash"]
            if same_op and same_blob:
                continue
            conflicts.append(
                {
                    "path": path,
                    "source_op": source_change["op"],
                    "source_blob_hash": source_change["blob_hash"],
                    "target_op": target_change["op"],
                    "target_blob_hash": target_change["blob_hash"],
                }
            )
        return conflicts

    def _commit_chain(
        self,
        conn: sqlite3.Connection,
        head_commit_id: str,
        stop_at: str | None = None,
    ) -> list[str]:
        chain: list[str] = []
        current = head_commit_id
        while current and current != stop_at:
            chain.append(current)
            row = conn.execute(
                "SELECT parent_commit_id FROM commits WHERE id = ?",
                (current,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Commit '{current}' was not found.")
            current = row["parent_commit_id"]
        if stop_at is not None and current != stop_at:
            raise ValidationError(f"Commit '{stop_at}' is not an ancestor of '{head_commit_id}'.")
        return chain

    def _emit_event(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_name: str,
        commit_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO events (workspace_name, commit_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workspace_name, commit_id, event_type, self._json(payload), self._now()),
        )

    def _evaluate_hooks(
        self,
        conn: sqlite3.Connection,
        *,
        workspace: str,
        event_type: str,
        context: dict[str, Any],
    ) -> None:
        rows = conn.execute(
            "SELECT id, workspace_pattern, hook_type, config_json FROM policy_hooks WHERE event_type = ? AND enabled = 1",
            (event_type,),
        ).fetchall()
        for row in rows:
            if not fnmatch.fnmatch(workspace, row["workspace_pattern"]):
                continue
            config = json.loads(row["config_json"])
            hook_type = row["hook_type"]
            hook_id = row["id"]

            if hook_type == "path_deny":
                deny_pattern = config.get("pattern", "")
                for path in context.get("paths", []):
                    if fnmatch.fnmatch(path, deny_pattern):
                        raise PolicyRejection(
                            f"Policy hook #{hook_id}: path '{path}' matches denied pattern '{deny_pattern}'.",
                            hook_id=hook_id,
                        )

            elif hook_type == "max_file_size":
                max_bytes = config.get("max_bytes", 0)
                if max_bytes > 0:
                    for path in context.get("paths", []):
                        staged = conn.execute(
                            "SELECT b.size_bytes FROM staged_changes sc JOIN blobs b ON sc.blob_hash = b.hash WHERE sc.workspace_name = ? AND sc.path = ?",
                            (workspace, path),
                        ).fetchone()
                        if staged and staged["size_bytes"] > max_bytes:
                            raise PolicyRejection(
                                f"Policy hook #{hook_id}: file '{path}' ({staged['size_bytes']} bytes) exceeds limit ({max_bytes} bytes).",
                                hook_id=hook_id,
                            )

            elif hook_type == "require_message_prefix":
                prefix = config.get("prefix", "")
                message = context.get("message", "")
                if prefix and not message.startswith(prefix):
                    raise PolicyRejection(
                        f"Policy hook #{hook_id}: commit message must start with '{prefix}'.",
                        hook_id=hook_id,
                    )

    def _touch_workspace(self, conn: sqlite3.Connection, workspace: str) -> None:
        conn.execute(
            "UPDATE workspaces SET updated_at = ? WHERE name = ?",
            (self._now(), workspace),
        )

    def _normalize_path(self, path: str) -> str:
        raw = path.strip().replace("\\", "/")
        if not raw:
            raise ValidationError("Path must not be empty.")
        normalized = posixpath.normpath(f"/{raw}").lstrip("/")
        if normalized in {"", "."}:
            raise ValidationError("Path must not be empty.")
        if normalized.startswith("../") or normalized == "..":
            raise ValidationError("Parent traversal is not allowed.")
        return normalized

    def _hash_content(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _new_commit_id(self) -> str:
        return uuid.uuid4().hex[:16]

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _json(self, value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
