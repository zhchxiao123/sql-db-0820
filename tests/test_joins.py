"""Unit tests for multi-table JOINs (INNER/LEFT/CROSS, comma FROM, ON/USING).

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


def rows(engine, sql):
    res = run(engine, sql)
    if res.error is not None:
        return None
    return [[render_value(v) for v in row] for row in res.rows]


class TestInnerJoin(unittest.TestCase):
    """a1: two-table INNER JOIN (equi / range / composite conditions)."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER, b TEXT);")
        run(self.engine, "CREATE TABLE t2(a INTEGER, c TEXT);")
        run(self.engine, "INSERT INTO t1 VALUES (1,'x'),(2,'y'),(3,'z');")
        run(self.engine, "INSERT INTO t2 VALUES (2,'p'),(3,'q'),(4,'r');")

    def test_equi(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 JOIN t2 ON t1.a = t2.a ORDER BY t1.a;"),
            [["2", "y", "2", "p"], ["3", "z", "3", "q"]],
        )

    def test_inner_keyword(self):
        self.assertEqual(
            rows(self.engine, "SELECT t1.a, t2.c FROM t1 INNER JOIN t2 ON t1.a = t2.a ORDER BY t1.a;"),
            [["2", "p"], ["3", "q"]],
        )

    def test_range_condition(self):
        run(self.engine, "CREATE TABLE r(x INTEGER, y INTEGER);")
        run(self.engine, "INSERT INTO r VALUES (0,25),(2,15),(5,5);")
        self.assertEqual(
            rows(self.engine, "SELECT t1.a, r.x FROM t1 JOIN r ON t1.a >= r.x AND t1.a <= r.y ORDER BY t1.a, r.x;"),
            [["1", "0"], ["2", "0"], ["2", "2"], ["3", "0"], ["3", "2"]],
        )

    def test_null_key_never_matches(self):
        run(self.engine, "CREATE TABLE n(a INTEGER);")
        run(self.engine, "INSERT INTO n VALUES (1),(NULL),(2);")
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 JOIN n ON t1.a = n.a ORDER BY t1.a;"),
            [["1", "x", "1"], ["2", "y", "2"]],
        )

    def test_no_matches(self):
        run(self.engine, "CREATE TABLE m(a INTEGER);")
        run(self.engine, "INSERT INTO m VALUES (9),(10);")
        self.assertEqual(flat(self.engine, "SELECT COUNT(*) FROM t1 JOIN m ON t1.a = m.a;"), ["0"])

    def test_qualified_columns(self):
        self.assertEqual(
            rows(self.engine, "SELECT t1.a, t2.a FROM t1 JOIN t2 ON t1.a = t2.a ORDER BY t1.a;"),
            [["2", "2"], ["3", "3"]],
        )


class TestLeftJoin(unittest.TestCase):
    """a2: LEFT OUTER JOIN pads unmatched rows with NULLs."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER);")
        run(self.engine, "CREATE TABLE t2(a INTEGER, c TEXT);")
        run(self.engine, "INSERT INTO t1 VALUES (1),(2),(3);")
        run(self.engine, "INSERT INTO t2 VALUES (2,'p'),(3,'q');")

    def test_padding(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 LEFT JOIN t2 ON t1.a = t2.a ORDER BY t1.a;"),
            [["1", "NULL", "NULL"], ["2", "2", "p"], ["3", "3", "q"]],
        )

    def test_outer_keyword(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 LEFT OUTER JOIN t2 ON t1.a = t2.a ORDER BY t1.a;"),
            [["1", "NULL", "NULL"], ["2", "2", "p"], ["3", "3", "q"]],
        )

    def test_anti_join_pattern(self):
        self.assertEqual(
            rows(self.engine, "SELECT t1.a FROM t1 LEFT JOIN t2 ON t1.a = t2.a WHERE t2.a IS NULL ORDER BY t1.a;"),
            [["1"]],
        )

    def test_left_null_key_does_not_match_null(self):
        run(self.engine, "CREATE TABLE n(a INTEGER, c TEXT);")
        run(self.engine, "INSERT INTO n VALUES (1,'p'),(NULL,'q');")
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 LEFT JOIN n ON t1.a = n.a ORDER BY t1.a;"),
            [["1", "1", "p"], ["2", "NULL", "NULL"], ["3", "NULL", "NULL"]],
        )

    def test_empty_right_table(self):
        run(self.engine, "CREATE TABLE e(a INTEGER);")
        self.assertEqual(
            rows(self.engine, "SELECT t1.a, e.a FROM t1 LEFT JOIN e ON t1.a = e.a ORDER BY t1.a;"),
            [["1", "NULL"], ["2", "NULL"], ["3", "NULL"]],
        )

    def test_on_always_false(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 LEFT JOIN t2 ON 0 ORDER BY t1.a;"),
            [["1", "NULL", "NULL"], ["2", "NULL", "NULL"], ["3", "NULL", "NULL"]],
        )


class TestCrossAndComma(unittest.TestCase):
    """a3: CROSS JOIN and comma FROM produce cartesian products."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER);")
        run(self.engine, "CREATE TABLE t2(b INTEGER);")
        run(self.engine, "INSERT INTO t1 VALUES (1),(2);")
        run(self.engine, "INSERT INTO t2 VALUES (10),(20),(30);")

    def test_cross(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 CROSS JOIN t2 ORDER BY t1.a, t2.b;"),
            [["1", "10"], ["1", "20"], ["1", "30"], ["2", "10"], ["2", "20"], ["2", "30"]],
        )

    def test_comma(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1, t2 ORDER BY t1.a, t2.b;"),
            [["1", "10"], ["1", "20"], ["1", "30"], ["2", "10"], ["2", "20"], ["2", "30"]],
        )

    def test_comma_with_where(self):
        self.assertEqual(
            rows(self.engine, "SELECT t1.a, t2.b FROM t1, t2 WHERE t1.a = t2.b/10 ORDER BY t1.a;"),
            [["1", "10"], ["2", "20"]],
        )

    def test_join_without_on_is_cross(self):
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 JOIN t2 ORDER BY t1.a, t2.b;"),
            [["1", "10"], ["1", "20"], ["1", "30"], ["2", "10"], ["2", "20"], ["2", "30"]],
        )


class TestUsing(unittest.TestCase):
    """USING merges the join column: one star occurrence, unqualified ref."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER, b TEXT);")
        run(self.engine, "CREATE TABLE t2(a INTEGER, c TEXT);")
        run(self.engine, "INSERT INTO t1 VALUES (1,'x'),(2,'y');")
        run(self.engine, "INSERT INTO t2 VALUES (2,'p'),(3,'q');")

    def test_star_merges_once(self):
        self.assertEqual(rows(self.engine, "SELECT * FROM t1 JOIN t2 USING (a) ORDER BY a;"),
                         [["2", "y", "p"]])

    def test_unqualified_merged_value(self):
        self.assertEqual(rows(self.engine, "SELECT a, b, c FROM t1 JOIN t2 USING (a) ORDER BY a;"),
                         [["2", "y", "p"]])

    def test_qualified_still_works(self):
        self.assertEqual(rows(self.engine, "SELECT t1.a, t2.a FROM t1 JOIN t2 USING (a) ORDER BY t1.a;"),
                         [["2", "2"]])

    def test_left_join_merged_is_left_value(self):
        self.assertEqual(
            rows(self.engine, "SELECT a, b, c FROM t1 LEFT JOIN t2 USING (a) ORDER BY a;"),
            [["1", "x", "NULL"], ["2", "y", "p"]],
        )

    def test_using_chain_three_tables(self):
        run(self.engine, "CREATE TABLE t3(k INTEGER, c INTEGER);")
        run(self.engine, "INSERT INTO t3 VALUES (1,30);")
        run(self.engine, "CREATE TABLE u1(k INTEGER, a INTEGER);")
        run(self.engine, "CREATE TABLE u2(k INTEGER, b INTEGER);")
        run(self.engine, "INSERT INTO u1 VALUES (1,10);")
        run(self.engine, "INSERT INTO u2 VALUES (1,20);")
        self.assertEqual(
            rows(self.engine, "SELECT * FROM u1 JOIN u2 USING (k) JOIN t3 ON u1.k = t3.k;"),
            [["1", "10", "20", "1", "30"]],
        )


class TestJoinErrors(unittest.TestCase):
    """a5: joins fail cleanly on bad tables / columns / ambiguous names."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER);")
        run(self.engine, "CREATE TABLE t2(a INTEGER, b INTEGER);")
        run(self.engine, "INSERT INTO t1 VALUES (1);")
        run(self.engine, "INSERT INTO t2 VALUES (1,2);")

    def test_ambiguous_column(self):
        err = run(self.engine, "SELECT a FROM t1 JOIN t2 ON t1.a = t2.a;").error
        self.assertIsNotNone(err)
        self.assertIn("ambiguous", err)

    def test_no_such_column_qualified(self):
        err = run(self.engine, "SELECT t1.nope FROM t1 JOIN t2 ON t1.a = t2.b;").error
        self.assertIsNotNone(err)
        self.assertIn("no such column", err)

    def test_no_such_column_in_on(self):
        err = run(self.engine, "SELECT * FROM t1 JOIN t2 ON t1.a = t2.nope;").error
        self.assertIsNotNone(err)
        self.assertIn("no such column", err)

    def test_no_such_table(self):
        err = run(self.engine, "SELECT * FROM t1 JOIN nope ON t1.a = nope.b;").error
        self.assertIsNotNone(err)
        self.assertIn("no such table", err)

    def test_on_cannot_see_later_table(self):
        run(self.engine, "CREATE TABLE t3(c INTEGER);")
        err = run(self.engine, "SELECT * FROM t1 JOIN t2 ON t1.a = t3.c;").error
        self.assertIsNotNone(err)
        self.assertIn("no such column", err)

    def test_using_column_missing(self):
        run(self.engine, "CREATE TABLE u(a INTEGER);")
        run(self.engine, "INSERT INTO u VALUES (1);")
        err = run(self.engine, "SELECT * FROM t1 JOIN u USING (b);").error
        self.assertIsNotNone(err)
        self.assertIn("using column", err)

    def test_duplicate_table_without_alias(self):
        err = run(self.engine, "SELECT * FROM t1 JOIN t1 ON t1.a = t1.a;").error
        self.assertIsNotNone(err)
        self.assertIn("more than once", err)

    def test_qualstar_bad_table(self):
        err = run(self.engine, "SELECT nope.* FROM t1 JOIN t2 ON 1;").error
        self.assertIsNotNone(err)
        self.assertIn("no such table", err)


class TestJoinCombos(unittest.TestCase):
    """Joins combined with the rest of the select pipeline."""

    def setUp(self):
        self.engine = Engine()
        run(self.engine, "CREATE TABLE t1(a INTEGER);")
        run(self.engine, "CREATE TABLE t2(a INTEGER, v TEXT);")
        run(self.engine, "INSERT INTO t1 VALUES (1),(2),(3),(4);")
        run(self.engine, "INSERT INTO t2 VALUES (2,'p'),(4,'q');")

    def test_left_join_order_limit(self):
        self.assertEqual(
            rows(self.engine, "SELECT t1.a, t2.v FROM t1 LEFT JOIN t2 ON t1.a = t2.a ORDER BY t1.a LIMIT 2;"),
            [["1", "NULL"], ["2", "p"]],
        )

    def test_left_join_group_count_null(self):
        self.assertEqual(
            rows(self.engine, "SELECT t1.a, COUNT(t2.v) FROM t1 LEFT JOIN t2 ON t1.a = t2.a GROUP BY t1.a ORDER BY t1.a;"),
            [["1", "0"], ["2", "1"], ["3", "0"], ["4", "1"]],
        )

    def test_join_distinct(self):
        run(self.engine, "INSERT INTO t1 VALUES (2);")
        run(self.engine, "INSERT INTO t2 VALUES (2,'p');")
        self.assertEqual(
            rows(self.engine, "SELECT DISTINCT t1.a FROM t1 JOIN t2 ON t1.a = t2.a ORDER BY t1.a;"),
            [["2"], ["4"]],
        )

    def test_three_table_chain(self):
        run(self.engine, "CREATE TABLE t3(b INTEGER, c INTEGER);")
        run(self.engine, "INSERT INTO t3 VALUES (10,100),(20,200);")
        run(self.engine, "CREATE TABLE l(a INTEGER, b INTEGER);")
        run(self.engine, "INSERT INTO l VALUES (1,10),(2,20);")
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 JOIN l ON t1.a = l.a JOIN t3 ON l.b = t3.b ORDER BY t1.a;"),
            [["1", "1", "10", "10", "100"], ["2", "2", "20", "20", "200"]],
        )

    def test_left_then_inner_filters(self):
        run(self.engine, "CREATE TABLE l(a INTEGER, b INTEGER);")
        run(self.engine, "INSERT INTO l VALUES (1,10),(2,20);")
        run(self.engine, "CREATE TABLE t3(b INTEGER);")
        run(self.engine, "INSERT INTO t3 VALUES (20),(30);")
        self.assertEqual(
            rows(self.engine, "SELECT * FROM t1 LEFT JOIN l ON t1.a = l.a JOIN t3 ON l.b = t3.b ORDER BY t1.a;"),
            [["2", "2", "20", "20"]],
        )

    def test_qualified_star(self):
        run(self.engine, "CREATE TABLE s(a INTEGER, b INTEGER);")
        run(self.engine, "INSERT INTO s VALUES (1,2);")
        self.assertEqual(rows(self.engine, "SELECT t1.*, s.* FROM t1 JOIN s ON 1 ORDER BY t1.a;"),
                         [["1", "1", "2"], ["2", "1", "2"], ["3", "1", "2"], ["4", "1", "2"]])

    def test_single_table_qualified_col(self):
        self.assertEqual(rows(self.engine, "SELECT t1.a FROM t1 ORDER BY t1.a;"),
                         [["1"], ["2"], ["3"], ["4"]])

    def test_join_expression_engine_reuse(self):
        run(self.engine, "CREATE TABLE m(b INTEGER);")
        run(self.engine, "INSERT INTO m VALUES (2),(4);")
        self.assertEqual(
            rows(self.engine, "SELECT t1.a, m.b FROM t1 JOIN m ON m.b = t1.a * 2 ORDER BY t1.a;"),
            [["1", "2"], ["2", "4"]],
        )


if __name__ == "__main__":
    unittest.main()
