from __future__ import annotations

import argparse
import json
import logging
import os
import posixpath
import tempfile
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .code_executor import CodeExecutor, ExecutionResult, ExecutorPolicy

logger = logging.getLogger(__name__)


def _read_json(handler: BaseHTTPRequestHandler, *, max_bytes: int) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    if length > max_bytes:
        raise ValueError(f"Request too large (>{max_bytes} bytes)")
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _normalize_rel_path(path: str) -> str:
    raw = path.strip().replace("\\", "/")
    if not raw:
        raise ValueError("Path must not be empty.")
    normalized = posixpath.normpath(f"/{raw}").lstrip("/")
    if normalized in {"", "."}:
        raise ValueError("Path must not be empty.")
    if normalized.startswith("../") or normalized == "..":
        raise ValueError("Parent traversal is not allowed.")
    return normalized


def _snapshot_dir(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        try:
            result[rel] = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # text2cli is primarily text-oriented; binary becomes replacement chars.
            result[rel] = p.read_bytes().decode("utf-8", errors="replace")
    return result


def _apply_files(root: Path, files: dict[str, str]) -> dict[str, str]:
    normalized_map: dict[str, str] = {}
    for raw_path, content in files.items():
        rel = _normalize_rel_path(raw_path)
        out_path = root / Path(rel)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        normalized_map[rel] = content
    return normalized_map


def _diff_snapshots(before: dict[str, str], after: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    changes: dict[str, str] = {}
    deletes: list[str] = []
    for p, content in after.items():
        if before.get(p) != content:
            changes[p] = content
    for p in before:
        if p not in after:
            deletes.append(p)
    return changes, sorted(deletes)


@dataclass
class SandboxConfig:
    image: str
    network: str
    docker_runtime: str
    token: str
    max_body_bytes: int
    policy: ExecutorPolicy

    @staticmethod
    def from_env(*, image: str | None = None, network: str | None = None, token: str | None = None) -> "SandboxConfig":
        image = image or os.environ.get("T2_EXEC_DOCKER_IMAGE") or os.environ.get("T2_SANDBOX_IMAGE") or "python:3.12-slim"
        network = network or os.environ.get("T2_EXEC_DOCKER_NETWORK") or os.environ.get("T2_SANDBOX_NETWORK") or "none"
        docker_runtime = os.environ.get("T2_SANDBOX_DOCKER_RUNTIME", "").strip()
        token = token or os.environ.get("T2_SANDBOX_TOKEN", "")
        max_body = int(os.environ.get("T2_SANDBOX_MAX_BODY_BYTES", "5000000") or "5000000")
        # We reuse ExecutorPolicy env parsing for output caps & rlimits.
        policy = ExecutorPolicy.from_env()
        return SandboxConfig(
            image=image,
            network=network,
            docker_runtime=docker_runtime,
            token=token,
            max_body_bytes=max_body,
            policy=policy,
        )


class SandboxApp:
    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    def _auth_ok(self, handler: BaseHTTPRequestHandler) -> bool:
        if not self.config.token:
            return True
        auth = handler.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return auth.removeprefix("Bearer ").strip() == self.config.token

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        path = urlsplit(handler.path).path

        if path == "/api/health" and handler.command == "GET":
            _write_json(handler, 200, {"status": "ok"})
            return

        if path == "/api/execute" and handler.command == "POST":
            if not self._auth_ok(handler):
                _write_json(handler, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                body = _read_json(handler, max_bytes=self.config.max_body_bytes)
                language = str(body.get("language", "")).strip()
                code = str(body.get("code", ""))
                timeout = int(body.get("timeout", self.config.policy.default_timeout) or self.config.policy.default_timeout)
                files = body.get("files", {})
                if not isinstance(files, dict):
                    raise ValueError("'files' must be a map of path->content")
                files_str = {str(k): str(v) for k, v in files.items()}
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                _write_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

            try:
                resp = self._execute(language=language, code=code, timeout=timeout, files=files_str)
                _write_json(handler, 200, resp)
            except Exception as exc:
                logger.exception("sandbox execute failed")
                _write_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return

        _write_json(handler, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _execute(self, *, language: str, code: str, timeout: int, files: dict[str, str]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="t2-sandbox-") as td:
            root = Path(td)
            before = _apply_files(root, files)

            # Build docker run command similar to CodeExecutor's docker backend.
            container_work_dir = "/work"
            container_name = f"t2-sandbox-{os.getpid()}-{uuid.uuid4().hex[:12]}"
            docker_cmd: list[str] = [
                "docker",
                "run",
                "--rm",
                "--name",
                container_name,
                "--network",
                self.config.network,
                "--workdir",
                container_work_dir,
                "--mount",
                f"type=bind,source={td},target={container_work_dir}",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
            ]

            if self.config.docker_runtime:
                docker_cmd.extend(["--runtime", self.config.docker_runtime])

            if self.config.policy.max_procs > 0:
                docker_cmd.extend(["--pids-limit", str(self.config.policy.max_procs)])
            if self.config.policy.max_nofile > 0:
                docker_cmd.extend(["--ulimit", f"nofile={self.config.policy.max_nofile}:{self.config.policy.max_nofile}"])
            if self.config.policy.max_fsize_bytes > 0:
                docker_cmd.extend(["--ulimit", f"fsize={self.config.policy.max_fsize_bytes}:{self.config.policy.max_fsize_bytes}"])

            # Minimal env inside container.
            docker_cmd.extend(["-e", "PYTHONUTF8=1", "-e", "PYTHONIOENCODING=utf-8", "-e", f"HOME={container_work_dir}"])

            docker_cmd.append(self.config.image)
            if language == "python":
                docker_cmd.extend(["python", "-c", code])
            elif language == "shell":
                docker_cmd.extend(["sh", "-c", code])
            else:
                return {
                    "stdout": "",
                    "stderr": f"Unsupported language: {language}",
                    "exit_code": 1,
                    "timed_out": False,
                    "changes": {},
                    "deletes": [],
                }

            def _kill_container() -> None:
                try:
                    import subprocess

                    subprocess.run(
                        ["docker", "rm", "-f", container_name],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass

            result: ExecutionResult = CodeExecutor._run_capped(  # noqa: SLF001
                docker_cmd,
                cwd=td,
                env=dict(os.environ),
                timeout=timeout,
                max_output_chars=self.config.policy.max_output_chars,
                on_timeout=_kill_container,
            )

            after = _snapshot_dir(root)
            changes, deletes = _diff_snapshots(before, after)
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "changes": changes,
                "deletes": deletes,
            }


def create_server(host: str, port: int, app: SandboxApp) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            app.handle(self)

        def do_POST(self) -> None:  # noqa: N802
            app.handle(self)

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("sandbox %s - %s", self.address_string(), fmt % args)

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    from .search import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the text2cli remote sandbox service.")
    parser.add_argument("--host", default=os.environ.get("T2_SANDBOX_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("T2_SANDBOX_PORT", "9770")))
    parser.add_argument("--image", default=None, help="Docker image (default: python:3.12-slim)")
    parser.add_argument("--network", default=None, help="Docker network mode (default: none)")
    parser.add_argument("--token", default=None, help="Optional Bearer token for /api/execute")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    cfg = SandboxConfig.from_env(image=args.image, network=args.network, token=args.token)
    app = SandboxApp(cfg)
    server = create_server(args.host, args.port, app)
    print(f"text2cli sandbox listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

