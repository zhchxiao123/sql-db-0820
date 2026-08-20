"""Benchmark: sql-db-0820 engine wall-clock vs sqlite3 baseline.

Slice-8 acceptance seam. Runs the same sqllogictest corpus through two
execution backends and compares total wall-clock time:

  * ``engine`` — the sql-db-0820 Engine (pure Python, in-memory)
  * ``sqlite3`` — the stdlib sqlite3 module (SQLite 3.46.1), the project's
    parity ground truth (the official sqlite3 CLI is not installed in this
    sandbox and the tool boundary forbids fetching it)

Fairness (acceptance a1): both sides parse the corpus with the *same*
``parse_sqllogictest`` parser, execute each record through an adapter that
implements the same ``execute() -> StatementResult`` contract, and validate
with the *same* ``run_record`` logic. The only difference is which engine
executes the SQL. Each file runs against a fresh engine/connection (like the
runner CLI and the reference sqllogictest tool).

Methodology: one warm-up pass per side (discarded), then ``--runs`` timed
passes; the reported figure is the *median* total wall-clock per side
(``time.perf_counter`` around the whole corpus, matching the "total wall
clock" acceptance wording). Exit code:

  * 0 — measurement completed and engine median <= sqlite3 median (default)
  * 0 — measurement completed, engine slower, but --strict not given
  * 1 — engine slower and --strict given
  * 2 — error (missing file, engine failure, ...)

Usage::

    python benchmark.py [--runs N] [--warmup N] [--strict] [FILES...]

Default corpus: the bundled success corpus (tests/data/*.test minus the
failure-path files, which are *designed* to fail and are not a throughput
load). Pass explicit files to benchmark anything else.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from sqldb.engine import Engine, StatementResult
from sqldb.runner import parse_sqllogictest, run_record

REPO_ROOT = Path(__file__).resolve().parent
DATA = REPO_ROOT / "tests" / "data"

# The success corpus: every record is expected to pass (slice-7 acceptance).
# The *_failures.test and malformed.test files are deliberately failing
# (failure-path seam), so they are not a throughput load.
SUCCESS_CORPUS = [
    "aggregates.test",
    "delete.test",
    "distinct.test",
    "expressions.test",
    "groupby.test",
    "hash.test",
    "index.test",
    "joins.test",
    "limit.test",
    "orderby.test",
    "select1.test",
    "statements.test",
    "subquery.test",
]


# ---------------------------------------------------------------------------
# sqlite3 backend adapter: same execute() contract as Engine
# ---------------------------------------------------------------------------

class SqliteBackend:
    """Executes SQL against an in-memory sqlite3 connection.

    Implements the same ``execute(sql) -> StatementResult`` contract as
    ``Engine`` so ``run_record`` validates both sides identically.
    """

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")

    def execute(self, sql: str) -> StatementResult:
        try:
            cur = self.conn.execute(sql)
        except sqlite3.Error as e:
            return StatementResult(error=str(e))
        if cur.description is None:
            return StatementResult(error=None, rows=None, rowcount=cur.rowcount)
        rows = [list(r) for r in cur.fetchall()]
        return StatementResult(error=None, rows=rows, rowcount=len(rows))

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# Corpus runner shared by both sides (mirrors sqldb.runner.run_file but takes
# a factory so the engine object is created per file, as the CLI does)
# ---------------------------------------------------------------------------

def run_corpus(files: List[Path], make_engine: Callable[[], object],
               label: str) -> Tuple[int, int, int]:
    """Run every record of every file; returns (passed, failed, skipped).

    One engine per file (same isolation as the runner CLI / reference tool).
    """
    total_passed = total_failed = total_skipped = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        records, issues = parse_sqllogictest(text)
        engine = make_engine()
        passed = failed = skipped = 0
        for rec in records:
            rr = run_record(rec, engine)  # type: ignore[arg-type]
            if rr.skipped:
                skipped += 1
            elif rr.passed:
                passed += 1
            else:
                failed += 1
        failed += len(issues)
        total_passed += passed
        total_failed += failed
        total_skipped += skipped
    return total_passed, total_failed, total_skipped


def time_one_pass(files: List[Path], make_engine: Callable[[], object]) -> float:
    """Wall-clock of one full-corpus pass, in seconds."""
    start = time.perf_counter()
    run_corpus(files, make_engine, label="")
    return time.perf_counter() - start


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmark",
        description="Benchmark sql-db-0820 engine vs sqlite3 on the same corpus.",
    )
    parser.add_argument("files", nargs="*", metavar="FILE", help="corpus files "
                        "(default: bundled success corpus)")
    parser.add_argument("--runs", type=int, default=7, help="timed passes per side")
    parser.add_argument("--warmup", type=int, default=1, help="warm-up passes per side")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when engine is slower than sqlite3")
    args = parser.parse_args(argv)

    # a4 failure path: sqlite3 baseline unavailable -> non-zero + diagnostic.
    if sqlite3 is None:  # pragma: no cover - import-level guard
        print("benchmark: sqlite3 module unavailable — baseline cannot be "
              "established", file=sys.stderr)
        return 2

    files = [Path(f) for f in args.files] if args.files else [DATA / n for n in SUCCESS_CORPUS]
    for f in files:
        if not f.is_file():
            print(f"benchmark: no such file: {f}", file=sys.stderr)
            return 2

    def engine_factory() -> Engine:
        return Engine()

    def sqlite_factory() -> SqliteBackend:
        return SqliteBackend()

    # a4 failure path: engine crash/hang must surface, not fabricate numbers.
    try:
        ep, ef, es = run_corpus(files, engine_factory, label="engine")
    except Exception as e:  # noqa: BLE001 - the benchmark must never fake a pass
        print(f"benchmark: engine crashed while running the corpus: {e!r}",
              file=sys.stderr)
        return 2
    if ef != 0:
        print(f"benchmark: engine failed {ef} record(s) on the corpus — not "
              f"benchmarking a broken engine", file=sys.stderr)
        return 2
    try:
        sp, sf, ss = run_corpus(files, sqlite_factory, label="sqlite3")
    except Exception as e:  # noqa: BLE001
        print(f"benchmark: sqlite3 crashed while running the corpus: {e!r}",
              file=sys.stderr)
        return 2

    # Warm-up passes (discarded).
    for _ in range(max(0, args.warmup)):
        time_one_pass(files, engine_factory)
        time_one_pass(files, sqlite_factory)

    # Timed passes.
    engine_times: List[float] = []
    sqlite_times: List[float] = []
    for _ in range(max(1, args.runs)):
        engine_times.append(time_one_pass(files, engine_factory))
        sqlite_times.append(time_one_pass(files, sqlite_factory))

    engine_med = statistics.median(engine_times)
    sqlite_med = statistics.median(sqlite_times)
    ratio = engine_med / sqlite_med if sqlite_med > 0 else float("inf")

    # a4 failure path: timing anomaly (e.g. all zero) must not silently pass.
    if engine_med <= 0 or sqlite_med <= 0:
        print("benchmark: timing anomaly (non-positive median) — refusing to "
              "report", file=sys.stderr)
        return 2

    print(f"corpus: {len(files)} file(s), engine {ep}/{ef + ep}/{es} "
          f"passed/failed/skipped, sqlite3 {sp}/{sf + sp}/{ss}")
    print(f"engine : median={engine_med * 1000:.2f} ms  "
          f"runs={[f'{t * 1000:.1f}' for t in engine_times]}")
    print(f"sqlite3: median={sqlite_med * 1000:.2f} ms  "
          f"runs={[f'{t * 1000:.1f}' for t in sqlite_times]}")
    print(f"ratio engine/sqlite3 = {ratio:.3f}")

    ok = engine_med <= sqlite_med
    print(f"RESULT: {'PASS' if ok else 'FAIL'} "
          f"(engine {'<=' if ok else '>'} sqlite3 median)")
    if not ok and args.strict:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
