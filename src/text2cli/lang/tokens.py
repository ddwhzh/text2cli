"""T2Script lexer: tokenizes source into a stream of typed tokens."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterator


class TT(Enum):
    """Token types."""
    STRING = auto()
    NUMBER = auto()
    IDENT = auto()
    DOTTED = auto()      # fs.read, ws.commit, http.get

    LET = auto()
    IF = auto()
    ELIF = auto()
    ELSE = auto()
    FOR = auto()
    IN = auto()
    FN = auto()
    RETURN = auto()
    TRY = auto()
    CATCH = auto()
    IMPORT = auto()
    TRUE = auto()
    FALSE = auto()
    NULL = auto()
    AND = auto()
    OR = auto()
    NOT = auto()

    PIPE = auto()        # |
    ASSIGN = auto()      # =
    DOLLAR = auto()      # $
    LBRACE = auto()      # {
    RBRACE = auto()      # }
    LBRACKET = auto()    # [
    RBRACKET = auto()    # ]
    LPAREN = auto()      # (
    RPAREN = auto()      # )
    COMMA = auto()       # ,
    COLON = auto()       # :

    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    EQ = auto()          # ==
    NE = auto()          # !=
    LT = auto()          # <
    GT = auto()          # >
    LE = auto()          # <=
    GE = auto()          # >=

    NEWLINE = auto()
    EOF = auto()


KEYWORDS: dict[str, TT] = {
    "let": TT.LET, "if": TT.IF, "elif": TT.ELIF, "else": TT.ELSE,
    "for": TT.FOR, "in": TT.IN, "fn": TT.FN, "return": TT.RETURN,
    "try": TT.TRY, "catch": TT.CATCH, "import": TT.IMPORT,
    "true": TT.TRUE, "false": TT.FALSE, "null": TT.NULL,
    "and": TT.AND, "or": TT.OR, "not": TT.NOT,
}


@dataclass
class Token:
    type: TT
    value: str
    line: int
    col: int

    def __repr__(self) -> str:
        return f"Token({self.type.name}, {self.value!r}, L{self.line})"


class LexError(Exception):
    def __init__(self, msg: str, line: int, col: int) -> None:
        super().__init__(f"Lex error at L{line}:{col}: {msg}")
        self.line = line
        self.col = col


def tokenize(source: str) -> list[Token]:
    """Tokenize T2Script source into a list of Tokens."""
    return list(_lex(source))


def _lex(source: str) -> Iterator[Token]:
    i = 0
    line = 1
    col = 1
    length = len(source)

    def peek(offset: int = 0) -> str:
        p = i + offset
        return source[p] if p < length else ""

    def advance() -> str:
        nonlocal i, col
        ch = source[i]
        i += 1
        col += 1
        return ch

    while i < length:
        ch = source[i]

        if ch == "\n":
            yield Token(TT.NEWLINE, "\\n", line, col)
            i += 1
            line += 1
            col = 1
            continue

        if ch in " \t\r":
            i += 1
            col += 1
            continue

        if ch == "#":
            while i < length and source[i] != "\n":
                i += 1
                col += 1
            continue

        if ch == '"':
            tok, new_pos = _lex_string(source, i, line, col, length)
            col += new_pos - i
            i = new_pos
            yield tok
            continue

        start_col = col
        if ch.isdigit() or (ch == "-" and peek(1).isdigit()):
            start = i
            if ch == "-":
                advance()
            while i < length and (source[i].isdigit() or source[i] == "."):
                advance()
            yield Token(TT.NUMBER, source[start:i], line, start_col)
            continue

        if ch == "_" or ch.isalpha():
            start = i
            while i < length and (source[i].isalnum() or source[i] == "_"):
                advance()
            word = source[start:i]
            if i < length and source[i] == "." and (i + 1 < length) and source[i + 1].isalpha():
                advance()  # skip dot
                while i < length and (source[i].isalnum() or source[i] == "_"):
                    advance()
                yield Token(TT.DOTTED, source[start:i], line, start_col)
            elif word in KEYWORDS:
                yield Token(KEYWORDS[word], word, line, start_col)
            else:
                yield Token(TT.IDENT, word, line, start_col)
            continue

        if ch == "|":
            yield Token(TT.PIPE, "|", line, col)
            advance()
            continue
        if ch == "$":
            yield Token(TT.DOLLAR, "$", line, col)
            advance()
            continue
        if ch == "{":
            yield Token(TT.LBRACE, "{", line, col)
            advance()
            continue
        if ch == "}":
            yield Token(TT.RBRACE, "}", line, col)
            advance()
            continue
        if ch == "[":
            yield Token(TT.LBRACKET, "[", line, col)
            advance()
            continue
        if ch == "]":
            yield Token(TT.RBRACKET, "]", line, col)
            advance()
            continue
        if ch == "(":
            yield Token(TT.LPAREN, "(", line, col)
            advance()
            continue
        if ch == ")":
            yield Token(TT.RPAREN, ")", line, col)
            advance()
            continue
        if ch == ",":
            yield Token(TT.COMMA, ",", line, col)
            advance()
            continue
        if ch == ":":
            yield Token(TT.COLON, ":", line, col)
            advance()
            continue
        if ch == "+":
            yield Token(TT.PLUS, "+", line, col)
            advance()
            continue
        if ch == "-":
            yield Token(TT.MINUS, "-", line, col)
            advance()
            continue
        if ch == "*":
            yield Token(TT.STAR, "*", line, col)
            advance()
            continue
        if ch == "/":
            yield Token(TT.SLASH, "/", line, col)
            advance()
            continue
        if ch == "=":
            if peek(1) == "=":
                yield Token(TT.EQ, "==", line, col)
                advance(); advance()
            else:
                yield Token(TT.ASSIGN, "=", line, col)
                advance()
            continue
        if ch == "!":
            if peek(1) == "=":
                yield Token(TT.NE, "!=", line, col)
                advance(); advance()
            else:
                raise LexError(f"Unexpected '!'", line, col)
            continue
        if ch == "<":
            if peek(1) == "=":
                yield Token(TT.LE, "<=", line, col)
                advance(); advance()
            else:
                yield Token(TT.LT, "<", line, col)
                advance()
            continue
        if ch == ">":
            if peek(1) == "=":
                yield Token(TT.GE, ">=", line, col)
                advance(); advance()
            else:
                yield Token(TT.GT, ">", line, col)
                advance()
            continue

        raise LexError(f"Unexpected character '{ch}'", line, col)

    yield Token(TT.EOF, "", line, col)


def _lex_string(source: str, start: int, line: int, col: int, length: int) -> tuple[Token, int]:
    """Lex a double-quoted string with escape sequences and $interpolation.

    Returns (Token, new_position) where new_position is one past the closing quote.
    """
    i = start + 1
    parts: list[str] = []
    buf: list[str] = []

    while i < length:
        ch = source[i]
        if ch == "\\":
            i += 1
            if i >= length:
                raise LexError("Unterminated escape", line, col)
            esc = source[i]
            esc_map = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "$": "$"}
            buf.append(esc_map.get(esc, "\\" + esc))
            i += 1
        elif ch == '"':
            parts.append("".join(buf))
            raw = "".join(parts)
            return Token(TT.STRING, raw, line, col), i + 1
        elif ch == "$":
            i += 1
            if i < length and (source[i].isalpha() or source[i] == "_"):
                name_start = i
                while i < length and (source[i].isalnum() or source[i] == "_"):
                    i += 1
                var_name = source[name_start:i]
                parts.append("".join(buf))
                buf.clear()
                parts.append(f"${{{var_name}}}")
            else:
                buf.append("$")
        else:
            buf.append(ch)
            i += 1

    raise LexError("Unterminated string", line, col)
