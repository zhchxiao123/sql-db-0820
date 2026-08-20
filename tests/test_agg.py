"""Unit tests for aggregation (COUNT/SUM/AVG/MIN/MAX/TOTAL, GROUP BY, HAVING).

Expected values verified against the real sqlite3 (live golden battery in
test_sqlite_parity.py).
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


def make(engine, columns, values):
    run(engine, f"CREATE TABLE t({columns});")
    run(engine, f"INSERT INTO t VALUES {values};")


class TestWholeTableAggregates(unittest.TestCase):
    """a1: aggregates without GROUP BY, including empty and all-NULL inputs."""

    def setUp(self):
        self.engine = Engine()
        make(self.engine, "x INTEGER", "(1),(2),(3)")

    def test_basic(self):
        self.assertEqual(flat(self.engine, "SELECT COUNT(*), SUM(x), AVG(x), MIN(x), MAX(x), TOTAL(x) FROM t;"),
                         ["3", "6", "2.0", "1", "3", "6.0"])

    def test_empty_table(self):
        run(self.engine, "CREATE TABLE e(x INTEGER);")
        self.assertEqual(flat(self.engine, "SELECT COUNT(*) FROM e;"), ["0"])
        self.assertEqual(flat(self.engine, "SELECT COUNT(x) FROM e;"), ["0"])
        self.assertEqual(flat(self.engine, "SELECT SUM(x) FROM e;"), ["NULL"])
        self.assertEqual(flat(self.engine, "SELECT AVG(x) FROM e;"), ["NULL"])
        self.assertEqual(flat(self.engine, "SELECT MIN(x) FROM e;"), ["NULL"])
        self.assertEqual(flat(self.engine, "SELECT MAX(x) FROM e;"), ["NULL"])
        self.assertEqual(flat(self.engine, "SELECT TOTAL(x) FROM e;"), ["0.0"])

    def test_all_null_column(self):
        run(self.engine, "CREATE TABLE n(x INTEGER);")
        run(self.engine, "INSERT INTO n VALUES (NULL), (NULL);")
        self.assertEqual(flat(self.engine, "SELECT COUNT(*) FROM n;"), ["2"])
        self.assertEqual(flat(self.engine, "SELECT COUNT(x) FROM n;"), ["0"])
        self.assertEqual(flat(self.engine, "SELECT SUM(x) FROM n;"), ["NULL"])
        self.assertEqual(flat(self.engine, "SELECT TOTAL(x) FROM n;"), ["0.0"])

    def test_text_values(self):
        run(self.engine, "CREATE TABLE s(x);")
        run(self.engine, "INSERT INTO s VALUES ('1'), ('abc'), (2), ('1.5');")
        self.assertEqual(flat(self.engine, "SELECT SUM(x) FROM s;"), ["4.5"])
        self.assertEqual(flat(self.engine, "SELECT AVG(x) FROM s;"), ["1.125"])
        self.assertEqual(flat(self.engine, "SELECT COUNT(x) FROM s;"), ["4"])
        # MIN/MAX keep storage-class ordering and original values
        self.assertEqual(flat(self.engine, "SELECT MIN(x) FROM s;"), ["2"])
        self.assertEqual(flat(self.engine, "SELECT MAX(x) FROM s;"), ["abc"])

    def test_sum_text_only(self):
        self.assertEqual(flat(self.engine, "SELECT SUM('abc');"), ["0.0"])
        self.assertEqual(flat(self.engine, "SELECT SUM('1');"), ["1"])

    def test_count_no_args_is_star(self):
        self.assertEqual(flat(self.engine, "SELECT COUNT() FROM t;"), ["3"])

    def test_aggregate_expressions(self):
        self.assertEqual(flat(self.engine, "SELECT COUNT(*)+1 FROM t;"), ["4"])
        self.assertEqual(flat(self.engine, "SELECT 2*COUNT(*) FROM t;"), ["6"])

    def test_no_from(self):
        self.assertEqual(flat(self.engine, "SELECT COUNT(*);"), ["1"])

    def test_sum_int_stays_int(self):
        self.assertEqual(flat(self.engine, "SELECT SUM(x) FROM t;"), ["6"])
        self.assertEqual(flat(self.engine, "SELECT SUM(x)*1.0 FROM t;"), ["6.0"])


class TestGroupBy(unittest.TestCase):
    """a2: GROUP BY with NULL keys and multi-column grouping."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE g(k TEXT, v INTEGER);")
        run(self.engine, "INSERT INTO g VALUES ('a',1),('b',2),('a',3),(NULL,4),(NULL,5),('a',NULL);")

    def test_group_aggregates(self):
        self.assertEqual(
            flat(self.engine, "SELECT k, COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM g GROUP BY k ORDER BY k;"),
            ["NULL", "2", "9", "4.5", "4", "5",
             "a", "3", "4", "2.0", "1", "3",
             "b", "1", "2", "2.0", "2", "2"],
        )

    def test_null_groups_together(self):
        self.assertEqual(flat(self.engine, "SELECT k, COUNT(*) FROM g GROUP BY k ORDER BY k;"),
                         ["NULL", "2", "a", "3", "b", "1"])

    def test_group_key_only(self):
        self.assertEqual(flat(self.engine, "SELECT k FROM g GROUP BY k ORDER BY k;"),
                         ["NULL", "a", "b"])

    def test_multi_column_group(self):
        run(self.engine, "CREATE TABLE m(a INTEGER, b TEXT, v INTEGER);")
        run(self.engine, "INSERT INTO m VALUES (1,'x',1),(1,'x',2),(1,'y',3),(2,'x',4);")
        self.assertEqual(
            flat(self.engine, "SELECT a, b, COUNT(*), SUM(v) FROM m GROUP BY a, b ORDER BY a, b;"),
            ["1", "x", "2", "3", "1", "y", "1", "3", "2", "x", "1", "4"],
        )

    def test_bare_column_takes_first_row(self):
        run(self.engine, "CREATE TABLE w(a INTEGER, b TEXT);")
        run(self.engine, "INSERT INTO w VALUES (1,'first'),(1,'second'),(2,'only');")
        self.assertEqual(flat(self.engine, "SELECT a, b, COUNT(*) FROM w GROUP BY a ORDER BY a;"),
                         ["1", "first", "2", "2", "only", "1"])

    def test_empty_table_group_by_no_rows(self):
        run(self.engine, "CREATE TABLE e2(x INTEGER);")
        self.assertEqual(flat(self.engine, "SELECT x, COUNT(*) FROM e2 GROUP BY x;"), [])


class TestHaving(unittest.TestCase):
    """a3: HAVING filters groups; references group keys and aggregates."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE g(k TEXT, v INTEGER);")
        run(self.engine, "INSERT INTO g VALUES ('a',1),('b',2),('a',3),(NULL,4),(NULL,5),('a',NULL);")

    def test_having_aggregate(self):
        self.assertEqual(flat(self.engine, "SELECT k, COUNT(*) FROM g GROUP BY k HAVING COUNT(*) >= 2 ORDER BY k;"),
                         ["NULL", "2", "a", "3"])
        self.assertEqual(flat(self.engine, "SELECT k, SUM(v) FROM g GROUP BY k HAVING SUM(v) > 5 ORDER BY k;"),
                         ["NULL", "9"])

    def test_having_group_key(self):
        self.assertEqual(flat(self.engine, "SELECT k FROM g GROUP BY k HAVING k IS NOT NULL ORDER BY k;"),
                         ["a", "b"])

    def test_having_without_group_by(self):
        self.assertEqual(flat(self.engine, "SELECT COUNT(*) FROM g HAVING COUNT(*) > 0;"), ["6"])

    def test_having_with_order_by(self):
        self.assertEqual(
            flat(self.engine, "SELECT k, COUNT(*) FROM g GROUP BY k HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC, k;"),
            ["a", "3", "NULL", "2"],
        )

    def test_having_filters_all(self):
        self.assertEqual(flat(self.engine, "SELECT k FROM g GROUP BY k HAVING COUNT(*) > 10;"), [])


class TestDistinctAggregates(unittest.TestCase):
    def test_count_distinct(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE g(k TEXT, v INTEGER);")
        run(self.engine, "INSERT INTO g VALUES ('a',1),('b',2),('a',3),(NULL,4),(NULL,5),('a',NULL);")
        self.assertEqual(flat(self.engine, "SELECT COUNT(DISTINCT k) FROM g;"), ["2"])
        self.assertEqual(flat(self.engine, "SELECT SUM(DISTINCT v) FROM g;"), ["15"])


class TestAggregateFailures(unittest.TestCase):
    """a5: aggregation/grouping failure paths are judged as failures."""

    def setUp(self):
        self.engine = Engine()
        make(self.engine, "k TEXT, v INTEGER", "('a',1)")

    def assert_error(self, sql, fragment=None):
        res = run(self.engine, sql)
        self.assertIsNotNone(res.error, f"expected error for {sql!r}")
        if fragment:
            self.assertIn(fragment, res.error)

    def test_missing_column(self):
        self.assert_error("SELECT COUNT(z) FROM t;", "no such column")
        self.assert_error("SELECT v FROM t GROUP BY z;", "no such column")
        self.assert_error("SELECT v FROM t HAVING z > 1;", "no such column")

    def test_wrong_arguments(self):
        self.assert_error("SELECT SUM(*) FROM t;", "wrong number of arguments")
        self.assert_error("SELECT SUM() FROM t;", "wrong number of arguments")
        self.assert_error("SELECT AVG() FROM t;", "wrong number of arguments")

    def test_misuse_outside_aggregate(self):
        # aggregates are only valid inside aggregate queries; a bare aggregate
        # in WHERE is rejected
        self.assert_error("SELECT * FROM t WHERE COUNT(*) > 0;")


if __name__ == "__main__":
    unittest.main()
