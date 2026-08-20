"""Overall acceptance for sql-db-0820 (consolidation seam).

Chains the parent requirement's acceptance criteria into one long-term
regression asset:

  a1  correctness — the bundled success corpus passes with 0 failed records
      and the runner CLI exits 0;
  a2  performance — engine total wall-clock median <= sqlite3 baseline median
      (reuses benchmark.py; the "comparable" threshold is a parameter, the
      requester-confirmed written threshold wins);
  a4  failure path — failure-path corpus files still fail (the a4 seam is
      preserved), and missing corpus / baseline failure / engine crash exits
      non-zero with diagnostics.

Exit codes:
  0  a1 + a4 pass and a2 passes (engine <= baseline * threshold)
  1  a2 fails (engine slower than baseline * threshold)
  2  a1/a4 fail or the script itself errors (missing corpus, engine crash)

Usage::

    python acceptance.py [--runs N] [--warmup N] [--threshold R]

The default threshold is 1.0 (engine not slower than sqlite3). Pass
``--threshold R`` with the requester-confirmed "comparable" ratio when one is
issued (待决问题②).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from sqldb.engine import Engine
from sqldb.runner import run_file

REPO_ROOT = Path(__file__).resolve().parent
DATA = REPO_ROOT / "tests" / "data"

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

FAILURE_PATH_CORPUS = [
    "agg_failures.test",
    "failures.test",
    "index_failures.test",
    "join_failures.test",
    "order_failures.test",
    "malformed.test",
]


def run_corpus(files: List[Path]) -> int:
    """Run the files through the engine; returns total failed records."""
    total_failed = 0
    for path in files:
        records, issues = _parse(path)
        engine = Engine()
        failed = 0
        for rec in records:
            rr = _run_record(rec, engine)
            if rr.skipped:
                continue
            if not rr.passed:
                failed += 1
        failed += len(issues)
        total_failed += failed
        print(f"  {path.name}: {failed} failed")
    return total_failed


def _parse(path: Path):
    from sqldb.runner import parse_sqllogictest
    return parse_sqllogictest(path.read_text(encoding="utf-8"))


def _run_record(rec, engine):
    from sqldb.runner import run_record
    return run_record(rec, engine)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="acceptance",
        description="Overall acceptance for sql-db-0820 (a1 correctness + "
                    "a2 performance + a4 failure paths).",
    )
    parser.add_argument("--runs", type=int, default=7, help="benchmark timed passes")
    parser.add_argument("--warmup", type=int, default=2, help="benchmark warm-up passes")
    parser.add_argument("--threshold", type=float, default=1.0,
                        help="max allowed engine/sqlite3 median ratio (a2; "
                             "the requester-confirmed 'comparable' threshold)")
    args = parser.parse_args(argv)

    print("== a1: success corpus green ==")
    success_files = [DATA / n for n in SUCCESS_CORPUS]
    missing = [f for f in success_files if not f.is_file()]
    if missing:
        print(f"acceptance: missing corpus file(s): {missing}", file=sys.stderr)
        return 2
    try:
        failed = run_corpus(success_files)
    except Exception as e:  # noqa: BLE001 - never fake a pass
        print(f"acceptance: engine crashed on success corpus: {e!r}",
              file=sys.stderr)
        return 2
    if failed != 0:
        print(f"acceptance: a1 FAIL — {failed} failed record(s) in success corpus",
              file=sys.stderr)
        return 2
    print("a1: PASS (0 failed)")

    print("== a4: failure-path seam preserved ==")
    fail_files = [DATA / n for n in FAILURE_PATH_CORPUS]
    fail_failed = run_corpus(fail_files)
    # the failure-path corpus must keep failing (a4 seam), so a zero failure
    # count here means the seam is broken.
    if fail_failed == 0:
        print("acceptance: a4 FAIL — failure-path corpus unexpectedly all green",
              file=sys.stderr)
        return 2
    print(f"a4: PASS ({fail_failed} intentional failure(s) preserved)")

    print("== a2: engine vs sqlite3 baseline (median) ==")
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "benchmark.py"),
         "--runs", str(args.runs), "--warmup", str(args.warmup),
         "--threshold", str(args.threshold), "--strict"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")
    if proc.returncode == 2:
        print("acceptance: benchmark failed to run", file=sys.stderr)
        return 2
    if proc.returncode == 1:
        print(f"acceptance: a2 FAIL — engine slower than "
              f"{args.threshold:g}x sqlite3 baseline", file=sys.stderr)
        return 1
    print(f"acceptance: a2 PASS (engine <= {args.threshold:g}x baseline)")

    print("OVERALL: PASS")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
