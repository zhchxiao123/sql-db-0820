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

    # -- slice 4: aggregation ----------------------------------------------------

    def test_aggregate_battery(self):
        self.assert_parity([
            "CREATE TABLE e(x INTEGER);",
            "SELECT COUNT(*) FROM e;",
            "SELECT COUNT(x) FROM e;",
            "SELECT SUM(x) FROM e;",
            "SELECT TOTAL(x) FROM e;",
            "SELECT AVG(x) FROM e;",
            "SELECT MIN(x) FROM e;",
            "SELECT MAX(x) FROM e;",
            "INSERT INTO e VALUES (NULL), (NULL);",
            "SELECT COUNT(*) FROM e;",
            "SELECT COUNT(x) FROM e;",
            "SELECT SUM(x) FROM e;",
            "SELECT TOTAL(x) FROM e;",
            "SELECT AVG(x) FROM e;",
            "SELECT MIN(x) FROM e;",
            "SELECT MAX(x) FROM e;",
            "CREATE TABLE s(x);",
            "INSERT INTO s VALUES ('1'), ('abc'), (2), ('1.5');",
            "SELECT SUM(x), TOTAL(x), AVG(x), MIN(x), MAX(x), COUNT(x) FROM s;",
            "SELECT SUM('abc');",
            "SELECT SUM('1');",
            "CREATE TABLE n(x INTEGER);",
            "INSERT INTO n VALUES (1), (2), (3);",
            "SELECT SUM(x), AVG(x), SUM(x)*1.0 FROM n;",
            "SELECT COUNT(*)+1, 2*COUNT(*), SUM(x)/COUNT(x) FROM n;",
            "SELECT COUNT(*);",
            "SELECT COUNT()+1;",
        ])

    def test_group_by_battery(self):
        self.assert_parity([
            "CREATE TABLE g(k TEXT, v INTEGER);",
            "INSERT INTO g VALUES ('a',1),('b',2),('a',3),(NULL,4),(NULL,5),('a',NULL);",
            "SELECT k, COUNT(*), SUM(v), AVG(v), MIN(v), MAX(v) FROM g GROUP BY k ORDER BY k;",
            "SELECT k FROM g GROUP BY k ORDER BY k;",
            "SELECT k, COUNT(*) FROM g GROUP BY k HAVING COUNT(*) >= 2 ORDER BY k;",
            "SELECT k, SUM(v) FROM g GROUP BY k HAVING SUM(v) > 5 ORDER BY k;",
            "SELECT k FROM g GROUP BY k HAVING k IS NOT NULL ORDER BY k;",
            "SELECT COUNT(*) FROM g HAVING COUNT(*) > 0;",
            "SELECT k, COUNT(*) FROM g GROUP BY k ORDER BY COUNT(*) DESC, k;",
            "SELECT COUNT(DISTINCT k), SUM(DISTINCT v) FROM g;",
            "CREATE TABLE m(a INTEGER, b TEXT, v INTEGER);",
            "INSERT INTO m VALUES (1,'x',1),(1,'x',2),(1,'y',3),(2,'x',4);",
            "SELECT a, b, COUNT(*), SUM(v) FROM m GROUP BY a, b ORDER BY a, b;",
            "CREATE TABLE w(a INTEGER, b TEXT);",
            "INSERT INTO w VALUES (1,'first'),(1,'second'),(2,'only');",
            "SELECT a, b, COUNT(*) FROM w GROUP BY a ORDER BY a;",
            "CREATE TABLE e2(x INTEGER);",
            "SELECT x, COUNT(*) FROM e2 GROUP BY x;",
        ])

    def test_aggregate_errors_agree(self):
        self.assert_parity([
            "CREATE TABLE g(k TEXT, v INTEGER);",
            "INSERT INTO g VALUES ('a', 1);",
            "SELECT COUNT(z) FROM g;",
            "SELECT SUM(*) FROM g;",
            "SELECT SUM() FROM g;",
            "SELECT AVG() FROM g;",
            "SELECT v FROM g GROUP BY z;",
            "SELECT * FROM g WHERE COUNT(*) > 0;",
        ])

    # -- slice 3: CREATE / DROP INDEX -------------------------------------------

    def test_index_parity(self):
        self.assert_parity([
            "CREATE TABLE t(a INTEGER, b TEXT, c REAL);",
            "INSERT INTO t VALUES (1, 'x', 1.5), (2, 'y', 2.5), (3, 'z', 3.5);",
            "CREATE INDEX idx_a ON t(a);",
            "CREATE INDEX idx_ab ON t(a, b);",
            "CREATE INDEX idx_a ON t(a);",
            "CREATE INDEX IF NOT EXISTS idx_a ON t(a);",
            "CREATE INDEX idx_missing ON nosuch(a);",
            "CREATE INDEX idx_missing ON t(z);",
            "CREATE UNIQUE INDEX uidx_a ON t(a);",
            "SELECT * FROM t WHERE a = 2;",
            "SELECT * FROM t WHERE a >= 2 AND a < 3;",
            "SELECT * FROM t WHERE b LIKE 'y%';",
            "SELECT a, b FROM t ORDER BY a DESC;",
            "INSERT INTO t VALUES (4, 'w', 4.5);",
            "DELETE FROM t WHERE a = 4;",
            "SELECT * FROM t WHERE a IS NOT NULL ORDER BY b;",
            "DROP INDEX idx_a;",
            "DROP INDEX idx_a;",
            "DROP INDEX nosuch;",
            "DROP INDEX IF EXISTS nosuch;",
            "CREATE INDEX idx_dirs ON t(a DESC, b);",
            "SELECT * FROM t ORDER BY a;",
        ])

    def test_index_errors_agree(self):
        self.assert_parity([
            "CREATE TABLE t(a INTEGER);",
            "CREATE INDEX i ON nosuch(a);",
            "CREATE INDEX i ON t(z);",
            "CREATE INDEX i ON t;",
            "DROP INDEX nosuch;",
            "CREATE INDEX;",
        ])

    # -- slice 4: multi-table JOINs ----------------------------------------------

    def test_join_inner_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER, b TEXT);",
            "CREATE TABLE t2(a INTEGER, c TEXT);",
            "INSERT INTO t1 VALUES (1,'x'),(2,'y'),(3,'z');",
            "INSERT INTO t2 VALUES (2,'p'),(3,'q'),(4,'r');",
            "SELECT * FROM t1 JOIN t2 ON t1.a = t2.a ORDER BY t1.a;",
            "SELECT t1.a, t2.c FROM t1 INNER JOIN t2 ON t1.a = t2.a ORDER BY t1.a;",
            # NULL keys never match (not even NULL = NULL)
            "CREATE TABLE n(a INTEGER);",
            "INSERT INTO n VALUES (1),(NULL),(2);",
            "SELECT * FROM t1 JOIN n ON t1.a = n.a ORDER BY t1.a;",
            # range + composite conditions reuse the expression engine
            "CREATE TABLE r(x INTEGER, y INTEGER);",
            "INSERT INTO r VALUES (0,25),(2,15),(5,5);",
            "SELECT * FROM t1 JOIN r ON t1.a >= r.x AND t1.a <= r.y ORDER BY t1.a, r.x;",
            "SELECT * FROM t1 JOIN r ON t1.a < r.x AND t1.b > 'w' ORDER BY t1.a, r.x;",
            # no matches
            "CREATE TABLE m(a INTEGER);",
            "INSERT INTO m VALUES (9);",
            "SELECT COUNT(*) FROM t1 JOIN m ON t1.a = m.a;",
        ])

    def test_join_left_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER);",
            "CREATE TABLE t2(a INTEGER, c TEXT);",
            "INSERT INTO t1 VALUES (1),(2),(3);",
            "INSERT INTO t2 VALUES (2,'p'),(3,'q');",
            "SELECT * FROM t1 LEFT JOIN t2 ON t1.a = t2.a ORDER BY t1.a;",
            "SELECT * FROM t1 LEFT OUTER JOIN t2 ON t1.a = t2.a ORDER BY t1.a;",
            "SELECT t1.a, t2.c FROM t1 LEFT JOIN t2 ON t1.a = t2.a WHERE t2.a IS NULL ORDER BY t1.a;",
            # left row with a NULL key does not match the right NULL key
            "CREATE TABLE n(a INTEGER, c TEXT);",
            "INSERT INTO n VALUES (1,'p'),(NULL,'q');",
            "SELECT * FROM t1 LEFT JOIN n ON t1.a = n.a ORDER BY t1.a;",
            # ON false keeps every left row, NULL-padded
            "SELECT * FROM t1 LEFT JOIN t2 ON 0 ORDER BY t1.a;",
            # empty right side: all left rows NULL-padded
            "CREATE TABLE e(a INTEGER);",
            "SELECT t1.a, e.a FROM t1 LEFT JOIN e ON t1.a = e.a ORDER BY t1.a;",
            "SELECT COUNT(*) FROM t1 LEFT JOIN e;",
        ])

    def test_join_cross_comma_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER);",
            "CREATE TABLE t2(b INTEGER);",
            "INSERT INTO t1 VALUES (1),(2);",
            "INSERT INTO t2 VALUES (10),(20),(30);",
            "SELECT * FROM t1 CROSS JOIN t2 ORDER BY t1.a, t2.b;",
            "SELECT * FROM t1, t2 ORDER BY t1.a, t2.b;",
            "SELECT COUNT(*) FROM t1, t2;",
            "SELECT t1.a, t2.b FROM t1, t2 WHERE t1.a = t2.b/10 ORDER BY t1.a;",
            "SELECT * FROM t1 JOIN t2 ORDER BY t1.a, t2.b;",
            "CREATE TABLE t3(c INTEGER);",
            "INSERT INTO t3 VALUES (100),(200);",
            "SELECT COUNT(*) FROM t1, t2 CROSS JOIN t3;",
        ])

    def test_join_using_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER, b TEXT);",
            "CREATE TABLE t2(a INTEGER, c TEXT);",
            "INSERT INTO t1 VALUES (1,'x'),(2,'y');",
            "INSERT INTO t2 VALUES (2,'p'),(3,'q');",
            "SELECT * FROM t1 JOIN t2 USING (a) ORDER BY a;",
            "SELECT a, b, c FROM t1 JOIN t2 USING (a) ORDER BY a;",
            "SELECT t1.a, t2.a FROM t1 JOIN t2 USING (a) ORDER BY t1.a;",
            "SELECT a, b, c FROM t1 LEFT JOIN t2 USING (a) ORDER BY a;",
            "SELECT * FROM t1 JOIN t2 USING (a) WHERE a > 1 ORDER BY a;",
            "SELECT b FROM t1 JOIN t2 USING (a) WHERE a = 2;",
            # merged column not first in either table
            "CREATE TABLE u1(p INTEGER, a INTEGER, q INTEGER);",
            "CREATE TABLE u2(r INTEGER, a INTEGER, s INTEGER);",
            "INSERT INTO u1 VALUES (1,5,2);",
            "INSERT INTO u2 VALUES (3,5,4);",
            "SELECT * FROM u1 JOIN u2 USING (a);",
            # extra same-name column that is not merged stays ambiguous / qualified
            "CREATE TABLE v1(k INTEGER, x INTEGER);",
            "CREATE TABLE v2(k INTEGER, x INTEGER);",
            "INSERT INTO v1 VALUES (1,10);",
            "INSERT INTO v2 VALUES (1,20);",
            "SELECT * FROM v1 JOIN v2 USING (k);",
            "SELECT t1.x, t2.x FROM v1 JOIN v2 USING (k);",
        ])

    def test_join_multitable_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER);",
            "CREATE TABLE t2(a INTEGER, b INTEGER);",
            "CREATE TABLE t3(b INTEGER, c INTEGER);",
            "INSERT INTO t1 VALUES (1),(2);",
            "INSERT INTO t2 VALUES (1,10),(2,20);",
            "INSERT INTO t3 VALUES (10,100),(20,200);",
            "SELECT * FROM t1 JOIN t2 ON t1.a = t2.a JOIN t3 ON t2.b = t3.b ORDER BY t1.a;",
            "SELECT * FROM t1, t2 JOIN t3 ON t2.a = t3.b;",
            # USING chain merges k across all three tables
            "CREATE TABLE s1(k INTEGER, a INTEGER);",
            "CREATE TABLE s2(k INTEGER, b INTEGER);",
            "CREATE TABLE s3(k INTEGER, c INTEGER);",
            "INSERT INTO s1 VALUES (1,10);",
            "INSERT INTO s2 VALUES (1,20);",
            "INSERT INTO s3 VALUES (1,30);",
            "SELECT * FROM s1 JOIN s2 USING (k) JOIN s3 USING (k);",
            "SELECT k FROM s1 JOIN s2 USING (k) JOIN s3 USING (k);",
            # LEFT JOIN then INNER JOIN filters the padded rows
            "CREATE TABLE l(a INTEGER, b INTEGER);",
            "INSERT INTO l VALUES (1,10),(2,20);",
            "CREATE TABLE x(b INTEGER);",
            "INSERT INTO x VALUES (20),(30);",
            "SELECT * FROM t1 LEFT JOIN l ON t1.a = l.a JOIN x ON l.b = x.b ORDER BY t1.a;",
            # joins combined with group by / distinct / limit
            "CREATE TABLE g1(a INTEGER, k INTEGER);",
            "CREATE TABLE g2(a INTEGER, v TEXT);",
            "INSERT INTO g1 VALUES (1,1),(2,1),(3,2);",
            "INSERT INTO g2 VALUES (1,'x'),(2,'y');",
            "SELECT g2.v, COUNT(*) FROM g1 JOIN g2 ON g1.a = g2.a GROUP BY g2.v ORDER BY g2.v;",
            "SELECT DISTINCT t1.a FROM t1 JOIN t2 ON t1.a = t2.a ORDER BY t1.a;",
            "SELECT t1.a, t2.c FROM t1 LEFT JOIN t2 ON t1.a = t2.a ORDER BY t1.a LIMIT 2;",
        ])

    def test_join_qualified_star_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER, b INTEGER);",
            "CREATE TABLE t2(a INTEGER);",
            "INSERT INTO t1 VALUES (1,2);",
            "INSERT INTO t2 VALUES (1);",
            "SELECT t1.* FROM t1 JOIN t2 ON t1.a = t2.a;",
            "SELECT t2.*, t1.* FROM t1 JOIN t2 ON t1.a = t2.a;",
            # qualified references work in single-table queries too
            "SELECT t1.a FROM t1;",
        ])

    def test_join_errors_agree(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER);",
            "CREATE TABLE t2(a INTEGER, b INTEGER);",
            "INSERT INTO t1 VALUES (1);",
            "INSERT INTO t2 VALUES (1,2);",
            "SELECT a FROM t1 JOIN t2 ON t1.a = t2.a;",
            "SELECT t1.nope FROM t1 JOIN t2 ON t1.a = t2.b;",
            "SELECT * FROM t1 JOIN t2 ON t1.a = t2.nope;",
            "SELECT * FROM t1 JOIN nope ON t1.a = nope.b;",
            "CREATE TABLE t3(c INTEGER);",
            "SELECT * FROM t1 JOIN t2 ON t1.a = t3.c;",
            "SELECT * FROM t1 JOIN t2 USING (b);",
            "CREATE TABLE u(a INTEGER);",
            "INSERT INTO u VALUES (1);",
            "SELECT * FROM t1 JOIN u USING (b);",
            "SELECT nope.* FROM t1 JOIN t2 ON 1;",
            "SELECT * FROM t1 JOIN t1 ON t1.a = t1.a;",
        ])

    # -- slice 5: subqueries --------------------------------------------------

    def test_scalar_subquery_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER, b TEXT);",
            "INSERT INTO t1 VALUES (1,'x'),(2,'y'),(3,'z');",
            "CREATE TABLE t2(c INTEGER);",
            "INSERT INTO t2 VALUES (10),(20);",
            "SELECT (SELECT 1);",
            "SELECT (SELECT c FROM t2 WHERE c = 10);",
            "SELECT (SELECT c FROM t2 WHERE c = 99);",
            "SELECT (SELECT c FROM t2);",
            "SELECT a, (SELECT c FROM t2 WHERE c = a*10) FROM t1 ORDER BY a;",
            "SELECT a, (SELECT COUNT(*) FROM t2 WHERE c > a*5) FROM t1 ORDER BY a;",
            "SELECT a, (SELECT SUM(c) FROM t2 WHERE c < a*15) FROM t1 ORDER BY a;",
            "SELECT a, (SELECT MAX(c) FROM t2 WHERE c <= a*10) FROM t1 ORDER BY a;",
            "SELECT (SELECT b FROM t1 WHERE a = 1);",
            "SELECT (SELECT b FROM t1 WHERE a = 99);",
            "SELECT (SELECT (SELECT a FROM t1 WHERE a = 1));",
            "SELECT (SELECT 1), (SELECT 'a');",
            "SELECT CASE WHEN (SELECT COUNT(*) FROM t2) > 1 THEN 'many' ELSE 'few' END;",
            "SELECT a FROM t1 WHERE (SELECT COUNT(*) FROM t2) > 0 ORDER BY a;",
            "SELECT a FROM t1 ORDER BY (SELECT COUNT(*) FROM t2 WHERE c > a*5), a;",
        ])

    def test_in_exists_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER);",
            "INSERT INTO t1 VALUES (1),(2);",
            "CREATE TABLE t2(c INTEGER);",
            "INSERT INTO t2 VALUES (10),(20);",
            "SELECT a FROM t1 WHERE a IN (SELECT c/10 FROM t2) ORDER BY a;",
            "SELECT a FROM t1 WHERE a NOT IN (SELECT c/10 FROM t2) ORDER BY a;",
            "SELECT 1 IN (SELECT c FROM t2);",
            "SELECT 5 IN (SELECT c FROM t2);",
            "SELECT NULL IN (SELECT c FROM t2);",
            "SELECT 5 IN (SELECT c FROM t2 WHERE 0);",
            "SELECT NULL IN (SELECT c FROM t2 WHERE 0);",
            "SELECT NULL NOT IN (SELECT c FROM t2 WHERE 0);",
            "SELECT EXISTS (SELECT c FROM t2);",
            "SELECT EXISTS (SELECT c FROM t2 WHERE c = 99);",
            "SELECT a FROM t1 WHERE EXISTS (SELECT 1 FROM t2 WHERE c = a*10) ORDER BY a;",
            "SELECT a FROM t1 WHERE NOT EXISTS (SELECT 1 FROM t2 WHERE c = a*10+1) ORDER BY a;",
            # NULL three-valued logic
            "CREATE TABLE n(v INTEGER);",
            "INSERT INTO n VALUES (NULL),(2);",
            "SELECT 1 IN (SELECT v FROM n);",
            "SELECT 2 IN (SELECT v FROM n);",
            "SELECT NULL IN (SELECT v FROM n);",
            "SELECT 1 NOT IN (SELECT v FROM n);",
            "SELECT 2 NOT IN (SELECT v FROM n);",
            "SELECT 3 IN (SELECT v FROM n);",
            "SELECT 3 NOT IN (SELECT v FROM n);",
            # affinity in IN
            "CREATE TABLE s(x TEXT);",
            "INSERT INTO s VALUES ('5');",
            "SELECT 5 IN (SELECT x FROM s);",
            "CREATE TABLE i(y INTEGER);",
            "INSERT INTO i VALUES (5);",
            "SELECT '5' IN (SELECT y FROM i);",
        ])

    def test_derived_table_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER, b TEXT);",
            "INSERT INTO t1 VALUES (1,'x'),(2,'y'),(3,'z');",
            "CREATE TABLE t2(c INTEGER);",
            "INSERT INTO t2 VALUES (10),(20);",
            "SELECT * FROM (SELECT 1);",
            "SELECT * FROM (SELECT 1) AS d;",
            "SELECT d.x FROM (SELECT 1 AS x) AS d;",
            "SELECT * FROM (SELECT a, a+1 AS p FROM t1) AS d ORDER BY a;",
            "SELECT * FROM (SELECT * FROM t1) AS d ORDER BY a;",
            "SELECT * FROM (SELECT * FROM (SELECT a FROM t1) AS e) AS d ORDER BY a;",
            "SELECT COUNT(*) FROM (SELECT a FROM t1);",
            "SELECT d.a, t2.c FROM (SELECT a FROM t1) AS d JOIN t2 ON d.a*10 = t2.c ORDER BY d.a;",
            "SELECT * FROM (SELECT a FROM t1) AS d LEFT JOIN t2 ON d.a*10 = t2.c ORDER BY d.a;",
            "SELECT * FROM (SELECT b, COUNT(*) AS n FROM t1 GROUP BY b) AS g ORDER BY b;",
            "SELECT * FROM (SELECT a FROM t1 ORDER BY a DESC LIMIT 1) AS d;",
            "SELECT * FROM (SELECT a FROM t1) AS d1 JOIN (SELECT a FROM t1) AS d2 USING (a) ORDER BY a;",
            # empty derived + LEFT JOIN pad
            "CREATE TABLE ee(x INTEGER);",
            "SELECT COUNT(*) FROM (SELECT x FROM ee) AS d;",
            "SELECT t1.a FROM t1 LEFT JOIN (SELECT x FROM ee) AS d ON t1.a = d.x ORDER BY t1.a;",
        ])

    def test_correlated_alias_battery(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER, b TEXT);",
            "INSERT INTO t1 VALUES (1,'x'),(2,'y');",
            "CREATE TABLE t2(c INTEGER);",
            "INSERT INTO t2 VALUES (10),(20);",
            "SELECT x.a FROM t1 AS x WHERE x.a = 2;",
            "SELECT a, (SELECT c FROM t2 AS y WHERE y.c = x.a*10) FROM t1 AS x ORDER BY a;",
            "SELECT t1.a FROM t1 WHERE t1.a IN (SELECT t2.c/10 FROM t2 WHERE t2.c > t1.a*5) ORDER BY t1.a;",
            # 2-level correlation
            "CREATE TABLE outer1(a INTEGER);",
            "INSERT INTO outer1 VALUES (1),(2);",
            "CREATE TABLE mid(b INTEGER);",
            "INSERT INTO mid VALUES (1),(2),(3);",
            "SELECT a FROM outer1 WHERE EXISTS (SELECT 1 FROM mid WHERE EXISTS (SELECT 1 FROM t2 WHERE t2.c = mid.b*10 AND t2.c > outer1.a*10)) ORDER BY a;",
            # scalar over LEFT JOIN padded NULL
            "CREATE TABLE l(k INTEGER);",
            "INSERT INTO l VALUES (1),(2);",
            "CREATE TABLE r(k INTEGER, v INTEGER);",
            "INSERT INTO r VALUES (2, 99);",
            "SELECT l.k, (SELECT v FROM r WHERE r.k = l.k) FROM l LEFT JOIN r ON l.k = r.k ORDER BY l.k;",
            # aliases in ORDER BY / GROUP BY
            "SELECT a AS z FROM t1 ORDER BY z;",
            "SELECT a AS m FROM t1 GROUP BY m ORDER BY m;",
            "SELECT a FROM t1 LIMIT (SELECT 1);",
        ])

    def test_subquery_errors_agree(self):
        self.assert_parity([
            "CREATE TABLE t1(a INTEGER);",
            "INSERT INTO t1 VALUES (1),(2);",
            "CREATE TABLE t2(c INTEGER);",
            "INSERT INTO t2 VALUES (10);",
            "SELECT (SELECT a, a FROM t1);",
            "SELECT (SELECT a, a FROM t1 WHERE 0);",
            "SELECT 1 IN (SELECT c, c FROM t2);",
            "SELECT a FROM t1 WHERE a IN (SELECT z FROM t2);",
            "SELECT a FROM t1 WHERE a IN (SELECT c FROM nosuch);",
            "SELECT (SELECT c FROM nosuch);",
            "SELECT (SELECT z FROM t1);",
            "SELECT * FROM (SELECT z FROM t2) AS d;",
            "SELECT * FROM (SELECT c FROM nosuch) AS d;",
        ])


if __name__ == "__main__":
    unittest.main()
