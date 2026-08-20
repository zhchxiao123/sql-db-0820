"""Unit tests for CREATE INDEX / DROP INDEX (slice 3).

Correctness-first slice: indexes are parsed, validated and recorded but do
not change query results (lookup stays a full scan until the performance
slice). Expected behaviors verified against the real sqlite3.
"""

import unittest

from sqldb.engine import Engine, render_value


def run(engine, sql):
    return engine.execute(sql)


def flat(engine, sql):
    res = run(engine, sql)
    if res.error is not None:
        return None
    return [render_value(v) for row in res.rows for v in row]


def make(engine):
    run(engine, "CREATE TABLE t(a INTEGER, b TEXT, c REAL);")
    run(engine, "INSERT INTO t VALUES (1, 'x', 1.5), (2, 'y', 2.5), (3, 'z', 3.5);")


class TestCreateIndex(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        make(self.engine)

    def test_single_column(self):
        self.assertIsNone(run(self.engine, "CREATE INDEX idx_a ON t(a);").error)

    def test_multi_column(self):
        self.assertIsNone(run(self.engine, "CREATE INDEX idx_ab ON t(a, b);").error)

    def test_column_directions_parsed(self):
        self.assertIsNone(run(self.engine, "CREATE INDEX idx_d ON t(a DESC, b);").error)

    def test_unique_index_parsed(self):
        self.assertIsNone(run(self.engine, "CREATE UNIQUE INDEX uidx ON t(a);").error)

    def test_duplicate_errors(self):
        run(self.engine, "CREATE INDEX idx_a ON t(a);")
        res = run(self.engine, "CREATE INDEX idx_a ON t(a);")
        self.assertIsNotNone(res.error)
        self.assertIn("already exists", res.error)

    def test_if_not_exists_silent(self):
        run(self.engine, "CREATE INDEX idx_a ON t(a);")
        self.assertIsNone(run(self.engine, "CREATE INDEX IF NOT EXISTS idx_a ON t(a);").error)

    def test_missing_table(self):
        res = run(self.engine, "CREATE INDEX idx ON nosuch(a);")
        self.assertIsNotNone(res.error)
        self.assertIn("no such table", res.error)

    def test_missing_column(self):
        res = run(self.engine, "CREATE INDEX idx ON t(z);")
        self.assertIsNotNone(res.error)
        self.assertIn("no such column", res.error)

    def test_syntax_error(self):
        res = run(self.engine, "CREATE INDEX bad ON t;")
        self.assertIsNotNone(res.error)

    def test_index_registered(self):
        run(self.engine, "CREATE INDEX idx_a ON t(a);")
        self.assertIn("idx_a", self.engine.indexes)
        self.assertEqual(self.engine.indexes["idx_a"].columns, ["a"])


class TestDropIndex(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        make(self.engine)
        run(self.engine, "CREATE INDEX idx_a ON t(a);")

    def test_drop(self):
        self.assertIsNone(run(self.engine, "DROP INDEX idx_a;").error)
        self.assertNotIn("idx_a", self.engine.indexes)

    def test_drop_missing_errors(self):
        res = run(self.engine, "DROP INDEX idx_a;")
        self.assertIsNone(res.error)
        res = run(self.engine, "DROP INDEX idx_a;")
        self.assertIsNotNone(res.error)
        self.assertIn("no such index", res.error)

    def test_drop_if_exists_silent(self):
        self.assertIsNone(run(self.engine, "DROP INDEX IF EXISTS nosuch;").error)


class TestIndexDoesNotChangeResults(unittest.TestCase):
    """a2: indexed queries return the same rows as unindexed ones, including
    after INSERT/DELETE data changes."""

    def setUp(self):
        self.engine = Engine()
        make(self.engine)

    QUERIES = [
        "SELECT * FROM t WHERE a = 2;",
        "SELECT * FROM t WHERE a >= 2 AND a < 3;",
        "SELECT * FROM t WHERE b = 'y';",
        "SELECT * FROM t WHERE b LIKE 'z%' OR a = 1;",
        "SELECT a, b FROM t ORDER BY a DESC;",
        "SELECT * FROM t WHERE a IS NOT NULL ORDER BY b;",
        "SELECT * FROM t WHERE a < 2 OR a > 2;",
        "SELECT * FROM t LIMIT 2 OFFSET 1;",
    ]

    def test_before_after_index_identical(self):
        before = [flat(self.engine, q) for q in self.QUERIES]
        run(self.engine, "CREATE INDEX idx_a ON t(a);")
        run(self.engine, "CREATE INDEX idx_ab ON t(a, b);")
        after = [flat(self.engine, q) for q in self.QUERIES]
        self.assertEqual(after, before)

    def test_after_data_changes_identical(self):
        run(self.engine, "CREATE INDEX idx_a ON t(a);")
        with_index = [flat(self.engine, q) for q in self.QUERIES]
        # mutate the data
        run(self.engine, "INSERT INTO t VALUES (4, 'w', 4.5), (5, 'v', 5.5);")
        run(self.engine, "DELETE FROM t WHERE a = 1;")
        run(self.engine, "DELETE FROM t WHERE a = 5;")
        run(self.engine, "DELETE FROM t WHERE a = 3;")
        after_mutation_indexed = [flat(self.engine, q) for q in self.QUERIES]
        # a fresh engine without any index, same mutations
        self.engine2 = Engine()
        make(self.engine2)
        run(self.engine2, "INSERT INTO t VALUES (4, 'w', 4.5), (5, 'v', 5.5);")
        run(self.engine2, "DELETE FROM t WHERE a = 1;")
        run(self.engine2, "DELETE FROM t WHERE a = 5;")
        run(self.engine2, "DELETE FROM t WHERE a = 3;")
        no_index = [flat(self.engine2, q) for q in self.QUERIES]
        self.assertEqual(after_mutation_indexed, no_index)


class TestIndexFailures(unittest.TestCase):
    """a4: failure paths are judged as failures, never a crash."""

    def setUp(self):
        self.engine = Engine()
        make(self.engine)

    def assert_error(self, sql, fragment=None):
        res = run(self.engine, sql)
        self.assertIsNotNone(res.error, f"expected error for {sql!r}")
        if fragment:
            self.assertIn(fragment, res.error)

    def test_errors(self):
        self.assert_error("CREATE INDEX i ON nosuch(a);", "no such table")
        self.assert_error("CREATE INDEX i ON t(z);", "no such column")
        self.assert_error("CREATE INDEX i ON t;")
        self.assert_error("DROP INDEX nosuch;", "no such index")
        self.assert_error("CREATE INDEX;")
        self.assert_error("DROP INDEX;")


if __name__ == "__main__":
    unittest.main()
