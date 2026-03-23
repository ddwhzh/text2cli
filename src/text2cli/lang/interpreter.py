"""T2Script tree-walking interpreter with execution bounds."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .ast_nodes import (
    AssignStmt, BinOp, BoolLit, Command, DotExpr, ExprStmt, Expr,
    FnCallExpr, FnDef, ForStmt, IfStmt, ImportStmt, IndexExpr,
    LetStmt, ListLit, MapLit, NullLit, NumberLit, Pipeline,
    PipelineExpr, Program, ReturnStmt, StringLit, Stmt,
    TryCatch, UnaryOp, VarRef,
)
from .builtins import BuiltinError, get_builtins


class ScriptError(Exception):
    pass


class ScriptTimeout(ScriptError):
    pass


class StepLimitExceeded(ScriptError):
    pass


class ReturnSignal(Exception):
    """Control-flow signal to unwind stack on return."""
    def __init__(self, value: Any) -> None:
        self.value = value


@dataclass
class ScriptContext:
    workspace: str
    db: Any
    search: Any = None
    scope: dict[str, Any] = field(default_factory=dict)
    functions: dict[str, FnDef] = field(default_factory=dict)
    output: list[str] = field(default_factory=list)
    step_count: int = 0
    call_depth: int = 0
    http_calls: int = 0
    start_time: float = 0.0
    max_steps: int = 10000
    max_time: float = 30.0
    max_call_depth: int = 50
    max_http_calls: int = 10
    max_output_bytes: int = 1_048_576
    imported: set[str] = field(default_factory=set)


@dataclass
class ScriptResult:
    output: str
    variables: dict[str, Any]
    steps: int
    elapsed: float


class Interpreter:
    def __init__(self, ctx: ScriptContext) -> None:
        self.ctx = ctx
        self.builtins = get_builtins()
        if ctx.start_time == 0.0:
            ctx.start_time = time.monotonic()

    def run(self, program: Program) -> ScriptResult:
        try:
            for stmt in program.statements:
                self._exec_stmt(stmt)
        except ReturnSignal:
            pass
        elapsed = time.monotonic() - self.ctx.start_time
        return ScriptResult(
            output="\n".join(self.ctx.output),
            variables={k: v for k, v in self.ctx.scope.items() if not callable(v)},
            steps=self.ctx.step_count,
            elapsed=round(elapsed, 3),
        )

    def _tick(self) -> None:
        self.ctx.step_count += 1
        if self.ctx.step_count > self.ctx.max_steps:
            raise StepLimitExceeded(f"Exceeded {self.ctx.max_steps} steps")
        if time.monotonic() - self.ctx.start_time > self.ctx.max_time:
            raise ScriptTimeout(f"Exceeded {self.ctx.max_time}s time limit")

    def _exec_stmt(self, stmt: Stmt) -> None:
        self._tick()

        if isinstance(stmt, LetStmt):
            self.ctx.scope[stmt.name] = self._eval(stmt.value)
            return

        if isinstance(stmt, AssignStmt):
            val = self._eval(stmt.value)
            self._assign_target(stmt.target, val)
            return

        if isinstance(stmt, IfStmt):
            if _truthy(self._eval(stmt.condition)):
                self._exec_block(stmt.body)
            else:
                done = False
                for cond, body in stmt.elif_clauses:
                    if _truthy(self._eval(cond)):
                        self._exec_block(body)
                        done = True
                        break
                if not done and stmt.else_body:
                    self._exec_block(stmt.else_body)
            return

        if isinstance(stmt, ForStmt):
            iterable = self._eval(stmt.iterable)
            items = _to_iterable(iterable)
            for item in items:
                self._tick()
                self.ctx.scope[stmt.var_name] = item
                self._exec_block(stmt.body)
            return

        if isinstance(stmt, FnDef):
            self.ctx.functions[stmt.name] = stmt
            return

        if isinstance(stmt, ReturnStmt):
            val = self._eval(stmt.value) if stmt.value else None
            raise ReturnSignal(val)

        if isinstance(stmt, TryCatch):
            try:
                self._exec_block(stmt.try_body)
            except (ScriptError, BuiltinError) as exc:
                self.ctx.scope[stmt.catch_var] = str(exc)
                self._exec_block(stmt.catch_body)
            return

        if isinstance(stmt, ImportStmt):
            self._exec_import(stmt.path)
            return

        if isinstance(stmt, ExprStmt):
            result = self._eval(stmt.expr)
            if isinstance(result, str) and result:
                self.ctx.output.append(result)
            return

    def _exec_block(self, program: Program) -> None:
        for stmt in program.statements:
            self._exec_stmt(stmt)

    def _exec_import(self, path: str) -> None:
        if path in self.ctx.imported:
            return
        self.ctx.imported.add(path)
        try:
            file_data = self.ctx.db.read_file(self.ctx.workspace, path)
        except Exception as exc:
            raise ScriptError(f"import: cannot read '{path}': {exc}") from exc
        from .parser import parse
        try:
            program = parse(file_data["content"])
        except Exception as exc:
            raise ScriptError(f"import: parse error in '{path}': {exc}") from exc
        self._exec_block(program)

    def _assign_target(self, target: Expr, val: Any) -> None:
        if isinstance(target, VarRef):
            self.ctx.scope[target.name] = val
        elif isinstance(target, IndexExpr):
            obj = self._eval(target.obj)
            idx = self._eval(target.index)
            if isinstance(obj, list) and isinstance(idx, (int, float)):
                obj[int(idx)] = val
            elif isinstance(obj, dict):
                obj[str(idx)] = val
            else:
                raise ScriptError(f"Cannot index-assign into {type(obj).__name__}")
        elif isinstance(target, DotExpr):
            obj = self._eval(target.obj)
            if isinstance(obj, dict):
                obj[target.attr] = val
            else:
                raise ScriptError(f"Cannot dot-assign on {type(obj).__name__}")
        else:
            raise ScriptError("Invalid assignment target")

    # ── Expression Evaluation ────────────────────────

    def _eval(self, expr: Expr) -> Any:
        self._tick()

        if isinstance(expr, StringLit):
            return self._interpolate(expr.value)

        if isinstance(expr, NumberLit):
            return expr.value

        if isinstance(expr, BoolLit):
            return expr.value

        if isinstance(expr, NullLit):
            return None

        if isinstance(expr, ListLit):
            return [self._eval(e) for e in expr.elements]

        if isinstance(expr, MapLit):
            return {k: self._eval(v) for k, v in expr.pairs}

        if isinstance(expr, VarRef):
            if expr.name in self.ctx.scope:
                return self.ctx.scope[expr.name]
            if expr.name in self.ctx.functions:
                return self.ctx.functions[expr.name]
            raise ScriptError(f"Undefined variable: {expr.name}")

        if isinstance(expr, IndexExpr):
            obj = self._eval(expr.obj)
            idx = self._eval(expr.index)
            if isinstance(obj, list):
                return obj[int(idx)]
            if isinstance(obj, dict):
                return obj.get(str(idx))
            if isinstance(obj, str):
                return obj[int(idx)]
            raise ScriptError(f"Cannot index into {type(obj).__name__}")

        if isinstance(expr, DotExpr):
            obj = self._eval(expr.obj)
            if isinstance(obj, dict):
                return obj.get(expr.attr)
            raise ScriptError(f"Cannot access .{expr.attr} on {type(obj).__name__}")

        if isinstance(expr, BinOp):
            return self._eval_binop(expr)

        if isinstance(expr, UnaryOp):
            val = self._eval(expr.operand)
            if expr.op == "not":
                return not _truthy(val)
            if expr.op == "-":
                return -_to_number(val)
            raise ScriptError(f"Unknown unary op: {expr.op}")

        if isinstance(expr, FnCallExpr):
            return self._call_fn(expr.name, [self._eval(a) for a in expr.args])

        if isinstance(expr, PipelineExpr):
            return self._exec_pipeline(expr.pipeline, stdin=None)

        raise ScriptError(f"Unknown expression type: {type(expr).__name__}")

    def _eval_binop(self, expr: BinOp) -> Any:
        if expr.op == "and":
            left = self._eval(expr.left)
            return left if not _truthy(left) else self._eval(expr.right)
        if expr.op == "or":
            left = self._eval(expr.left)
            return left if _truthy(left) else self._eval(expr.right)

        left = self._eval(expr.left)
        right = self._eval(expr.right)

        if expr.op == "+":
            if isinstance(left, str) or isinstance(right, str):
                return _to_str(left) + _to_str(right)
            return _to_number(left) + _to_number(right)
        if expr.op == "-":
            return _to_number(left) - _to_number(right)
        if expr.op == "*":
            return _to_number(left) * _to_number(right)
        if expr.op == "/":
            r = _to_number(right)
            if r == 0:
                raise ScriptError("Division by zero")
            return _to_number(left) / r
        if expr.op == "==":
            return left == right
        if expr.op == "!=":
            return left != right
        if expr.op == "<":
            return _to_number(left) < _to_number(right)
        if expr.op == ">":
            return _to_number(left) > _to_number(right)
        if expr.op == "<=":
            return _to_number(left) <= _to_number(right)
        if expr.op == ">=":
            return _to_number(left) >= _to_number(right)
        raise ScriptError(f"Unknown operator: {expr.op}")

    # ── Pipeline Execution ───────────────────────────

    def _exec_pipeline(self, pipeline: Pipeline, *, stdin: str | None) -> Any:
        data = stdin
        for cmd in pipeline.commands:
            data = self._exec_command(cmd, data)
        return data

    def _exec_command(self, cmd: Command, stdin: str | None) -> Any:
        self._tick()
        name = cmd.name

        if name == "__call__":
            return _to_str(self._eval(cmd.args[0]))

        args = [self._eval(a) for a in cmd.args]

        if name in self.builtins:
            try:
                return self.builtins[name](stdin, args, self.ctx)
            except BuiltinError:
                raise
            except Exception as exc:
                raise ScriptError(f"Error in builtin '{name}': {exc}") from exc

        if name in self.ctx.functions:
            return self._call_user_fn(self.ctx.functions[name], args, stdin)

        raise ScriptError(f"Unknown command: {name}")

    # ── Function Calls ───────────────────────────────

    def _call_fn(self, name: str, args: list[Any]) -> Any:
        if name in _UTILITY_FNS:
            return _UTILITY_FNS[name](args)

        if name in self.ctx.functions:
            return self._call_user_fn(self.ctx.functions[name], args, None)

        if name in self.builtins:
            return self.builtins[name](None, args, self.ctx)

        raise ScriptError(f"Unknown function: {name}")

    def _call_user_fn(self, fn_def: FnDef, args: list[Any], stdin: str | None) -> Any:
        if self.ctx.call_depth >= self.ctx.max_call_depth:
            raise ScriptError(f"Max call depth exceeded ({self.ctx.max_call_depth})")
        self.ctx.call_depth += 1

        saved_scope = dict(self.ctx.scope)
        for i, param in enumerate(fn_def.params):
            self.ctx.scope[param] = args[i] if i < len(args) else None
        if stdin is not None:
            self.ctx.scope["__stdin__"] = stdin

        result: Any = None
        try:
            self._exec_block(fn_def.body)
        except ReturnSignal as ret:
            result = ret.value
        finally:
            self.ctx.scope = saved_scope
            self.ctx.call_depth -= 1
        return result

    # ── String Interpolation ─────────────────────────

    def _interpolate(self, s: str) -> str:
        def replacer(m: re.Match) -> str:
            var = m.group(1)
            val = self.ctx.scope.get(var)
            return _to_str(val) if val is not None else ""
        return re.sub(r"\$\{(\w+)\}", replacer, s)


# ── Utility Functions (pure, no ctx needed) ──────────

def _fn_len(args: list) -> int:
    if not args:
        return 0
    v = args[0]
    if isinstance(v, (str, list, dict)):
        return len(v)
    return 0

def _fn_type(args: list) -> str:
    if not args:
        return "null"
    v = args[0]
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "map"
    return type(v).__name__

def _fn_str(args: list) -> str:
    return _to_str(args[0] if args else None)

def _fn_num(args: list) -> float | int:
    return _to_number(args[0] if args else 0)

def _fn_keys(args: list) -> list:
    v = args[0] if args else None
    if isinstance(v, dict):
        return list(v.keys())
    return []

def _fn_values(args: list) -> list:
    v = args[0] if args else None
    if isinstance(v, dict):
        return list(v.values())
    return []

def _fn_range(args: list) -> list:
    if len(args) == 1:
        return list(range(int(args[0])))
    if len(args) == 2:
        return list(range(int(args[0]), int(args[1])))
    if len(args) >= 3:
        return list(range(int(args[0]), int(args[1]), int(args[2])))
    return []

def _fn_append(args: list) -> list:
    if len(args) < 2:
        raise ScriptError("append requires (list, value)")
    lst = args[0]
    if not isinstance(lst, list):
        raise ScriptError("append: first argument must be a list")
    lst.append(args[1])
    return lst

def _fn_set(args: list) -> Any:
    if len(args) < 3:
        raise ScriptError("set requires (map, key, value)")
    m = args[0]
    if isinstance(m, dict):
        m[str(args[1])] = args[2]
        return m
    raise ScriptError("set: first argument must be a map")

def _fn_int(args: list) -> int:
    return int(_to_number(args[0] if args else 0))

def _fn_float(args: list) -> float:
    return float(_to_number(args[0] if args else 0))

def _fn_split(args: list) -> list:
    if len(args) < 1:
        return []
    s = _to_str(args[0])
    delim = str(args[1]) if len(args) > 1 else "\n"
    return s.split(delim)

def _fn_join(args: list) -> str:
    if not args:
        return ""
    lst = args[0]
    delim = str(args[1]) if len(args) > 1 else "\n"
    if isinstance(lst, list):
        return delim.join(_to_str(x) for x in lst)
    return _to_str(lst)


_UTILITY_FNS: dict[str, Any] = {
    "len": _fn_len, "type": _fn_type, "str": _fn_str, "num": _fn_num,
    "keys": _fn_keys, "values": _fn_values, "range": _fn_range,
    "append": _fn_append, "set": _fn_set,
    "int": _fn_int, "float": _fn_float,
    "split": _fn_split, "join": _fn_join,
}


# ── Type Coercion Helpers ────────────────────────────

def _truthy(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val != "" and val != "false"
    if isinstance(val, (list, dict)):
        return len(val) > 0
    return True


def _to_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float):
        if val == int(val):
            return str(int(val))
        return str(val)
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def _to_number(val: Any) -> float | int:
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return 0
        try:
            return int(val)
        except ValueError:
            try:
                return float(val)
            except ValueError:
                return 0
    if isinstance(val, bool):
        return 1 if val else 0
    return 0


def _to_iterable(val: Any) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        return list(val.keys())
    if isinstance(val, str):
        lines = val.splitlines()
        return [ln for ln in lines if ln.strip()]
    if val is None:
        return []
    return [val]
