"""T2Script built-in command registry.

Each builtin receives (stdin: str | None, args: list, ctx: ScriptContext)
and returns a string (which becomes the next pipe's stdin or the expression value).
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .interpreter import ScriptContext

BuiltinFn = Callable[["str | None", list, "ScriptContext"], str]

_REGISTRY: dict[str, BuiltinFn] = {}


def builtin(name: str) -> Callable[[BuiltinFn], BuiltinFn]:
    def decorator(fn: BuiltinFn) -> BuiltinFn:
        _REGISTRY[name] = fn
        return fn
    return decorator


def get_builtins() -> dict[str, BuiltinFn]:
    return dict(_REGISTRY)


class BuiltinError(Exception):
    pass


def _lines(s: str | None) -> list[str]:
    return (s or "").splitlines()


def _require_stdin(stdin: str | None, cmd: str) -> str:
    if stdin is None:
        raise BuiltinError(f"'{cmd}' requires piped input")
    return stdin


def _arg_str(args: list, idx: int, default: str = "") -> str:
    return str(args[idx]) if idx < len(args) else default


def _arg_int(args: list, idx: int, default: int = 0) -> int:
    if idx < len(args):
        v = args[idx]
        return int(float(v)) if not isinstance(v, int) else v
    return default


# ── Text Processing ──────────────────────────────────

@builtin("echo")
def _echo(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    from .interpreter import _to_str
    text = " ".join(_to_str(a) for a in args)
    return text


@builtin("grep")
def _grep(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "grep")
    invert = False
    ignore_case = False
    count_only = False
    pattern_str = ""
    for a in args:
        s = str(a)
        if s == "-v":
            invert = True
        elif s == "-i":
            ignore_case = True
        elif s == "-c":
            count_only = True
        else:
            pattern_str = s
    if not pattern_str:
        raise BuiltinError("grep requires a pattern argument")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        pat = re.compile(pattern_str, flags)
    except re.error as e:
        raise BuiltinError(f"grep: invalid regex: {e}") from e
    result = [ln for ln in _lines(data) if (bool(pat.search(ln)) != invert)]
    if count_only:
        return str(len(result))
    return "\n".join(result)


@builtin("sort")
def _sort(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "sort")
    reverse = "-r" in [str(a) for a in args]
    numeric = "-n" in [str(a) for a in args]
    lines = _lines(data)
    if numeric:
        def num_key(s: str) -> float:
            m = re.match(r"[-+]?\d*\.?\d+", s.strip())
            return float(m.group()) if m else 0.0
        lines.sort(key=num_key, reverse=reverse)
    else:
        lines.sort(reverse=reverse)
    return "\n".join(lines)


@builtin("uniq")
def _uniq(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "uniq")
    count_mode = "-c" in [str(a) for a in args]
    lines = _lines(data)
    if not lines:
        return ""
    result: list[str] = []
    counts: list[int] = []
    prev = lines[0]
    cnt = 1
    for ln in lines[1:]:
        if ln == prev:
            cnt += 1
        else:
            result.append(prev)
            counts.append(cnt)
            prev = ln
            cnt = 1
    result.append(prev)
    counts.append(cnt)
    if count_mode:
        return "\n".join(f"{c:>4} {r}" for c, r in zip(counts, result))
    return "\n".join(result)


@builtin("head")
def _head(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "head")
    n = _arg_int(args, 0, 10)
    if n <= 0:
        n = 10
    return "\n".join(_lines(data)[:n])


@builtin("tail")
def _tail(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "tail")
    n = _arg_int(args, 0, 10)
    if n <= 0:
        n = 10
    return "\n".join(_lines(data)[-n:])


@builtin("wc")
def _wc(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "wc")
    flags = {str(a) for a in args}
    lines = data.count("\n")
    words = len(data.split())
    chars = len(data)
    if "-l" in flags:
        return str(lines)
    if "-w" in flags:
        return str(words)
    if "-c" in flags:
        return str(chars)
    return f"{lines} {words} {chars}"


@builtin("tr")
def _tr(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "tr")
    if len(args) < 2:
        raise BuiltinError("tr requires two arguments: <from> <to>")
    table = str.maketrans(str(args[0]), str(args[1]))
    return data.translate(table)


@builtin("replace")
def _replace(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "replace")
    if len(args) < 2:
        raise BuiltinError("replace requires: <old> <new>")
    return data.replace(str(args[0]), str(args[1]))


@builtin("split")
def _split(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "split")
    delim = _arg_str(args, 0, ",")
    return "\n".join(data.split(delim))


@builtin("join")
def _join(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "join")
    delim = _arg_str(args, 0, ",")
    return delim.join(_lines(data))


@builtin("cut")
def _cut(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "cut")
    delim = ","
    field_idx = 0
    sa = [str(a) for a in args]
    for i, a in enumerate(sa):
        if a == "-d" and i + 1 < len(sa):
            delim = sa[i + 1]
        elif a == "-f" and i + 1 < len(sa):
            field_idx = int(sa[i + 1]) - 1
    result = []
    for ln in _lines(data):
        parts = ln.split(delim)
        if 0 <= field_idx < len(parts):
            result.append(parts[field_idx].strip())
    return "\n".join(result)


@builtin("upper")
def _upper(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    return _require_stdin(stdin, "upper").upper()


@builtin("lower")
def _lower(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    return _require_stdin(stdin, "lower").lower()


@builtin("trim")
def _trim(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "trim")
    return "\n".join(ln.strip() for ln in _lines(data))


@builtin("rev")
def _rev(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    return "\n".join(reversed(_lines(_require_stdin(stdin, "rev"))))


@builtin("toc")
def _toc(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "toc")
    toc: list[str] = []
    for line in _lines(data):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped[level:].strip()
            anchor = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
            indent = "  " * (level - 1)
            toc.append(f"{indent}- [{title}](#{anchor})")
    return "\n".join(toc)


@builtin("wordfreq")
def _wordfreq(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "wordfreq")
    n = _arg_int(args, 0, 30)
    words = re.findall(r"\b\w+\b", data.lower())
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:n]
    return "\n".join(f"{c:>6} {w}" for w, c in top)


# ── Numeric Aggregation ──────────────────────────────

def _extract_numbers(data: str) -> list[float]:
    nums = []
    for ln in _lines(data):
        ln = ln.strip()
        if ln:
            try:
                nums.append(float(ln))
            except ValueError:
                m = re.match(r"[-+]?\d*\.?\d+", ln)
                if m:
                    nums.append(float(m.group()))
    return nums


@builtin("sum")
def _sum(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    return str(sum(_extract_numbers(_require_stdin(stdin, "sum"))))

@builtin("avg")
def _avg(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    nums = _extract_numbers(_require_stdin(stdin, "avg"))
    return str(sum(nums) / len(nums)) if nums else "0"

@builtin("min")
def _min(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    nums = _extract_numbers(_require_stdin(stdin, "min"))
    return str(min(nums)) if nums else ""

@builtin("max")
def _max(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    nums = _extract_numbers(_require_stdin(stdin, "max"))
    return str(max(nums)) if nums else ""

@builtin("count")
def _count(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "count")
    return str(len([ln for ln in _lines(data) if ln.strip()]))


# ── Data Processing ──────────────────────────────────

@builtin("json.get")
def _json_get(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "json.get")
    path = _arg_str(args, 0)
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as e:
        raise BuiltinError(f"json.get: invalid JSON: {e}") from e
    for key in path.strip(".").split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list):
            try:
                obj = obj[int(key)]
            except (ValueError, IndexError):
                obj = None
        else:
            obj = None
        if obj is None:
            return "null"
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, ensure_ascii=False)
    return str(obj)


@builtin("json.pick")
def _json_pick(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "json.pick")
    fields = _arg_str(args, 0).split(",")
    try:
        obj = json.loads(data)
    except json.JSONDecodeError as e:
        raise BuiltinError(f"json.pick: invalid JSON: {e}") from e

    def pick_one(item: Any) -> dict:
        if not isinstance(item, dict):
            return {}
        return {f.strip(): item.get(f.strip()) for f in fields}

    if isinstance(obj, list):
        result = [pick_one(it) for it in obj]
    else:
        result = pick_one(obj)
    return json.dumps(result, ensure_ascii=False)


@builtin("csv.col")
def _csv_col(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "csv.col")
    col = _arg_int(args, 0, 1) - 1
    result = []
    for ln in _lines(data):
        parts = ln.split(",")
        if 0 <= col < len(parts):
            result.append(parts[col].strip())
    return "\n".join(result)


@builtin("format")
def _format(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    data = _require_stdin(stdin, "format")
    fmt = _arg_str(args, 0, "table")
    if fmt == "json":
        lines = _lines(data)
        return json.dumps(lines, ensure_ascii=False)
    if fmt == "md":
        return "\n".join(f"- {ln}" for ln in _lines(data) if ln.strip())
    rows = [ln.split("\t") for ln in _lines(data) if ln.strip()]
    if not rows:
        return ""
    widths = [max(len(r[c]) if c < len(r) else 0 for r in rows) for c in range(max(len(r) for r in rows))]
    formatted = []
    for r in rows:
        formatted.append("  ".join((r[c] if c < len(r) else "").ljust(widths[c]) for c in range(len(widths))))
    return "\n".join(formatted)


# ── FS Operations (bridged through ctx.db) ───────────

@builtin("fs.read")
def _fs_read(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    path = _arg_str(args, 0)
    if not path:
        raise BuiltinError("fs.read requires a path argument")
    result = ctx.db.read_file(ctx.workspace, path)
    return result["content"]


@builtin("fs.write")
def _fs_write(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    path = _arg_str(args, 0)
    if not path:
        raise BuiltinError("fs.write requires a path argument")
    content = _arg_str(args, 1, "") or stdin or ""
    ctx.db.write_file(ctx.workspace, path, content)
    return ""


@builtin("fs.append")
def _fs_append(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    path = _arg_str(args, 0)
    if not path:
        raise BuiltinError("fs.append requires a path argument")
    new_content = _arg_str(args, 1, "") or stdin or ""
    try:
        existing = ctx.db.read_file(ctx.workspace, path)["content"]
    except Exception:
        existing = ""
    combined = existing + new_content
    ctx.db.write_file(ctx.workspace, path, combined)
    return ""


@builtin("fs.delete")
def _fs_delete(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    path = _arg_str(args, 0)
    if not path:
        raise BuiltinError("fs.delete requires a path argument")
    ctx.db.delete_file(ctx.workspace, path)
    return ""  # side-effect only


@builtin("fs.list")
def _fs_list(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    result = ctx.db.list_files(ctx.workspace)
    return "\n".join(f["path"] for f in result.get("files", []))


@builtin("fs.tree")
def _fs_tree(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    result = ctx.db.tree_workspace(ctx.workspace)
    return result.get("text", "")


@builtin("fs.find")
def _fs_find(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    pattern = _arg_str(args, 0, "*")
    result = ctx.db.find_files(ctx.workspace, pattern)
    return "\n".join(m["path"] for m in result.get("matches", []))


@builtin("fs.grep")
def _fs_grep(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    pattern = _arg_str(args, 0)
    if not pattern:
        raise BuiltinError("fs.grep requires a pattern argument")
    result = ctx.db.grep_files(ctx.workspace, pattern)
    lines = []
    for r in result.get("results", []):
        for m in r.get("matches", []):
            lines.append(f"{r['path']}:{m.get('line_number', '?')}: {m.get('text', '')}")
    return "\n".join(lines)


@builtin("fs.exists")
def _fs_exists(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    path = _arg_str(args, 0)
    try:
        ctx.db.read_file(ctx.workspace, path)
        return "true"
    except Exception:
        return "false"


# ── Workspace Operations ─────────────────────────────

@builtin("ws.commit")
def _ws_commit(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    msg = _arg_str(args, 0, "auto commit")
    result = ctx.db.commit_workspace(ctx.workspace, msg)
    return result.get("commit_id", "")


@builtin("ws.diff")
def _ws_diff(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    result = ctx.db.diff_workspace(ctx.workspace)
    lines = []
    for d in result.get("changes", []):
        lines.append(f"{d.get('op', '?')} {d.get('path', '')}")
    return "\n".join(lines)


@builtin("ws.rollback")
def _ws_rollback(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    result = ctx.db.rollback_staged(ctx.workspace)
    return f"discarded {result.get('discarded_count', 0)} changes"


@builtin("ws.snapshot")
def _ws_snapshot(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    name = _arg_str(args, 0)
    if not name:
        raise BuiltinError("ws.snapshot requires a name argument")
    result = ctx.db.create_snapshot(ctx.workspace, name)
    return result.get("commit_id", "")


@builtin("ws.log")
def _ws_log(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    limit = _arg_int(args, 0, 10)
    result = ctx.db.log_workspace(ctx.workspace, limit=limit)
    lines = []
    for c in result.get("commits", []):
        lines.append(f"{c['id'][:10]} {c.get('message', '')}")
    return "\n".join(lines)


# ── Web / HTTP ───────────────────────────────────────

@builtin("web.search")
def _web_search(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    query = _arg_str(args, 0)
    if not query:
        raise BuiltinError("web.search requires a query argument")
    if ctx.search is None:
        raise BuiltinError("web.search: Brave Search not configured (BRAVE_API_KEY)")
    ctx.http_calls += 1
    if ctx.http_calls > ctx.max_http_calls:
        raise BuiltinError(f"HTTP call limit exceeded ({ctx.max_http_calls})")
    n = _arg_int(args, 1, 5)
    result = ctx.search.search(query, count=n)
    return json.dumps(result.get("results", []), ensure_ascii=False)


@builtin("http.get")
def _http_get(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    url = _arg_str(args, 0)
    if not url:
        raise BuiltinError("http.get requires a URL argument")
    ctx.http_calls += 1
    if ctx.http_calls > ctx.max_http_calls:
        raise BuiltinError(f"HTTP call limit exceeded ({ctx.max_http_calls})")
    return _do_http("GET", url, None, ctx)


@builtin("http.post")
def _http_post(stdin: str | None, args: list, ctx: ScriptContext) -> str:
    url = _arg_str(args, 0)
    if not url:
        raise BuiltinError("http.post requires a URL argument")
    ctx.http_calls += 1
    if ctx.http_calls > ctx.max_http_calls:
        raise BuiltinError(f"HTTP call limit exceeded ({ctx.max_http_calls})")
    body: str | None = None
    if len(args) > 1:
        b = args[1]
        if isinstance(b, dict):
            body = json.dumps(b, ensure_ascii=False)
        else:
            body = str(b)
    elif stdin:
        body = stdin
    return _do_http("POST", url, body, ctx)


_SSL_CTX = ssl.create_default_context()


def _do_http(method: str, url: str, body: str | None, ctx: ScriptContext) -> str:
    headers: dict[str, str] = {"Accept": "application/json"}
    data: bytes | None = None
    if body:
        data = body.encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise BuiltinError(f"HTTP {exc.code}: {err_body}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise BuiltinError(f"HTTP error: {exc}") from exc
