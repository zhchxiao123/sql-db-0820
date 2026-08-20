# sql-db-0820

A sqlite-compatible database engine whose goal is to pass the full
sqllogictest corpus. Work in progress.

## Current slice: foundation

* **sqllogictest runner** (`sqldb/runner.py`) — parses sqllogictest protocol
  files (`statement` / `query` records, `----` result separator, plaintext and
  sqlite-style hash expectations), executes each record against the engine,
  aggregates pass/fail per file and exits non-zero when anything fails.
* **Minimal expression engine** (`sqldb/engine.py`) — parses and evaluates
  `SELECT <expr-list>` without a `FROM` clause: integer/real/text/NULL
  literals, `+ - * / %` with parentheses, comparisons, `AND`/`OR`/`NOT`,
  `CASE WHEN`, and sqlite-compatible NULL semantics (`NULL` in comparisons
  yields `NULL`, `IS NULL` / `IS NOT NULL` available).

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
tests/data/expressions.test: 25 passed, 0 failed, 0 skipped
TOTAL: 25 passed, 0 failed, 0 skipped
```

With `-v`, failing records are printed with expected vs. actual detail.
A malformed file (missing `----`, unreadable, illegal SQL) is judged as
failed records and the runner still processes the remaining file, exiting
with a non-zero code.

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
  yield `NULL` (`NULL IS NULL` is true).
* Statements outside the current scope (`FROM`, DDL, DML, ...) fail the
  record with a clear error instead of crashing the runner.
