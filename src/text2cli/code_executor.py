"""Subprocess-based code executor with FUSE-backed workspace access.

Runs Python or Shell code in a subprocess whose CWD is a FUSE mount
of the workspace.  All file I/O the code performs is transparently
forwarded to :class:`WorkspaceDB` via FUSE.

Requires a working :class:`FuseManager`.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass

from .workspace_fuse import FuseManager

logger = logging.getLogger(__name__)

MAX_TIMEOUT = 120
DEFAULT_TIMEOUT = 30
MAX_OUTPUT_CHARS = 50_000


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

    def __init__(self, fuse_manager: FuseManager) -> None:
        self.fuse_manager = fuse_manager

    def execute(
        self,
        workspace: str,
        language: str,
        code: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> ExecutionResult:
        timeout = min(max(timeout, 1), MAX_TIMEOUT)

        work_dir = self.fuse_manager.ensure_mounted(workspace)

        if language == "python":
            cmd = [sys.executable, "-c", code]
        elif language == "shell":
            cmd = ["bash", "-c", code]
        else:
            return ExecutionResult(
                stdout="",
                stderr=f"Unsupported language: {language}",
                exit_code=1,
            )

        try:
            result = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ExecutionResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                stdout="",
                stderr=f"Execution timed out after {timeout}s",
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
