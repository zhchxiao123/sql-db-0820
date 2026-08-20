"""Minimal SQL expression engine for sql-db-0820.

Scope of this slice: parse and evaluate the simplest SELECT statements —
an expression list with no FROM clause — with sqlite-compatible value
semantics and NULL handling.

Supported language (subset of SQLite):
  * literals: INTEGER, REAL, TEXT ('...' / "..." with doubled-quote escape),
    NULL, TRUE, FALSE
  * arithmetic: + - * / % with parentheses, unary +/-
  * comparison: = == != <> < <= > >=
  * logical: AND OR NOT (three-valued logic)
  * IS / IS NOT (never yields NULL)
  * CASE WHEN ... THEN ... [ELSE ...] END (searched and simple forms)

Statements outside this scope (FROM, DDL, DML, ...) produce an EngineError
so that callers — in particular the sqllogictest runner — can judge the
record as failed instead of crashing.

Value model (mirrors SQLite's storage classes):
    None   -> SQL NULL
    int    -> INTEGER
    float  -> REAL
    str    -> TEXT
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


class EngineError(Exception):
    """Syntax error or unsupported statement."""


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def is_number(v: Any) -> bool:
    """True for INTEGER/REAL values (bool is excluded)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text_to_number(s: str) -> Optional[Any]:
    """Best-effort SQLite-style text -> number conversion."""
    if re.fullmatch(r"[+-]?\d+", s):
        return int(s)
    try:
        return float(s)
    except ValueError:
        return None


def format_real(v: float) -> str:
    """Render a REAL like the sqlite3 CLI: %.15g, always with '.' or 'e'."""
    s = "%.15g" % v
    if "e" not in s and "E" not in s and "." not in s:
        s += ".0"
    return s


def render_value(v: Any) -> str:
    """Text form of a value as sqllogictest expects it (NULL -> 'NULL')."""
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return format_real(v)
    return str(v)


def _compare(a: Any, b: Any) -> Optional[int]:
    """Three-way comparison following sqlite rules; None if either side NULL.

    - both numeric  -> numeric comparison
    - mixed numeric/text: if the text converts to a number, compare
      numerically; otherwise numbers sort before text
    - both text -> byte-wise string comparison
    """
    if a is None or b is None:
        return None
    if is_number(a) and is_number(b):
        return (a > b) - (a < b)
    if is_number(a) and isinstance(b, str):
        nb = _text_to_number(b)
        if nb is not None:
            return _compare(a, nb)
        return -1  # number < text
    if isinstance(a, str) and is_number(b):
        na = _text_to_number(a)
        if na is not None:
            return _compare(na, b)
        return 1  # text > number
    return (a > b) - (a < b)


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

KEYWORDS = {
    "AND", "OR", "NOT", "NULL", "IS",
    "CASE", "WHEN", "THEN", "ELSE", "END",
    "TRUE", "FALSE", "SELECT",
}

_NUM_RE = re.compile(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_OP2 = {"<=", ">=", "!=", "<>", "=="}
_OP1 = set("+-*/%()=,;<>")


class Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: Any, pos: int):
        self.kind = kind      # 'num' | 'str' | 'kw' | 'id' | 'op' | 'eof'
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind}, {self.value!r})"


def _scan_quoted(sql: str, i: int, quote: str) -> Tuple[str, int]:
    """Scan a quoted literal; doubled quote is an escaped quote."""
    buf: List[str] = []
    j = i + 1
    n = len(sql)
    while j < n:
        c = sql[j]
        if c == quote:
            if j + 1 < n and sql[j + 1] == quote:
                buf.append(quote)
                j += 2
                continue
            return "".join(buf), j + 1
        buf.append(c)
        j += 1
    raise EngineError("unterminated string literal")


def tokenize(sql: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                raise EngineError("unterminated /* comment")
            i = j + 2
            continue
        if c in ("'", '"'):
            text, i = _scan_quoted(sql, i, c)
            tokens.append(Token("str", text, i))
            continue
        if c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            m = _NUM_RE.match(sql, i)
            text = m.group(0)
            if any(ch in text for ch in ".eE"):
                tokens.append(Token("num", float(text), i))
            else:
                tokens.append(Token("num", int(text), i))
            i = m.end()
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            up = word.upper()
            tokens.append(Token("kw", up, i) if up in KEYWORDS else Token("id", word, i))
            i = j
            continue
        two = sql[i:i + 2]
        if two in _OP2:
            tokens.append(Token("op", two, i))
            i += 2
            continue
        if c in _OP1:
            tokens.append(Token("op", c, i))
            i += 1
            continue
        raise EngineError(f"syntax error near {c!r} at position {i}")
    tokens.append(Token("eof", "", n))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
# AST nodes are tuples; the first element is the node kind:
#   ('num', v) ('str', s) ('null',)
#   ('neg', e) ('add', l, r) ('sub', l, r) ('mul', l, r) ('div', l, r) ('mod', l, r)
#   ('cmp', op, l, r) ('is', l, r, negate)
#   ('and', l, r) ('or', l, r) ('not', e)
#   ('case', base|None, [(when, then), ...], else_|None)


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def _next(self) -> Token:
        t = self.tokens[self.pos]
        if t.kind != "eof":
            self.pos += 1
        return t

    def _expect_op(self, op: str) -> Token:
        t = self._next()
        if t.kind != "op" or t.value != op:
            raise EngineError(f"expected {op!r}, got {t.value!r}")
        return t

    def _expect_kw(self, kw: str) -> Token:
        t = self._next()
        if t.kind != "kw" or t.value != kw:
            raise EngineError(f"expected {kw}, got {t.value!r}")
        return t

    def parse_select(self) -> List[Tuple]:
        t = self.peek()
        if t.kind == "kw" and t.value == "SELECT":
            self._next()
        else:
            raise EngineError(f"expected SELECT, got {t.value!r}")
        exprs = [self.parse_expr()]
        while self.peek().kind == "op" and self.peek().value == ",":
            self._next()
            exprs.append(self.parse_expr())
        while self.peek().kind == "op" and self.peek().value == ";":
            self._next()
        if self.peek().kind != "eof":
            raise EngineError(f"unexpected token {self.peek().value!r} after expression list")
        return exprs

    # precedence climbing: or < and < not < comparison < additive < multiplicative < unary < primary
    def parse_expr(self) -> Tuple:
        return self.parse_or()

    def parse_or(self) -> Tuple:
        left = self.parse_and()
        while self.peek().kind == "kw" and self.peek().value == "OR":
            self._next()
            right = self.parse_and()
            left = ("or", left, right)
        return left

    def parse_and(self) -> Tuple:
        left = self.parse_not()
        while self.peek().kind == "kw" and self.peek().value == "AND":
            self._next()
            right = self.parse_not()
            left = ("and", left, right)
        return left

    def parse_not(self) -> Tuple:
        if self.peek().kind == "kw" and self.peek().value == "NOT":
            self._next()
            return ("not", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Tuple:
        left = self.parse_additive()
        while True:
            t = self.peek()
            if t.kind == "op" and t.value in ("=", "==", "!=", "<>", "<", "<=", ">", ">="):
                self._next()
                right = self.parse_additive()
                left = ("cmp", t.value, left, right)
            elif t.kind == "kw" and t.value == "IS":
                self._next()
                negate = False
                if self.peek().kind == "kw" and self.peek().value == "NOT":
                    self._next()
                    negate = True
                right = self.parse_additive()
                left = ("is", left, right, negate)
            else:
                break
        return left

    def parse_additive(self) -> Tuple:
        left = self.parse_multiplicative()
        while self.peek().kind == "op" and self.peek().value in ("+", "-"):
            op = self._next().value
            right = self.parse_multiplicative()
            left = ("add" if op == "+" else "sub", left, right)
        return left

    def parse_multiplicative(self) -> Tuple:
        left = self.parse_unary()
        while self.peek().kind == "op" and self.peek().value in ("*", "/", "%"):
            op = self._next().value
            right = self.parse_unary()
            left = ({"*": "mul", "/": "div", "%": "mod"}[op], left, right)
        return left

    def parse_unary(self) -> Tuple:
        t = self.peek()
        if t.kind == "op" and t.value == "-":
            self._next()
            return ("neg", self.parse_unary())
        if t.kind == "op" and t.value == "+":
            self._next()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> Tuple:
        t = self.peek()
        if t.kind == "num":
            self._next()
            return ("num", t.value)
        if t.kind == "str":
            self._next()
            return ("str", t.value)
        if t.kind == "kw":
            if t.value == "NULL":
                self._next()
                return ("null",)
            if t.value == "TRUE":
                self._next()
                return ("num", 1)
            if t.value == "FALSE":
                self._next()
                return ("num", 0)
            if t.value == "CASE":
                return self.parse_case()
            raise EngineError(f"unexpected keyword {t.value}")
        if t.kind == "op" and t.value == "(":
            self._next()
            e = self.parse_expr()
            self._expect_op(")")
            return e
        if t.kind == "id":
            raise EngineError(f"unknown column or identifier {t.value!r} (no FROM clause)")
        raise EngineError(f"unexpected token {t.value!r}")

    def parse_case(self) -> Tuple:
        self._expect_kw("CASE")
        base = None
        t = self.peek()
        if not (t.kind == "kw" and t.value == "WHEN"):
            base = self.parse_expr()
        whens: List[Tuple[Tuple, Tuple]] = []
        while self.peek().kind == "kw" and self.peek().value == "WHEN":
            self._next()
            cond = self.parse_expr()
            self._expect_kw("THEN")
            val = self.parse_expr()
            whens.append((cond, val))
        if not whens:
            raise EngineError("CASE requires at least one WHEN")
        else_ = None
        if self.peek().kind == "kw" and self.peek().value == "ELSE":
            self._next()
            else_ = self.parse_expr()
        self._expect_kw("END")
        return ("case", base, whens, else_)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _arith(op: str, a: Any, b: Any) -> Any:
    """Arithmetic with sqlite semantics: NULL propagates, int/int division
    truncates toward zero, division/modulo by zero yields NULL."""
    if a is None or b is None:
        return None
    if isinstance(a, float) or isinstance(b, float):
        af = float(a)
        bf = float(b)
        if op == "add":
            return af + bf
        if op == "sub":
            return af - bf
        if op == "mul":
            return af * bf
        if op == "div":
            return None if bf == 0 else af / bf
        if op == "mod":
            return None if bf == 0 else math.fmod(af, bf)
    # both INTEGER
    if op == "add":
        return a + b
    if op == "sub":
        return a - b
    if op == "mul":
        return a * b
    if op == "div":
        if b == 0:
            return None
        q = abs(a) // abs(b)
        return q if (a >= 0) == (b >= 0) else -q
    if op == "mod":
        if b == 0:
            return None
        q = abs(a) // abs(b)
        q = q if (a >= 0) == (b >= 0) else -q
        return a - q * b
    raise EngineError(f"unknown arithmetic operator {op!r}")  # pragma: no cover


def _cmp(op: str, a: Any, b: Any) -> Any:
    c = _compare(a, b)
    if c is None:
        return None
    if op in ("=", "=="):
        return 1 if c == 0 else 0
    if op in ("!=", "<>"):
        return 1 if c != 0 else 0
    if op == "<":
        return 1 if c < 0 else 0
    if op == "<=":
        return 1 if c <= 0 else 0
    if op == ">":
        return 1 if c > 0 else 0
    if op == ">=":
        return 1 if c >= 0 else 0
    raise EngineError(f"unknown comparison operator {op!r}")  # pragma: no cover


def _is_op(a: Any, b: Any, negate: bool) -> int:
    """IS / IS NOT: never NULL; NULL IS NULL is true."""
    if a is None and b is None:
        r = True
    elif a is None or b is None:
        r = False
    else:
        r = _compare(a, b) == 0
    return 1 if (r != negate) else 0


def _and(a: Any, b: Any) -> Any:
    if a == 0 or b == 0:
        return 0
    if a is None or b is None:
        return None
    return 1


def _or(a: Any, b: Any) -> Any:
    if a not in (None, 0) or b not in (None, 0):
        return 1
    if a is None or b is None:
        return None
    return 0


def _not(a: Any) -> Any:
    if a is None:
        return None
    return 0 if a != 0 else 1


def eval_expr(node: Tuple) -> Any:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "str":
        return node[1]
    if kind == "null":
        return None
    if kind == "neg":
        v = eval_expr(node[1])
        return None if v is None else -v
    if kind == "add":
        return _arith("add", eval_expr(node[1]), eval_expr(node[2]))
    if kind == "sub":
        return _arith("sub", eval_expr(node[1]), eval_expr(node[2]))
    if kind == "mul":
        return _arith("mul", eval_expr(node[1]), eval_expr(node[2]))
    if kind == "div":
        return _arith("div", eval_expr(node[1]), eval_expr(node[2]))
    if kind == "mod":
        return _arith("mod", eval_expr(node[1]), eval_expr(node[2]))
    if kind == "cmp":
        return _cmp(node[1], eval_expr(node[2]), eval_expr(node[3]))
    if kind == "is":
        return _is_op(eval_expr(node[1]), eval_expr(node[2]), node[3])
    if kind == "and":
        return _and(eval_expr(node[1]), eval_expr(node[2]))
    if kind == "or":
        return _or(eval_expr(node[1]), eval_expr(node[2]))
    if kind == "not":
        return _not(eval_expr(node[1]))
    if kind == "case":
        return _case(node[1], node[2], node[3])
    raise EngineError(f"unknown AST node {kind!r}")  # pragma: no cover


def _case(base: Optional[Tuple], whens: List[Tuple[Tuple, Tuple]], else_: Optional[Tuple]) -> Any:
    base_v = eval_expr(base) if base is not None else None
    for cond, val in whens:
        if base is None:
            c = eval_expr(cond)
            if c is not None and c != 0:
                return eval_expr(val)
        else:
            w = eval_expr(cond)
            # simple CASE matches with = semantics: NULL never matches
            if base_v is not None and w is not None and _compare(base_v, w) == 0:
                return eval_expr(val)
    if else_ is not None:
        return eval_expr(else_)
    return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class StatementResult:
    """Outcome of executing one SQL statement.

    error:    None on success, otherwise a human-readable message.
    rows:     List of rows (each row a list of raw values) for SELECT;
              None when the statement failed or returned no rows.
    rowcount: number of result rows (0 for failed statements).
    """

    error: Optional[str] = None
    rows: Optional[List[List[Any]]] = None
    rowcount: int = 0


class Engine:
    """Minimal sqlite-compatible expression engine."""

    def execute(self, sql: str) -> StatementResult:
        sql = sql.strip()
        if not sql:
            return StatementResult(error="empty statement")
        try:
            tokens = tokenize(sql)
            exprs = Parser(tokens).parse_select()
            values = [eval_expr(e) for e in exprs]
        except EngineError as e:
            return StatementResult(error=str(e))
        return StatementResult(rows=[values], rowcount=1)
