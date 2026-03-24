"""Subprocess-based code executor with FUSE-backed workspace access.

Runs Python or Shell code in a subprocess whose CWD is a FUSE mount
of the workspace.  All file I/O the code performs is transparently
forwarded to :class:`WorkspaceDB` via FUSE.

Requires a working :class:`FuseManager`.
"""
from __future__ import annotations

import codecs
import logging
import os
import posixpath
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass, field

from .workspace_fuse import FuseManager
from .remote_sandbox import RemoteSandboxClient, RemoteSandboxError

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 120
DEFAULT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 50_000

DEFAULT_EXEC_MODE = "local-trusted"
EXEC_MODE_TRUSTED = "local-trusted"
EXEC_MODE_UNTRUSTED = "local-untrusted"

DEFAULT_EXEC_BACKEND = "host"
EXEC_BACKEND_HOST = "host"
EXEC_BACKEND_DOCKER = "docker"
EXEC_BACKEND_MICROVM = "microvm"
EXEC_BACKEND_REMOTE = "remote"
EXEC_BACKEND_AUTO = "auto"

DEFAULT_DOCKER_IMAGE = "python:3.12-slim"
DEFAULT_DOCKER_NETWORK = "none"

DEFAULT_ENV_DENYLIST = {
    # LLM / providers
    "LLM_API_KEY",
    "GLM_API_KEY",
    "ZHIPUAI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    # Observability / telemetry
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    # Search
    "BRAVE_API_KEY",
    # Cache / storage
    "T2_REDIS_URL",
    # Common cloud creds (best-effort)
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}

DEFAULT_ENV_ALLOWLIST = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
}


def _split_csv(value: str) -> set[str]:
    items = [v.strip() for v in value.split(",")] if value else []
    return {v for v in items if v}


def _clamp_int(value: int, *, lo: int, hi: int) -> int:
    return min(max(value, lo), hi)


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


def _parse_weights(value: str) -> dict[str, int]:
    # Format: "security=5,latency=2,cost=1"
    weights: dict[str, int] = {}
    for part in _split_csv(value):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        try:
            weights[k] = int(v.strip())
        except ValueError:
            continue
    return weights


def _parse_backend_int_map(value: str) -> dict[str, int]:
    # Format: "host=99,docker=98,microvm=97,remote=96"
    out: dict[str, int] = {}
    for part in _split_csv(value):
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if not k:
            continue
        try:
            out[k] = int(v.strip())
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class ExecutorPolicy:
    mode: str = DEFAULT_EXEC_MODE
    backend: str = DEFAULT_EXEC_BACKEND
    docker_image: str = DEFAULT_DOCKER_IMAGE
    docker_network: str = DEFAULT_DOCKER_NETWORK
    microvm_runtime: str = ""
    remote_url: str = ""
    remote_token: str = ""
    scheduler_enabled: bool = False
    pool_weights: dict[str, int] = field(default_factory=lambda: {"security": 5, "latency": 4, "cost": 2})
    pool_hit_rates: dict[str, int] = field(default_factory=dict)
    required_residency: str = ""
    required_permission_domain: str = ""
    default_timeout: int = DEFAULT_TIMEOUT
    max_output_chars: int = MAX_OUTPUT_CHARS
    env_denylist: frozenset[str] = frozenset(DEFAULT_ENV_DENYLIST)
    env_allowlist: frozenset[str] = frozenset(DEFAULT_ENV_ALLOWLIST)
    max_procs: int = 64
    max_nofile: int = 256
    max_fsize_bytes: int = 10 * 1024 * 1024
    remote_max_files: int = 2000
    remote_max_bytes: int = 5_000_000

    @staticmethod
    def from_env() -> "ExecutorPolicy":
        mode = os.environ.get("T2_EXEC_MODE", DEFAULT_EXEC_MODE).strip() or DEFAULT_EXEC_MODE
        if mode not in {EXEC_MODE_TRUSTED, EXEC_MODE_UNTRUSTED}:
            mode = DEFAULT_EXEC_MODE

        backend = os.environ.get("T2_EXEC_BACKEND", DEFAULT_EXEC_BACKEND).strip() or DEFAULT_EXEC_BACKEND
        if backend not in {
            EXEC_BACKEND_HOST,
            EXEC_BACKEND_DOCKER,
            EXEC_BACKEND_MICROVM,
            EXEC_BACKEND_REMOTE,
            EXEC_BACKEND_AUTO,
        }:
            backend = DEFAULT_EXEC_BACKEND

        docker_image = os.environ.get("T2_EXEC_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE).strip() or DEFAULT_DOCKER_IMAGE
        docker_network = os.environ.get("T2_EXEC_DOCKER_NETWORK", DEFAULT_DOCKER_NETWORK).strip() or DEFAULT_DOCKER_NETWORK
        microvm_runtime = os.environ.get("T2_EXEC_MICROVM_RUNTIME", "").strip()
        remote_url = os.environ.get("T2_EXEC_REMOTE_URL", "").strip()
        remote_token = os.environ.get("T2_EXEC_REMOTE_TOKEN", "").strip()
        scheduler_enabled = os.environ.get("T2_EXEC_SCHEDULER", "0").strip() in {"1", "true", "True", "yes", "YES"}
        weights = _parse_weights(os.environ.get("T2_EXEC_POOL_WEIGHTS", ""))
        hit_rates = _parse_backend_int_map(os.environ.get("T2_EXEC_POOL_HIT_RATES", ""))
        required_residency = os.environ.get("T2_EXEC_REQUIRED_RESIDENCY", "").strip().lower()
        if required_residency not in {"", "local", "remote"}:
            required_residency = ""

        required_perm = os.environ.get("T2_EXEC_REQUIRED_PERMISSION_DOMAIN", "").strip().lower()
        if required_perm not in {"", "host", "container", "microvm", "remote"}:
            required_perm = ""

        timeout_raw = os.environ.get("T2_EXEC_TIMEOUT", "")
        try:
            timeout = int(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        timeout = _clamp_int(timeout, lo=1, hi=MAX_TIMEOUT)

        out_raw = os.environ.get("T2_EXEC_MAX_OUTPUT_CHARS", "")
        try:
            max_output = int(out_raw) if out_raw else MAX_OUTPUT_CHARS
        except ValueError:
            max_output = MAX_OUTPUT_CHARS
        max_output = _clamp_int(max_output, lo=1_000, hi=5_000_000)

        deny = set(DEFAULT_ENV_DENYLIST) | _split_csv(os.environ.get("T2_EXEC_ENV_DENYLIST", ""))
        allow = set(DEFAULT_ENV_ALLOWLIST) | _split_csv(os.environ.get("T2_EXEC_ENV_ALLOWLIST", ""))

        def _int_env(name: str, default: int) -> int:
            raw = os.environ.get(name, "")
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        max_procs = _clamp_int(_int_env("T2_EXEC_MAX_PROCS", 64), lo=0, hi=1_000_000)
        max_nofile = _clamp_int(_int_env("T2_EXEC_MAX_NOFILE", 256), lo=0, hi=1_000_000)
        max_fsize = _clamp_int(_int_env("T2_EXEC_MAX_FSIZE_BYTES", 10 * 1024 * 1024), lo=0, hi=1_000_000_000)
        remote_max_files = _clamp_int(_int_env("T2_EXEC_REMOTE_MAX_FILES", 2000), lo=1, hi=1_000_000)
        remote_max_bytes = _clamp_int(_int_env("T2_EXEC_REMOTE_MAX_BYTES", 5_000_000), lo=10_000, hi=500_000_000)

        return ExecutorPolicy(
            mode=mode,
            backend=backend,
            docker_image=docker_image,
            docker_network=docker_network,
            microvm_runtime=microvm_runtime,
            remote_url=remote_url,
            remote_token=remote_token,
            scheduler_enabled=scheduler_enabled,
            pool_weights=weights or {"security": 5, "latency": 4, "cost": 2},
            pool_hit_rates=hit_rates,
            required_residency=required_residency,
            required_permission_domain=required_perm,
            default_timeout=timeout,
            max_output_chars=max_output,
            env_denylist=frozenset(deny),
            env_allowlist=frozenset(allow),
            max_procs=max_procs,
            max_nofile=max_nofile,
            max_fsize_bytes=max_fsize,
            remote_max_files=remote_max_files,
            remote_max_bytes=remote_max_bytes,
        )


@dataclass(frozen=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False

    def to_display(self) -> str:
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout[:MAX_OUTPUT_CHARS])
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr[:MAX_OUTPUT_CHARS]}")
        if self.timed_out:
            parts.append("[timeout]")
        elif self.exit_code != 0:
            parts.append(f"[exit_code={self.exit_code}]")
        return "\n".join(parts) if parts else "(no output)"


class CodeExecutor:
    """Execute code in a subprocess whose CWD is the workspace FUSE mount."""

    def __init__(self, fuse_manager: FuseManager, *, policy: ExecutorPolicy | None = None) -> None:
        self.fuse_manager = fuse_manager
        self.policy = policy or ExecutorPolicy.from_env()

    def execute(
        self,
        workspace: str,
        language: str,
        code: str,
        *,
        timeout: int | None = None,
    ) -> ExecutionResult:
        effective_timeout = timeout if timeout is not None else self.policy.default_timeout
        effective_timeout = _clamp_int(effective_timeout, lo=1, hi=MAX_TIMEOUT)

        work_dir = self.fuse_manager.ensure_mounted(workspace)

        if language not in {"python", "shell"}:
            return ExecutionResult(
                stdout="",
                stderr=f"Unsupported language: {language}",
                exit_code=1,
            )

        backend = self._select_backend()

        # Security default: local-untrusted must not execute on host backend.
        if self.policy.mode == EXEC_MODE_UNTRUSTED and backend == EXEC_BACKEND_HOST:
            return ExecutionResult(
                stdout="",
                stderr=(
                    "Execution is disabled in local-untrusted mode when exec backend is 'host'. "
                    "Set T2_EXEC_BACKEND=docker (Linux-first), T2_EXEC_BACKEND=microvm, or "
                    "T2_EXEC_BACKEND=remote with T2_EXEC_REMOTE_URL."
                ),
                exit_code=1,
            )

        try:
            if backend == EXEC_BACKEND_DOCKER:
                return self._execute_docker(
                    work_dir=work_dir,
                    language=language,
                    code=code,
                    timeout=effective_timeout,
                )
            if backend == EXEC_BACKEND_MICROVM:
                return self._execute_microvm(
                    work_dir=work_dir,
                    language=language,
                    code=code,
                    timeout=effective_timeout,
                )
            if backend == EXEC_BACKEND_REMOTE:
                return self._execute_remote(
                    work_dir=work_dir,
                    language=language,
                    code=code,
                    timeout=effective_timeout,
                )

            # Host backend (local-trusted).
            if language == "python":
                cmd = [sys.executable, "-c", code]
            else:
                cmd = ["bash", "-c", code]
            env = self._build_env(work_dir)
            return self._run_capped(
                cmd,
                cwd=work_dir,
                env=env,
                timeout=effective_timeout,
                max_output_chars=self.policy.max_output_chars,
                preexec_fn=None,
            )
        except subprocess.TimeoutExpired:  # pragma: no cover (safety net)
            return ExecutionResult(
                stdout="",
                stderr=f"Execution timed out after {effective_timeout}s",
                exit_code=-1,
                timed_out=True,
            )
        except OSError as exc:
            logger.exception("Code execution OS error for workspace=%s", workspace)
            return ExecutionResult(
                stdout="",
                stderr=str(exc),
                exit_code=-1,
            )

    def _select_backend(self) -> str:
        backend = self.policy.backend
        if backend != EXEC_BACKEND_AUTO and not self.policy.scheduler_enabled:
            return backend

        # Candidate pools.
        candidates: list[str] = []
        if self.policy.mode == EXEC_MODE_TRUSTED:
            candidates.append(EXEC_BACKEND_HOST)
        if shutil.which("docker") is not None:
            candidates.append(EXEC_BACKEND_DOCKER)
            if self.policy.microvm_runtime:
                candidates.append(EXEC_BACKEND_MICROVM)
        if self.policy.remote_url:
            candidates.append(EXEC_BACKEND_REMOTE)

        # Filter.
        residency_req = self.policy.required_residency.strip()
        perm_req = self.policy.required_permission_domain.strip()

        def _residency(b: str) -> str:
            return "remote" if b == EXEC_BACKEND_REMOTE else "local"

        def _perm_domain(b: str) -> str:
            if b == EXEC_BACKEND_HOST:
                return "host"
            if b == EXEC_BACKEND_DOCKER:
                return "container"
            if b == EXEC_BACKEND_MICROVM:
                return "microvm"
            if b == EXEC_BACKEND_REMOTE:
                return "remote"
            return ""

        filtered: list[str] = []
        for b in candidates:
            if self.policy.mode == EXEC_MODE_UNTRUSTED and b == EXEC_BACKEND_HOST:
                continue
            if b == EXEC_BACKEND_REMOTE and not self.policy.remote_url:
                continue
            if residency_req and _residency(b) != residency_req:
                continue
            if perm_req and _perm_domain(b) != perm_req:
                continue
            filtered.append(b)

        if not filtered:
            return backend if backend != EXEC_BACKEND_AUTO else EXEC_BACKEND_HOST

        # Score.
        w = self.policy.pool_weights or {"security": 5, "latency": 2, "cost": 1}

        def _attrs(b: str) -> tuple[int, int, int, int]:
            # (security, latency, cost, hit_rate)
            # - higher security/hit_rate is better
            # - lower latency/cost is better
            if b == EXEC_BACKEND_HOST:
                return (1, 1, 1, 99)
            if b == EXEC_BACKEND_DOCKER:
                return (3, 2, 2, 98)
            if b == EXEC_BACKEND_MICROVM:
                return (4, 3, 3, 97)
            if b == EXEC_BACKEND_REMOTE:
                return (4, 4, 4, 96)
            return (0, 10, 10, 0)

        def _score(b: str) -> tuple[int, int, int, int]:
            sec, lat, cost, hit = _attrs(b)
            hit = self.policy.pool_hit_rates.get(b, hit)
            total = (
                w.get("security", 0) * sec
                - w.get("latency", 0) * lat
                - w.get("cost", 0) * cost
                + w.get("hit_rate", 0) * hit
            )
            # tie-breakers: prefer higher hit_rate, then lower latency, then higher security
            return (total, hit, -lat, sec)

        return max(filtered, key=_score)

    def _build_env(self, work_dir: str) -> dict[str, str]:
        if self.policy.mode == EXEC_MODE_UNTRUSTED:
            env: dict[str, str] = {}
            for k in self.policy.env_allowlist:
                if k in os.environ:
                    env[k] = os.environ[k]
        else:
            env = dict(os.environ)

        for k in self.policy.env_denylist:
            env.pop(k, None)

        # Keep all transient writes inside the mounted workspace by default.
        env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"))
        env["HOME"] = work_dir
        env["TMPDIR"] = work_dir
        env["TMP"] = work_dir
        env["TEMP"] = work_dir
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def _build_container_env(self, container_work_dir: str) -> dict[str, str]:
        env: dict[str, str] = {}
        # In containers we default to a strict allowlist to avoid leaking host env.
        for k in self.policy.env_allowlist:
            if k in os.environ and k not in self.policy.env_denylist:
                env[k] = os.environ[k]

        # Normalize key runtime vars.
        env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env["HOME"] = container_work_dir
        env["TMPDIR"] = "/tmp"
        env["TMP"] = "/tmp"
        env["TEMP"] = "/tmp"
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def _execute_docker(
        self,
        *,
        work_dir: str,
        language: str,
        code: str,
        timeout: int,
    ) -> ExecutionResult:
        # Linux-first: relies on bind-mounting the host FUSE mount into a container.
        image = self.policy.docker_image
        network = self.policy.docker_network
        container_work_dir = "/work"
        container_name = f"t2-exec-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        container_env = self._build_container_env(container_work_dir)

        cmd: list[str] = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            network,
            "--workdir",
            container_work_dir,
            "--mount",
            f"type=bind,source={work_dir},target={container_work_dir}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
        ]

        if self.policy.max_procs > 0:
            cmd.extend(["--pids-limit", str(self.policy.max_procs)])
        if self.policy.max_nofile > 0:
            cmd.extend(["--ulimit", f"nofile={self.policy.max_nofile}:{self.policy.max_nofile}"])
        if self.policy.max_fsize_bytes > 0:
            cmd.extend(["--ulimit", f"fsize={self.policy.max_fsize_bytes}:{self.policy.max_fsize_bytes}"])

        for k, v in container_env.items():
            cmd.extend(["-e", f"{k}={v}"])

        cmd.append(image)
        if language == "python":
            cmd.extend(["python", "-c", code])
        else:
            cmd.extend(["sh", "-c", code])

        def _kill_container() -> None:
            # Best-effort cleanup: ensure container is stopped/removed on timeout.
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass

        # Run docker CLI with host env (so docker context works); cap output + enforce timeout.
        return self._run_capped(
            cmd,
            cwd=work_dir,
            env=dict(os.environ),
            timeout=timeout,
            max_output_chars=self.policy.max_output_chars,
            preexec_fn=None,
            on_timeout=_kill_container,
        )

    def _execute_microvm(
        self,
        *,
        work_dir: str,
        language: str,
        code: str,
        timeout: int,
    ) -> ExecutionResult:
        # Linux-first: relies on Docker daemon having a microVM-capable runtime shim installed.
        runtime = self.policy.microvm_runtime
        if not runtime:
            return ExecutionResult(
                stdout="",
                stderr=(
                    "microvm backend requires T2_EXEC_MICROVM_RUNTIME (for example: io.containerd.kata.v2). "
                    "See docs/20260323-v0.4-MICROVM-DEPLOYMENT.md."
                ),
                exit_code=1,
            )

        image = self.policy.docker_image
        network = self.policy.docker_network
        container_work_dir = "/work"
        container_name = f"t2-exec-microvm-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        container_env = self._build_container_env(container_work_dir)

        cmd: list[str] = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--runtime",
            runtime,
            "--network",
            network,
            "--workdir",
            container_work_dir,
            "--mount",
            f"type=bind,source={work_dir},target={container_work_dir}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
        ]

        if self.policy.max_procs > 0:
            cmd.extend(["--pids-limit", str(self.policy.max_procs)])
        if self.policy.max_nofile > 0:
            cmd.extend(["--ulimit", f"nofile={self.policy.max_nofile}:{self.policy.max_nofile}"])
        if self.policy.max_fsize_bytes > 0:
            cmd.extend(["--ulimit", f"fsize={self.policy.max_fsize_bytes}:{self.policy.max_fsize_bytes}"])

        for k, v in container_env.items():
            cmd.extend(["-e", f"{k}={v}"])

        cmd.append(image)
        if language == "python":
            cmd.extend(["python", "-c", code])
        else:
            cmd.extend(["sh", "-c", code])

        def _kill_container() -> None:
            # Best-effort cleanup: ensure container is stopped/removed on timeout.
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                pass

        return self._run_capped(
            cmd,
            cwd=work_dir,
            env=dict(os.environ),
            timeout=timeout,
            max_output_chars=self.policy.max_output_chars,
            preexec_fn=None,
            on_timeout=_kill_container,
        )

    def _execute_remote(
        self,
        *,
        work_dir: str,
        language: str,
        code: str,
        timeout: int,
    ) -> ExecutionResult:
        if not self.policy.remote_url:
            return ExecutionResult(stdout="", stderr="Remote backend requires T2_EXEC_REMOTE_URL.", exit_code=1)

        try:
            files = self._snapshot_work_dir(work_dir)
        except (OSError, ValueError) as exc:
            return ExecutionResult(stdout="", stderr=str(exc), exit_code=1)

        client = RemoteSandboxClient(self.policy.remote_url, token=self.policy.remote_token)
        try:
            resp = client.execute(language=language, code=code, timeout=timeout, files=files)
        except RemoteSandboxError as exc:
            return ExecutionResult(stdout="", stderr=f"Remote sandbox error: {exc}", exit_code=1)

        # Apply remote file changes back into the workspace via FUSE.
        try:
            self._apply_remote_changes(work_dir, resp.changes, resp.deletes)
        except (OSError, ValueError) as exc:
            return ExecutionResult(stdout=resp.stdout, stderr=f"{resp.stderr}\n[apply_error]\n{exc}", exit_code=resp.exit_code, timed_out=resp.timed_out)

        return ExecutionResult(stdout=resp.stdout, stderr=resp.stderr, exit_code=resp.exit_code, timed_out=resp.timed_out)

    def _snapshot_work_dir(self, work_dir: str) -> dict[str, str]:
        root = Path(work_dir)
        files: dict[str, str] = {}
        total_bytes = 0
        count = 0
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            # Skip common noise.
            if rel.startswith(".git/"):
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = p.read_bytes().decode("utf-8", errors="replace")
            total_bytes += len(content.encode("utf-8", errors="replace"))
            count += 1
            if count > self.policy.remote_max_files:
                raise ValueError(f"Remote snapshot too large: files>{self.policy.remote_max_files}")
            if total_bytes > self.policy.remote_max_bytes:
                raise ValueError(f"Remote snapshot too large: bytes>{self.policy.remote_max_bytes}")
            files[rel] = content
        return files

    @staticmethod
    def _apply_remote_changes(work_dir: str, changes: dict[str, str], deletes: list[str]) -> None:
        root = Path(work_dir)
        for raw in deletes:
            rel = _normalize_rel_path(raw)
            target = root / Path(rel)
            if target.exists():
                target.unlink()

        for raw, content in changes.items():
            rel = _normalize_rel_path(raw)
            target = root / Path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _preexec_fn(self) -> Callable[[], None] | None:
        # POSIX-only; on unsupported platforms, run without rlimits.
        try:
            import resource  # type: ignore
        except ImportError:
            return None

        max_procs = self.policy.max_procs
        max_nofile = self.policy.max_nofile
        max_fsize = self.policy.max_fsize_bytes

        def _apply() -> None:
            # Tighten default permissions for any files created by the child.
            os.umask(0o077)

            def _set(limit_name: int, value: int) -> None:
                if value <= 0:
                    return
                soft, hard = resource.getrlimit(limit_name)
                # Avoid increasing any limits; only clamp down.
                if hard != resource.RLIM_INFINITY:
                    value_clamped = min(value, int(hard))
                else:
                    value_clamped = value
                resource.setrlimit(limit_name, (value_clamped, value_clamped))

            if hasattr(resource, "RLIMIT_NPROC"):
                _set(resource.RLIMIT_NPROC, max_procs)
            _set(resource.RLIMIT_NOFILE, max_nofile)
            _set(resource.RLIMIT_FSIZE, max_fsize)

        return _apply

    @staticmethod
    def _run_capped(
        cmd: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
        max_output_chars: int,
        preexec_fn: Callable[[], None] | None = None,
        on_timeout: Callable[[], None] | None = None,
    ) -> ExecutionResult:
        # We cap output during capture (not only display) to prevent memory blowups.
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=preexec_fn,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, data="stdout")
        sel.register(proc.stderr, selectors.EVENT_READ, data="stderr")

        out_dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        err_dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        out_parts: list[str] = []
        err_parts: list[str] = []
        out_len = 0
        err_len = 0
        out_trunc = False
        err_trunc = False

        def _append(stream: str, chunk: bytes) -> None:
            nonlocal out_len, err_len, out_trunc, err_trunc
            if not chunk:
                return
            if stream == "stdout":
                text = out_dec.decode(chunk)
                if out_len >= max_output_chars:
                    out_trunc = True
                    return
                remain = max_output_chars - out_len
                if len(text) > remain:
                    out_parts.append(text[:remain])
                    out_len += remain
                    out_trunc = True
                    return
                out_parts.append(text)
                out_len += len(text)
            else:
                text = err_dec.decode(chunk)
                if err_len >= max_output_chars:
                    err_trunc = True
                    return
                remain = max_output_chars - err_len
                if len(text) > remain:
                    err_parts.append(text[:remain])
                    err_len += remain
                    err_trunc = True
                    return
                err_parts.append(text)
                err_len += len(text)

        deadline = time.monotonic() + max(timeout, 1)
        timed_out = False
        timeout_hook_called = False

        try:
            while True:
                if not sel.get_map():
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0 and not timed_out:
                    timed_out = True
                    if on_timeout is not None and not timeout_hook_called:
                        timeout_hook_called = True
                        try:
                            on_timeout()
                        except OSError:
                            pass
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        proc.kill()

                # Poll frequently to keep UI responsive and avoid long blocks.
                events = sel.select(timeout=0.2 if remaining > 0 else 0.05)
                if not events:
                    if proc.poll() is not None and not sel.get_map():
                        break
                    continue

                for key, _mask in events:
                    stream = key.data
                    fileobj = key.fileobj
                    try:
                        data = fileobj.read1(4096) if hasattr(fileobj, "read1") else fileobj.read(4096)
                    except OSError:
                        data = b""
                    if not data:
                        try:
                            sel.unregister(fileobj)
                        except (KeyError, ValueError):
                            pass
                        continue
                    _append(stream, data)

            # Drain any remaining decoded bytes.
            out_parts.append(out_dec.decode(b"", final=True))
            err_parts.append(err_dec.decode(b"", final=True))
        finally:
            try:
                sel.close()
            except OSError:
                pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except (OSError, ValueError):
                pass
            try:
                if proc.stderr:
                    proc.stderr.close()
            except (OSError, ValueError):
                pass

        if timed_out:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            stdout = "".join(out_parts)
            stderr = "".join(err_parts)
            if out_trunc and len(stdout) < max_output_chars:
                stdout += "\n...[stdout truncated]"
            if err_trunc and len(stderr) < max_output_chars:
                stderr += "\n...[stderr truncated]"
            return ExecutionResult(stdout=stdout, stderr=stderr or f"Execution timed out after {timeout}s", exit_code=-1, timed_out=True)

        proc.wait()
        stdout = "".join(out_parts)
        stderr = "".join(err_parts)
        if out_trunc and len(stdout) < max_output_chars:
            stdout += "\n...[stdout truncated]"
        if err_trunc and len(stderr) < max_output_chars:
            stderr += "\n...[stderr truncated]"
        return ExecutionResult(stdout=stdout, stderr=stderr, exit_code=proc.returncode or 0)
