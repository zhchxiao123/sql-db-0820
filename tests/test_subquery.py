"""Unit tests for subqueries: scalar, IN/EXISTS, correlated, derived tables.

Expected values verified against the real sqlite3 (live golden battery in
test_sqlite_parity.py; corpus files tests/data/subquery*.test carry the
sqllogictest golden records). Focus here is on engine behavior that the
runner-level corpus cannot express: statement-level error reporting, output
column metadata, and NULL three-valued logic details.
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


def rows(engine, sql):
    res = run(engine, sql)
    if res.error is not None:
        return None
    return [[render_value(v) for v in row] for row in res.rows]


def err(engine, sql):
    res = run(engine, sql)
    return res.error


class TestScalarSubquery(unittest.TestCase):
    """a1: scalar subqueries (plain and correlated) match sqlite."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER, b TEXT);")
        run(self.engine, "INSERT INTO t1 VALUES (1,'x'),(2,'y'),(3,'z');")
        run(self.engine, "CREATE TABLE t2(c INTEGER);")
        run(self.engine, "INSERT INTO t2 VALUES (10),(20);")

    def test_constant(self):
        self.assertEqual(flat(self.engine, "SELECT (SELECT 1);"), ["1"])

    def test_single_row(self):
        self.assertEqual(flat(self.engine, "SELECT (SELECT c FROM t2 WHERE c = 10);"), ["10"])

    def test_empty_is_null(self):
        self.assertEqual(flat(self.engine, "SELECT (SELECT c FROM t2 WHERE c = 99);"), ["NULL"])

    def test_multi_row_takes_first(self):
        # sqlite 3.46 returns the first row for a multi-row scalar subquery
        # (verified against the sandbox sqlite; no 'more than one row' error).
        self.assertEqual(flat(self.engine, "SELECT (SELECT c FROM t2);"), ["10"])

    def test_multi_column_errors(self):
        self.assertIn("sub-select returns 2 columns", err(self.engine, "SELECT (SELECT a, b FROM t1);"))
        self.assertIn("sub-select returns 2 columns", err(self.engine, "SELECT (SELECT a, b FROM t1 WHERE 0);"))

    def test_correlated(self):
        self.assertEqual(
            rows(self.engine, "SELECT a, (SELECT COUNT(*) FROM t2 WHERE c > a*5) FROM t1 ORDER BY a;"),
            [["1", "2"], ["2", "1"], ["3", "1"]],
        )

    def test_correlated_sum(self):
        self.assertEqual(
            rows(self.engine, "SELECT a, (SELECT SUM(c) FROM t2 WHERE c < a*15) FROM t1 ORDER BY a;"),
            [["1", "10"], ["2", "30"], ["3", "30"]],
        )

    def test_nested_scalar(self):
        self.assertEqual(
            flat(self.engine, "SELECT (SELECT (SELECT a FROM t1 WHERE a = 1));"),
            ["1"],
        )

    def test_in_where(self):
        self.assertEqual(
            rows(self.engine, "SELECT a FROM t1 WHERE (SELECT COUNT(*) FROM t2) > 1 ORDER BY a;"),
            [["1"], ["2"], ["3"]],
        )

    def test_in_order_by(self):
        self.assertEqual(
            flat(self.engine, "SELECT a FROM t1 ORDER BY (SELECT COUNT(*) FROM t2 WHERE c > a*5), a;"),
            ["2", "3", "1"],
        )

    def test_missing_table_errors(self):
        self.assertIn("no such table", err(self.engine, "SELECT (SELECT c FROM nosuch);"))

    def test_missing_column_errors(self):
        self.assertIn("no such column", err(self.engine, "SELECT (SELECT z FROM t1);"))


class TestInExistsSubquery(unittest.TestCase):
    """a2: IN / NOT IN / EXISTS with sqlite three-valued NULL logic."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER);")
        run(self.engine, "INSERT INTO t1 VALUES (1),(2);")
        run(self.engine, "CREATE TABLE n(v INTEGER);")
        run(self.engine, "INSERT INTO n VALUES (NULL),(2);")

    def test_in_match(self):
        self.assertEqual(flat(self.engine, "SELECT 2 IN (SELECT v FROM n);"), ["1"])

    def test_in_no_match_with_null_is_null(self):
        # 1 not in {NULL,2}: NULL present and no match -> NULL
        self.assertEqual(flat(self.engine, "SELECT 1 IN (SELECT v FROM n);"), ["NULL"])

    def test_null_left_is_null(self):
        self.assertEqual(flat(self.engine, "SELECT NULL IN (SELECT v FROM n);"), ["NULL"])

    def test_in_empty_set_is_false(self):
        self.assertEqual(flat(self.engine, "SELECT 1 IN (SELECT v FROM n WHERE 0);"), ["0"])

    def test_null_in_empty_set_is_false(self):
        self.assertEqual(flat(self.engine, "SELECT NULL IN (SELECT v FROM n WHERE 0);"), ["0"])

    def test_not_in_with_null_is_null(self):
        self.assertEqual(flat(self.engine, "SELECT 1 NOT IN (SELECT v FROM n);"), ["NULL"])

    def test_not_in_empty_set_is_true(self):
        self.assertEqual(flat(self.engine, "SELECT NULL NOT IN (SELECT v FROM n WHERE 0);"), ["1"])

    def test_exists(self):
        self.assertEqual(flat(self.engine, "SELECT EXISTS (SELECT v FROM n);"), ["1"])
        self.assertEqual(flat(self.engine, "SELECT EXISTS (SELECT v FROM n WHERE v = 99);"), ["0"])

    def test_not_exists(self):
        self.assertEqual(
            flat(self.engine, "SELECT NOT EXISTS (SELECT v FROM n WHERE v = 99);"),
            ["1"],
        )

    def test_exists_multi_column_ok(self):
        # EXISTS accepts any column count
        self.assertEqual(
            flat(self.engine, "SELECT EXISTS (SELECT a, v FROM n CROSS JOIN t1);"),
            ["1"],
        )

    def test_in_multi_column_errors(self):
        self.assertIn("sub-select returns 2 columns", err(self.engine, "SELECT 1 IN (SELECT v, v FROM n);"))

    def test_in_subquery_column_affinity(self):
        run(self.engine, "CREATE TABLE s(x TEXT);")
        run(self.engine, "INSERT INTO s VALUES ('5');")
        self.assertEqual(flat(self.engine, "SELECT 5 IN (SELECT x FROM s);"), ["1"])

    def test_missing_column_errors(self):
        self.assertIn("no such column", err(self.engine, "SELECT a FROM t1 WHERE a IN (SELECT z FROM n);"))


class TestDerivedTable(unittest.TestCase):
    """a3: FROM (SELECT ...) derived tables, aliases, nesting."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER, b TEXT);")
        run(self.engine, "INSERT INTO t1 VALUES (1,'x'),(2,'y'),(3,'z');")
        run(self.engine, "CREATE TABLE t2(c INTEGER);")
        run(self.engine, "INSERT INTO t2 VALUES (10),(20);")

    def test_no_alias(self):
        self.assertEqual(flat(self.engine, "SELECT * FROM (SELECT 1);"), ["1"])

    def test_alias_and_column(self):
        self.assertEqual(flat(self.engine, "SELECT d.x FROM (SELECT 1 AS x) AS d;"), ["1"])

    def test_expression_column(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM (SELECT a, a+1 AS p FROM t1) AS d ORDER BY a;"),
            [["1", "2"], ["2", "3"], ["3", "4"]],
        )

    def test_star_through_derived(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM (SELECT * FROM t1) AS d ORDER BY a;"),
            [["1", "x"], ["2", "y"], ["3", "z"]],
        )

    def test_nested_derived(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM (SELECT * FROM (SELECT a FROM t1) AS e) AS d ORDER BY a;"),
            [["1"], ["2"], ["3"]],
        )

    def test_join_with_derived(self):
        self.assertEqual(
            rows(self.engine, "SELECT d.a, t2.c FROM (SELECT a FROM t1) AS d JOIN t2 ON d.a*10 = t2.c ORDER BY d.a;"),
            [["1", "10"], ["2", "20"]],
        )

    def test_left_join_derived(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM (SELECT a FROM t1) AS d LEFT JOIN t2 ON d.a*10 = t2.c ORDER BY d.a;"),
            [["1", "10"], ["2", "20"], ["3", "NULL"]],
        )

    def test_group_by_inside_derived(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM (SELECT b, COUNT(*) AS n FROM t1 GROUP BY b) AS g ORDER BY b;"),
            [["x", "1"], ["y", "1"], ["z", "1"]],
        )

    def test_order_limit_inside_derived(self):
        self.assertEqual(
            flat(self.engine, "SELECT * FROM (SELECT a FROM t1 ORDER BY a DESC LIMIT 1) AS d;"),
            ["3"],
        )

    def test_count_derived_empty(self):
        run(self.engine, "CREATE TABLE ee(x INTEGER);")
        self.assertEqual(flat(self.engine, "SELECT COUNT(*) FROM (SELECT x FROM ee) AS d;"), ["0"])

    def test_correlated_reference_in_derived_errors(self):
        # sqlite: a derived table cannot reference the enclosing query
        self.assertIn("no such column", err(self.engine, "SELECT * FROM (SELECT a FROM t1 WHERE t1.a = t2.c) AS d;"))

    def test_missing_column_in_derived_errors(self):
        self.assertIn("no such column", err(self.engine, "SELECT * FROM (SELECT z FROM t1) AS d;"))


class TestTableAlias(unittest.TestCase):
    """Table aliases are required by correlated self-references."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t(a INTEGER, b TEXT);")
        run(self.engine, "INSERT INTO t VALUES (1,'x'),(2,'y');")

    def test_as_alias(self):
        self.assertEqual(rows(self.engine, "SELECT x.a FROM t AS x WHERE x.a = 2;"), [["2"]])

    def test_bare_alias(self):
        self.assertEqual(rows(self.engine, "SELECT x.a FROM t x WHERE x.a = 2;"), [["2"]])

    def test_original_name_hidden(self):
        self.assertIn("no such column", err(self.engine, "SELECT t.a FROM t AS x;"))

    def test_correlated_self_reference(self):
        self.assertEqual(
            rows(self.engine, "SELECT a, (SELECT b FROM t AS x WHERE x.a = t.a) FROM t ORDER BY a;"),
            [["1", "x"], ["2", "y"]],
        )

    def test_correlated_in(self):
        self.assertEqual(
            rows(self.engine, "SELECT t.a FROM t WHERE t.a IN (SELECT x.a FROM t AS x WHERE x.a <= t.a) ORDER BY t.a;"),
            [["1"], ["2"]],
        )


class TestOutputAlias(unittest.TestCase):
    """Select-list aliases usable in ORDER BY / GROUP BY (sqlite behavior)."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t(a INTEGER, b INTEGER);")
        run(self.engine, "INSERT INTO t VALUES (2,10),(1,20);")

    def test_order_by_alias(self):
        self.assertEqual(rows(self.engine, "SELECT a AS z FROM t ORDER BY z;"), [["1"], ["2"]])

    def test_order_by_alias_desc(self):
        self.assertEqual(rows(self.engine, "SELECT a AS z FROM t ORDER BY z DESC;"), [["2"], ["1"]])

    def test_group_by_alias(self):
        self.assertEqual(rows(self.engine, "SELECT a AS m FROM t GROUP BY m ORDER BY m;"), [["1"], ["2"]])


class TestSubqueryFailurePaths(unittest.TestCase):
    """a5: subquery errors fail the statement (runner continues, exits non-zero)."""

    def test_engine_returns_error_not_crash(self):
        eng = Engine()
        run(eng, "CREATE TABLE t1(a INTEGER);")
        run(eng, "INSERT INTO t1 VALUES (1);")
        run(eng, "CREATE TABLE t2(c INTEGER);")
        res = eng.execute("SELECT a FROM t1 WHERE a IN (SELECT z FROM t2);")
        self.assertIsNotNone(res.error)
        self.assertIn("no such column", res.error)


if __name__ == "__main__":
    unittest.main()
