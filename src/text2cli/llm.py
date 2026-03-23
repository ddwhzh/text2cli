from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4-flash"


class LLMClientError(Exception):
    """Raised when the LLM API call fails after retries."""


def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = os.environ.get(key)
        if val:
            return val
    return default


class GLMClient:
    """OpenAI-compatible LLM client using only stdlib (urllib).

    Supports any OpenAI-compatible endpoint (GLM, DeepSeek, Moonshot, etc.).
    Environment variables (checked in order):
        API key:  LLM_API_KEY > GLM_API_KEY > ZHIPUAI_API_KEY
        Base URL: LLM_BASE_URL > GLM_BASE_URL (default: GLM v4)
        Model:    LLM_MODEL > GLM_MODEL (default: glm-4-flash)
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_retries: int = 2,
        temperature: float | None = None,
    ) -> None:
        self.api_key = api_key or _env("LLM_API_KEY", "GLM_API_KEY", "ZHIPUAI_API_KEY")
        if not self.api_key:
            raise LLMClientError(
                "LLM API key required. Set LLM_API_KEY (or GLM_API_KEY / ZHIPUAI_API_KEY) env var."
            )
        self.model = model or _env("LLM_MODEL", "GLM_MODEL", default=DEFAULT_MODEL)
        self.base_url = (base_url or _env("LLM_BASE_URL", "GLM_BASE_URL", default=DEFAULT_BASE_URL)).rstrip("/")
        self.timeout = timeout if timeout is not None else int(_env("LLM_TIMEOUT", "GLM_TIMEOUT", default="60"))
        self.max_retries = max_retries
        self.temperature = temperature if temperature is not None else float(_env("LLM_TEMPERATURE", "GLM_TEMPERATURE", default="0.7"))
        self._ssl_ctx = ssl.create_default_context()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        """Single chat completion call. Returns the assistant message dict."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]
            payload["tool_choice"] = tool_choice
        return self._do_request(payload)

    def _do_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                    raw = resp.read()
                    data = json.loads(raw.decode("utf-8"))
                    choices = data.get("choices", [])
                    if not choices:
                        raise LLMClientError(f"Empty choices in response: {data}")
                    return choices[0]["message"]
            except urllib.error.HTTPError as exc:
                last_exc = exc
                error_body = exc.read().decode("utf-8", errors="replace")
                logger.warning(
                    "LLM API HTTP %d on attempt %d: %s", exc.code, attempt, error_body[:500]
                )
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMClientError(f"LLM API error {exc.code}: {error_body[:500]}") from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_exc = exc
                logger.warning("LLM API network error on attempt %d: %s", attempt, exc)
                time.sleep(min(2 ** attempt, 8))
                continue
        raise LLMClientError(f"LLM API failed after {self.max_retries + 1} attempts: {last_exc}")

    @staticmethod
    def is_configured() -> bool:
        return bool(
            os.environ.get("LLM_API_KEY")
            or os.environ.get("GLM_API_KEY")
            or os.environ.get("ZHIPUAI_API_KEY")
        )
