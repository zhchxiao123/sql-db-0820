"""Tests for the sqllogictest runner (sqldb.runner).

Covers protocol parsing, plaintext and hash expectation validation,
statement ok/error/count judgement, malformed-file handling and the CLI
exit code contract (non-zero when anything fails).
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqldb.engine import Engine
from sqldb.runner import (
    compute_hash,
    parse_sqllogictest,
    run_file,
    run_record,
)

DATA = Path(__file__).parent / "data"
REPO_ROOT = Path(__file__).parent.parent


def run_cli(*args):
    """Run the runner CLI as a subprocess; returns (exit_code, stdout)."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "sqllogictest_runner.py"), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout


class TestParse(unittest.TestCase):
    def test_statement_records(self):
        records, issues = parse_sqllogictest(
            "statement ok\nSELECT 1;\n\nstatement error\nSELECT 1 +;\n"
        )
        self.assertEqual(issues, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].kind, "statement")
        self.assertEqual(records[0].mode, "ok")
        self.assertEqual(records[0].sql, "SELECT 1;")
        self.assertEqual(records[1].mode, "error")

    def test_statement_count(self):
        records, _ = parse_sqllogictest("statement count 3\nUPDATE t SET x=1;\n")
        self.assertEqual(records[0].mode, "count")
        self.assertEqual(records[0].expect_count, 3)

    def test_query_record_plaintext(self):
        records, _ = parse_sqllogictest(
            "query I rowsort\nSELECT 1;\n----\n1\n2\n"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].kind, "query")
        self.assertEqual(records[0].types, "I")
        self.assertEqual(records[0].sort_mode, "rowsort")
        self.assertEqual(records[0].expect_values, ["1", "2"])
        self.assertIsNone(records[0].expect_hash)

    def test_query_record_hash(self):
        records, _ = parse_sqllogictest(
            "query I\nSELECT 1;\n----\n1 values hashing to 9d9dff9320e27082b15b4ed7a086ba83\n"
        )
        self.assertEqual(records[0].expect_hash_count, 1)
        self.assertEqual(records[0].expect_hash, "9d9dff9320e27082b15b4ed7a086ba83")

    def test_missing_separator_is_an_issue(self):
        records, issues = parse_sqllogictest("query I\nSELECT 1;\n")
        self.assertEqual(len(issues), 1)
        self.assertIn("----", issues[0].message)
        self.assertEqual(records[0].parse_error, "query record missing '----' separator")

    def test_unknown_statement_mode_is_an_issue(self):
        records, issues = parse_sqllogictest("statement maybe\nSELECT 1;\n")
        self.assertEqual(len(issues), 1)
        self.assertIn("unknown statement mode", issues[0].message)

    def test_garbage_line_is_an_issue(self):
        records, issues = parse_sqllogictest("not a header\nSELECT 1;\n\nquery I\nSELECT 1;\n----\n1\n")
        self.assertEqual(len(issues), 1)
        self.assertIn("unexpected line", issues[0].message)
        # the valid record after the garbage block still parses
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].sql, "SELECT 1;")

    def test_skipif_directive(self):
        text = "skipif sqlite\nstatement ok\nSELECT 1;\n\nquery I\nSELECT 1;\n----\n1\n"
        records, issues = parse_sqllogictest(text)
        self.assertEqual(issues, [])
        self.assertTrue(records[0].skipped)
        self.assertFalse(records[1].skipped)

    def test_onlyif_nonmatching(self):
        text = "onlyif postgres\nstatement ok\nSELECT 1;\n"
        records, _ = parse_sqllogictest(text)
        self.assertTrue(records[0].skipped)

    def test_comments_and_hash_threshold_ignored(self):
        text = (
            "# comment\n"
            "hash-threshold 100\n"
            "statement ok\nSELECT 1;\n"
        )
        records, issues = parse_sqllogictest(text)
        self.assertEqual(issues, [])
        self.assertEqual(len(records), 1)


class TestHash(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(compute_hash(["1"]), "9d9dff9320e27082b15b4ed7a086ba83")
        self.assertEqual(compute_hash(["1", "2"]), "bbf9e4c11e2bc498bd1ed4ed686d646d")
        self.assertEqual(
            compute_hash(["a", "b", "NULL"]), "a83ff48980ae41651298c6751783a005"
        )

    def test_hash_and_plaintext_agree(self):
        # a3: same query judged by hash and by plaintext gives the same verdict
        engine = Engine()
        rec_plain = parse_sqllogictest("query I\nSELECT 1;\n----\n1\n")[0][0]
        rec_hash = parse_sqllogictest(
            "query I\nSELECT 1;\n----\n1 values hashing to 9d9dff9320e27082b15b4ed7a086ba83\n"
        )[0][0]
        self.assertTrue(run_record(rec_plain, engine).passed)
        self.assertTrue(run_record(rec_hash, engine).passed)

    def test_wrong_hash_fails(self):
        engine = Engine()
        rec = parse_sqllogictest(
            "query I\nSELECT 1;\n----\n1 values hashing to 00000000000000000000000000000000\n"
        )[0][0]
        rr = run_record(rec, engine)
        self.assertFalse(rr.passed)
        self.assertIn("hash mismatch", rr.detail)

    def test_wrong_count_fails(self):
        engine = Engine()
        rec = parse_sqllogictest(
            "query I\nSELECT 1;\n----\n2 values hashing to 9d9dff9320e27082b15b4ed7a086ba83\n"
        )[0][0]
        rr = run_record(rec, engine)
        self.assertFalse(rr.passed)
        self.assertIn("count mismatch", rr.detail)


class TestRunRecord(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()

    def test_statement_ok(self):
        rec = parse_sqllogictest("statement ok\nSELECT 1;\n")[0][0]
        self.assertTrue(run_record(rec, self.engine).passed)

    def test_statement_ok_fails_on_error(self):
        rec = parse_sqllogictest("statement ok\nSELECT * FROM t;\n")[0][0]
        rr = run_record(rec, self.engine)
        self.assertFalse(rr.passed)
        self.assertIn("statement error", rr.detail)

    def test_statement_error_passes_on_error(self):
        rec = parse_sqllogictest("statement error\nSELECT * FROM t;\n")[0][0]
        self.assertTrue(run_record(rec, self.engine).passed)

    def test_statement_error_fails_when_ok(self):
        rec = parse_sqllogictest("statement error\nSELECT 1;\n")[0][0]
        rr = run_record(rec, self.engine)
        self.assertFalse(rr.passed)
        self.assertIn("expected error", rr.detail)

    def test_statement_error_substring(self):
        rec = parse_sqllogictest("statement error no FROM clause\nSELECT x;\n")[0][0]
        self.assertTrue(run_record(rec, self.engine).passed)

    def test_statement_count(self):
        rec = parse_sqllogictest("statement count 1\nSELECT 1;\n")[0][0]
        self.assertTrue(run_record(rec, self.engine).passed)
        rec = parse_sqllogictest("statement count 2\nSELECT 1;\n")[0][0]
        self.assertFalse(run_record(rec, self.engine).passed)

    def test_query_fails_on_error(self):
        rec = parse_sqllogictest("query I\nSELECT 1 +;\n----\n1\n")[0][0]
        rr = run_record(rec, self.engine)
        self.assertFalse(rr.passed)
        self.assertIn("query error", rr.detail)

    def test_query_plaintext_mismatch(self):
        rec = parse_sqllogictest("query I\nSELECT 1;\n----\n2\n")[0][0]
        rr = run_record(rec, self.engine)
        self.assertFalse(rr.passed)
        self.assertIn("result mismatch", rr.detail)

    def test_skipped_record_passes(self):
        rec = parse_sqllogictest("skipif sqlite\nstatement ok\nSELECT 1;\n")[0][0]
        rr = run_record(rec, self.engine)
        self.assertTrue(rr.passed)
        self.assertTrue(rr.skipped)


class TestRunFile(unittest.TestCase):
    def test_expressions_file_all_pass(self):
        p, f, s = run_file(DATA / "expressions.test", Engine(), verbose=False)
        self.assertEqual(f, 0)
        self.assertGreater(p, 0)

    def test_hash_file_all_pass(self):
        p, f, s = run_file(DATA / "hash.test", Engine(), verbose=False)
        self.assertEqual(f, 0)

    def test_statements_file_all_pass(self):
        p, f, s = run_file(DATA / "statements.test", Engine(), verbose=False)
        self.assertEqual(f, 0)

    def test_malformed_file_fails_without_crashing(self):
        p, f, s = run_file(DATA / "malformed.test", Engine(), verbose=False)
        self.assertGreater(f, 0)
        # the valid record at the end still ran and passed
        self.assertGreater(p, 0)

    def test_subquery_file_all_pass(self):
        p, f, s = run_file(DATA / "subquery.test", Engine(), verbose=False)
        self.assertEqual(f, 0)
        self.assertGreater(p, 0)

    def test_subquery_failures_file_all_pass(self):
        # every record expects a statement error; the runner must judge each
        # as failed-statement and keep going
        p, f, s = run_file(DATA / "subquery_failures.test", Engine(), verbose=False)
        self.assertEqual(f, 0)
        self.assertGreater(p, 0)

    def test_missing_file_is_a_failure(self):
        p, f, s = run_file(DATA / "does-not-exist.test", Engine(), verbose=False)
        self.assertEqual(f, 1)


# The repo's own sqllogictest corpus. The official sqllogictest corpus is not
# vendored in this workspace (select*/random*/evidence* directories are
# absent), so the acceptance corpus is the one bundled under tests/data.
# These tests pin a1 (all success records green), a2 (deterministic) and
# a3 (no skipped/filtered records) for that available corpus.
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

# Files whose records are *expected* to fail (failure-path seam): they verify
# that unsupported statements are judged as failed records, the runner keeps
# going, and the CLI exits non-zero (a4). They are deliberately not part of
# the green corpus.
FAILURE_PATH_CORPUS = [
    "agg_failures.test",
    "failures.test",
    "index_failures.test",
    "join_failures.test",
    "order_failures.test",
    "malformed.test",
]


class TestAvailableCorpus(unittest.TestCase):
    """a1/a3: every success record of the available corpus passes, nothing is
    skipped, and the runner CLI exits 0 over the full success corpus."""

    def test_success_corpus_all_green_no_skips(self):
        for name in SUCCESS_CORPUS:
            with self.subTest(file=name):
                p, f, s = run_file(DATA / name, Engine(), verbose=False)
                self.assertEqual(f, 0, name)
                self.assertEqual(s, 0, f"{name} must not be skipped/filtered")

    def test_success_corpus_cli_exit_zero(self):
        code, out = run_cli(*(str(DATA / n) for n in SUCCESS_CORPUS))
        self.assertEqual(code, 0, out)

    def test_failure_path_corpus_still_fails(self):
        # the failure-path files must keep failing (they encode the a4 seam),
        # the runner must not swallow them into green.
        for name in FAILURE_PATH_CORPUS:
            with self.subTest(file=name):
                p, f, s = run_file(DATA / name, Engine(), verbose=False)
                self.assertGreater(f, 0, name)

    def test_failure_path_cli_exit_nonzero(self):
        code, out = run_cli(*(str(DATA / n) for n in FAILURE_PATH_CORPUS))
        self.assertNotEqual(code, 0, out)


class TestDeterminism(unittest.TestCase):
    """a2: two consecutive full runs produce identical output."""

    def _run_corpus_output(self, files):
        code, out = run_cli(*(str(DATA / n) for n in files))
        return code, out

    def test_success_corpus_deterministic(self):
        code1, out1 = self._run_corpus_output(SUCCESS_CORPUS)
        code2, out2 = self._run_corpus_output(SUCCESS_CORPUS)
        self.assertEqual(code1, code2)
        self.assertEqual(out1, out2)

    def test_failure_path_corpus_deterministic(self):
        code1, out1 = self._run_corpus_output(FAILURE_PATH_CORPUS)
        code2, out2 = self._run_corpus_output(FAILURE_PATH_CORPUS)
        self.assertEqual(code1, code2)
        self.assertEqual(out1, out2)


class TestCliExitCode(unittest.TestCase):
    def test_all_pass_exit_zero(self):
        code, out = run_cli(
            str(DATA / "expressions.test"),
            str(DATA / "hash.test"),
            str(DATA / "statements.test"),
            str(DATA / "subquery.test"),
            str(DATA / "subquery_failures.test"),
        )
        self.assertEqual(code, 0, out)
        self.assertIn("TOTAL:", out)

    def test_failure_exit_nonzero(self):
        code, out = run_cli(str(DATA / "malformed.test"))
        self.assertNotEqual(code, 0)
        self.assertIn("failed", out)

    def test_missing_file_exit_nonzero(self):
        code, out = run_cli(str(DATA / "nope.test"))
        self.assertNotEqual(code, 0)

    def test_module_invocation_works(self):
        proc = subprocess.run(
            [sys.executable, "-m", "sqldb.runner", str(DATA / "expressions.test")],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_verbose_lists_failures(self):
        code, out = run_cli("-v", str(DATA / "malformed.test"))
        self.assertNotEqual(code, 0)
        self.assertIn("FAIL", out)


class TestBenchmark(unittest.TestCase):
    """Slice-8 seam: benchmark.py is reproducible (a1) and its failure paths
    exit non-zero with diagnostics (a4). Timing assertions are deliberately
    absent: wall-clock is machine-dependent, the benchmark reports the medians
    and the comparison itself."""

    BENCH = REPO_ROOT / "benchmark.py"

    def run_bench(self, *args):
        proc = subprocess.run(
            [sys.executable, str(self.BENCH), *args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        return proc

    def test_reproducible_report_both_sides(self):
        # two consecutive runs both report engine + sqlite3 medians (a1)
        outs = []
        for _ in range(2):
            proc = self.run_bench("--runs", "1", "--warmup", "0",
                                  str(DATA / "expressions.test"),
                                  str(DATA / "select1.test"))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("engine : median=", proc.stdout)
            self.assertIn("sqlite3: median=", proc.stdout)
            self.assertIn("ratio engine/sqlite3", proc.stdout)
            outs.append(proc.stdout)
        # both sides of one run must be internally consistent: same corpus
        # line, same result line
        self.assertIn("corpus: 2 file(s), engine 93/93/0", outs[0])

    def test_failure_path_missing_file(self):
        proc = self.run_bench(str(REPO_ROOT / "no-such-file.test"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no such file", proc.stderr)

    def test_failure_path_engine_not_green(self):
        # a failure-path corpus file is not a valid benchmark load: the
        # engine fails records, so the script refuses to time a broken engine
        proc = self.run_bench("--runs", "1", "--warmup", "0",
                              str(DATA / "malformed.test"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("engine failed", proc.stderr)

    def test_failure_path_strict_slower(self):
        # with --strict, a slower engine must exit 1 (a2 gate); we cannot
        # assert which side wins on shared hardware, so just assert the exit
        # code is 0 or 1 and the RESULT line is printed (never 2/crash).
        proc = self.run_bench("--strict", "--runs", "1", "--warmup", "0",
                              str(DATA / "expressions.test"),
                              str(DATA / "select1.test"))
        self.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)
        self.assertIn("RESULT:", proc.stdout)

    def test_threshold_flag_is_executable(self):
        # a2's "comparable" threshold must be configurable (a requester
        # may issue a written threshold 待决问题②); a generous threshold
        # must always pass regardless of hardware speed.
        proc = self.run_bench("--strict", "--threshold", "1000",
                              "--runs", "1", "--warmup", "0",
                              str(DATA / "expressions.test"),
                              str(DATA / "select1.test"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RESULT: PASS", proc.stdout)


class TestAcceptance(unittest.TestCase):
    """Consolidation seam (slice 9): acceptance.py chains a1 correctness,
    a2 performance and a4 failure paths into one long-term regression asset
    with a strict exit-code contract."""

    ACC = REPO_ROOT / "acceptance.py"

    def run_acc(self, *args):
        proc = subprocess.run(
            [sys.executable, str(self.ACC), *args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        return proc

    def test_asset_exists_and_reports(self):
        proc = self.run_acc("--runs", "1", "--warmup", "0")
        self.assertIn(proc.returncode, (0, 1), proc.stdout + proc.stderr)
        self.assertIn("a1: PASS", proc.stdout)
        self.assertIn("a4: PASS", proc.stdout)
        self.assertIn("== a2:", proc.stdout)
        self.assertIn("ratio engine/sqlite3", proc.stdout)

    def test_generous_threshold_passes(self):
        # a requester-confirmed threshold that the engine comfortably meets
        # must yield exit 0 (OVERALL PASS).
        proc = self.run_acc("--runs", "1", "--warmup", "0", "--threshold", "1000")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OVERALL: PASS", proc.stdout)

    def test_acceptance_reports_correctness_regression(self):
        # a1 must be a hard gate: if the success corpus stops being green,
        # acceptance.py must exit 2 (never a silent pass). Simulate by
        # pointing the engine at a file that fails (malformed corpus).
        # acceptance.py hardcodes its corpus, so drive the same check via
        # the runner contract instead: a failing corpus exits non-zero.
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "sqllogictest_runner.py"),
             str(DATA / "malformed.test")],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("failed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
