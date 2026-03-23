"""Brave Search API client and .env loader (stdlib only)."""
from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class SearchError(Exception):
    """Raised when search API call fails."""


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """Minimal .env loader -- no external dependency.

    Reads KEY=VALUE lines (supports quoting, comments, blank lines).
    Sets values into os.environ only if not already set (env vars take precedence).
    Returns the dict of loaded key-value pairs.
    """
    if path is None:
        path = Path.cwd() / ".env"
    else:
        path = Path(path)
    if not path.is_file():
        return {}

    loaded: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


class BraveSearchClient:
    """Thread-safe Brave Web Search API client (stdlib urllib).

    Stateless per-call HTTP, safe to share across threads.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: int = 15,
        max_retries: int = 1,
    ) -> None:
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        if not self.api_key:
            raise SearchError(
                "Brave API key is required. Set BRAVE_API_KEY in .env or env var."
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self._ssl_ctx = ssl.create_default_context()

    def search(self, query: str, *, count: int = 5) -> dict[str, Any]:
        """Search the web. Returns a clean results dict."""
        params = urllib.request.quote(query, safe="")
        url = f"{BRAVE_SEARCH_URL}?q={params}&count={count}"
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "X-Subscription-Token": self.api_key,
        }

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 2):
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_ctx) as resp:
                    raw = resp.read()
                    data = json.loads(raw.decode("utf-8"))
                    return self._extract(query, data)
            except urllib.error.HTTPError as exc:
                last_exc = exc
                body = exc.read().decode("utf-8", errors="replace")[:500]
                logger.warning("Brave API HTTP %d attempt %d: %s", exc.code, attempt, body)
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise SearchError(f"Brave API error {exc.code}: {body}") from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_exc = exc
                logger.warning("Brave API network error attempt %d: %s", attempt, exc)
                time.sleep(min(2 ** attempt, 4))
                continue
        raise SearchError(f"Brave API failed after {self.max_retries + 1} attempts: {last_exc}")

    @staticmethod
    def _extract(query: str, data: dict[str, Any]) -> dict[str, Any]:
        web = data.get("web", {})
        raw_results = web.get("results", [])
        results = []
        for r in raw_results[:10]:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
                "age": r.get("age", ""),
            })
        return {
            "status": "ok",
            "query": query,
            "result_count": len(results),
            "results": results,
        }

    @staticmethod
    def is_configured() -> bool:
        return bool(os.environ.get("BRAVE_API_KEY"))


WEB_SEARCH_SCHEMA = {
    "name": "web.search",
    "description": "Search the web using Brave Search. Returns titles, URLs and descriptions.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query string"},
            "count": {"type": "integer", "description": "Number of results (1-10, default 5)"},
        },
        "required": ["query"],
    },
}
