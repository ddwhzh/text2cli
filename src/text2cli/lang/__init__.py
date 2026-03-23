"""T2Script -- in-process workspace scripting engine.

Public API:
    execute_script(source, workspace, db, ...) -> ScriptResult
    parse_script(source) -> Program
"""
from __future__ import annotations

from typing import Any

from .ast_nodes import Program
from .interpreter import Interpreter, ScriptContext, ScriptResult, ScriptError, ScriptTimeout, StepLimitExceeded
from .parser import ParseError, parse
from .tokens import LexError


def parse_script(source: str) -> Program:
    """Parse T2Script source into an AST. Raises LexError or ParseError."""
    return parse(source)


def execute_script(
    source: str,
    *,
    workspace: str,
    db: Any,
    search: Any = None,
    max_steps: int = 10000,
    max_time: float = 30.0,
    max_http_calls: int = 10,
) -> ScriptResult:
    """Parse and execute a T2Script program.

    Thread-safe: each call creates its own ScriptContext with isolated
    scope, output buffer, and step counters.
    """
    program = parse(source)
    ctx = ScriptContext(
        workspace=workspace,
        db=db,
        search=search,
        max_steps=max_steps,
        max_time=max_time,
        max_http_calls=max_http_calls,
    )
    interpreter = Interpreter(ctx)
    return interpreter.run(program)


__all__ = [
    "execute_script",
    "parse_script",
    "ScriptResult",
    "ScriptError",
    "ScriptTimeout",
    "StepLimitExceeded",
    "ParseError",
    "LexError",
]
