from __future__ import annotations

import argparse
import json
import mimetypes
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .agent import LLMWorkspaceAgent, WorkspaceAgent
from .cache import create_cache
from .code_executor import CodeExecutor
from .db import ConflictError, NotFoundError, PolicyRejection, ValidationError, WorkspaceDB, WorkspaceError
from .graph_agent import LangGraphAgent, create_chat_model, create_langfuse_handler
from .search import BraveSearchClient, SearchError, load_dotenv
from .workspace_fuse import FuseManager

STATIC_ROOT = Path(__file__).resolve().parent / "static"


@dataclass
class AppResponse:
    status: int
    payload: dict[str, Any]


class Text2CLIApp:
    def __init__(
        self,
        db_path: str | Path,
        *,
        api_key: str | None = None,
        model: str | None = None,
        brave_key: str | None = None,
        redis_url: str | None = None,
    ) -> None:
        import logging as _logging
        import os

        _log = _logging.getLogger(__name__)

        self.cache = create_cache(redis_url)
        self.db = WorkspaceDB(db_path, cache=self.cache)
        self.db.init()
        self.rule_agent = WorkspaceAgent(self.db)

        self.fuse_manager: FuseManager | None = None
        self.code_executor: CodeExecutor | None = None
        try:
            self.fuse_manager = FuseManager(self.db)
            self.code_executor = CodeExecutor(self.fuse_manager)
        except RuntimeError as exc:
            _log.warning("FUSE unavailable, code execution disabled: %s", exc)

        resolved_brave = brave_key or os.environ.get("BRAVE_API_KEY")
        self.search_client: BraveSearchClient | None = None
        if resolved_brave:
            self.search_client = BraveSearchClient(api_key=resolved_brave)

        chat_model = create_chat_model(api_key=api_key, model=model)
        langfuse = create_langfuse_handler()
        self.llm_agent = LLMWorkspaceAgent(
            self.db,
            chat_model=chat_model,
            langfuse_handler=langfuse,
            code_executor=self.code_executor,
            search_client=self.search_client,
        )

    @property
    def agent(self) -> LLMWorkspaceAgent:
        return self.llm_agent

    def handle_api(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any] | None = None,
    ) -> AppResponse:
        body = body or {}
        try:
            if path == "/api/health" and method == "GET":
                return AppResponse(200, {"status": "ok"})
            if path == "/api/workspaces" and method == "GET":
                return AppResponse(200, self.db.list_workspaces())
            if path == "/api/workspaces" and method == "POST":
                payload = self.db.create_workspace(
                    body["name"],
                    from_workspace=body.get("from_workspace", "main"),
                )
                return AppResponse(201, payload)
            if path == "/api/state" and method == "GET":
                workspace = self._require_query(query, "workspace", default="main")
                return AppResponse(200, self._workspace_state(workspace))
            if path == "/api/file" and method == "GET":
                workspace = self._require_query(query, "workspace", default="main")
                file_path = self._require_query(query, "path")
                return AppResponse(200, self.db.read_file(workspace, file_path))
            if path == "/api/file" and method == "PUT":
                payload = self.db.write_file(
                    body.get("workspace", "main"),
                    body["path"],
                    body["content"],
                )
                return AppResponse(200, payload)
            if path == "/api/file" and method == "PATCH":
                payload = self.db.patch_file(
                    body.get("workspace", "main"),
                    body["path"],
                    find=body.get("find"),
                    replace=body.get("replace"),
                    append=body.get("append"),
                )
                return AppResponse(200, payload)
            if path == "/api/file" and method == "DELETE":
                workspace = self._require_query(query, "workspace", default="main")
                file_path = self._require_query(query, "path")
                return AppResponse(200, self.db.delete_file(workspace, file_path))
            if path == "/api/commit" and method == "POST":
                payload = self.db.commit_workspace(
                    body.get("workspace", "main"),
                    body["message"],
                )
                return AppResponse(200, payload)
            if path == "/api/chat" and method == "POST":
                payload = self.agent.handle_message(
                    body.get("workspace", "main"),
                    body["message"],
                )
                return AppResponse(200, payload)
            if path == "/api/exec" and method == "POST":
                payload = self.db.exec_run(
                    body.get("workspace", "main"),
                    body["command"],
                    path=body.get("path"),
                    args=body.get("args"),
                )
                return AppResponse(200, payload)
            if path == "/api/tool-schemas" and method == "GET":
                return AppResponse(200, {"status": "ok", "tools": WorkspaceDB.tool_schemas()})
            if path == "/api/rollback" and method == "POST":
                payload = self.db.rollback_staged(body.get("workspace", "main"))
                return AppResponse(200, payload)
            if path == "/api/snapshot" and method == "POST":
                payload = self.db.create_snapshot(
                    body.get("workspace", "main"),
                    body["name"],
                )
                return AppResponse(200, payload)
            if path == "/api/snapshots" and method == "GET":
                workspace = self._require_query(query, "workspace", default=None)
                return AppResponse(200, self.db.list_snapshots(workspace=workspace))
            if path == "/api/find" and method == "GET":
                workspace = self._require_query(query, "workspace", default="main")
                pattern = self._require_query(query, "pattern")
                return AppResponse(200, self.db.find_files(workspace, pattern))
            if path == "/api/grep" and method == "GET":
                workspace = self._require_query(query, "workspace", default="main")
                pattern = self._require_query(query, "pattern")
                return AppResponse(200, self.db.grep_files(workspace, pattern))
            if path == "/api/tree" and method == "GET":
                workspace = self._require_query(query, "workspace", default="main")
                return AppResponse(200, self.db.tree_workspace(workspace))
            if path == "/api/exec-script" and method == "POST":
                workspace = body.get("workspace", "main")
                code = body.get("code", "")
                if not code:
                    return AppResponse(400, {"status": "error", "error": "Missing 'code' field"})
                return AppResponse(200, self.db.exec_script(
                    workspace, code, search=self.search_client,
                ))
            if path == "/api/config" and method == "GET":
                return AppResponse(200, {
                    "status": "ok",
                    "llm_enabled": self.llm_agent is not None,
                    "llm_model": getattr(self.llm_agent.llm, "model_name", None) or getattr(self.llm_agent.llm, "model", None) if self.llm_agent else None,
                    "search_enabled": self.search_client is not None,
                })
            if path == "/api/cache-stats" and method == "GET":
                return AppResponse(200, {"status": "ok", **self.cache.stats()})
            return AppResponse(404, {"status": "error", "error": f"Unknown route: {method} {path}"})
        except ConflictError as exc:
            return AppResponse(
                409,
                {"status": "conflict", "error": str(exc), "conflicts": exc.conflicts},
            )
        except PolicyRejection as exc:
            return AppResponse(
                403,
                {"status": "rejected", "error": str(exc), "hook_id": exc.hook_id},
            )
        except (ValidationError, NotFoundError, WorkspaceError, KeyError) as exc:
            return AppResponse(400, {"status": "error", "error": str(exc)})

    def _workspace_state(self, workspace: str) -> dict[str, Any]:
        return {
            "status": "ok",
            "workspace": workspace,
            "files": self.db.list_files(workspace)["files"],
            "diff": self.db.diff_workspace(workspace)["changes"],
            "commits": self.db.log_workspace(workspace, limit=8)["commits"],
            "events": self.db.list_events(workspace=workspace, limit=8)["events"],
        }

    def _require_query(
        self,
        query: dict[str, list[str]],
        name: str,
        *,
        default: str | None = None,
    ) -> str:
        values = query.get(name)
        if values and values[0]:
            return values[0]
        if default is not None:
            return default
        raise ValidationError(f"Missing query param: {name}")


def create_server(
    host: str,
    port: int,
    db_path: str | Path,
    *,
    api_key: str | None = None,
    model: str | None = None,
    brave_key: str | None = None,
    redis_url: str | None = None,
) -> tuple[ThreadingHTTPServer, Text2CLIApp]:
    app = Text2CLIApp(db_path, api_key=api_key, model=model, brave_key=brave_key, redis_url=redis_url)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_PATCH(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _dispatch(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/api/chat-stream" and self.command == "POST":
                self._handle_chat_stream()
                return
            if parsed.path.startswith("/api/"):
                body = self._read_json_body()
                response = app.handle_api(self.command, parsed.path, parse_qs(parsed.query), body)
                self._write_json(response.status, response.payload)
                return
            self._serve_static(parsed.path)

        def _handle_chat_stream(self) -> None:
            body = self._read_json_body() or {}
            workspace = body.get("workspace", "main")
            message = body.get("message", "")
            if not message:
                self._write_json(400, {"status": "error", "error": "Missing message"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            try:
                for event in app.agent.handle_message_stream(workspace, message):
                    event_type = event.get("event", "message")
                    data = json.dumps(event.get("data", ""), ensure_ascii=False)
                    chunk = f"event: {event_type}\ndata: {data}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_json_body(self) -> dict[str, Any] | None:
            if self.command in {"GET", "DELETE"}:
                return None
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _serve_static(self, path: str) -> None:
            relative = "index.html" if path in {"", "/"} else path.lstrip("/")
            if relative.startswith(".."):
                self._write_json(404, {"status": "error", "error": "Not found"})
                return
            target = STATIC_ROOT / relative
            if not target.exists() or not target.is_file():
                self._write_json(404, {"status": "error", "error": "Not found"})
                return
            content_type, _ = mimetypes.guess_type(str(target))
            data = target.read_bytes()
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return ThreadingHTTPServer((host, port), Handler), app


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    import os as _os
    parser = argparse.ArgumentParser(description="Run the text2cli web app.")
    parser.add_argument("--host", default=_os.environ.get("T2_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(_os.environ.get("T2_PORT", "8770")))
    parser.add_argument("--db", default=_os.environ.get("T2_DB", ".text2cli/workspace.db"))
    parser.add_argument("--api-key", default=None, help="LLM API key (or set LLM_API_KEY in .env).")
    parser.add_argument("--model", default=None, help="LLM model (or set LLM_MODEL in .env, default: glm-4-flash).")
    parser.add_argument("--brave-key", default=None, help="Brave Search API key (or set BRAVE_API_KEY in .env).")
    parser.add_argument("--redis-url", default=None, help="Redis URL (or set T2_REDIS_URL in .env).")
    args = parser.parse_args(argv)

    server, app = create_server(
        args.host, args.port, args.db,
        api_key=args.api_key, model=args.model, brave_key=args.brave_key,
        redis_url=args.redis_url or _os.environ.get("T2_REDIS_URL"),
    )
    search_tag = "+search" if (args.brave_key or BraveSearchClient.is_configured()) else ""
    langfuse_tag = "+langfuse" if _os.environ.get("LANGFUSE_PUBLIC_KEY") else ""
    exec_tag = "+exec:fuse" if app.fuse_manager else ""
    redis_url = args.redis_url or _os.environ.get("T2_REDIS_URL")
    cache_tag = "redis" if redis_url else "local_lru"
    print(f"text2cli web listening on http://{args.host}:{args.port}  [LangGraph-ReAct{langfuse_tag}{search_tag}{exec_tag} cache:{cache_tag}]", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.fuse_manager:
            app.fuse_manager.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
