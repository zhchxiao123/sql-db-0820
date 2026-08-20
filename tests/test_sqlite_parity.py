"""Golden parity tests against the real sqlite3 (stdlib).

The slice requirement calls for sqlite-identical type affinity, comparison
and LIKE semantics. Each scenario is a list of SQL statements executed both
by our engine and by an in-memory sqlite3 connection; SELECT results are
rendered with the same sqlite-CLI formatting and compared line by line,
and statement success/failure must agree.

This directly addresses the known risk that affinity/comparison semantics
are the deepest part of sqlite behavior.
"""

import sqlite3
import unittest

from sqldb.engine import Engine, render_value


def _is_select(sql: str) -> bool:
    return sql.strip().upper().startswith("SELECT")


class SqliteParityTest(unittest.TestCase):
    def assert_parity(self, script):
        engine = Engine()
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        for sql in script:
            ours = engine.execute(sql)
            try:
                if _is_select(sql):
                    rows = cur.execute(sql).fetchall()
                    err = None
                else:
                    cur.execute(sql)
                    rows = None
                    err = None
            except sqlite3.Error as e:
                rows = None
                err = str(e)
            if rows is not None:
                self.assertIsNone(ours.error, f"{sql}: our engine error: {ours.error}")
                expected = [[render_value(v) for v in row] for row in rows]
                actual = [[render_value(v) for v in row] for row in (ours.rows or [])]
                self.assertEqual(actual, expected, f"SELECT mismatch for {sql!r}")
            else:
                self.assertEqual(
                    ours.error is not None,
                    err is not None,
                    f"success/error mismatch for {sql!r}: ours={ours.error!r} sqlite={err!r}",
                )
        conn.close()

    # -- the acceptance scenarios -------------------------------------------------

    def test_a1_insert_order(self):
        self.assert_parity([
            "CREATE TABLE t(a INTEGER, b TEXT, c REAL);",
            "INSERT INTO t VALUES (1, 'one', 1.5), (2, 'two', 2.5), (3, 'three', 3.5);",
            "SELECT * FROM t;",
        ])

    def test_a2_where_battery(self):
        self.assert_parity([
            "CREATE TABLE t(a INTEGER, b TEXT, c REAL);",
            "INSERT INTO t VALUES (1, 'one', 1.5), (2, 'two', 2.5), (3, 'three', 3.5), (NULL, 'none', NULL);",
            "SELECT * FROM t WHERE a = 2;",
            "SELECT * FROM t WHERE a > 1;",
            "SELECT * FROM t WHERE a >= 2 AND a < 3;",
            "SELECT * FROM t WHERE a = 1 OR a = 3;",
            "SELECT * FROM t WHERE a IS NULL;",
            "SELECT * FROM t WHERE a IS NOT NULL;",
            "SELECT * FROM t WHERE b LIKE 't%';",
            "SELECT * FROM t WHERE b NOT LIKE 't%';",
            "SELECT * FROM t WHERE b LIKE '%O';",
            "SELECT * FROM t WHERE b LIKE '_wo';",
            "SELECT * FROM t WHERE c < 2.0;",
            "SELECT a FROM t WHERE b = 'two';",
        ])

    def test_a3_affinity_mixed(self):
        self.assert_parity([
            "CREATE TABLE m(a INTEGER, b TEXT, c REAL);",
            "INSERT INTO m VALUES (1, '2', 3), ('2', 'abc', '4'), ('abc', 5, 'x'), (3.5, '3.5', 7), (NULL, NULL, NULL);",
            "SELECT * FROM m;",
            "SELECT * FROM m WHERE a < 10;",
            "SELECT * FROM m WHERE a < '10';",
            "SELECT * FROM m WHERE a = '2';",
            "SELECT * FROM m WHERE b = 5;",
            "SELECT * FROM m WHERE b < '9';",
            "SELECT * FROM m WHERE c < '4';",
            "SELECT * FROM m WHERE c = 3;",
            "SELECT * FROM m WHERE a < b;",
            "SELECT * FROM m WHERE b < a;",
        ])

    def test_a4_delete(self):
        self.assert_parity([
            "CREATE TABLE d(x INTEGER, y TEXT);",
            "INSERT INTO d VALUES (1, 'a'), (2, 'b'), (3, 'c');",
            "DELETE FROM d WHERE x = 2;",
            "SELECT * FROM d;",
            "DELETE FROM d;",
            "SELECT * FROM d;",
            "INSERT INTO d VALUES (4, 'd'), (5, 'e');",
            "DELETE FROM d WHERE x > 3;",
            "SELECT * FROM d;",
            "DELETE FROM d WHERE x = 99;",
            "SELECT * FROM d;",
        ])

    # -- bare literal comparisons (no affinity) -----------------------------------

    def test_bare_literal_comparisons(self):
        self.assert_parity([
            "SELECT 5 = '5';",
            "SELECT '5' = 5;",
            "SELECT 5 < '9';",
            "SELECT 5 <= '5';",
            "SELECT 5 < '10';",
            "SELECT 10 < '9';",
            "SELECT 5 > 'abc';",
            "SELECT 5 = 5.0;",
            "SELECT 'a' < 'b';",
            "SELECT NULL < 5;",
            "SELECT NULL IS NULL;",
            "SELECT 5 IS '5';",
            "SELECT '5' IS 5;",
        ])

    # -- LIKE battery -------------------------------------------------------------

    def test_like_battery(self):
        self.assert_parity([
            "CREATE TABLE l(s TEXT);",
            "INSERT INTO l VALUES ('Hello'), ('World'), ('help'), (NULL), ('HELLO!'), ('100%'), ('a_b');",
            "SELECT * FROM l WHERE s LIKE 'h%';",
            "SELECT * FROM l WHERE s LIKE '%LLO';",
            "SELECT * FROM l WHERE s LIKE '_ello';",
            "SELECT * FROM l WHERE s LIKE 'h%' AND s NOT LIKE '%!';",
            "SELECT * FROM l WHERE s LIKE '100!%' ESCAPE '!';",
            "SELECT * FROM l WHERE s LIKE 'a!_b' ESCAPE '!';",
            "SELECT * FROM l WHERE s LIKE 'a%';",
            "SELECT * FROM l WHERE s LIKE '%%%';",
            "SELECT * FROM l WHERE s NOT LIKE '%';",
            "SELECT * FROM l WHERE s LIKE NULL;",
            "SELECT 1 LIKE 1;",
            "SELECT 'abc' LIKE 'A%';",
            "SELECT 'abc' LIKE 'x%';",
        ])

    # -- misc statement semantics ---------------------------------------------------

    def test_insert_column_list(self):
        self.assert_parity([
            "CREATE TABLE m(a INTEGER, b TEXT);",
            "INSERT INTO m(a) VALUES (1), (2);",
            "SELECT * FROM m;",
            "INSERT INTO m VALUES (3, 'x'), (4, 'y');",
            "SELECT * FROM m;",
        ])

    def test_select_const_and_expr(self):
        self.assert_parity([
            "CREATE TABLE s1(x INTEGER);",
            "INSERT INTO s1 VALUES (1), (2);",
            "SELECT 7 FROM s1;",
            "SELECT x+1 FROM s1;",
            "SELECT x*2, x FROM s1;",
            "SELECT * FROM s1 WHERE x = 1;",
        ])

    def test_errors_agree(self):
        self.assert_parity([
            "CREATE TABLE t(a INTEGER, b TEXT);",
            "INSERT INTO t VALUES (1);",
            "INSERT INTO t VALUES (1, 'x', 3);",
            "SELECT * FROM nosuch;",
            "DELETE FROM nosuch;",
            "CREATE TABLE t(x INTEGER);",
        ])

    def test_real_affinity_and_format(self):
        self.assert_parity([
            "CREATE TABLE r(x REAL);",
            "INSERT INTO r VALUES ('3'), (2), (1.5), ('0.5');",
            "SELECT * FROM r;",
            "SELECT x+0 FROM r;",
        ])

    # -- slice 2: ORDER BY / DISTINCT / LIMIT ----------------------------------

    def test_order_by_battery(self):
        self.assert_parity([
            "CREATE TABLE t(a INTEGER, b TEXT);",
            "INSERT INTO t VALUES (3, 'c'), (1, 'a'), (NULL, 'n'), (2, 'b'), (3, 'C'), (1, 'A');",
            "SELECT a FROM t ORDER BY a;",
            "SELECT a FROM t ORDER BY a DESC;",
            "SELECT a, b FROM t ORDER BY a, b;",
            "SELECT a, b FROM t ORDER BY a DESC, b ASC;",
            "SELECT b FROM t ORDER BY b;",
            "SELECT b FROM t ORDER BY b DESC;",
            "SELECT a, b FROM t ORDER BY a+1;",
            "SELECT a, b FROM t ORDER BY 1;",
            "SELECT a, b FROM t ORDER BY 2 DESC;",
            "SELECT a FROM t ORDER BY -a;",
            "SELECT a FROM t WHERE a IS NOT NULL ORDER BY a DESC;",
        ])

    def test_order_by_errors(self):
        self.assert_parity([
            "CREATE TABLE t(a INTEGER);",
            "INSERT INTO t VALUES (1), (2);",
            "SELECT a FROM t ORDER BY z;",
            "SELECT a FROM t ORDER BY 3;",
        ])

    def test_distinct_battery(self):
        self.assert_parity([
            "CREATE TABLE d(x INTEGER, y TEXT);",
            "INSERT INTO d VALUES (1, 'a'), (1, 'a'), (2, 'b'), (NULL, 'n'), (NULL, 'n'), (1, 'A');",
            "SELECT DISTINCT x FROM d;",
            "SELECT DISTINCT x, y FROM d;",
            "SELECT DISTINCT y FROM d;",
            "SELECT DISTINCT x FROM d ORDER BY x;",
            "SELECT DISTINCT x, y FROM d ORDER BY y;",
            "SELECT DISTINCT 5, 5.0;",
            "SELECT DISTINCT 5, '5';",
        ])

    def test_distinct_no_affinity_cross_type(self):
        self.assert_parity([
            "CREATE TABLE e(x);",
            "INSERT INTO e VALUES (5), ('5'), (5.0), ('abc');",
            "SELECT DISTINCT x FROM e;",
        ])

    def test_limit_battery(self):
        self.assert_parity([
            "CREATE TABLE n(v INTEGER);",
            "INSERT INTO n VALUES (1), (2), (3), (4), (5);",
            "SELECT v FROM n LIMIT 2;",
            "SELECT v FROM n LIMIT 0;",
            "SELECT v FROM n LIMIT -1;",
            "SELECT v FROM n LIMIT 2 OFFSET 2;",
            "SELECT v FROM n LIMIT 2 OFFSET 10;",
            "SELECT v FROM n LIMIT 2 OFFSET -1;",
            "SELECT v FROM n LIMIT 2, 1;",
            "SELECT v FROM n ORDER BY v DESC LIMIT 2;",
            "SELECT v FROM n LIMIT 1+1;",
            "SELECT v FROM n LIMIT -1 OFFSET 3;",
            "SELECT v FROM n LIMIT 3 OFFSET -5;",
            "SELECT v FROM n LIMIT '2';",
            "SELECT DISTINCT v FROM n ORDER BY v DESC LIMIT 2;",
        ])

    def test_limit_errors(self):
        self.assert_parity([
            "CREATE TABLE n(v INTEGER);",
            "INSERT INTO n VALUES (1), (2);",
            "SELECT v FROM n LIMIT NULL;",
            "SELECT v FROM n LIMIT 1.5;",
            "SELECT v FROM n LIMIT x;",
            "SELECT v FROM n LIMIT 1 OFFSET NULL;",
        ])


if __name__ == "__main__":
    unittest.main()
