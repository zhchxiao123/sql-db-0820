"""SQL engine for sql-db-0820: expression evaluation plus single-table storage.

Built on the expression engine from slice 0. This slice adds:

  * CREATE TABLE (column names + declared types -> sqlite type affinity)
  * INSERT (VALUES with optional column list, multi-row)
  * DELETE (with or without WHERE)
  * SELECT with FROM and WHERE; column references in expressions; ``*``
  * sqlite type affinity: storage conversion and comparison rules
  * LIKE / NOT LIKE (ASCII case-insensitive, % _ wildcards, ESCAPE)

Value model (mirrors SQLite storage classes):
    None   -> SQL NULL
    int    -> INTEGER
    float  -> REAL
    str    -> TEXT

Statements outside this scope produce an EngineError so the sqllogictest
runner can judge the record as failed instead of crashing.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Affinity model
# ---------------------------------------------------------------------------
# SQLite type affinity, determined from the declared column type:
#   contains INT            -> INTEGER
#   contains CHAR/CLOB/TEXT -> TEXT
#   contains BLOB or empty  -> NONE (BLOB)
#   contains REAL/FLOA/DOUB -> REAL
#   otherwise               -> NUMERIC

_NUMERIC_AFFINITIES = ("INTEGER", "REAL", "NUMERIC")


def affinity_of_type(declared: Optional[str]) -> str:
    if not declared:
        return "NONE"
    t = declared.upper()
    if "INT" in t:
        return "INTEGER"
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return "TEXT"
    if "BLOB" in t or t == "":
        return "NONE"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "REAL"
    return "NUMERIC"


class EngineError(Exception):
    """Syntax error or unsupported statement."""


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def is_number(v: Any) -> bool:
    """True for INTEGER/REAL values (bool is excluded)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text_to_number(s: str) -> Optional[Any]:
    """SQLite-style text -> number conversion (integer, else real)."""
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


def _convert_numeric(v: Any) -> Any:
    """Apply NUMERIC affinity to a value: text -> number when possible."""
    if isinstance(v, str):
        n = _text_to_number(v)
        return v if n is None else n
    return v


def _convert_text(v: Any) -> Any:
    """Apply TEXT affinity to a value: number -> its text form."""
    if is_number(v):
        return render_value(v)
    return v


def _convert_to_affinity(v: Any, affinity: str) -> Any:
    """Storage conversion applied when inserting into a column."""
    if v is None:
        return None
    if affinity == "TEXT":
        if is_number(v):
            return render_value(v)
        return v
    if affinity in ("INTEGER", "NUMERIC"):
        return _convert_numeric(v)
    if affinity == "REAL":
        if isinstance(v, str):
            n = _text_to_number(v)
            if n is not None:
                return float(n)
            return v
        if isinstance(v, int):
            return float(v)
        return v
    return v  # NONE (BLOB): store as-is


def _binary_compare(a: Any, b: Any) -> Optional[int]:
    """Three-way comparison with NO affinity applied (sqlite BINARY order).

    Returns None if either side is NULL. Ordering: numbers compare
    numerically; numbers sort before text; text compares byte-wise.
    """
    if a is None or b is None:
        return None
    an, bn = is_number(a), is_number(b)
    if an and bn:
        return (a > b) - (a < b)
    if an and isinstance(b, str):
        return -1  # number < text
    if isinstance(a, str) and bn:
        return 1  # text > number
    return (a > b) - (a < b)


def apply_comparison_affinity(la: str, lv: Any, ra: str, rv: Any) -> Tuple[Any, Any]:
    """Apply sqlite's affinity rules for a comparison between two operands.

    Rule 1: an INTEGER/REAL/NUMERIC affinity operand converts the *other*
            operand with NUMERIC affinity (text -> number when possible).
    Rule 2: a TEXT affinity operand converts a no-affinity operand with
            TEXT affinity (number -> text).
    """
    if la in _NUMERIC_AFFINITIES and ra not in _NUMERIC_AFFINITIES:
        return lv, _convert_numeric(rv)
    if ra in _NUMERIC_AFFINITIES and la not in _NUMERIC_AFFINITIES:
        return _convert_numeric(lv), rv
    if la == "TEXT" and ra == "NONE":
        return lv, _convert_text(rv)
    if ra == "TEXT" and la == "NONE":
        return _convert_text(lv), rv
    return lv, rv


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

KEYWORDS = {
    "AND", "OR", "NOT", "NULL", "IS",
    "CASE", "WHEN", "THEN", "ELSE", "END",
    "TRUE", "FALSE", "SELECT",
    "CREATE", "TABLE", "INSERT", "INTO", "VALUES", "DELETE",
    "FROM", "WHERE", "LIKE", "ESCAPE",
    "DISTINCT", "ORDER", "BY", "ASC", "DESC", "LIMIT", "OFFSET",
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
# Tables
# ---------------------------------------------------------------------------

@dataclass
class ColumnDef:
    name: str
    affinity: str


@dataclass
class Table:
    name: str
    columns: List[ColumnDef]
    rows: List[List[Any]] = field(default_factory=list)


class RowContext:
    """Binds a table row to column names for expression evaluation."""

    __slots__ = ("affinity", "values")

    def __init__(self, table: Table, row: List[Any]):
        self.affinity = {c.name: c.affinity for c in table.columns}
        self.values = {c.name: v for c, v in zip(table.columns, row)}

    def value_of(self, name: str) -> Any:
        if name not in self.values:
            raise EngineError(f"no such column: {name}")
        return self.values[name]

    def affinity_of(self, name: str) -> str:
        return self.affinity.get(name, "NONE")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
# Expression AST nodes (first element is the node kind):
#   ('num', v) ('str', s) ('null',) ('col', name)
#   ('neg', e) ('add', l, r) ('sub', l, r) ('mul', l, r) ('div', l, r) ('mod', l, r)
#   ('cmp', op, l, r) ('is', l, r, negate) ('like', l, r, negate, esc)
#   ('and', l, r) ('or', l, r) ('not', e)
#   ('case', base|None, [(when, then), ...], else_|None)
#   ('star',)  -- SELECT * item


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0
        self.allow_columns = False

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def peek2(self) -> Token:
        nxt = self.pos + 1
        return self.tokens[nxt] if nxt < len(self.tokens) else self.tokens[-1]

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

    def _name(self) -> str:
        """Accept an identifier or keyword as a table/column name."""
        t = self._next()
        if t.kind not in ("id", "kw"):
            raise EngineError(f"expected identifier, got {t.value!r}")
        return t.value.lower()

    def _finish(self) -> None:
        while self.peek().kind == "op" and self.peek().value == ";":
            self._next()
        if self.peek().kind != "eof":
            raise EngineError(f"unexpected token {self.peek().value!r}")

    # -- statements ---------------------------------------------------------

    def parse_statement(self) -> Tuple:
        t = self.peek()
        if t.kind == "kw":
            if t.value == "SELECT":
                return self.parse_select_stmt()
            if t.value == "CREATE":
                return self.parse_create_stmt()
            if t.value == "INSERT":
                return self.parse_insert_stmt()
            if t.value == "DELETE":
                return self.parse_delete_stmt()
        raise EngineError(f"expected SELECT, CREATE, INSERT or DELETE, got {t.value!r}")

    def _has_from(self) -> bool:
        """True if a depth-0 FROM keyword follows (no subqueries, so a top-level
        FROM can only be the SELECT's FROM clause)."""
        depth = 0
        for t in self.tokens[self.pos:]:
            if t.kind == "op" and t.value == "(":
                depth += 1
            elif t.kind == "op" and t.value == ")":
                depth -= 1
            elif depth == 0 and t.kind == "kw" and t.value == "FROM":
                return True
        return False

    def parse_select_stmt(self) -> Tuple:
        self._expect_kw("SELECT")
        distinct = False
        if self.peek().kind == "kw" and self.peek().value == "DISTINCT":
            self._next()
            distinct = True
        # column references are only legal when a FROM clause is present; the
        # select list is parsed before FROM, so scan ahead to find it.
        self.allow_columns = self._has_from()
        items: List[Tuple] = []
        while True:
            if self.peek().kind == "op" and self.peek().value == "*":
                self._next()
                items.append(("star",))
            else:
                items.append(self.parse_expr())
            if self.peek().kind == "op" and self.peek().value == ",":
                self._next()
                continue
            break
        table_name: Optional[str] = None
        where: Optional[Tuple] = None
        if self.peek().kind == "kw" and self.peek().value == "FROM":
            self._next()
            table_name = self._name()
            self.allow_columns = True
            if self.peek().kind == "kw" and self.peek().value == "WHERE":
                self._next()
                where = self.parse_expr()
        order_by: Optional[List[Tuple[Tuple, bool]]] = None
        if self.peek().kind == "kw" and self.peek().value == "ORDER":
            self._next()
            self._expect_kw("BY")
            order_by = []
            while True:
                term = self.parse_expr()
                asc = True
                if self.peek().kind == "kw" and self.peek().value == "ASC":
                    self._next()
                elif self.peek().kind == "kw" and self.peek().value == "DESC":
                    self._next()
                    asc = False
                order_by.append((term, asc))
                if self.peek().kind == "op" and self.peek().value == ",":
                    self._next()
                    continue
                break
        limit: Optional[Tuple[Optional[Tuple], Optional[Tuple]]] = None
        if self.peek().kind == "kw" and self.peek().value == "LIMIT":
            self._next()
            e1 = self.parse_expr()
            if self.peek().kind == "op" and self.peek().value == ",":
                # comma form: LIMIT <offset>, <count>
                self._next()
                limit = (self.parse_expr(), e1)
            elif self.peek().kind == "kw" and self.peek().value == "OFFSET":
                self._next()
                limit = (e1, self.parse_expr())
            else:
                limit = (e1, None)
        self._finish()
        return ("select", distinct, items, table_name, where, order_by, limit)

    def parse_create_stmt(self) -> Tuple:
        self._expect_kw("CREATE")
        self._expect_kw("TABLE")
        name = self._name()
        self._expect_op("(")
        columns: List[ColumnDef] = []
        while True:
            colname = self._name()
            # consume the declared type / constraints up to a top-level ',' or ')'
            depth = 0
            first_type: Optional[str] = None
            while True:
                t = self.peek()
                if t.kind == "eof":
                    raise EngineError("unterminated CREATE TABLE column list")
                if t.kind == "op" and t.value == "(":
                    depth += 1
                    self._next()
                    continue
                if t.kind == "op" and t.value == ")":
                    if depth == 0:
                        break
                    depth -= 1
                    self._next()
                    continue
                if t.kind == "op" and t.value == "," and depth == 0:
                    break
                if first_type is None and t.kind in ("id", "kw"):
                    first_type = t.value
                self._next()
            columns.append(ColumnDef(name=colname, affinity=affinity_of_type(first_type)))
            t = self.peek()
            if t.kind == "op" and t.value == ",":
                self._next()
                continue
            if t.kind == "op" and t.value == ")":
                self._next()
                break
            raise EngineError(f"expected ',' or ')', got {t.value!r}")
        self._finish()
        return ("create", name, columns)

    def parse_insert_stmt(self) -> Tuple:
        self._expect_kw("INSERT")
        self._expect_kw("INTO")
        name = self._name()
        columns: Optional[List[str]] = None
        if self.peek().kind == "op" and self.peek().value == "(":
            self._next()
            columns = []
            while True:
                columns.append(self._name())
                t = self._next()
                if t.kind == "op" and t.value == ",":
                    continue
                if t.kind == "op" and t.value == ")":
                    break
                raise EngineError(f"expected ',' or ')', got {t.value!r}")
        self._expect_kw("VALUES")
        rows: List[List[Tuple]] = []
        while True:
            self._expect_op("(")
            exprs: List[Tuple] = []
            while True:
                exprs.append(self.parse_expr())
                t = self._next()
                if t.kind == "op" and t.value == ",":
                    continue
                if t.kind == "op" and t.value == ")":
                    break
                raise EngineError(f"expected ',' or ')', got {t.value!r}")
            rows.append(exprs)
            if self.peek().kind == "op" and self.peek().value == ",":
                self._next()
                continue
            break
        self._finish()
        return ("insert", name, columns, rows)

    def parse_delete_stmt(self) -> Tuple:
        self._expect_kw("DELETE")
        self._expect_kw("FROM")
        name = self._name()
        where: Optional[Tuple] = None
        self.allow_columns = True
        if self.peek().kind == "kw" and self.peek().value == "WHERE":
            self._next()
            where = self.parse_expr()
        self._finish()
        return ("delete", name, where)

    # -- expressions --------------------------------------------------------

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
            elif t.kind == "kw" and t.value == "LIKE":
                self._next()
                right = self.parse_additive()
                esc = None
                if self.peek().kind == "kw" and self.peek().value == "ESCAPE":
                    self._next()
                    esc = self.parse_additive()
                left = ("like", left, right, False, esc)
            elif t.kind == "kw" and t.value == "NOT" and self.peek2().kind == "kw" and self.peek2().value == "LIKE":
                self._next()
                self._next()
                right = self.parse_additive()
                esc = None
                if self.peek().kind == "kw" and self.peek().value == "ESCAPE":
                    self._next()
                    esc = self.parse_additive()
                left = ("like", left, right, True, esc)
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
            self._next()
            if not self.allow_columns:
                raise EngineError(f"unknown column or identifier {t.value!r} (no FROM clause)")
            return ("col", t.value.lower())
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

def expr_affinity(node: Tuple, ctx: Optional[RowContext]) -> str:
    """Affinity of an expression: only bare column refs carry one."""
    if node[0] == "col" and ctx is not None:
        return ctx.affinity_of(node[1])
    return "NONE"


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


def _cmp_result(op: str, c: int) -> int:
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


def _cmp_affinity(op: str, lnode: Tuple, rnode: Tuple, ctx: Optional[RowContext]) -> Any:
    """Comparison with sqlite affinity rules applied."""
    lv = eval_expr(lnode, ctx)
    rv = eval_expr(rnode, ctx)
    if lv is None or rv is None:
        return None
    la = expr_affinity(lnode, ctx)
    ra = expr_affinity(rnode, ctx)
    lv, rv = apply_comparison_affinity(la, lv, ra, rv)
    c = _binary_compare(lv, rv)
    return _cmp_result(op, c)


def _is_affinity(lnode: Tuple, rnode: Tuple, negate: bool, ctx: Optional[RowContext]) -> int:
    """IS / IS NOT: never NULL; NULL IS NULL is true. Affinity-aware equality."""
    lv = eval_expr(lnode, ctx)
    rv = eval_expr(rnode, ctx)
    if lv is None and rv is None:
        r = True
    elif lv is None or rv is None:
        r = False
    else:
        la = expr_affinity(lnode, ctx)
        ra = expr_affinity(rnode, ctx)
        lv, rv = apply_comparison_affinity(la, lv, ra, rv)
        r = _binary_compare(lv, rv) == 0
    return 0 if (r == negate) else 1


def _ascii_lower(s: str) -> str:
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def _like_to_regex(pattern: str, esc: Optional[str]) -> str:
    out: List[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if esc is not None and ch == esc:
            if i + 1 < n:
                out.append(re.escape(pattern[i + 1]))
                i += 2
                continue
            out.append(re.escape(esc))
            i += 1
            continue
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
        i += 1
    return "^" + "".join(out) + "$"


def _like(lnode: Tuple, rnode: Tuple, negate: bool, esc_node: Optional[Tuple],
          ctx: Optional[RowContext]) -> Any:
    lv = eval_expr(lnode, ctx)
    rv = eval_expr(rnode, ctx)
    if lv is None or rv is None:
        return None
    ev = eval_expr(esc_node, ctx) if esc_node is not None else None
    if ev is not None and len(render_value(ev)) != 1:
        raise EngineError("ESCAPE expression must be a single character")
    text = _ascii_lower(render_value(lv))
    pattern = _ascii_lower(render_value(rv))
    regex = _like_to_regex(pattern, None if ev is None else render_value(ev))
    matched = re.fullmatch(regex, text) is not None
    r = 1 if matched else 0
    return 0 if (r == negate) else 1


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


def eval_expr(node: Tuple, ctx: Optional[RowContext] = None) -> Any:
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "str":
        return node[1]
    if kind == "null":
        return None
    if kind == "col":
        if ctx is None:
            raise EngineError(f"no such column: {node[1]}")
        return ctx.value_of(node[1])
    if kind == "neg":
        v = eval_expr(node[1], ctx)
        return None if v is None else -v
    if kind == "add":
        return _arith("add", eval_expr(node[1], ctx), eval_expr(node[2], ctx))
    if kind == "sub":
        return _arith("sub", eval_expr(node[1], ctx), eval_expr(node[2], ctx))
    if kind == "mul":
        return _arith("mul", eval_expr(node[1], ctx), eval_expr(node[2], ctx))
    if kind == "div":
        return _arith("div", eval_expr(node[1], ctx), eval_expr(node[2], ctx))
    if kind == "mod":
        return _arith("mod", eval_expr(node[1], ctx), eval_expr(node[2], ctx))
    if kind == "cmp":
        return _cmp_affinity(node[1], node[2], node[3], ctx)
    if kind == "is":
        return _is_affinity(node[1], node[2], node[3], ctx)
    if kind == "like":
        return _like(node[1], node[2], node[3], node[4], ctx)
    if kind == "and":
        return _and(eval_expr(node[1], ctx), eval_expr(node[2], ctx))
    if kind == "or":
        return _or(eval_expr(node[1], ctx), eval_expr(node[2], ctx))
    if kind == "not":
        return _not(eval_expr(node[1], ctx))
    if kind == "case":
        return _case(node[1], node[2], node[3], ctx)
    raise EngineError(f"unknown AST node {kind!r}")  # pragma: no cover


def _case(base: Optional[Tuple], whens: List[Tuple[Tuple, Tuple]], else_: Optional[Tuple],
          ctx: Optional[RowContext]) -> Any:
    base_v = eval_expr(base, ctx) if base is not None else None
    for cond, val in whens:
        if base is None:
            c = eval_expr(cond, ctx)
            if c is not None and c != 0:
                return eval_expr(val, ctx)
        else:
            # simple CASE matches with = semantics (affinity-aware), NULL never matches
            w = eval_expr(cond, ctx)
            if base_v is not None and w is not None:
                la = expr_affinity(base, ctx)
                ra = expr_affinity(cond, ctx)
                b2, w2 = apply_comparison_affinity(la, base_v, ra, w)
                if _binary_compare(b2, w2) == 0:
                    return eval_expr(val, ctx)
    if else_ is not None:
        return eval_expr(else_, ctx)
    return None


def _truthy(v: Any) -> bool:
    return v is not None and v != 0


def _walk_expr_cols(node: Any):
    """Yield column names referenced by an expression AST."""
    if isinstance(node, tuple) and node:
        if node[0] == "col":
            yield node[1]
        for child in node[1:]:
            yield from _walk_expr_cols(child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_expr_cols(item)


def _output_width(items: List[Tuple], table: Optional[Table]) -> int:
    """Number of output columns of a select list (for ORDER BY ordinals)."""
    width = 0
    for item in items:
        if item[0] == "star":
            width += len(table.columns)
        else:
            width += 1
    return width


def _ordinal_suffix(i: int) -> str:
    if 10 <= i % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(i % 10, "th")


def _order_sort_key(term: Tuple, p: Tuple[List[Any], Optional[RowContext]]) -> Tuple:
    """Sort key for one ORDER BY term: (class, value) with sqlite ordering
    (class 0 = NULL smallest, 1 = numbers, 2 = text)."""
    out, ctx = p
    if term[0] == "num" and isinstance(term[1], int):
        v = out[term[1] - 1]
    else:
        v = eval_expr(term, ctx)
    if v is None:
        return (0, None)
    if is_number(v):
        return (1, v)
    return (2, v)


def _limit_int(v: Any) -> int:
    """sqlite LIMIT/OFFSET value: integer or integer-looking text, else error."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, str):
        n = _text_to_number(v)
        if n is not None and isinstance(n, int):
            return n
    raise EngineError("datatype mismatch")


def _apply_limit(proj: List[Tuple[List[Any], Optional[RowContext]]],
                 limit: Tuple[Optional[Tuple], Optional[Tuple]]) -> List[Tuple[List[Any], Optional[RowContext]]]:
    limit_node, offset_node = limit
    n: Optional[int] = None
    if limit_node is not None:
        n = _limit_int(eval_expr(limit_node, None))
    m = 0
    if offset_node is not None:
        m = _limit_int(eval_expr(offset_node, None))
    if n is not None and n < 0:
        n = None  # negative LIMIT = no limit
    if m < 0:
        m = 0  # negative OFFSET = 0
    if n is None:
        return proj[m:]
    return proj[m:m + n]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

@dataclass
class StatementResult:
    """Outcome of executing one SQL statement.

    error:    None on success, otherwise a human-readable message.
    rows:     List of rows (each row a list of raw values) for SELECT;
              None for statements that do not return rows.
    rowcount: number of affected rows (INSERT/DELETE) or result rows
              (SELECT); 0 for CREATE and failed statements.
    """

    error: Optional[str] = None
    rows: Optional[List[List[Any]]] = None
    rowcount: int = 0


class Engine:
    """In-memory single-table SQL engine with sqlite-compatible semantics."""

    def __init__(self):
        self.tables: Dict[str, Table] = {}

    def execute(self, sql: str) -> StatementResult:
        sql = sql.strip()
        if not sql:
            return StatementResult(error="empty statement")
        try:
            tokens = tokenize(sql)
            stmt = Parser(tokens).parse_statement()
            return self._run(stmt)
        except EngineError as e:
            return StatementResult(error=str(e))

    def _run(self, stmt: Tuple) -> StatementResult:
        kind = stmt[0]
        if kind == "select":
            return self._run_select(stmt)
        if kind == "create":
            return self._run_create(stmt)
        if kind == "insert":
            return self._run_insert(stmt)
        if kind == "delete":
            return self._run_delete(stmt)
        raise EngineError(f"unknown statement {kind!r}")  # pragma: no cover

    def _run_create(self, stmt: Tuple) -> StatementResult:
        _, name, columns = stmt
        if name in self.tables:
            raise EngineError(f"table {name} already exists")
        self.tables[name] = Table(name=name, columns=columns)
        return StatementResult(rowcount=0)

    def _run_insert(self, stmt: Tuple) -> StatementResult:
        _, name, columns, rows = stmt
        table = self.tables.get(name)
        if table is None:
            raise EngineError(f"no such table: {name}")
        ncols = len(table.columns)
        col_index = {c.name: i for i, c in enumerate(table.columns)}
        # Validate and convert every row before mutating the table, so an
        # error anywhere leaves the table untouched (statement atomicity).
        prepared: List[List[Any]] = []
        for exprs in rows:
            values = [eval_expr(e, None) for e in exprs]
            if columns is not None:
                if len(columns) != len(values):
                    raise EngineError(
                        f"table {name} has {ncols} columns but {len(values)} values were supplied"
                    )
                row = [None] * ncols
                for cname, v in zip(columns, values):
                    if cname not in col_index:
                        raise EngineError(f"table {name} has no column named {cname}")
                    row[col_index[cname]] = v
                values = row
            elif len(values) != ncols:
                raise EngineError(
                    f"table {name} has {ncols} columns but {len(values)} values were supplied"
                )
            prepared.append(
                [_convert_to_affinity(v, c.affinity) for v, c in zip(values, table.columns)]
            )
        table.rows.extend(prepared)
        return StatementResult(rowcount=len(prepared))

    def _run_delete(self, stmt: Tuple) -> StatementResult:
        _, name, where = stmt
        table = self.tables.get(name)
        if table is None:
            raise EngineError(f"no such table: {name}")
        if where is not None:
            known = {c.name for c in table.columns}
            for col in _walk_expr_cols(where):
                if col not in known:
                    raise EngineError(f"no such column: {col}")
        if where is None:
            deleted = len(table.rows)
            table.rows = []
            return StatementResult(rowcount=deleted)
        kept = []
        deleted = 0
        for row in table.rows:
            ctx = RowContext(table, row)
            if _truthy(eval_expr(where, ctx)):
                deleted += 1
            else:
                kept.append(row)
        table.rows = kept
        return StatementResult(rowcount=deleted)

    def _run_select(self, stmt: Tuple) -> StatementResult:
        _, distinct, items, table_name, where, order_by, limit = stmt
        if table_name is None:
            # expression-only SELECT (no FROM): a single row
            if any(it[0] == "star" for it in items):
                raise EngineError("SELECT * requires a FROM clause")
            values = [eval_expr(e, None) for e in items]
            proj: List[Tuple[List[Any], Optional[RowContext]]] = [(values, None)]
        else:
            table = self.tables.get(table_name)
            if table is None:
                raise EngineError(f"no such table: {table_name}")
            # validate column references up front (sqlite resolves columns at
            # prepare time, so an empty table must still reject bad columns)
            known = {c.name for c in table.columns}

            def check_expr(e: Tuple) -> None:
                for col in _walk_expr_cols(e):
                    if col not in known:
                        raise EngineError(f"no such column: {col}")

            for item in items:
                if item[0] != "star":
                    check_expr(item)
            if where is not None:
                check_expr(where)
            if order_by is not None:
                for term, _asc in order_by:
                    check_expr(term)
            proj = []
            for row in table.rows:
                ctx = RowContext(table, row)
                if where is not None and not _truthy(eval_expr(where, ctx)):
                    continue
                out: List[Any] = []
                for item in items:
                    if item[0] == "star":
                        out.extend(row)
                    else:
                        out.append(eval_expr(item, ctx))
                proj.append((out, ctx))
        # DISTINCT: whole-row dedup; NULLs are equal, numbers compare
        # numerically (5 == 5.0), text is byte-wise (5 != '5').
        if distinct:
            seen = set()
            dedup = []
            for out, ctx in proj:
                key = tuple(out)
                if key in seen:
                    continue
                seen.add(key)
                dedup.append((out, ctx))
            proj = dedup
        # ORDER BY: sqlite ordering (NULL smallest, then numbers, then text).
        # Integer literals are 1-based ordinals of the output row. A stable
        # multi-pass sort (rightmost term first) gives per-column ASC/DESC.
        if order_by:
            width = _output_width(items, table if table_name is not None else None)
            for i, (term, _asc) in enumerate(order_by, start=1):
                if term[0] == "num" and isinstance(term[1], int):
                    idx = term[1]
                    if idx < 1 or idx > width:
                        raise EngineError(
                            f"{i}{_ordinal_suffix(i)} ORDER BY term out of range - "
                            f"should be between 1 and {width}"
                        )
            for term, asc in reversed(order_by):
                proj = sorted(
                    proj, key=lambda p, t=term: _order_sort_key(t, p), reverse=not asc
                )
        # LIMIT / OFFSET: applied after sorting.
        if limit is not None:
            proj = _apply_limit(proj, limit)
        return StatementResult(rows=[out for out, _ctx in proj], rowcount=len(proj))
