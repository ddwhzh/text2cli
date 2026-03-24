"""Remote sandbox client (stdlib only).

This module provides a minimal HTTP client for the `t2-sandbox` service.
It is used by the execution layer as an optional high-isolation backend.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class RemoteSandboxError(Exception):
    """Raised when remote sandbox request fails."""


@dataclass(frozen=True)
class RemoteSandboxResponse:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    changes: dict[str, str]
    deletes: list[str]

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RemoteSandboxResponse":
        return RemoteSandboxResponse(
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            exit_code=int(data.get("exit_code", 0) or 0),
            timed_out=bool(data.get("timed_out", False)),
            changes={str(k): str(v) for k, v in (data.get("changes", {}) or {}).items()},
            deletes=[str(x) for x in (data.get("deletes", []) or [])],
        )


class RemoteSandboxClient:
    """HTTP client for the remote sandbox service."""

    def __init__(self, base_url: str, *, token: str = "", timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ssl_ctx = ssl.create_default_context()

    def execute(
        self,
        *,
        language: str,
        code: str,
        timeout: int,
        files: dict[str, str],
    ) -> RemoteSandboxResponse:
        url = f"{self.base_url}/api/execute"
        payload = {
            "language": language,
            "code": code,
            "timeout": int(timeout),
            "files": files,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                raw = resp.read()
                data = json.loads(raw.decode("utf-8"))
                return RemoteSandboxResponse.from_dict(data)
        except urllib.error.HTTPError as exc:
            msg = exc.read().decode("utf-8", errors="replace")[:500]
            raise RemoteSandboxError(f"Remote sandbox HTTP {exc.code}: {msg}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RemoteSandboxError(str(exc)) from exc

