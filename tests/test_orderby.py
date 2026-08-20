"""Unit tests for ORDER BY / DISTINCT / LIMIT-OFFSET (slice 2).

Expected values in these tests were verified against the real sqlite3
(see test_sqlite_parity.py for the live golden battery).
"""

import unittest

from sqldb.engine import Engine, render_value


def rows(engine, sql):
    res = engine.execute(sql)
    if res.error is not None:
        return None
    return [[render_value(v) for v in row] for row in res.rows]


def flat(engine, sql):
    r = rows(engine, sql)
    return None if r is None else [v for row in r for v in row]


def make(engine, columns, values):
    run(engine, f"CREATE TABLE t({columns});")
    run(engine, f"INSERT INTO t VALUES {values};")


def run(engine, sql):
    return engine.execute(sql)


class TestOrderBy(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        make(self.engine, "a INTEGER, b TEXT",
             "(3,'c'),(1,'a'),(NULL,'n'),(2,'b'),(3,'C'),(1,'A')")

    def test_asc_null_first(self):
        self.assertEqual(flat(self.engine, "SELECT a FROM t ORDER BY a;"),
                         ["NULL", "1", "1", "2", "3", "3"])

    def test_desc_null_last(self):
        self.assertEqual(flat(self.engine, "SELECT a FROM t ORDER BY a DESC;"),
                         ["3", "3", "2", "1", "1", "NULL"])

    def test_multi_column_lexicographic(self):
        self.assertEqual(flat(self.engine, "SELECT a, b FROM t ORDER BY a, b;"),
                         ["NULL", "n", "1", "A", "1", "a", "2", "b", "3", "C", "3", "c"])

    def test_multi_column_mixed_direction(self):
        self.assertEqual(
            flat(self.engine, "SELECT a, b FROM t ORDER BY a DESC, b ASC;"),
            ["3", "C", "3", "c", "2", "b", "1", "A", "1", "a", "NULL", "n"],
        )

    def test_text_byte_order(self):
        self.assertEqual(flat(self.engine, "SELECT b FROM t ORDER BY b;"),
                         ["A", "C", "a", "b", "c", "n"])

    def test_order_by_expression(self):
        self.assertEqual(
            flat(self.engine, "SELECT a, b FROM t ORDER BY a+1;"),
            ["NULL", "n", "1", "a", "1", "A", "2", "b", "3", "c", "3", "C"],
        )

    def test_order_by_ordinal(self):
        self.assertEqual(flat(self.engine, "SELECT a, b FROM t ORDER BY 1;"),
                         ["NULL", "n", "1", "a", "1", "A", "2", "b", "3", "c", "3", "C"])
        self.assertEqual(
            flat(self.engine, "SELECT a, b FROM t ORDER BY 2 DESC;"),
            ["NULL", "n", "3", "c", "2", "b", "1", "a", "3", "C", "1", "A"],
        )

    def test_order_by_negative_expr(self):
        self.assertEqual(flat(self.engine, "SELECT a FROM t ORDER BY -a;"),
                         ["NULL", "3", "3", "2", "1", "1"])

    def test_ties_stable_insertion_order(self):
        self.assertEqual(flat(self.engine, "SELECT a FROM t ORDER BY a;"),
                         ["NULL", "1", "1", "2", "3", "3"])


class TestDistinct(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        make(self.engine, "x INTEGER, y TEXT",
             "(1,'a'),(1,'a'),(2,'b'),(NULL,'n'),(NULL,'n'),(1,'A')")

    def test_distinct_null_collapses(self):
        self.assertEqual(flat(self.engine, "SELECT DISTINCT x FROM t;"), ["1", "2", "NULL"])

    def test_distinct_whole_row(self):
        self.assertEqual(
            flat(self.engine, "SELECT DISTINCT x, y FROM t;"),
            ["1", "a", "2", "b", "NULL", "n", "1", "A"],
        )

    def test_distinct_text(self):
        self.assertEqual(flat(self.engine, "SELECT DISTINCT y FROM t;"), ["a", "b", "n", "A"])

    def test_distinct_then_order(self):
        self.assertEqual(flat(self.engine, "SELECT DISTINCT x FROM t ORDER BY x;"),
                         ["NULL", "1", "2"])

    def test_distinct_numeric_equality(self):
        self.assertEqual(rows(self.engine, "SELECT DISTINCT 5, 5.0;"), [["5", "5.0"]])

    def test_distinct_no_affinity_cross_type(self):
        # no-affinity column: 5 and 5.0 collapse, '5' stays distinct (sqlite)
        run(self.engine, "CREATE TABLE e(x);")
        run(self.engine, "INSERT INTO e VALUES (5), ('5'), (5.0), ('abc');")
        self.assertEqual(flat(self.engine, "SELECT DISTINCT x FROM e;"), ["5", "5", "abc"])

    def test_distinct_number_vs_text(self):
        self.assertEqual(rows(self.engine, "SELECT DISTINCT 5, '5';"), [["5", "5"]])

    def test_distinct_no_from(self):
        self.assertEqual(flat(self.engine, "SELECT DISTINCT 1;"), ["1"])


class TestLimit(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        make(self.engine, "v INTEGER", "(1),(2),(3),(4),(5)")

    def test_limit_n(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t LIMIT 2;"), ["1", "2"])

    def test_limit_zero(self):
        self.assertEqual(rows(self.engine, "SELECT v FROM t LIMIT 0;"), [])

    def test_limit_negative_is_all(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t LIMIT -1;"), ["1", "2", "3", "4", "5"])

    def test_limit_offset(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t LIMIT 2 OFFSET 2;"), ["3", "4"])

    def test_offset_beyond_end(self):
        self.assertEqual(rows(self.engine, "SELECT v FROM t LIMIT 2 OFFSET 10;"), [])

    def test_negative_offset_is_zero(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t LIMIT 2 OFFSET -1;"), ["1", "2"])

    def test_comma_form_offset_count(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t LIMIT 2, 1;"), ["3"])

    def test_order_then_limit(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t ORDER BY v DESC LIMIT 2;"), ["5", "4"])

    def test_limit_expression(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t LIMIT 1+1;"), ["1", "2"])

    def test_negative_limit_with_offset(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t LIMIT -1 OFFSET 3;"), ["4", "5"])

    def test_text_limit(self):
        self.assertEqual(flat(self.engine, "SELECT v FROM t LIMIT '2';"), ["1", "2"])

    def test_limit_plus_distinct(self):
        self.assertEqual(flat(self.engine, "SELECT DISTINCT v FROM t ORDER BY v DESC LIMIT 2;"),
                         ["5", "4"])


class TestOrderFailures(unittest.TestCase):
    """a5: ORDER BY / DISTINCT / LIMIT failure paths are judged as failures."""

    def setUp(self):
        self.engine = Engine()
        make(self.engine, "a INTEGER", "(1),(2)")

    def assert_error(self, sql, fragment=None):
        res = run(self.engine, sql)
        self.assertIsNotNone(res.error, f"expected error for {sql!r}")
        if fragment:
            self.assertIn(fragment, res.error)

    def test_unknown_column_order_by(self):
        self.assert_error("SELECT a FROM t ORDER BY z;", "no such column")

    def test_unknown_column_distinct(self):
        self.assert_error("SELECT DISTINCT z FROM t;", "no such column")

    def test_ordinal_out_of_range(self):
        self.assert_error("SELECT a FROM t ORDER BY 5;", "out of range")

    def test_limit_unknown_column(self):
        self.assert_error("SELECT a FROM t LIMIT x;", "no such column")

    def test_limit_null(self):
        self.assert_error("SELECT a FROM t LIMIT NULL;", "datatype mismatch")

    def test_limit_float(self):
        self.assert_error("SELECT a FROM t LIMIT 1.5;", "datatype mismatch")

    def test_order_by_missing_terms(self):
        self.assert_error("SELECT a FROM t ORDER BY;")


if __name__ == "__main__":
    unittest.main()
