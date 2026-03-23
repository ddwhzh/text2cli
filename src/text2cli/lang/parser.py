"""T2Script recursive-descent parser."""
from __future__ import annotations

from typing import Any

from .ast_nodes import (
    AssignStmt, BinOp, BoolLit, Command, DotExpr, ExprStmt, Expr,
    FnCallExpr, FnDef, ForStmt, IfStmt, ImportStmt, IndexExpr,
    LetStmt, ListLit, MapLit, NullLit, NumberLit, Pipeline,
    PipelineExpr, Program, ReturnStmt, StringLit, Stmt,
    TryCatch, UnaryOp, VarRef,
)
from .tokens import TT, Token, tokenize


class ParseError(Exception):
    def __init__(self, msg: str, token: Token) -> None:
        super().__init__(f"Parse error at L{token.line}:{token.col}: {msg}")
        self.token = token


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else self.tokens[-1]

    def at(self, *types: TT) -> bool:
        return self.peek().type in types

    def eat(self, tt: TT) -> Token:
        tok = self.peek()
        if tok.type != tt:
            raise ParseError(f"Expected {tt.name}, got {tok.type.name} ({tok.value!r})", tok)
        self.pos += 1
        return tok

    def maybe(self, tt: TT) -> Token | None:
        if self.at(tt):
            return self.eat(tt)
        return None

    def skip_newlines(self) -> None:
        while self.at(TT.NEWLINE):
            self.pos += 1

    def parse(self) -> Program:
        prog = self._program(until={TT.EOF})
        self.eat(TT.EOF)
        return prog

    def _program(self, until: set[TT]) -> Program:
        stmts: list[Stmt] = []
        self.skip_newlines()
        while not self.at(*until):
            stmts.append(self._statement())
            self.skip_newlines()
        return Program(stmts)

    def _statement(self) -> Stmt:
        tok = self.peek()

        if tok.type == TT.LET:
            return self._let_stmt()
        if tok.type == TT.IF:
            return self._if_stmt()
        if tok.type == TT.FOR:
            return self._for_stmt()
        if tok.type == TT.FN:
            return self._fn_def()
        if tok.type == TT.RETURN:
            return self._return_stmt()
        if tok.type == TT.TRY:
            return self._try_catch()
        if tok.type == TT.IMPORT:
            return self._import_stmt()

        expr = self._expression()

        if self.at(TT.ASSIGN) and isinstance(expr, (VarRef, IndexExpr, DotExpr)):
            self.eat(TT.ASSIGN)
            val = self._expression()
            self._eat_terminator()
            return AssignStmt(target=expr, value=val)

        self._eat_terminator()
        return ExprStmt(expr=expr)

    def _eat_terminator(self) -> None:
        if self.at(TT.NEWLINE):
            self.skip_newlines()
        elif self.at(TT.EOF, TT.RBRACE):
            pass

    def _let_stmt(self) -> LetStmt:
        self.eat(TT.LET)
        name = self.eat(TT.IDENT).value
        self.eat(TT.ASSIGN)
        value = self._expression()
        self._eat_terminator()
        return LetStmt(name=name, value=value)

    def _if_stmt(self) -> IfStmt:
        self.eat(TT.IF)
        cond = self._expression()
        body = self._block()
        elifs: list[tuple[Expr, Program]] = []
        else_body: Program | None = None
        self.skip_newlines()
        while self.at(TT.ELIF):
            self.eat(TT.ELIF)
            ec = self._expression()
            eb = self._block()
            elifs.append((ec, eb))
            self.skip_newlines()
        if self.at(TT.ELSE):
            self.eat(TT.ELSE)
            else_body = self._block()
        return IfStmt(condition=cond, body=body, elif_clauses=elifs, else_body=else_body)

    def _for_stmt(self) -> ForStmt:
        self.eat(TT.FOR)
        var = self.eat(TT.IDENT).value
        self.eat(TT.IN)
        iterable = self._expression()
        body = self._block()
        return ForStmt(var_name=var, iterable=iterable, body=body)

    def _fn_def(self) -> FnDef:
        self.eat(TT.FN)
        name = self.eat(TT.IDENT).value
        self.eat(TT.LPAREN)
        params: list[str] = []
        if not self.at(TT.RPAREN):
            params.append(self.eat(TT.IDENT).value)
            while self.at(TT.COMMA):
                self.eat(TT.COMMA)
                params.append(self.eat(TT.IDENT).value)
        self.eat(TT.RPAREN)
        body = self._block()
        return FnDef(name=name, params=params, body=body)

    def _return_stmt(self) -> ReturnStmt:
        self.eat(TT.RETURN)
        val: Expr | None = None
        if not self.at(TT.NEWLINE, TT.RBRACE, TT.EOF):
            val = self._expression()
        self._eat_terminator()
        return ReturnStmt(value=val)

    def _try_catch(self) -> TryCatch:
        self.eat(TT.TRY)
        try_body = self._block()
        self.skip_newlines()
        self.eat(TT.CATCH)
        var = self.eat(TT.IDENT).value
        catch_body = self._block()
        return TryCatch(try_body=try_body, catch_var=var, catch_body=catch_body)

    def _import_stmt(self) -> ImportStmt:
        self.eat(TT.IMPORT)
        path = self.eat(TT.STRING).value
        self._eat_terminator()
        return ImportStmt(path=path)

    def _block(self) -> Program:
        self.skip_newlines()
        self.eat(TT.LBRACE)
        prog = self._program(until={TT.RBRACE})
        self.eat(TT.RBRACE)
        return prog

    # ── Expressions ──────────────────────────────────

    def _expression(self) -> Expr:
        return self._or_expr()

    def _or_expr(self) -> Expr:
        left = self._and_expr()
        while self.at(TT.OR):
            self.eat(TT.OR)
            right = self._and_expr()
            left = BinOp(left, "or", right)
        return left

    def _and_expr(self) -> Expr:
        left = self._compare()
        while self.at(TT.AND):
            self.eat(TT.AND)
            right = self._compare()
            left = BinOp(left, "and", right)
        return left

    def _compare(self) -> Expr:
        left = self._addition()
        if self.at(TT.EQ, TT.NE, TT.LT, TT.GT, TT.LE, TT.GE):
            op = self.eat(self.peek().type).value
            right = self._addition()
            left = BinOp(left, op, right)
        return left

    def _addition(self) -> Expr:
        left = self._term()
        while self.at(TT.PLUS, TT.MINUS):
            op = self.eat(self.peek().type).value
            right = self._term()
            left = BinOp(left, op, right)
        return left

    def _term(self) -> Expr:
        left = self._unary()
        while self.at(TT.STAR, TT.SLASH):
            op = self.eat(self.peek().type).value
            right = self._unary()
            left = BinOp(left, op, right)
        return left

    def _unary(self) -> Expr:
        if self.at(TT.NOT):
            self.eat(TT.NOT)
            return UnaryOp("not", self._unary())
        if self.at(TT.MINUS):
            self.eat(TT.MINUS)
            return UnaryOp("-", self._unary())
        return self._postfix()

    def _postfix(self) -> Expr:
        expr = self._primary()
        while True:
            if self.at(TT.LBRACKET):
                self.eat(TT.LBRACKET)
                idx = self._expression()
                self.eat(TT.RBRACKET)
                expr = IndexExpr(expr, idx)
            elif self.at(TT.DOTTED):
                break
            elif self.peek().type == TT.IDENT and isinstance(expr, VarRef):
                # could be dot access: parsed separately
                break
            else:
                break
        return expr

    def _primary(self) -> Expr:
        tok = self.peek()

        if tok.type == TT.STRING:
            self.eat(TT.STRING)
            return StringLit(tok.value)

        if tok.type == TT.NUMBER:
            self.eat(TT.NUMBER)
            v = float(tok.value) if "." in tok.value else int(tok.value)
            return NumberLit(v)

        if tok.type == TT.TRUE:
            self.eat(TT.TRUE)
            return BoolLit(True)

        if tok.type == TT.FALSE:
            self.eat(TT.FALSE)
            return BoolLit(False)

        if tok.type == TT.NULL:
            self.eat(TT.NULL)
            return NullLit()

        if tok.type == TT.LBRACKET:
            return self._list_lit()

        if tok.type == TT.LBRACE:
            return self._map_lit()

        if tok.type == TT.LPAREN:
            self.eat(TT.LPAREN)
            expr = self._expression()
            self.eat(TT.RPAREN)
            return expr

        if tok.type == TT.DOLLAR:
            self.eat(TT.DOLLAR)
            name = self.eat(TT.IDENT).value
            expr: Expr = VarRef(name)
            while self.at(TT.LBRACKET):
                self.eat(TT.LBRACKET)
                idx = self._expression()
                self.eat(TT.RBRACKET)
                expr = IndexExpr(expr, idx)
            return expr

        if tok.type == TT.DOTTED:
            self.eat(TT.DOTTED)
            return self._command_as_expr(tok.value)

        if tok.type == TT.IDENT:
            self.eat(TT.IDENT)
            if self.at(TT.LPAREN):
                return self._fn_call_expr(tok.value)
            return self._command_or_var(tok.value)

        raise ParseError(f"Unexpected token {tok.type.name} ({tok.value!r})", tok)

    def _command_as_expr(self, name: str) -> Expr:
        """Parse a dotted command (fs.read "path") as a pipeline expression."""
        args = self._command_args()
        cmd = Command(name=name, args=args)
        pipeline = Pipeline(commands=[cmd])
        pipeline = self._maybe_extend_pipeline(pipeline)
        return PipelineExpr(pipeline)

    def _command_or_var(self, name: str) -> Expr:
        """An IDENT that could be a variable reference or a bare command."""
        if self._looks_like_command_arg():
            args = self._command_args()
            cmd = Command(name=name, args=args)
            pipeline = Pipeline(commands=[cmd])
            pipeline = self._maybe_extend_pipeline(pipeline)
            return PipelineExpr(pipeline)

        expr: Expr = VarRef(name)
        if self._at_pipe():
            cmd = Command(name=name, args=[])
            pipeline = Pipeline(commands=[cmd])
            pipeline = self._maybe_extend_pipeline(pipeline)
            return PipelineExpr(pipeline)
        return expr

    def _fn_call_expr(self, name: str) -> Expr:
        self.eat(TT.LPAREN)
        args: list[Expr] = []
        if not self.at(TT.RPAREN):
            args.append(self._expression())
            while self.at(TT.COMMA):
                self.eat(TT.COMMA)
                args.append(self._expression())
        self.eat(TT.RPAREN)
        result: Expr = FnCallExpr(name=name, args=args)

        if self._at_pipe():
            cmd = Command(name="__call__", args=[result])
            pipeline = Pipeline(commands=[cmd])
            pipeline = self._maybe_extend_pipeline(pipeline)
            return PipelineExpr(pipeline)
        return result

    def _looks_like_command_arg(self) -> bool:
        tok = self.peek()
        if tok.type in (TT.STRING, TT.NUMBER, TT.DOLLAR, TT.MINUS):
            return True
        if tok.type == TT.IDENT and self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].type == TT.LPAREN:
            return True
        return False

    def _at_pipe(self) -> bool:
        return self.at(TT.PIPE)

    def _command_args(self) -> list[Expr]:
        args: list[Expr] = []
        while True:
            tok = self.peek()
            if tok.type == TT.STRING:
                self.eat(TT.STRING)
                args.append(StringLit(tok.value))
            elif tok.type == TT.NUMBER:
                self.eat(TT.NUMBER)
                v = float(tok.value) if "." in tok.value else int(tok.value)
                args.append(NumberLit(v))
            elif tok.type == TT.DOLLAR:
                self.eat(TT.DOLLAR)
                name = self.eat(TT.IDENT).value
                expr: Expr = VarRef(name)
                while self.at(TT.LBRACKET):
                    self.eat(TT.LBRACKET)
                    idx = self._expression()
                    self.eat(TT.RBRACKET)
                    expr = IndexExpr(expr, idx)
                args.append(expr)
            elif tok.type == TT.TRUE:
                self.eat(TT.TRUE)
                args.append(BoolLit(True))
            elif tok.type == TT.FALSE:
                self.eat(TT.FALSE)
                args.append(BoolLit(False))
            elif tok.type == TT.IDENT and self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].type == TT.LPAREN:
                name = self.eat(TT.IDENT).value
                args.append(self._fn_call_expr(name))
            elif tok.type == TT.LBRACKET:
                args.append(self._list_lit())
            elif tok.type == TT.MINUS and self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].type == TT.NUMBER:
                self.eat(TT.MINUS)
                n = self.eat(TT.NUMBER)
                v = float(n.value) if "." in n.value else int(n.value)
                args.append(NumberLit(-v))
            elif tok.type == TT.MINUS and self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].type == TT.IDENT:
                self.eat(TT.MINUS)
                flag = self.eat(TT.IDENT).value
                args.append(StringLit(f"-{flag}"))
            else:
                break
        return args

    def _maybe_extend_pipeline(self, pipeline: Pipeline) -> Pipeline:
        while self.at(TT.PIPE):
            self.eat(TT.PIPE)
            self.skip_newlines()
            tok = self.peek()
            if tok.type == TT.DOTTED:
                self.eat(TT.DOTTED)
                name = tok.value
            elif tok.type == TT.IDENT:
                self.eat(TT.IDENT)
                name = tok.value
            else:
                raise ParseError(f"Expected command after '|', got {tok.type.name}", tok)
            args = self._command_args()
            pipeline.commands.append(Command(name=name, args=args))
        return pipeline

    def _list_lit(self) -> ListLit:
        self.eat(TT.LBRACKET)
        self.skip_newlines()
        elements: list[Expr] = []
        if not self.at(TT.RBRACKET):
            elements.append(self._expression())
            while self.at(TT.COMMA):
                self.eat(TT.COMMA)
                self.skip_newlines()
                if self.at(TT.RBRACKET):
                    break
                elements.append(self._expression())
        self.skip_newlines()
        self.eat(TT.RBRACKET)
        return ListLit(elements)

    def _map_lit(self) -> MapLit:
        self.eat(TT.LBRACE)
        self.skip_newlines()
        pairs: list[tuple[str, Expr]] = []
        if not self.at(TT.RBRACE):
            key = self.eat(TT.IDENT).value
            self.eat(TT.COLON)
            val = self._expression()
            pairs.append((key, val))
            while self.at(TT.COMMA):
                self.eat(TT.COMMA)
                self.skip_newlines()
                if self.at(TT.RBRACE):
                    break
                key = self.eat(TT.IDENT).value
                self.eat(TT.COLON)
                val = self._expression()
                pairs.append((key, val))
        self.skip_newlines()
        self.eat(TT.RBRACE)
        return MapLit(pairs)


def parse(source: str) -> Program:
    """Parse T2Script source code into an AST Program."""
    tokens = tokenize(source)
    return Parser(tokens).parse()
