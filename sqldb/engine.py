"""SQL engine for sql-db-0820: expression evaluation plus single-table storage.

Built on the expression engine from slice 0. This slice adds:

  * CREATE TABLE (column names + declared types -> sqlite type affinity)
  * INSERT (VALUES with optional column list, multi-row)
  * DELETE (with or without WHERE)
  * SELECT with FROM and WHERE; column references in expressions; ``*``
  * sqlite type affinity: storage conversion and comparison rules
  * LIKE / NOT LIKE (ASCII case-insensitive, % _ wildcards, ESCAPE)
  * multi-table FROM: INNER/LEFT [OUTER]/CROSS JOIN, comma-separated tables,
    ON and USING conditions (join conditions reuse the expression engine)
  * qualified column references (``t.c``) and qualified star (``t.*``)
  * table aliases (``FROM t AS x`` / ``FROM t x``) and derived tables
    (``FROM (SELECT ...) [AS] d``, including multi-level nesting)
  * subqueries: scalar subqueries in expressions (``(SELECT ...)``),
    ``IN (SELECT ...)`` / ``NOT IN (SELECT ...)``, ``EXISTS (SELECT ...)``,
    correlated subqueries (inner scope wins, then outer), select-list
    output aliases (``expr AS name``) usable in ORDER BY
  * sqlite join semantics: NULL never matches (even NULL), LEFT JOIN pads
    unmatched rows with NULL, USING columns are merged for ``*`` and for
    unqualified references, ambiguous columns are rejected

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
    "INDEX", "UNIQUE", "IF", "EXISTS", "ON", "DROP",
    "GROUP", "HAVING", "COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL",
    "JOIN", "INNER", "LEFT", "OUTER", "CROSS", "USING",
    "RIGHT", "FULL", "NATURAL",
    "IN", "AS",
}

AGG_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL"}

_NUM_RE = re.compile(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
_OP2 = {"<=", ">=", "!=", "<>", "=="}
_OP1 = set("+-*/%()=,;<>.")


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
    append = tokens.append
    i = 0
    n = len(sql)
    _KEYWORDS = KEYWORDS
    while i < n:
        c = sql[i]
        if c in " \t\n\r\v\f":
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
            append(Token("str", text, i))
            continue
        if ("0" <= c <= "9") or (c == "." and i + 1 < n and "0" <= sql[i + 1] <= "9"):
            m = _NUM_RE.match(sql, i)
            text = m.group(0)
            if any(ch in text for ch in ".eE"):
                append(Token("num", float(text), i))
            else:
                append(Token("num", int(text), i))
            i = m.end()
            continue
        if ("a" <= c <= "z") or ("A" <= c <= "Z") or c == "_":
            j = i + 1
            while j < n:
                ch = sql[j]
                if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch == "_":
                    j += 1
                else:
                    break
            word = sql[i:j]
            up = word.upper()
            append(Token("kw", up, i) if up in _KEYWORDS else Token("id", word, i))
            i = j
            continue
        two = sql[i:i + 2]
        if two in _OP2:
            append(Token("op", two, i))
            i += 2
            continue
        if c in _OP1:
            append(Token("op", c, i))
            i += 1
            continue
        raise EngineError(f"syntax error near {c!r} at position {i}")
    append(Token("eof", "", n))
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

    def __post_init__(self) -> None:
        # Per-table maps used by RowContext. Built once per table instead of
        # once per row (the hot path for single-table scans). Qualified
        # spellings (``t.c``) are included so single-table lookups resolve
        # uniformly, matching the previous per-row dict construction.
        self.col_names: List[str] = [c.name for c in self.columns]
        self._colmap: Dict[str, int] = {c.name: i for i, c in enumerate(self.columns)}
        self.affinity_map: Dict[str, str] = {c.name: c.affinity for c in self.columns}
        self.affinity_map.update(
            {f"{self.name}.{c.name}": c.affinity for c in self.columns}
        )


@dataclass
class IndexDef:
    """Index metadata. This slice is correctness-first: indexes are recorded
    and validated but do not participate in query execution (lookup paths
    remain full table scans until the performance slice)."""

    name: str
    table: str
    columns: List[str]
    unique: bool = False


class RowContext:
    """Binds a table row to column names for expression evaluation.

    ``engine`` carries the owning Engine so subquery nodes inside expressions
    can be executed; ``outer`` is the enclosing (outer query) context used for
    correlated subqueries: a column that is absent from this row falls through
    to the outer scope (sqlite resolution rule).
    """

    __slots__ = ("table", "row", "affinity", "values", "qual_values",
                 "engine", "outer")

    def __init__(self, table: Table, row: List[Any], engine: Optional[Any] = None,
                 outer: Optional[Any] = None):
        self.table = table
        self.row = row
        self.engine = engine
        self.outer = outer
        # affinity (unqualified + qualified) is table-constant and cached on
        # the Table; values vary per row. The qualified-values dict is built
        # lazily on first qualified lookup (rare in single-table scans).
        self.affinity = table.affinity_map
        self.values = dict(zip(table.col_names, row))
        self.qual_values: Optional[Dict[str, Any]] = None

    def _qual(self, name: str) -> Optional[Any]:
        """Value for a qualified ``table.col`` spelling, or None when the
        qualifier is not this context's table (caller falls through)."""
        if self.qual_values is None:
            self.qual_values = {
                f"{self.table.name}.{c}": v
                for c, v in zip(self.table.col_names, self.row)
            }
        return self.qual_values.get(name)

    def value_of(self, name: str) -> Any:
        if name not in self.affinity:
            if self.outer is not None:
                return self.outer.value_of(name)
            raise EngineError(f"no such column: {name}")
        if name in self.values:
            return self.values[name]
        # qualified spelling ``t.c`` of this table: affinity contains it (so
        # we got here) but the row values dict is unqualified; resolve lazily.
        return self._qual(name)

    def affinity_of(self, name: str) -> str:
        if name in self.affinity:
            return self.affinity[name]
        if self.outer is not None:
            return self.outer.affinity_of(name)
        return "NONE"

    def has_column(self, name: str) -> bool:
        if name in self.affinity:
            return True
        return self.outer is not None and self.outer.has_column(name)


class JoinContext:
    """Binds one combined joined row (one row per FROM table) to column names.

    Resolution follows sqlite:
      * ``t.c`` addresses table t directly (error when t is not in the FROM
        list or c is not a column of t);
      * unqualified names must be unambiguous: a column merged through a
        USING join counts as one occurrence and resolves to the leftmost
        table of its merged component; a name present in several tables that
        are not merged together is ``ambiguous column name``.
    ``engine``/``outer`` support subqueries and correlated resolution (a
    column missing from the joined row falls through to the outer query).
    """

    __slots__ = ("tables", "merged_cols", "rows", "_colmaps", "engine", "outer")

    def __init__(self, tables: List[Table], merged_cols: Dict[str, List[List[int]]],
                 rows: List[List[Any]], engine: Optional[Any] = None,
                 outer: Optional[Any] = None):
        self.tables = tables
        self.merged_cols = merged_cols
        self.rows = rows
        self.engine = engine
        self.outer = outer
        self._colmaps = _colmaps_for(tables)

    def _resolve(self, name: str) -> Tuple[int, int]:
        if "." in name:
            tname, cname = name.split(".", 1)
            for ti, t in enumerate(self.tables):
                if t.name == tname:
                    cm = self._colmaps[ti]
                    if cname in cm:
                        return ti, cm[cname]
                    raise EngineError(f"no such column: {name}")
            raise EngineError(f"no such column: {name}")
        ti = _resolve_unqualified(self.tables, self._colmaps, self.merged_cols, name)
        return ti, self._colmaps[ti][name]

    def value_of(self, name: str) -> Any:
        try:
            ti, ci = self._resolve(name)
        except EngineError as e:
            if str(e).startswith("no such column") and self.outer is not None:
                return self.outer.value_of(name)
            raise
        row = self.rows[ti]
        # synthetic empty row (aggregation over an empty join) yields NULL
        return row[ci] if ci < len(row) else None

    def affinity_of(self, name: str) -> str:
        try:
            ti, ci = self._resolve(name)
        except EngineError as e:
            if str(e).startswith("no such column") and self.outer is not None:
                return self.outer.affinity_of(name)
            raise
        return self.tables[ti].columns[ci].affinity

    def has_column(self, name: str) -> bool:
        try:
            self._resolve(name)
            return True
        except EngineError as e:
            if str(e).startswith("no such column") and self.outer is not None:
                return self.outer.has_column(name)
            return False  # ambiguous counts as present; let eval raise it


def _colmaps_for(tables: List[Table]) -> List[Dict[str, int]]:
    """Per-table column-name->index maps, cached by table identity.

    Column layouts are fixed at CREATE time, so the maps can be computed once
    per table instead of once per joined row (the per-row JoinContext hot
    path). The cache is attached to the Table object itself.
    """
    return [t._colmap for t in tables]  # type: ignore[attr-defined]


def _resolve_unqualified(tables: List[Table], colmaps: List[Dict[str, int]],
                         merged_cols: Dict[str, List[List[int]]], name: str) -> int:
    """Table index for an unqualified column name, or raise like sqlite.

    Tables connected by USING on ``name`` form one merged component and count
    as a single occurrence; the component resolves to its leftmost table.
    """
    present = [ti for ti, cm in enumerate(colmaps) if name in cm]
    if not present:
        raise EngineError(f"no such column: {name}")
    comps = merged_cols.get(name)
    if comps:
        groups: Dict[Optional[int], List[int]] = {}
        for ti in present:
            key = next((ci for ci, comp in enumerate(comps) if ti in comp), None)
            groups.setdefault(key, []).append(ti)
        if len(groups) > 1:
            raise EngineError(f"ambiguous column name: {name}")
        idxs = next(iter(groups.values()))
    else:
        if len(present) > 1:
            raise EngineError(f"ambiguous column name: {name}")
        idxs = present
    return min(idxs)


def _merged_components(pairs_by_col: Dict[str, List[Tuple[int, int]]]) -> Dict[str, List[List[int]]]:
    """USING pairs -> connected components per column name.

    ``t1 JOIN t2 USING (k) JOIN t3 USING (k)`` merges k across all three
    tables, so overlapping pairs must be unioned (union-find)."""
    out: Dict[str, List[List[int]]] = {}
    for cn, pairs in pairs_by_col.items():
        parent: Dict[int, int] = {}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for a, b in pairs:
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            union(a, b)
        comps: Dict[int, List[int]] = {}
        for ti in parent:
            comps.setdefault(find(ti), []).append(ti)
        out[cn] = [sorted(v) for v in comps.values()]
    return out


def _resolve_col(tables: List[Table], colmaps: List[Dict[str, int]],
                 merged_cols: Dict[str, List[List[int]]], name: str) -> None:
    """Raise unless ``name`` resolves against the visible tables (sqlite
    resolves columns at prepare time). Used for up-front validation."""
    if "." in name:
        tname, cname = name.split(".", 1)
        for ti, t in enumerate(tables):
            if t.name == tname:
                if cname in colmaps[ti]:
                    return
                raise EngineError(f"no such column: {name}")
        raise EngineError(f"no such column: {name}")
    _resolve_unqualified(tables, colmaps, merged_cols, name)


class AggContext:
    """Expression context for one GROUP BY group (or the single whole-table
    group): plain column references resolve to a representative row (the
    first row of the group, matching sqlite), while aggregate nodes compute
    over every row of the group.

    ``tables``/``merged_cols`` describe the FROM clause (empty for
    expression-only SELECT); ``group_rows`` holds combined rows (one list of
    values per table, aligned with ``tables``). ``engine``/``outer`` support
    subqueries and correlated resolution like the row contexts.
    """

    __slots__ = ("tables", "merged_cols", "group_rows", "rep", "engine", "outer")

    def __init__(self, tables: List[Table], merged_cols: Dict[str, List[List[int]]],
                 group_rows: List[List[List[Any]]], rep: Optional[Any],
                 engine: Optional[Any] = None, outer: Optional[Any] = None):
        self.tables = tables
        self.merged_cols = merged_cols
        self.group_rows = group_rows
        self.rep = rep
        self.engine = engine
        self.outer = outer

    def value_of(self, name: str) -> Any:
        if self.rep is None:
            if self.outer is not None:
                return self.outer.value_of(name)
            raise EngineError(f"no such column: {name}")
        return self.rep.value_of(name)

    def affinity_of(self, name: str) -> str:
        if self.rep is None:
            if self.outer is not None:
                return self.outer.affinity_of(name)
            return "NONE"
        return self.rep.affinity_of(name)

    def has_column(self, name: str) -> bool:
        if self.rep is not None and self.rep.has_column(name):
            return True
        return self.outer is not None and self.outer.has_column(name)


def _scope_has_column(ctx: Optional[Any], name: str) -> bool:
    """True if ``name`` resolves in ``ctx`` or any enclosing correlated scope.

    Used by prepare-time validation so a correlated subquery may reference
    columns of the enclosing query without raising.
    """
    return ctx is not None and ctx.has_column(name)


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
#   ('alias', expr, name)  -- SELECT expr AS name item
#   ('scalar', select_stmt)  -- scalar subquery
#   ('exists', select_stmt)  -- EXISTS (SELECT ...)
#   ('in', left, select_stmt, negate)  -- left [NOT] IN (SELECT ...)
#
# FROM source tuples:
#   ('table', name, alias|None)
#   ('derived', select_stmt, alias|None)
#   ('join', jtype, source, cond)  -- source is a table/derived tuple


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
            if t.value == "DROP":
                return self.parse_drop_stmt()
        raise EngineError(f"expected SELECT, CREATE, INSERT, DELETE or DROP, got {t.value!r}")

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

    def _parse_subquery(self) -> Tuple:
        """Parse ``(SELECT ...)`` after the opening paren was consumed; returns
        the select statement tuple and leaves the parser at the closing paren."""
        saved_allow = self.allow_columns
        try:
            stmt = self._parse_select_core()
        finally:
            self.allow_columns = saved_allow
        self._expect_op(")")
        return stmt

    def _parse_from_source(self) -> Tuple:
        """One FROM source: a table (with optional alias) or a derived table
        ``(SELECT ...) [AS] alias``. Returns ('table', name, alias|None) or
        ('derived', stmt, alias|None)."""
        t = self.peek()
        if t.kind == "op" and t.value == "(":
            self._next()
            stmt = self._parse_subquery()
            alias = None
            if self.peek().kind == "kw" and self.peek().value == "AS":
                self._next()
                alias = self._name()
            elif self.peek().kind == "id":
                alias = self._name()
            return ("derived", stmt, alias)
        name = self._name()
        alias = None
        if self.peek().kind == "kw" and self.peek().value == "AS":
            self._next()
            alias = self._name()
        elif self.peek().kind == "id":
            alias = self._name()
        return ("table", name, alias)

    def parse_select_stmt(self) -> Tuple:
        stmt = self._parse_select_core()
        self._finish()
        return stmt

    def _parse_select_core(self) -> Tuple:
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
                expr = self.parse_expr()
                # optional output alias: ``expr AS name`` or bare ``expr name``
                if self.peek().kind == "kw" and self.peek().value == "AS":
                    self._next()
                    items.append(("alias", expr, self._name()))
                elif self.peek().kind == "id":
                    items.append(("alias", expr, self._name()))
                else:
                    items.append(expr)
            if self.peek().kind == "op" and self.peek().value == ",":
                self._next()
                continue
            break
        from_clause: Optional[List[Tuple]] = None
        where: Optional[Tuple] = None
        if self.peek().kind == "kw" and self.peek().value == "FROM":
            self._next()
            from_clause = [self._parse_from_source()]
            self.allow_columns = True
            while True:
                t = self.peek()
                if t.kind == "op" and t.value == ",":
                    self._next()
                    from_clause.append(self._parse_from_source())
                elif t.kind == "kw" and t.value in ("JOIN", "INNER", "LEFT", "CROSS"):
                    if t.value == "JOIN":
                        self._next()
                        jtype = "inner"
                    elif t.value == "INNER":
                        self._next()
                        self._expect_kw("JOIN")
                        jtype = "inner"
                    elif t.value == "LEFT":
                        self._next()
                        if self.peek().kind == "kw" and self.peek().value == "OUTER":
                            self._next()
                        self._expect_kw("JOIN")
                        jtype = "left"
                    else:  # CROSS
                        self._next()
                        self._expect_kw("JOIN")
                        jtype = "cross"
                    source = self._parse_from_source()
                    cond: Optional[Tuple] = None
                    if self.peek().kind == "kw" and self.peek().value == "ON":
                        self._next()
                        cond = ("on", self.parse_expr())
                    elif self.peek().kind == "kw" and self.peek().value == "USING":
                        self._next()
                        self._expect_op("(")
                        cols: List[str] = []
                        while True:
                            cols.append(self._name())
                            if self.peek().kind == "op" and self.peek().value == ",":
                                self._next()
                                continue
                            break
                        self._expect_op(")")
                        cond = ("using", cols)
                    from_clause.append(("join", jtype, source, cond))
                else:
                    break
            if self.peek().kind == "kw" and self.peek().value == "WHERE":
                self._next()
                where = self.parse_expr()
        group_by: Optional[List[Tuple]] = None
        if self.peek().kind == "kw" and self.peek().value == "GROUP":
            self._next()
            self._expect_kw("BY")
            group_by = []
            while True:
                group_by.append(self.parse_expr())
                if self.peek().kind == "op" and self.peek().value == ",":
                    self._next()
                    continue
                break
        having: Optional[Tuple] = None
        if self.peek().kind == "kw" and self.peek().value == "HAVING":
            self._next()
            having = self.parse_expr()
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
        return ("select", distinct, items, from_clause, where, group_by, having, order_by, limit)

    def parse_create_stmt(self) -> Tuple:
        self._expect_kw("CREATE")
        unique = False
        if self.peek().kind == "kw" and self.peek().value == "UNIQUE":
            self._next()
            unique = True
        t = self.peek()
        if t.kind == "kw" and t.value == "TABLE":
            self._next()
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
        if t.kind == "kw" and t.value == "INDEX":
            self._next()
            if_not_exists = False
            if self.peek().kind == "kw" and self.peek().value == "IF":
                self._next()
                self._expect_kw("NOT")
                self._expect_kw("EXISTS")
                if_not_exists = True
            name = self._name()
            self._expect_kw("ON")
            table = self._name()
            self._expect_op("(")
            columns: List[str] = []
            while True:
                col = self._name()
                if self.peek().kind == "kw" and self.peek().value in ("ASC", "DESC"):
                    self._next()  # index column direction is parsed but unused
                columns.append(col)
                t = self._next()
                if t.kind == "op" and t.value == ",":
                    continue
                if t.kind == "op" and t.value == ")":
                    break
                raise EngineError(f"expected ',' or ')', got {t.value!r}")
            self._finish()
            return ("create_index", name, table, columns, unique, if_not_exists)
        raise EngineError(f"expected TABLE or INDEX after CREATE, got {t.value!r}")

    def parse_drop_stmt(self) -> Tuple:
        self._expect_kw("DROP")
        self._expect_kw("INDEX")
        if_exists = False
        if self.peek().kind == "kw" and self.peek().value == "IF":
            self._next()
            self._expect_kw("EXISTS")
            if_exists = True
        name = self._name()
        self._finish()
        return ("drop_index", name, if_exists)

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
            elif t.kind == "kw" and t.value == "IN":
                self._next()
                self._expect_op("(")
                if not (self.peek().kind == "kw" and self.peek().value == "SELECT"):
                    raise EngineError("expected SELECT in IN (SELECT ...)")
                stmt = self._parse_subquery()
                left = ("in", left, stmt, False)
            elif t.kind == "kw" and t.value == "NOT" and self.peek2().kind == "kw" and self.peek2().value == "IN":
                self._next()
                self._next()
                self._expect_op("(")
                if not (self.peek().kind == "kw" and self.peek().value == "SELECT"):
                    raise EngineError("expected SELECT in NOT IN (SELECT ...)")
                stmt = self._parse_subquery()
                left = ("in", left, stmt, True)
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
            if t.value == "EXISTS":
                self._next()
                self._expect_op("(")
                if not (self.peek().kind == "kw" and self.peek().value == "SELECT"):
                    raise EngineError("expected SELECT after EXISTS (")
                stmt = self._parse_subquery()
                return ("exists", stmt)
            if t.value in AGG_FUNCS:
                return self.parse_agg_call()
            raise EngineError(f"unexpected keyword {t.value}")
        if t.kind == "op" and t.value == "(":
            self._next()
            if self.peek().kind == "kw" and self.peek().value == "SELECT":
                stmt = self._parse_subquery()
                return ("scalar", stmt)
            e = self.parse_expr()
            self._expect_op(")")
            return e
        if t.kind == "id":
            self._next()
            if self.peek().kind == "op" and self.peek().value == ".":
                self._next()
                nxt = self.peek()
                if nxt.kind == "op" and nxt.value == "*":
                    self._next()
                    if not self.allow_columns:
                        raise EngineError(f"unknown column or identifier {t.value!r} (no FROM clause)")
                    return ("qualstar", t.value.lower())
                if nxt.kind in ("id", "kw"):
                    self._next()
                    if not self.allow_columns:
                        raise EngineError(f"unknown column or identifier {t.value!r} (no FROM clause)")
                    return ("col", t.value.lower() + "." + nxt.value.lower())
                raise EngineError("expected column name after '.'")
            if not self.allow_columns:
                raise EngineError(f"unknown column or identifier {t.value!r} (no FROM clause)")
            return ("col", t.value.lower())
        raise EngineError(f"unexpected token {t.value!r}")

    def parse_agg_call(self) -> Tuple:
        name = self._next().value  # kw COUNT/SUM/AVG/MIN/MAX/TOTAL
        self._expect_op("(")
        distinct = False
        if self.peek().kind == "kw" and self.peek().value == "DISTINCT":
            self._next()
            distinct = True
        if self.peek().kind == "op" and self.peek().value == ")":
            # COUNT() is COUNT(*) in sqlite; other functions reject no args.
            self._next()
            if name != "COUNT":
                raise EngineError(f"wrong number of arguments to function {name}()")
            return ("agg", name, None, distinct)
        if self.peek().kind == "op" and self.peek().value == "*":
            if distinct:
                raise EngineError(f"wrong number of arguments to function {name}()")
            self._next()
            if name != "COUNT":
                raise EngineError(f"wrong number of arguments to function {name}()")
            self._expect_op(")")
            return ("agg", name, None, distinct)
        arg = self.parse_expr()
        self._expect_op(")")
        return ("agg", name, arg, distinct)

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
    if kind == "alias":
        return eval_expr(node[1], ctx)
    if kind == "agg":
        return _eval_agg(node, ctx)
    if kind == "scalar":
        return _eval_scalar_subquery(node[1], ctx)
    if kind == "exists":
        return _eval_exists_subquery(node[1], ctx)
    if kind == "in":
        return _eval_in_subquery(node[1], node[2], node[3], ctx)
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


def _frame_ctx(tables: List[Table], merged_cols: Dict[str, List[List[int]]],
               frame: List[List[Any]], engine: Optional[Any] = None,
               outer: Optional[Any] = None) -> Optional[Any]:
    """Expression context for one combined row of a FROM clause: None when
    there is no table (expression-only SELECT), a RowContext for a single
    table, a JoinContext otherwise. ``engine``/``outer`` are threaded through
    so subquery nodes can execute and resolve correlated columns."""
    if not tables:
        return None
    if len(tables) == 1:
        return RowContext(tables[0], frame[0], engine, outer)
    return JoinContext(tables, merged_cols, frame, engine, outer)


def _to_agg_number(v: Any) -> Any:
    """Numeric conversion for SUM/AVG/TOTAL: text converts when possible,
    non-numeric text contributes 0 (sqlite behavior, verified)."""
    if is_number(v):
        return v
    if isinstance(v, str):
        n = _text_to_number(v)
        if n is not None:
            return n
        return 0.0  # non-numeric text contributes 0.0 (sqlite: SUM('abc') = 0.0)
    return 0


def _eval_agg(node: Tuple, ctx: Optional[Any]) -> Any:
    name = node[1]
    arg = node[2]
    distinct = node[3]
    if not isinstance(ctx, AggContext):
        raise EngineError("misuse of aggregate function")
    tables = ctx.tables
    merged_cols = ctx.merged_cols
    rows = ctx.group_rows
    engine = ctx.engine
    outer = ctx.outer
    if name == "COUNT":
        if arg is None:
            return len(rows)
        values = [eval_expr(arg, _frame_ctx(tables, merged_cols, frame, engine, outer)) for frame in rows]
        values = [v for v in values if v is not None]
        if distinct:
            values = list(dict.fromkeys(values))
        return len(values)
    if arg is None:
        raise EngineError(f"wrong number of arguments to function {name}()")
    values = [eval_expr(arg, _frame_ctx(tables, merged_cols, frame, engine, outer)) for frame in rows]
    non_null = [v for v in values if v is not None]
    if distinct:
        non_null = list(dict.fromkeys(non_null))
    if name == "MIN":
        best = None
        for v in non_null:
            if best is None or _binary_compare(v, best) < 0:
                best = v
        return best
    if name == "MAX":
        best = None
        for v in non_null:
            if best is None or _binary_compare(v, best) > 0:
                best = v
        return best
    if name in ("SUM", "TOTAL", "AVG"):
        nums = [_to_agg_number(v) for v in non_null]
        total = 0
        for v in nums:
            total += v
        if name == "SUM":
            return None if not nums else total
        if name == "TOTAL":
            return float(total)
        if name == "AVG":
            return None if not nums else float(total) / len(nums)
    raise EngineError(f"unknown aggregate {name}")  # pragma: no cover


def _truthy(v: Any) -> bool:
    return v is not None and v != 0


def _subquery_env(ctx: Optional[Any]) -> Tuple[Optional[Any], Optional[Any]]:
    """Return (engine, outer_ctx) from an evaluation context.

    The subquery executes with the *current* context as its outer scope so
    correlated column references fall through to the enclosing row. Expression-
    only contexts (no FROM) carry the engine so ``(SELECT 1)`` still works;
    every context created inside _run_select carries it.
    """
    if ctx is None:
        return None, None
    return getattr(ctx, "engine", None), ctx


def _eval_scalar_subquery(stmt: Tuple, ctx: Optional[Any]) -> Any:
    """Scalar subquery: run the inner SELECT; 0 rows -> NULL, exactly one
    row -> its single value. sqlite 3.46 takes the first row when the
    subquery returns several rows (verified against the sandbox sqlite); the
    column-count check happens first, exactly like sqlite (''sub-select
    returns N columns - expected 1'', raised even for an empty result)."""
    engine, outer = _subquery_env(ctx)
    if engine is None:
        raise EngineError("scalar subquery requires an engine context")
    res = engine._run_select(stmt, outer=outer)
    if res.error is not None:
        raise EngineError(res.error)
    cols = res.columns or []
    if len(cols) != 1:
        raise EngineError(f"sub-select returns {len(cols)} columns - expected 1")
    rows = res.rows or []
    if not rows:
        return None
    return rows[0][0]


def _eval_exists_subquery(stmt: Tuple, ctx: Optional[Any]) -> int:
    """EXISTS (SELECT ...): 1 when the subquery yields at least one row."""
    engine, outer = _subquery_env(ctx)
    if engine is None:
        raise EngineError("EXISTS subquery requires an engine context")
    res = engine._run_select(stmt, outer=outer)
    if res.error is not None:
        raise EngineError(res.error)
    return 1 if (res.rows or []) else 0


def _eval_in_subquery(left: Tuple, stmt: Tuple, negate: bool, ctx: Optional[Any]) -> Any:
    """x [NOT] IN (SELECT ...) with sqlite three-valued logic.

    NULL handling (verified against sqlite 3.46):
      * x NULL: NULL if the set is non-empty, else 0;
      * x non-NULL with a match: 1;
      * no match and the set contains NULL: NULL;
      * no match, no NULL: 0.
    NOT IN is the NOT of the result (NULL propagates). The comparison applies
    affinity like a plain equality (left operand affinity vs subquery column
    affinity)."""
    engine, outer = _subquery_env(ctx)
    if engine is None:
        raise EngineError("IN subquery requires an engine context")
    lv = eval_expr(left, ctx)
    res = engine._run_select(stmt, outer=outer)
    if res.error is not None:
        raise EngineError(res.error)
    cols = res.columns or []
    if len(cols) != 1:
        raise EngineError(f"sub-select returns {len(cols)} columns - expected 1")
    values = [row[0] for row in (res.rows or [])]
    if lv is None:
        r: Any = 0 if not values else None
    else:
        la = expr_affinity(left, ctx)
        ra = cols[0].affinity if cols else "NONE"
        found = False
        has_null = False
        for v in values:
            if v is None:
                has_null = True
                continue
            lv2, v2 = apply_comparison_affinity(la, lv, ra, v)
            if _binary_compare(lv2, v2) == 0:
                found = True
                break
        r = 1 if found else (None if has_null else 0)
    return _not(r) if negate else r


def _walk_expr_cols(node: Any):
    """Yield column names referenced by an expression AST.

    Subquery nodes (``scalar``/``exists``/``in``) do not contribute columns
    here: their select statements resolve columns against their own scope
    (plus the outer scope) at prepare time, so the caller must validate the
    subquery statement separately. For ``in`` the left operand belongs to the
    current scope and is walked.
    """
    if isinstance(node, tuple) and node:
        if node[0] == "col":
            yield node[1]
        elif node[0] == "scalar" or node[0] == "exists":
            return
        elif node[0] == "in":
            yield from _walk_expr_cols(node[1])
        elif node[0] == "alias":
            yield from _walk_expr_cols(node[1])
        else:
            for child in node[1:]:
                yield from _walk_expr_cols(child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_expr_cols(item)


def _walk_expr_subqueries(node: Any):
    """Yield the select statement of every subquery node in an expression."""
    if isinstance(node, tuple) and node:
        if node[0] == "scalar" or node[0] == "exists":
            yield node[1]
        elif node[0] == "in":
            yield from _walk_expr_subqueries(node[1])
            yield node[2]
        elif node[0] == "alias":
            yield from _walk_expr_subqueries(node[1])
        else:
            for child in node[1:]:
                yield from _walk_expr_subqueries(child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_expr_subqueries(item)


def _contains_agg(node: Any) -> bool:
    """True if an expression tree contains an aggregate node.

    Aggregates inside a subquery belong to the subquery's own aggregate
    classification, so subquery nodes do not count for the enclosing query.
    """
    if isinstance(node, tuple) and node:
        if node[0] == "agg":
            return True
        if node[0] == "scalar" or node[0] == "exists":
            return False
        if node[0] == "in":
            return _contains_agg(node[1])
        if node[0] == "alias":
            return _contains_agg(node[1])
        return any(_contains_agg(child) for child in node[1:])
    if isinstance(node, list):
        return any(_contains_agg(item) for item in node)
    return False


def _expand_star(tables: List[Table], colmaps: List[Dict[str, int]],
                 merged_cols: Dict[str, List[List[int]]]) -> List[Tuple[int, str]]:
    """Columns a bare ``*`` expands to, in FROM order. A USING-merged column
    appears once per merged component, at the position of its leftmost table;
    tables outside the component keep their own copy (sqlite behavior)."""
    out: List[Tuple[int, str]] = []
    emitted: set = set()  # (column name, component id) already emitted
    for ti, t in enumerate(tables):
        for c in t.columns:
            name = c.name
            if name in merged_cols:
                comp_id = next(
                    (ci for ci, comp in enumerate(merged_cols[name]) if ti in comp),
                    None,
                )
                if comp_id is not None:
                    key = (name, comp_id)
                    if key in emitted:
                        continue
                    emitted.add(key)
            out.append((ti, name))
    return out


def _project_items(items: List[Tuple], tables: List[Table], colmaps: List[Dict[str, int]],
                   merged_cols: Dict[str, List[List[int]]], frame: List[List[Any]],
                   ctx: Optional[Any]) -> List[Any]:
    """Evaluate a select list against one combined row. ``*`` expands via the
    FROM tables, ``t.*`` expands one table's columns verbatim, an ``alias``
    item evaluates its inner expression, anything else is an expression
    evaluated with ``ctx``."""
    out: List[Any] = []
    for item in items:
        if item[0] == "star":
            for ti, cn in _expand_star(tables, colmaps, merged_cols):
                row = frame[ti]
                ci = colmaps[ti][cn]
                out.append(row[ci] if ci < len(row) else None)
        elif item[0] == "qualstar":
            tname = item[1]
            ti = next(i for i, t in enumerate(tables) if t.name == tname)
            out.extend(frame[ti])
        elif item[0] == "alias":
            out.append(eval_expr(item[1], ctx))
        else:
            out.append(eval_expr(item, ctx))
    return out


def _output_width(items: List[Tuple], tables: List[Table], colmaps: List[Dict[str, int]],
                  merged_cols: Dict[str, List[List[int]]]) -> int:
    """Number of output columns of a select list (for ORDER BY ordinals)."""
    width = 0
    for item in items:
        if item[0] == "star":
            width += len(_expand_star(tables, colmaps, merged_cols))
        elif item[0] == "qualstar":
            tname = item[1]
            ti = next(i for i, t in enumerate(tables) if t.name == tname)
            width += len(tables[ti].columns)
        else:
            width += 1
    return width


def _ordinal_suffix(i: int) -> str:
    if 10 <= i % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(i % 10, "th")


def _order_sort_key(term: Tuple, p: Tuple[List[Any], Optional[RowContext]],
                    alias_index: Optional[Dict[str, int]] = None) -> Tuple:
    """Sort key for one ORDER BY term: (class, value) with sqlite ordering
    (class 0 = NULL smallest, 1 = numbers, 2 = text). Integer literals are
    1-based ordinals of the output row; a bare column name that matches an
    output alias uses the projected value (sqlite: output alias wins)."""
    out, ctx = p
    if term[0] == "num" and isinstance(term[1], int):
        v = out[term[1] - 1]
    elif term[0] == "col" and alias_index and term[1] in alias_index:
        v = out[alias_index[term[1]]]
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
                 limit: Tuple[Optional[Tuple], Optional[Tuple]],
                 env: Optional[Any] = None) -> List[Tuple[List[Any], Optional[RowContext]]]:
    limit_node, offset_node = limit
    n: Optional[int] = None
    if limit_node is not None:
        n = _limit_int(eval_expr(limit_node, env))
    m = 0
    if offset_node is not None:
        m = _limit_int(eval_expr(offset_node, env))
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
    columns:  output column definitions for SELECT (used by scalar/IN
              subqueries for the ``sub-select returns N columns`` check and
              by derived-table materialization); None otherwise.
    """

    error: Optional[str] = None
    rows: Optional[List[List[Any]]] = None
    rowcount: int = 0
    columns: Optional[List[ColumnDef]] = None


class EngineScope:
    """Column-less evaluation scope carrying engine + outer (for
    expression-only SELECTs like ``SELECT (SELECT 1)`` and LIMIT clauses)."""

    __slots__ = ("engine", "outer")

    def __init__(self, engine: Optional[Any], outer: Optional[Any] = None):
        self.engine = engine
        self.outer = outer

    def value_of(self, name: str) -> Any:
        if self.outer is not None:
            return self.outer.value_of(name)
        raise EngineError(f"no such column: {name}")

    def affinity_of(self, name: str) -> str:
        if self.outer is not None:
            return self.outer.affinity_of(name)
        return "NONE"

    def has_column(self, name: str) -> bool:
        return self.outer is not None and self.outer.has_column(name)


def _schema_ctx(tables: List[Table], merged_cols: Dict[str, List[List[int]]],
                outer: Optional[Any]) -> Optional[Any]:
    """Schema-only evaluation context (dummy rows) used during prepare-time
    validation of a correlated subquery: columns resolve against the table
    schemas, and lookups fall through to the enclosing query's schema."""
    if not tables:
        return outer
    dummy = [[None] * len(t.columns) for t in tables]
    return _frame_ctx(tables, merged_cols, dummy, None, outer)


def _expr_output_name(node: Tuple) -> str:
    """Render an expression to a column name, sqlite-style (used to name
    derived-table columns; the exact text is not referenceable without
    quoting, but must not equal a bare column name)."""
    k = node[0]
    if k == "num":
        return str(node[1])
    if k == "str":
        return "'" + node[1] + "'"
    if k == "null":
        return "NULL"
    if k == "col":
        return node[1].split(".")[-1]
    if k == "neg":
        return "-" + _expr_output_name(node[1])
    if k in ("add", "sub", "mul", "div", "mod"):
        op = {"add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%"}[k]
        return f"{_expr_output_name(node[1])}{op}{_expr_output_name(node[2])}"
    if k == "alias":
        return node[2]
    return "expr"


def _output_columns(items: List[Tuple], tables: List[Table],
                    colmaps: List[Dict[str, int]],
                    merged_cols: Dict[str, List[List[int]]]) -> List[ColumnDef]:
    """Column definitions of a select list's output, expanding ``*``/``t.*``
    and preserving column affinity (sqlite keeps a derived column's affinity
    through the subquery)."""
    ctx = _frame_ctx(tables, merged_cols, [[None] * len(t.columns) for t in tables])
    out: List[ColumnDef] = []
    for item in items:
        if item[0] == "star":
            for ti, cn in _expand_star(tables, colmaps, merged_cols):
                ci = colmaps[ti][cn]
                out.append(tables[ti].columns[ci])
        elif item[0] == "qualstar":
            tname = item[1]
            ti = next(i for i, t in enumerate(tables) if t.name == tname)
            out.extend(tables[ti].columns)
        elif item[0] == "alias":
            out.append(ColumnDef(name=item[2], affinity=expr_affinity(item[1], ctx)))
        elif item[0] == "col":
            out.append(ColumnDef(name=item[1].split(".")[-1], affinity=expr_affinity(item, ctx)))
        else:
            out.append(ColumnDef(name=_expr_output_name(item), affinity="NONE"))
    return out


def _alias_expr_map(items: List[Tuple]) -> Dict[str, Tuple]:
    """Output alias -> aliased expression for a select list."""
    return {item[2]: item[1] for item in items if item[0] == "alias"}


class Engine:
    """In-memory single-table SQL engine with sqlite-compatible semantics."""

    def __init__(self):
        self.tables: Dict[str, Table] = {}
        self.indexes: Dict[str, IndexDef] = {}

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
        if kind == "create_index":
            return self._run_create_index(stmt)
        if kind == "drop_index":
            return self._run_drop_index(stmt)
        raise EngineError(f"unknown statement {kind!r}")  # pragma: no cover

    def _run_create(self, stmt: Tuple) -> StatementResult:
        _, name, columns = stmt
        if name in self.tables:
            raise EngineError(f"table {name} already exists")
        self.tables[name] = Table(name=name, columns=columns)
        return StatementResult(rowcount=0)

    def _run_create_index(self, stmt: Tuple) -> StatementResult:
        _, name, table_name, columns, unique, if_not_exists = stmt
        table = self.tables.get(table_name)
        if table is None:
            raise EngineError(f"no such table: {table_name}")
        known = {c.name for c in table.columns}
        for col in columns:
            if col not in known:
                raise EngineError(f"no such column: {col}")
        if name in self.indexes:
            if if_not_exists:
                return StatementResult(rowcount=0)
            raise EngineError(f"index {name} already exists")
        self.indexes[name] = IndexDef(name=name, table=table_name, columns=columns, unique=unique)
        return StatementResult(rowcount=0)

    def _run_drop_index(self, stmt: Tuple) -> StatementResult:
        _, name, if_exists = stmt
        if name in self.indexes:
            del self.indexes[name]
            return StatementResult(rowcount=0)
        if if_exists:
            return StatementResult(rowcount=0)
        raise EngineError(f"no such index: {name}")

    def _run_insert(self, stmt: Tuple) -> StatementResult:
        _, name, columns, rows = stmt
        table = self.tables.get(name)
        if table is None:
            raise EngineError(f"no such table: {name}")
        ncols = len(table.columns)
        col_index = {c.name: i for i, c in enumerate(table.columns)}
        env = EngineScope(self)
        # Validate and convert every row before mutating the table, so an
        # error anywhere leaves the table untouched (statement atomicity).
        prepared: List[List[Any]] = []
        for exprs in rows:
            values = [eval_expr(e, env) for e in exprs]
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
            ctx = RowContext(table, row, self, None)
            if _truthy(eval_expr(where, ctx)):
                deleted += 1
            else:
                kept.append(row)
        table.rows = kept
        return StatementResult(rowcount=deleted)

    # -- FROM resolution -----------------------------------------------------

    def _resolve_from_sources(self, from_clause: List[Tuple]) -> Tuple:
        """Resolve FROM sources into (tables, steps, merged_cols, colmaps).

        Sources are ``('table', name, alias|None)`` or
        ``('derived', select_stmt, alias|None)``; join steps are
        ``('join', jtype, source, cond)``. Derived tables are materialized by
        executing their inner select (they cannot be correlated, matching
        sqlite), with output columns named after the select list.
        """
        tables: List[Table] = []
        steps: List[Tuple[str, int, Optional[Tuple]]] = []
        merged_cols: Dict[str, List[List[int]]] = {}
        using_pairs: Dict[str, List[Tuple[int, int]]] = {}
        derived_seq = 0

        def add_source(source: Tuple) -> Table:
            nonlocal derived_seq
            if source[0] == "table":
                name, alias = source[1], source[2]
                t = self.tables.get(name)
                if t is None:
                    raise EngineError(f"no such table: {name}")
                if alias is not None:
                    # sqlite hides the original table name once aliased; a
                    # shallow copy under the alias gives the same columns/rows.
                    return Table(name=alias, columns=t.columns, rows=t.rows)
                return t
            # derived table
            stmt, alias = source[1], source[2]
            res = self._run_select(stmt)  # outer=None: no correlation allowed
            if res.error is not None:
                raise EngineError(res.error)
            derived_seq += 1
            name = alias or f"__derived{derived_seq}__"
            return Table(name=name, columns=res.columns or [], rows=res.rows or [])

        for src in from_clause:
            if src[0] == "table":
                t = add_source(src)
                if any(existing.name == t.name for existing in tables):
                    raise EngineError(f"table {t.name} specified more than once")
                tables.append(t)
                if len(tables) > 1:
                    steps.append(("cross", len(tables) - 1, None))
            elif src[0] == "derived":
                t = add_source(src)
                if any(existing.name == t.name for existing in tables):
                    raise EngineError(f"table {t.name} specified more than once")
                tables.append(t)
                if len(tables) > 1:
                    steps.append(("cross", len(tables) - 1, None))
            else:  # ('join', jtype, source, cond)
                _jtype, source, cond = src[1], src[2], src[3]
                t = add_source(source)
                if any(existing.name == t.name for existing in tables):
                    raise EngineError(f"table {t.name} specified more than once")
                idx = len(tables)
                tables.append(t)
                if cond is not None and cond[0] == "using":
                    left_cols = {c.name for c in tables[idx - 1].columns}
                    right_cols = {c.name for c in tables[idx].columns}
                    for cn in cond[1]:
                        if cn not in left_cols or cn not in right_cols:
                            raise EngineError(
                                f"cannot join using column {cn} - column not "
                                f"present in both tables"
                            )
                        using_pairs.setdefault(cn, []).append((idx - 1, idx))
                steps.append((_jtype, idx, cond))
        merged_cols = _merged_components(using_pairs)
        colmaps = [{c.name: i for i, c in enumerate(t.columns)} for t in tables]
        return tables, steps, merged_cols, colmaps

    # -- prepare-time validation (shared by top-level and subqueries) ---------

    def _validate_select_clause(self, items: List[Tuple], where: Optional[Tuple],
                                group_by: Optional[List[Tuple]], having: Optional[Tuple],
                                order_by: Optional[List[Tuple[Tuple, bool]]],
                                steps: List[Tuple[str, int, Optional[Tuple]]],
                                tables: List[Table], colmaps: List[Dict[str, int]],
                                merged_cols: Dict[str, List[List[int]]],
                                outer: Optional[Any]) -> None:
        """Validate column references at prepare time (sqlite resolves columns
        when the statement is prepared, so an empty table must still reject
        bad columns). Correlated references resolve against the outer scope;
        subquery statements are validated recursively.

        ``outer`` is the schema context of the enclosing query (None at the
        top level); the schema context of this clause is passed on so nested
        subqueries can see these tables too.
        """
        schema = _schema_ctx(tables, merged_cols, outer)
        alias_map = _alias_expr_map(items)
        alias_names = set(alias_map)

        def alias_expr(name: str) -> Optional[Tuple]:
            return alias_map.get(name)

        def check_expr(e: Tuple, visible: int) -> None:
            for col in _walk_expr_cols(e):
                try:
                    _resolve_col(tables[:visible], colmaps[:visible], merged_cols, col)
                except EngineError as err:
                    if str(err).startswith("no such column") and _scope_has_column(schema, col):
                        continue
                    raise
            for sub in _walk_expr_subqueries(e):
                self._validate_select(sub, schema)

        def check_items(visible: int) -> None:
            for item in items:
                if item[0] == "qualstar":
                    if not any(t.name == item[1] for t in tables[:visible]):
                        raise EngineError(f"no such table: {item[1]}")
                elif item[0] == "star":
                    continue
                elif item[0] == "alias":
                    check_expr(item[1], visible)
                else:
                    check_expr(item, visible)

        check_items(len(tables))
        if where is not None:
            check_expr(where, len(tables))
        if group_by is not None:
            for g in group_by:
                # a bare column matching an output alias groups by that
                # expression (sqlite resolves GROUP BY output names first)
                if g[0] == "col" and alias_expr(g[1]) is not None:
                    check_expr(alias_expr(g[1]), len(tables))
                else:
                    check_expr(g, len(tables))
        if having is not None:
            check_expr(having, len(tables))
        if order_by is not None:
            for term, _asc in order_by:
                # ORDER BY output alias: no input column needs to exist
                if term[0] == "col" and term[1] in alias_names:
                    continue
                check_expr(term, len(tables))
        for _jtype, idx, cond in steps:
            if cond is not None and cond[0] == "on":
                check_expr(cond[1], idx + 1)

    def _validate_select(self, stmt: Tuple, outer: Optional[Any]) -> None:
        """Prepare-time validation of a (sub)query select statement."""
        _, _, items, from_clause, where, group_by, having, order_by, _limit = stmt
        if from_clause is None:
            tables, colmaps, merged_cols, steps = [], [], {}, []
        else:
            tables, steps, merged_cols, colmaps = self._resolve_from_sources(from_clause)
        self._validate_select_clause(items, where, group_by, having, order_by,
                                     steps, tables, colmaps, merged_cols, outer)

    def _run_select(self, stmt: Tuple, outer: Optional[Any] = None) -> StatementResult:
        _, distinct, items, from_clause, where, group_by, having, order_by, limit = stmt
        has_agg = (
            group_by is not None
            or having is not None
            or any(_contains_agg(it) for it in items)
            or bool(order_by and any(_contains_agg(t) for t, _a in order_by))
        )
        if from_clause is None:
            # expression-only SELECT (no FROM)
            tables: List[Table] = []
            colmaps: List[Dict[str, int]] = []
            merged_cols: Dict[str, List[List[int]]] = {}
            steps: List[Tuple[str, int, Optional[Tuple]]] = []
            if any(it[0] in ("star", "qualstar") for it in items):
                raise EngineError("SELECT * requires a FROM clause")
            self._validate_select_clause(items, where, group_by, having, order_by,
                                         steps, tables, colmaps, merged_cols, outer)
            if has_agg:
                # a single group containing one synthetic empty row
                agg_ctx = AggContext([], {}, [[]], None, self, outer)
                if having is not None and not _truthy(eval_expr(having, agg_ctx)):
                    proj: List[Tuple[List[Any], Optional[Any]]] = []
                else:
                    values = [eval_expr(e, agg_ctx) for e in items]
                    proj = [(values, agg_ctx)]
            else:
                env = EngineScope(self, outer)
                values = [eval_expr(e, env) for e in items]
                proj = [(values, env)]
        else:
            tables, steps, merged_cols, colmaps = self._resolve_from_sources(from_clause)

            # validate column references up front (sqlite resolves columns at
            # prepare time, so an empty table must still reject bad columns);
            # correlated references fall through to the outer scope.
            self._validate_select_clause(items, where, group_by, having, order_by,
                                         steps, tables, colmaps, merged_cols, outer)

            # nested-loop join. A combined row ("frame") is one list of values
            # per table, aligned with ``tables``. LEFT JOIN keeps every left
            # row that found no match, padded with NULLs for the right side.
            frames: List[List[List[Any]]] = [[row] for row in tables[0].rows]
            for jtype, idx, cond in steps:
                right = tables[idx]
                new_frames: List[List[List[Any]]] = []
                for frame in frames:
                    matched_any = False
                    for rrow in right.rows:
                        cand = frame + [rrow]
                        ctx = _frame_ctx(tables[:idx + 1], merged_cols, cand, self, outer)
                        ok = True
                        if cond is not None and cond[0] == "on":
                            ok = _truthy(eval_expr(cond[1], ctx))
                        elif cond is not None and cond[0] == "using":
                            for cn in cond[1]:
                                eq = (
                                    "cmp", "=",
                                    ("col", tables[idx - 1].name + "." + cn),
                                    ("col", tables[idx].name + "." + cn),
                                )
                                if not _truthy(eval_expr(eq, ctx)):
                                    ok = False
                                    break
                        if ok:
                            matched_any = True
                            new_frames.append(cand)
                    if jtype == "left" and not matched_any:
                        new_frames.append(frame + [[None] * len(right.columns)])
                frames = new_frames

            filtered: List[Tuple[List[List[Any]], Any]] = []
            for frame in frames:
                ctx = _frame_ctx(tables, merged_cols, frame, self, outer)
                if where is not None and not _truthy(eval_expr(where, ctx)):
                    continue
                filtered.append((frame, ctx))

            if has_agg:
                if group_by is not None:
                    groups: Dict[Tuple, List[List[List[Any]]]] = {}
                    key_order: List[Tuple] = []
                    alias_map = _alias_expr_map(items)
                    for frame, ctx in filtered:
                        key_vals = []
                        for g in group_by:
                            if g[0] == "col" and g[1] in alias_map:
                                key_vals.append(eval_expr(alias_map[g[1]], ctx))
                            else:
                                key_vals.append(eval_expr(g, ctx))
                        key = tuple(key_vals)
                        if key not in groups:
                            groups[key] = []
                            key_order.append(key)
                        groups[key].append(frame)
                    group_list = [(key, groups[key]) for key in key_order]
                else:
                    group_list = [(None, [frame for frame, _ctx in filtered])]
                proj = []
                for _key, group_rows in group_list:
                    first_frame = group_rows[0] if group_rows else [[] for _ in tables]
                    rep = _frame_ctx(tables, merged_cols, first_frame, self, outer)
                    agg_ctx = AggContext(tables, merged_cols, group_rows, rep, self, outer)
                    if having is not None and not _truthy(eval_expr(having, agg_ctx)):
                        continue
                    out = _project_items(items, tables, colmaps, merged_cols, first_frame, agg_ctx)
                    proj.append((out, agg_ctx))
            else:
                proj = []
                for frame, ctx in filtered:
                    out = _project_items(items, tables, colmaps, merged_cols, frame, ctx)
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
        # Integer literals are 1-based ordinals of the output row; a bare
        # column name that matches an output alias uses the projected value.
        # A stable multi-pass sort (rightmost term first) gives per-column
        # ASC/DESC.
        alias_index: Dict[str, int] = {}
        if order_by:
            width = _output_width(items, tables, colmaps, merged_cols)
            idx = 0
            for item in items:
                if item[0] == "star":
                    idx += len(_expand_star(tables, colmaps, merged_cols))
                elif item[0] == "qualstar":
                    tname = item[1]
                    ti = next(i for i, t in enumerate(tables) if t.name == tname)
                    idx += len(tables[ti].columns)
                elif item[0] == "alias":
                    alias_index[item[2]] = idx
                    idx += 1
                else:
                    idx += 1
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
                    proj, key=lambda p, t=term: _order_sort_key(t, p, alias_index),
                    reverse=not asc,
                )
        # LIMIT / OFFSET: applied after sorting.
        if limit is not None:
            env = EngineScope(self, outer)
            proj = _apply_limit(proj, limit, env)
        columns = _output_columns(items, tables, colmaps, merged_cols)
        return StatementResult(rows=[out for out, _ctx in proj], rowcount=len(proj),
                               columns=columns)
