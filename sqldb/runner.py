"""sqllogictest protocol runner for sql-db-0820.

Implements the sqllogictest file protocol used by the SQLite test corpus:

  * record headers: ``statement ok|error|count N`` and ``query <types> [sort]``
  * ``----`` separator between a query's SQL and its expected results
  * expected results as plain text (one value per line, ``NULL`` for null)
    or as a sqlite-style hash line: ``<N> values hashing to <md5>``
  * per-file pass/fail aggregation and a non-zero exit code when any record
    fails (malformed files, illegal SQL and hash mismatches included)

The runner is the test seam for the whole project: every later slice of the
sqlite-compatible engine is accepted through this CLI.

Hash rule (matches the reference sqllogictest C tool): the MD5 is computed
over the concatenation of every rendered result value followed by a single
space; NULL values are rendered as the three characters ``NULL``.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .engine import Engine, render_value

# Database type this engine claims, used for skipif/onlyif directives.
# sql-db-0820 aims to be sqlite-compatible, so corpus records that sqlite
# skips are skipped here too.
DB_TYPE = "sqlite"

_HASH_RE = re.compile(r"^(\d+) values hashing to ([0-9a-fA-F]{32})$")
_QUERY_TYPES = set("TIR")


# ---------------------------------------------------------------------------
# Protocol model & parser
# ---------------------------------------------------------------------------

@dataclass
class Record:
    kind: str = ""                 # 'statement' | 'query'
    mode: str = ""                 # 'ok' | 'error' | 'count' (statements)
    types: str = ""                # query type string
    sort_mode: str = "nosort"      # 'nosort' | 'rowsort' | 'valuesort'
    sql: str = ""
    expect_error_substr: Optional[str] = None
    expect_count: Optional[int] = None
    expect_values: Optional[List[str]] = None
    expect_hash_count: Optional[int] = None
    expect_hash: Optional[str] = None
    skipped: bool = False
    parse_error: Optional[str] = None


@dataclass
class ParseIssue:
    line: int
    message: str


def parse_sqllogictest(text: str) -> Tuple[List[Record], List[ParseIssue]]:
    """Parse a sqllogictest file into records plus non-fatal parse issues.

    Malformed constructs are reported as issues and, where possible, also
    surface as failed records so the caller can keep going and exit non-zero.
    """
    lines = text.splitlines()
    records: List[Record] = []
    issues: List[ParseIssue] = []
    i = 0
    n = len(lines)
    skip_next = False

    def skip_to_blank() -> None:
        nonlocal i
        i += 1
        while i < n and lines[i].strip() != "":
            i += 1

    while i < n:
        line = lines[i]
        stripped = line.strip()
        lineno = i + 1

        if stripped == "" or stripped.startswith("#"):
            i += 1
            continue

        # Directives. skipif/onlyif mark the next record as skipped.
        dm = re.match(r"^(hash-threshold|skipif|onlyif|mode|testname)\b(.*)$", stripped)
        if dm:
            directive, rest = dm.group(1), dm.group(2).strip()
            if directive in ("skipif", "onlyif"):
                matched = DB_TYPE in rest.split()
                skip_next = (directive == "skipif" and matched) or (
                    directive == "onlyif" and not matched
                )
            # hash-threshold / mode / testname are informational for running;
            # they affect how the reference tool *generates* files, not how we
            # validate them, so they are ignored here.
            i += 1
            continue

        if stripped.startswith("statement"):
            parts = stripped.split(None, 1)
            rest = parts[1].strip() if len(parts) > 1 else ""
            mode_parts = rest.split(None, 1)
            mode = mode_parts[0]
            arg = mode_parts[1].strip() if len(mode_parts) > 1 else ""
            if mode not in ("ok", "error", "count"):
                issues.append(ParseIssue(lineno, f"unknown statement mode {mode!r}"))
                skip_to_blank()
                continue
            sql_lines: List[str] = []
            i += 1
            while i < n and lines[i].strip() != "":
                sql_lines.append(lines[i])
                i += 1
            rec = Record(kind="statement", mode=mode, sql="\n".join(sql_lines).strip())
            if mode == "error" and arg:
                rec.expect_error_substr = arg
            if mode == "count":
                if arg.isdigit():
                    rec.expect_count = int(arg)
                else:
                    issues.append(ParseIssue(lineno, f"invalid statement count {arg!r}"))
            if skip_next:
                rec.skipped = True
                skip_next = False
            records.append(rec)
            continue

        if stripped.startswith("query"):
            parts = stripped.split(None, 2)
            types = parts[1].strip() if len(parts) > 1 else ""
            rest = parts[2].strip() if len(parts) > 2 else ""
            rest_parts = rest.split()
            sort_mode = "nosort"
            label = ""
            if rest_parts and rest_parts[0] in ("nosort", "rowsort", "valuesort"):
                sort_mode = rest_parts[0]
                rest_parts = rest_parts[1:]
            label = " ".join(rest_parts)
            if types and not set(types) <= _QUERY_TYPES:
                issues.append(
                    ParseIssue(lineno, f"query type string {types!r} contains unknown type char")
                )
            sql_lines = []
            i += 1
            while i < n and lines[i].strip() != "----":
                sql_lines.append(lines[i])
                i += 1
            sql = "\n".join(sql_lines).strip()
            rec = Record(kind="query", types=types, sort_mode=sort_mode, sql=sql)
            if i >= n:
                rec.parse_error = "query record missing '----' separator"
                issues.append(ParseIssue(lineno, rec.parse_error))
                if skip_next:
                    rec.skipped = True
                    skip_next = False
                records.append(rec)
                break
            i += 1  # skip the '----' separator
            exp_lines: List[str] = []
            while i < n and lines[i].strip() != "":
                exp_lines.append(lines[i].strip())
                i += 1
            if len(exp_lines) == 1:
                hm = _HASH_RE.match(exp_lines[0])
                if hm:
                    rec.expect_hash_count = int(hm.group(1))
                    rec.expect_hash = hm.group(2).lower()
            if rec.expect_hash is None:
                rec.expect_values = exp_lines
            if skip_next:
                rec.skipped = True
                skip_next = False
            records.append(rec)
            continue

        # Anything else at record level is malformed: report and skip the
        # offending block so the rest of the file can still run.
        issues.append(ParseIssue(lineno, f"unexpected line {stripped!r}"))
        skip_to_blank()
        continue

    return records, issues


# ---------------------------------------------------------------------------
# Hash
# ---------------------------------------------------------------------------

def compute_hash(rendered_values: List[str]) -> str:
    """sqlite-style result hash: MD5 of each value followed by a space."""
    data = "".join(v + " " for v in rendered_values).encode("utf-8")
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _flatten_and_sort(rows: List[List[object]], sort_mode: str) -> List[str]:
    """Flatten result rows to a value list, applying the query's sort mode
    on the rendered text (as the reference tool does)."""
    if sort_mode == "nosort":
        values = [v for row in rows for v in row]
    elif sort_mode == "rowsort":
        rows = sorted(rows, key=lambda r: [render_value(v) for v in r])
        values = [v for row in rows for v in row]
    elif sort_mode == "valuesort":
        values = sorted((v for row in rows for v in row), key=render_value)
    else:  # pragma: no cover - guarded by the parser
        values = [v for row in rows for v in row]
    return [render_value(v) for v in values]


@dataclass
class RecordResult:
    passed: bool
    skipped: bool = False
    detail: str = ""


def run_record(rec: Record, engine: Engine) -> RecordResult:
    if rec.skipped:
        return RecordResult(passed=True, skipped=True)
    if rec.parse_error:
        return RecordResult(passed=False, detail=rec.parse_error)
    if not rec.sql:
        return RecordResult(passed=False, detail="empty SQL body")

    result = engine.execute(rec.sql)

    if rec.kind == "statement":
        if rec.mode == "ok":
            if result.error is not None:
                return RecordResult(passed=False, detail=f"statement error: {result.error}")
            return RecordResult(passed=True)
        if rec.mode == "error":
            if result.error is None:
                return RecordResult(passed=False, detail="expected error, but statement succeeded")
            if rec.expect_error_substr and rec.expect_error_substr not in result.error:
                return RecordResult(
                    passed=False,
                    detail=f"error message {result.error!r} does not contain "
                           f"{rec.expect_error_substr!r}",
                )
            return RecordResult(passed=True)
        if rec.mode == "count":
            if result.error is not None:
                return RecordResult(passed=False, detail=f"statement error: {result.error}")
            if result.rowcount != rec.expect_count:
                return RecordResult(
                    passed=False,
                    detail=f"expected {rec.expect_count} rows affected, got {result.rowcount}",
                )
            return RecordResult(passed=True)
        return RecordResult(passed=False, detail=f"unknown statement mode {rec.mode!r}")

    # query
    if result.error is not None:
        return RecordResult(passed=False, detail=f"query error: {result.error}")
    actual = _flatten_and_sort(result.rows or [], rec.sort_mode)

    if rec.expect_hash is not None:
        if len(actual) != rec.expect_hash_count:
            return RecordResult(
                passed=False,
                detail=f"hash count mismatch: expected {rec.expect_hash_count} values, "
                       f"got {len(actual)}",
            )
        actual_hash = compute_hash(actual)
        if actual_hash != rec.expect_hash:
            return RecordResult(
                passed=False,
                detail=f"hash mismatch: expected {rec.expect_hash}, got {actual_hash}",
            )
        return RecordResult(passed=True)

    expected = rec.expect_values or []
    if actual != expected:
        return RecordResult(
            passed=False,
            detail=f"result mismatch: expected {expected!r}, got {actual!r}",
        )
    return RecordResult(passed=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_file(path: Path, engine: Engine, verbose: bool) -> Tuple[int, int, int]:
    """Run one .test file; returns (passed, failed, skipped)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"{path}: ERROR cannot read file: {e}", file=sys.stderr)
        return 0, 1, 0

    records, issues = parse_sqllogictest(text)
    passed = failed = skipped = 0
    for idx, rec in enumerate(records, start=1):
        rr = run_record(rec, engine)
        if rr.skipped:
            skipped += 1
        elif rr.passed:
            passed += 1
        else:
            failed += 1
            if verbose:
                head = f"{rec.kind} {rec.mode or rec.types}".strip()
                print(f"  FAIL record #{idx} ({head}): {rr.detail}")
    for issue in issues:
        failed += 1
        if verbose:
            print(f"  FAIL parse issue at line {issue.line}: {issue.message}")
    print(f"{path}: {passed} passed, {failed} failed, {skipped} skipped")
    return passed, failed, skipped


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqllogictest-runner",
        description="Run sqllogictest protocol files against the sql-db-0820 engine.",
    )
    parser.add_argument("files", nargs="+", metavar="FILE", help="sqllogictest .test file(s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print failing records")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress per-file summaries")
    args = parser.parse_args(argv)

    engine = Engine()
    total_passed = total_failed = total_skipped = 0
    for fname in args.files:
        p, f, s = run_file(Path(fname), engine, verbose=args.verbose)
        total_passed += p
        total_failed += f
        total_skipped += s
    if not args.quiet:
        print(f"TOTAL: {total_passed} passed, {total_failed} failed, {total_skipped} skipped")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
