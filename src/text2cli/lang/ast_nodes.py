"""T2Script AST node definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Expressions ──────────────────────────────────────

@dataclass
class StringLit:
    value: str  # may contain ${var} interpolation markers

@dataclass
class NumberLit:
    value: float | int

@dataclass
class BoolLit:
    value: bool

@dataclass
class NullLit:
    pass

@dataclass
class ListLit:
    elements: list[Expr]

@dataclass
class MapLit:
    pairs: list[tuple[str, Expr]]

@dataclass
class VarRef:
    name: str

@dataclass
class IndexExpr:
    obj: Expr
    index: Expr

@dataclass
class DotExpr:
    obj: Expr
    attr: str

@dataclass
class BinOp:
    left: Expr
    op: str
    right: Expr

@dataclass
class UnaryOp:
    op: str
    operand: Expr

@dataclass
class FnCallExpr:
    """Function call as expression: len(items), append(list, val)."""
    name: str
    args: list[Expr]

@dataclass
class PipelineExpr:
    """Pipeline used as an expression (in let or for)."""
    pipeline: Pipeline


Expr = (
    StringLit | NumberLit | BoolLit | NullLit |
    ListLit | MapLit | VarRef | IndexExpr | DotExpr |
    BinOp | UnaryOp | FnCallExpr | PipelineExpr
)


# ── Commands & Pipelines ─────────────────────────────

@dataclass
class Command:
    """A single command: name arg1 arg2 ..."""
    name: str           # plain or dotted: "sort", "fs.read", "http.get"
    args: list[Expr]

@dataclass
class Pipeline:
    """A chain of commands: cmd1 | cmd2 | cmd3."""
    commands: list[Command]


# ── Statements ───────────────────────────────────────

@dataclass
class LetStmt:
    name: str
    value: Expr

@dataclass
class AssignStmt:
    """Re-assignment: name = expr, or name[idx] = expr, or name.field = expr."""
    target: Expr  # VarRef, IndexExpr, or DotExpr
    value: Expr

@dataclass
class IfStmt:
    condition: Expr
    body: Program
    elif_clauses: list[tuple[Expr, Program]]
    else_body: Program | None

@dataclass
class ForStmt:
    var_name: str
    iterable: Expr
    body: Program

@dataclass
class FnDef:
    name: str
    params: list[str]
    body: Program

@dataclass
class ReturnStmt:
    value: Expr | None

@dataclass
class TryCatch:
    try_body: Program
    catch_var: str
    catch_body: Program

@dataclass
class ImportStmt:
    path: str

@dataclass
class ExprStmt:
    """A pipeline or expression used as a statement."""
    expr: Expr


Stmt = (
    LetStmt | AssignStmt | IfStmt | ForStmt | FnDef |
    ReturnStmt | TryCatch | ImportStmt | ExprStmt
)


# ── Program ──────────────────────────────────────────

@dataclass
class Program:
    statements: list[Stmt] = field(default_factory=list)
