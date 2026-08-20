# sql-db-0820

A sqlite-compatible database engine whose goal is to pass the full
sqllogictest corpus. Work in progress.

## Current capabilities

* **sqllogictest runner** (`sqldb/runner.py`) — parses sqllogictest protocol
  files (`statement` / `query` records, `----` result separator, plaintext and
  sqlite-style hash expectations), executes each record against a fresh
  engine per file, aggregates pass/fail per file and exits non-zero when
  anything fails.
* **Expression engine** (`sqldb/engine.py`) — `SELECT <expr-list>` without
  `FROM`: integer/real/text/NULL literals, `+ - * / %` with parentheses,
  comparisons, `AND`/`OR`/`NOT`, `CASE WHEN`, `IS NULL` / `IS NOT NULL`,
  sqlite-compatible NULL semantics.
* **Single-table storage** — `CREATE TABLE` (sqlite type affinity),
  `INSERT ... VALUES` (multi-row, optional column list), `DELETE` (with or
  without `WHERE`), `SELECT` with `FROM` and `WHERE` (equality, ranges,
  `AND`/`OR`, `IS NULL`, `LIKE` / `NOT LIKE` / `ESCAPE`, column references
  in expressions, `*`).
* **Ordering, dedup, slicing** — `ORDER BY` (multi-column lexicographic,
  per-column `ASC`/`DESC`, NULL smallest like sqlite, expressions and
  1-based output ordinals), `SELECT DISTINCT` (whole-row dedup; NULLs
  collapse, numbers compare numerically), `LIMIT n` / `LIMIT n OFFSET m`
  / `LIMIT o, c` (negative LIMIT = no limit, negative OFFSET = 0,
  sort-then-slice).

The runner CLI is the project's test seam — every later slice is accepted
through it.

## Usage

```bash
# Run a sqllogictest file (exit code 0 = all passed)
python sqllogictest_runner.py path/to/file.test
python -m sqldb.runner path/to/file.test          # same, as a module
sqllogictest-runner path/to/file.test             # after pip install -e .

# Run the unit test suite (stdlib unittest, no dependencies)
make test
python -m unittest discover -s tests -v
```

### Runner output

```
tests/data/select1.test: 26 passed, 0 failed, 0 skipped
TOTAL: 122 passed, 0 failed, 0 skipped
```

With `-v`, failing records are printed with expected vs. actual detail.
A malformed file (missing `----`, unreadable, illegal SQL) is judged as
failed records and the runner still processes the remaining file, exiting
with a non-zero code.

## Type affinity (sqlite semantics)

Declared column types map to affinities (`INT` -> INTEGER, `CHAR/CLOB/TEXT`
-> TEXT, `REAL/FLOA/DOUB` -> REAL, `BLOB`/empty -> NONE, otherwise NUMERIC).

* **Storage**: values are converted to the column affinity on insert — a
  REAL column stores `3` as `3.0` and `'4'` as `4.0`; a TEXT column stores
  `5` as `'5'`; an INTEGER column stores `'2'` as `2` but keeps `'abc'` as
  text.
* **Comparison**: sqlite's two affinity rules — an INTEGER/REAL/NUMERIC
  affinity operand converts the other operand numerically (text -> number
  when possible); a TEXT affinity operand converts a no-affinity operand to
  text. With no affinity involved, values compare by storage class: numbers
  numerically, text byte-wise, and numbers always sort before text
  (`SELECT 5 = '5';` is `0`).

## Design notes

* **Hash expectations**: `<N> values hashing to <md5>` is validated with the
  reference sqllogictest algorithm — MD5 over each rendered value followed by
  a single space, `NULL` rendered as `NULL` — and the value count `N` must
  match.
* **Output formatting** follows the sqlite3 CLI: REAL values use `%.15g` and
  always carry a `.` or exponent (`3.0`, `1e+20`); NULL prints as `NULL`.
* **Integer division** truncates toward zero (`7 / 2 = 3`, `-7 / 2 = -3`);
  division or modulo by zero yields `NULL` (no error), matching sqlite.
* **NULL semantics**: `NULL` in comparisons and arithmetic yields `NULL`;
  `AND`/`OR`/`NOT` follow sqlite's three-valued logic; `IS`/`IS NOT` never
  yield `NULL` (`NULL IS NULL` is true); `LIKE` with a NULL operand yields
  `NULL`.
* **LIKE** is ASCII case-insensitive by default; `%` matches any sequence,
  `_` matches one character, `ESCAPE` overrides the escape character.
* **ORDER BY / DISTINCT / LIMIT** follow sqlite: ORDER BY orders NULL
  smallest (ASC first / DESC last), then numbers, then text (byte-wise);
  integer ORDER BY terms are 1-based output ordinals (out-of-range is an
  error); DISTINCT collapses NULLs and compares numbers numerically
  (`5 == 5.0`, but `5 != '5'` without affinity); LIMIT/OFFSET evaluate to
  integers (NULL/float is a "datatype mismatch" error), negative LIMIT
  means no limit, negative OFFSET means 0, and ORDER BY runs before LIMIT.
* **Storage is in-memory** per engine instance; the runner gives each test
  file a fresh engine (like a fresh sqlite connection per file). Durability
  is out of scope for this slice.
* Statements outside the current scope (`FROM` joins, `UPDATE`, `DROP`, ...)
  fail the record with a clear error instead of crashing the runner.

## Test strategy

`tests/test_sqlite_parity.py` runs a battery of scenarios against the real
sqlite3 (Python stdlib) and asserts our engine's rendered output matches
line by line — this pins the affinity/comparison/LIKE semantics to actual
sqlite behavior.
