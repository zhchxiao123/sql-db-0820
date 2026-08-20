"""Tests for single-table storage and basic queries (slice 1).

Covers CREATE TABLE / INSERT / DELETE / SELECT-with-WHERE, sqlite type
affinity (storage + comparison), LIKE, rowcount semantics and the failure
paths (a1-a5 of the slice requirement).
"""

import unittest

from sqldb.engine import Engine, render_value


def run(engine, sql):
    return engine.execute(sql)


def rows(engine, sql):
    """Rendered rows of a SELECT, or None on error."""
    res = run(engine, sql)
    if res.error is not None:
        return None
    return [[render_value(v) for v in row] for row in res.rows]


def flat(engine, sql):
    """Flattened rendered values of a SELECT (as sqllogictest compares)."""
    r = rows(engine, sql)
    return None if r is None else [v for row in r for v in row]


class TestCreateInsertSelect(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()

    def test_a1_insert_order_preserved(self):
        self.assertIsNone(run(self.engine, "CREATE TABLE t(a INTEGER, b TEXT, c REAL);").error)
        self.assertIsNone(
            run(self.engine, "INSERT INTO t VALUES (1, 'one', 1.5), (2, 'two', 2.5), (3, 'three', 3.5);").error
        )
        self.assertEqual(
            flat(self.engine, "SELECT * FROM t;"),
            ["1", "one", "1.5", "2", "two", "2.5", "3", "three", "3.5"],
        )

    def test_insert_rowcount(self):
        run(self.engine, "CREATE TABLE t(a INTEGER);")
        res = run(self.engine, "INSERT INTO t VALUES (1), (2), (3);")
        self.assertIsNone(res.error)
        self.assertEqual(res.rowcount, 3)

    def test_select_rowcount(self):
        run(self.engine, "CREATE TABLE t(a INTEGER);")
        run(self.engine, "INSERT INTO t VALUES (1), (2);")
        res = run(self.engine, "SELECT * FROM t;")
        self.assertEqual(res.rowcount, 2)
        self.assertEqual(len(res.rows), 2)

    def test_empty_table_select(self):
        run(self.engine, "CREATE TABLE e(x INTEGER);")
        self.assertEqual(rows(self.engine, "SELECT * FROM e;"), [])

    def test_select_const_from_table(self):
        run(self.engine, "CREATE TABLE s1(x INTEGER);")
        run(self.engine, "INSERT INTO s1 VALUES (1), (2);")
        self.assertEqual(flat(self.engine, "SELECT 7 FROM s1;"), ["7", "7"])
        self.assertEqual(flat(self.engine, "SELECT x+1 FROM s1;"), ["2", "3"])

    def test_column_projection(self):
        run(self.engine, "CREATE TABLE t(a INTEGER, b TEXT, c REAL);")
        run(self.engine, "INSERT INTO t VALUES (1, 'one', 1.5);")
        self.assertEqual(flat(self.engine, "SELECT b FROM t;"), ["one"])
        self.assertEqual(flat(self.engine, "SELECT a, c FROM t;"), ["1", "1.5"])
        self.assertEqual(flat(self.engine, "SELECT *, a FROM t;"), ["1", "one", "1.5", "1"])

    def test_case_insensitive_names(self):
        run(self.engine, "CREATE TABLE CaseT(A INTEGER);")
        run(self.engine, "INSERT INTO caset VALUES (1);")
        self.assertEqual(flat(self.engine, "SELECT a FROM caset;"), ["1"])

    def test_multirow_insert_column_list(self):
        run(self.engine, "CREATE TABLE m(a INTEGER, b TEXT);")
        self.assertIsNone(run(self.engine, "INSERT INTO m(a) VALUES (1), (2);").error)
        self.assertEqual(rows(self.engine, "SELECT * FROM m;"), [["1", "NULL"], ["2", "NULL"]])
        run(self.engine, "INSERT INTO m VALUES (3, 'x'), (4, 'y');")
        self.assertEqual(
            rows(self.engine, "SELECT * FROM m;"),
            [["1", "NULL"], ["2", "NULL"], ["3", "x"], ["4", "y"]],
        )


class TestWhere(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t(a INTEGER, b TEXT, c REAL);")
        run(
            self.engine,
            "INSERT INTO t VALUES (1, 'one', 1.5), (2, 'two', 2.5), (3, 'three', 3.5);",
        )

    def test_equality(self):
        self.assertEqual(rows(self.engine, "SELECT * FROM t WHERE a = 2;"), [["2", "two", "2.5"]])
        self.assertEqual(rows(self.engine, "SELECT * FROM t WHERE b = 'two';"), [["2", "two", "2.5"]])

    def test_range(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t WHERE a > 1;"),
            [["2", "two", "2.5"], ["3", "three", "3.5"]],
        )
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t WHERE a >= 2 AND a < 3;"),
            [["2", "two", "2.5"]],
        )

    def test_and_or(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t WHERE a = 1 OR a = 3;"),
            [["1", "one", "1.5"], ["3", "three", "3.5"]],
        )
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t WHERE a > 0 AND b LIKE 't%';"),
            [["2", "two", "2.5"], ["3", "three", "3.5"]],
        )

    def test_is_null(self):
        run(self.engine, "INSERT INTO t VALUES (NULL, 'none', NULL);")
        self.assertEqual(rows(self.engine, "SELECT * FROM t WHERE a IS NULL;"), [["NULL", "none", "NULL"]])
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t WHERE a IS NOT NULL;"),
            [["1", "one", "1.5"], ["2", "two", "2.5"], ["3", "three", "3.5"]],
        )

    def test_like(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t WHERE b LIKE 't%';"),
            [["2", "two", "2.5"], ["3", "three", "3.5"]],
        )
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t WHERE b NOT LIKE 't%';"),
            [["1", "one", "1.5"]],
        )
        # case-insensitive
        run(self.engine, "CREATE TABLE l(s TEXT);")
        run(self.engine, "INSERT INTO l VALUES ('Hello'), ('world');")
        self.assertEqual(flat(self.engine, "SELECT * FROM l WHERE s LIKE 'h%';"), ["Hello"])
        self.assertEqual(flat(self.engine, "SELECT * FROM l WHERE s LIKE '%LLO';"), ["Hello"])

    def test_like_escape(self):
        run(self.engine, "CREATE TABLE l(s TEXT);")
        run(self.engine, "INSERT INTO l VALUES ('100%'), ('101');")
        self.assertEqual(flat(self.engine, "SELECT * FROM l WHERE s LIKE '100!%' ESCAPE '!';"), ["100%"])
        self.assertEqual(flat(self.engine, "SELECT * FROM l WHERE s LIKE '10_%';"), ["100%", "101"])

    def test_where_on_expression(self):
        run(self.engine, "CREATE TABLE n(x INTEGER);")
        run(self.engine, "INSERT INTO n VALUES (1), (2), (3);")
        self.assertEqual(flat(self.engine, "SELECT x FROM n WHERE x*2 > 4;"), ["3"])


class TestAffinity(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()

    def test_storage_conversion(self):
        # verified against sqlite3: INTEGER col stores '2' as 2, 'abc' stays
        # text; TEXT col stores 5 as '5'; REAL col stores 3 as 3.0 and '4' as 4.0
        run(self.engine, "CREATE TABLE t(a INTEGER, b TEXT, c REAL);")
        run(self.engine, "INSERT INTO t VALUES (1, '2', 3), ('2', 'abc', '4'), ('abc', 5, 'x'), (3.5, '3.5', 7);")
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t;"),
            [
                ["1", "2", "3.0"],
                ["2", "abc", "4.0"],
                ["abc", "5", "x"],
                ["3.5", "3.5", "7.0"],
            ],
        )

    def test_a3_mixed_type_where(self):
        run(self.engine, "CREATE TABLE m(a INTEGER);")
        run(self.engine, "INSERT INTO m VALUES (1), ('2'), ('abc'), (3.5), (NULL);")
        # matches sqlite: 1, 2, 3.5 are < 10; 'abc' (text) sorts after numbers; NULL excluded
        self.assertEqual(flat(self.engine, "SELECT * FROM m WHERE a < 10;"), ["1", "2", "3.5"])
        self.assertEqual(flat(self.engine, "SELECT * FROM m WHERE a = '2';"), ["2"])
        self.assertEqual(flat(self.engine, "SELECT * FROM m WHERE a IS NULL;"), ["NULL"])

    def test_text_affinity_comparison(self):
        run(self.engine, "CREATE TABLE t(b TEXT);")
        run(self.engine, "INSERT INTO t VALUES ('5'), ('abc');")
        # rule 2: TEXT affinity column vs number -> number converted to text
        self.assertEqual(flat(self.engine, "SELECT * FROM t WHERE b = 5;"), ["5"])
        self.assertEqual(flat(self.engine, "SELECT * FROM t WHERE b < '9';"), ["5"])
        self.assertEqual(flat(self.engine, "SELECT * FROM t WHERE b < 'abc';"), ["5"])

    def test_real_affinity_storage(self):
        run(self.engine, "CREATE TABLE r(x REAL);")
        run(self.engine, "INSERT INTO r VALUES ('3'), (2), (1.5);")
        self.assertEqual(flat(self.engine, "SELECT * FROM r;"), ["3.0", "2.0", "1.5"])

    def test_affinity_across_columns(self):
        run(self.engine, "CREATE TABLE t(a INTEGER, b TEXT);")
        run(self.engine, "INSERT INTO t VALUES (1, '2'), (2, 'abc');")
        # rule 1: a has INTEGER affinity -> numeric applied to b's value.
        # Row (2,'abc'): 'abc' cannot convert, and numbers sort before text,
        # so 2 < 'abc' is still true (verified against sqlite3).
        self.assertEqual(rows(self.engine, "SELECT * FROM t WHERE a < b;"), [["1", "2"], ["2", "abc"]])
        self.assertEqual(rows(self.engine, "SELECT * FROM t WHERE b < a;"), [])


class TestDelete(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE d(x INTEGER, y TEXT);")
        run(self.engine, "INSERT INTO d VALUES (1, 'a'), (2, 'b'), (3, 'c');")

    def test_a4_delete_where(self):
        res = run(self.engine, "DELETE FROM d WHERE x = 2;")
        self.assertIsNone(res.error)
        self.assertEqual(res.rowcount, 1)
        self.assertEqual(rows(self.engine, "SELECT * FROM d;"), [["1", "a"], ["3", "c"]])

    def test_delete_all(self):
        res = run(self.engine, "DELETE FROM d;")
        self.assertEqual(res.rowcount, 3)
        self.assertEqual(rows(self.engine, "SELECT * FROM d;"), [])

    def test_delete_no_match(self):
        res = run(self.engine, "DELETE FROM d WHERE x = 99;")
        self.assertEqual(res.rowcount, 0)
        self.assertEqual(len(rows(self.engine, "SELECT * FROM d;")), 3)


class TestFailures(unittest.TestCase):
    """a5: illegal statements are judged as failures, never a crash."""

    def setUp(self):
        self.engine = Engine()

    def assert_error(self, sql, fragment=None):
        res = run(self.engine, sql)
        self.assertIsNotNone(res.error, f"expected error for {sql!r}")
        if fragment:
            self.assertIn(fragment, res.error)

    def test_insert_value_count_mismatch(self):
        run(self.engine, "CREATE TABLE f(a INTEGER, b TEXT);")
        self.assert_error("INSERT INTO f VALUES (1);", "columns")
        self.assert_error("INSERT INTO f VALUES (1, 'x', 3);", "columns")
        # table unchanged after failed inserts
        self.assertEqual(rows(self.engine, "SELECT * FROM f;"), [])

    def test_missing_table(self):
        self.assert_error("SELECT * FROM nosuch;", "no such table")
        self.assert_error("DELETE FROM nosuch;", "no such table")
        self.assert_error("INSERT INTO nosuch VALUES (1);", "no such table")

    def test_duplicate_table(self):
        run(self.engine, "CREATE TABLE t(x INTEGER);")
        self.assert_error("CREATE TABLE t(y INTEGER);", "already exists")

    def test_unknown_column(self):
        run(self.engine, "CREATE TABLE t(a INTEGER);")
        self.assert_error("SELECT * FROM t WHERE b = 1;", "no such column")
        # valid statements are not errors
        self.assertIsNone(run(self.engine, "SELECT a FROM t;").error)
        self.assertIsNone(run(self.engine, "SELECT * FROM t WHERE a = 1;").error)

    def test_star_without_from(self):
        self.assert_error("SELECT *;", "FROM")

    def test_unsupported_statements(self):
        self.assert_error("UPDATE t SET a=1;")
        self.assert_error("DROP TABLE t;")
        # JOIN is supported since the join slice: the statement parses and the
        # failure moves to table resolution (a5 graceful-failure path).
        self.assert_error("SELECT * FROM t JOIN u;", "no such table")


if __name__ == "__main__":
    unittest.main()
