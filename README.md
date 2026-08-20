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
* **Indexes** — `CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON t (col, ...)`
  and `DROP INDEX [IF EXISTS] name` (single and multi-column, per-column
  `ASC`/`DESC`). Correctness-first: indexes are validated and recorded but
  do not change query results (lookup stays a full scan until the
  performance slice); duplicate creation errors per sqlite.
* **Aggregation** — `COUNT` / `SUM` / `AVG` / `MIN` / `MAX` / `TOTAL`
  (incl. `COUNT(*)`, `COUNT()`, `COUNT(DISTINCT x)`), `GROUP BY`
  (multi-column, NULL keys group together), `HAVING` (aggregates and group
  keys; whole table is one group without GROUP BY). sqlite semantics: SUM/
  AVG of an empty/all-NULL group is NULL, COUNT is 0, TOTAL always returns
  a number, MIN/MAX ignore NULL, text converts to number in SUM/AVG/TOTAL
  (non-numeric text counts as 0).
* **Multi-table joins** — `INNER JOIN` / `LEFT [OUTER] JOIN` / `CROSS JOIN`
  and comma-separated multi-table `FROM`, with `ON` (any expression, reusing
  the expression engine) and `USING (col, ...)` conditions; qualified column
  references (`t.c`) and qualified star (`t.*`). sqlite semantics: NULL never
  matches in a join condition (not even NULL = NULL), `LEFT JOIN` pads
  unmatched rows with NULL, `USING` columns are merged for `*` (emitted once,
  at the leftmost position) and for unqualified references (resolved to the
  leftmost table of the merged component; a chain of `USING` merges across
  all tables). Ambiguous unqualified columns, unknown tables/columns (at
  prepare time, even on empty tables) and unparseable join syntax fail
  cleanly. RIGHT/FULL/NATURAL joins are not implemented yet (rejected with a
  clean error).
* **Table aliases** — `FROM t AS x` / `FROM t x` and `JOIN ... AS x`. Once
  aliased, only the alias is visible for qualified references (sqlite
  behavior). Required for correlated self-references.
* **Subqueries** — scalar subqueries in expressions (`(SELECT ...)`, 0 rows
  -> NULL, multi-column -> ``sub-select returns N columns`` error),
  `IN`/`NOT IN (SELECT ...)` and `EXISTS (SELECT ...)` with sqlite's
  three-valued NULL logic (NULL left operand -> NULL when the set is
  non-empty, else 0; no match with NULL in the set -> NULL), correlated
  subqueries (inner scope wins, outer columns fall through; arbitrary
  nesting), and `FROM (SELECT ...) [AS] d` derived tables (multi-level
  nesting, materialized once, output columns named after the select list
  with affinity preserved, cannot reference outer columns like sqlite).
  Select-list aliases (`expr AS name`) are usable in `ORDER BY` / `GROUP BY`
  (output name wins, sqlite behavior). Subquery failures (missing table or
  column, multi-column scalar/IN, unparseable SQL) fail the statement
  cleanly instead of crashing.

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
  error); a bare column name matching an output alias uses the projected
  value (alias wins, sqlite behavior); DISTINCT collapses NULLs and compares
  numbers numerically (`5 == 5.0`, but `5 != '5'` without affinity);
  LIMIT/OFFSET evaluate to integers (NULL/float is a "datatype mismatch"
  error), negative LIMIT means no limit, negative OFFSET means 0, and ORDER
  BY runs before LIMIT.
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

### Bundled sqllogictest corpus

The official sqllogictest corpus is not vendored in this workspace; the
acceptance corpus is the one bundled under `tests/data/`:

* **success corpus** (`aggregates` `delete` `distinct` `expressions`
  `groupby` `hash` `index` `joins` `limit` `orderby` `select1` `statements`
  `subquery`) — every record passes, no records are skipped:
  `python sqllogictest_runner.py tests/data/aggregates.test ... tests/data/subquery.test`
  exits 0.
* **failure-path corpus** (`*_failures.test`, `malformed.test`) — records are
  *expected* to fail: they verify unsupported statements are judged as failed
  records, the runner keeps going, and the CLI exits non-zero.

Running the success corpus twice yields identical output (deterministic; the
engine uses no randomness). `tests/test_runner.py` pins these properties:
all success records green with zero skips, CLI exit 0, and byte-identical
output across two runs.

### Benchmark (slice 8)

`benchmark.py` (beside the runner) measures the engine's total wall-clock
against a sqlite3 baseline on the same corpus:

* both sides reuse the same `parse_sqllogictest` parser and `run_record`
  validation — only the execution backend differs (sql-db-0820 Engine vs the
  stdlib `sqlite3` module, SQLite 3.46.1);
* methodology: one warm-up pass per side, then `--runs` timed passes; the
  reported figure is the median wall-clock (a1 reproducible);
* exit codes: 0 = measured (and `--strict` gates engine ≤ sqlite3), 1 =
  engine slower with `--strict`, 2 = corpus/sqlite3/engine/timing failure.

```
python benchmark.py --runs 7 --warmup 1
```

The official sqlite3 CLI is not installed in this sandbox (and the tool
boundary forbids fetching it), so the baseline is the Python `sqlite3` module
— the same SQLite library the parity harness uses as ground truth.

### Overall acceptance (consolidation)

`acceptance.py` chains the parent requirement's acceptance criteria into one
long-term regression asset:

* **a1 correctness** — the bundled success corpus runs green (0 failed) via
  the engine, exit non-zero otherwise;
* **a4 failure path** — the failure-path corpus still fails (the seam is
  preserved), missing corpus / engine crash / baseline failure exit non-zero
  with diagnostics;
* **a2 performance** — reuses `benchmark.py`; exit 0 only when the engine
  median ≤ sqlite3 baseline median × `--threshold` (default 1.0; pass the
  requester-confirmed "comparable" threshold when one is issued).

```
python acceptance.py --runs 7 --warmup 2            # strict (engine <= sqlite3)
python acceptance.py --threshold 1.0 --runs 7       # same, explicit
```
