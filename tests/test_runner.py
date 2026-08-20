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

    def test_missing_file_is_a_failure(self):
        p, f, s = run_file(DATA / "does-not-exist.test", Engine(), verbose=False)
        self.assertEqual(f, 1)


class TestCliExitCode(unittest.TestCase):
    def test_all_pass_exit_zero(self):
        code, out = run_cli(
            str(DATA / "expressions.test"),
            str(DATA / "hash.test"),
            str(DATA / "statements.test"),
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


if __name__ == "__main__":
    unittest.main()
