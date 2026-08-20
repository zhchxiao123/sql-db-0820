"""Unit tests for the minimal expression engine (sqldb.engine).

Each test drives the engine through its public entry point, Engine.execute(),
and checks the rendered row values, exactly as the sqllogictest runner does.
"""

import unittest

from sqldb.engine import Engine, EngineError, render_value


def run(sql):
    return Engine().execute(sql)


def values(sql):
    """Rendered values of the single result row, or None on error."""
    res = run(sql)
    if res.error is not None:
        return None
    return [render_value(v) for v in res.rows[0]]


class TestLiterals(unittest.TestCase):
    def test_integer(self):
        self.assertEqual(values("SELECT 1;"), ["1"])
        self.assertEqual(values("SELECT -42;"), ["-42"])
        self.assertEqual(values("SELECT 0;"), ["0"])

    def test_real(self):
        self.assertEqual(values("SELECT 3.5;"), ["3.5"])
        self.assertEqual(values("SELECT 3.0;"), ["3.0"])
        self.assertEqual(values("SELECT .5;"), ["0.5"])
        self.assertEqual(values("SELECT 1e3;"), ["1000.0"])
        self.assertEqual(values("SELECT 1.5e-2;"), ["0.015"])

    def test_text(self):
        self.assertEqual(values("SELECT 'hello';"), ["hello"])
        self.assertEqual(values("SELECT 'it''s';"), ["it's"])
        self.assertEqual(values('SELECT "dq";'), ["dq"])
        self.assertEqual(values("SELECT '';"), [""])

    def test_null_and_booleans(self):
        self.assertEqual(values("SELECT NULL;"), ["NULL"])
        self.assertEqual(values("SELECT TRUE;"), ["1"])
        self.assertEqual(values("SELECT FALSE;"), ["0"])


class TestArithmetic(unittest.TestCase):
    def test_precedence_and_parens(self):
        self.assertEqual(values("SELECT 1+2*3;"), ["7"])
        self.assertEqual(values("SELECT (1+2)*3;"), ["9"])
        self.assertEqual(values("SELECT 2*3+1;"), ["7"])
        self.assertEqual(values("SELECT 10-4-3;"), ["3"])
        self.assertEqual(values("SELECT 2*(3+4);"), ["14"])

    def test_integer_division_truncates_toward_zero(self):
        self.assertEqual(values("SELECT 7/2;"), ["3"])
        self.assertEqual(values("SELECT -7/2;"), ["-3"])
        self.assertEqual(values("SELECT 7/-2;"), ["-3"])

    def test_real_division(self):
        self.assertEqual(values("SELECT 7.0/2;"), ["3.5"])
        self.assertEqual(values("SELECT 7/2.0;"), ["3.5"])

    def test_modulo(self):
        self.assertEqual(values("SELECT 7%3;"), ["1"])
        self.assertEqual(values("SELECT -7%3;"), ["-1"])
        self.assertEqual(values("SELECT 7%-3;"), ["1"])

    def test_division_by_zero_is_null(self):
        self.assertEqual(values("SELECT 1/0;"), ["NULL"])
        self.assertEqual(values("SELECT 1.0/0.0;"), ["NULL"])
        self.assertEqual(values("SELECT 7%0;"), ["NULL"])

    def test_unary(self):
        self.assertEqual(values("SELECT -5+3;"), ["-2"])
        self.assertEqual(values("SELECT - -3;"), ["3"])
        self.assertEqual(values("SELECT +3;"), ["3"])
        self.assertEqual(values("SELECT -NULL;"), ["NULL"])

    def test_null_propagates(self):
        self.assertEqual(values("SELECT NULL+1;"), ["NULL"])
        self.assertEqual(values("SELECT 1*NULL;"), ["NULL"])
        self.assertEqual(values("SELECT NULL/2;"), ["NULL"])

    def test_mixed_types(self):
        self.assertEqual(values("SELECT 1+2.0;"), ["3.0"])
        self.assertEqual(values("SELECT 2*2.5;"), ["5.0"])

    def test_real_rounding_like_sqlite(self):
        # sqlite prints 0.1+0.2 as 0.3 (%.15g rounding)
        self.assertEqual(values("SELECT 0.1+0.2;"), ["0.3"])


class TestComparison(unittest.TestCase):
    def test_ordering(self):
        self.assertEqual(values("SELECT 1<2;"), ["1"])
        self.assertEqual(values("SELECT 2<=2;"), ["1"])
        self.assertEqual(values("SELECT 3>4;"), ["0"])
        self.assertEqual(values("SELECT 4>=4;"), ["1"])

    def test_equality_forms(self):
        self.assertEqual(values("SELECT 1=1;"), ["1"])
        self.assertEqual(values("SELECT 1==2;"), ["0"])
        self.assertEqual(values("SELECT 1!=2;"), ["1"])
        self.assertEqual(values("SELECT 1<>1;"), ["0"])

    def test_text_comparison(self):
        self.assertEqual(values("SELECT 'a'<'b';"), ["1"])
        self.assertEqual(values("SELECT 'b'='b';"), ["1"])
        self.assertEqual(values("SELECT 'a'='b';"), ["0"])

    def test_mixed_numeric_text(self):
        self.assertEqual(values("SELECT 5='5';"), ["1"])
        self.assertEqual(values("SELECT 5<='5';"), ["1"])
        self.assertEqual(values("SELECT 5<'abc';"), ["1"])
        self.assertEqual(values("SELECT 5='abc';"), ["0"])

    def test_null_comparison_is_null(self):
        self.assertEqual(values("SELECT NULL=1;"), ["NULL"])
        self.assertEqual(values("SELECT 1=NULL;"), ["NULL"])
        self.assertEqual(values("SELECT NULL<2;"), ["NULL"])
        self.assertEqual(values("SELECT NULL=NULL;"), ["NULL"])

    def test_is_null_operators(self):
        self.assertEqual(values("SELECT NULL IS NULL;"), ["1"])
        self.assertEqual(values("SELECT 1 IS NULL;"), ["0"])
        self.assertEqual(values("SELECT NULL IS NOT NULL;"), ["0"])
        self.assertEqual(values("SELECT 1 IS NOT NULL;"), ["1"])
        self.assertEqual(values("SELECT NULL IS 1;"), ["0"])
        self.assertEqual(values("SELECT 2 IS 2;"), ["1"])
        self.assertEqual(values("SELECT 2 IS NOT 3;"), ["1"])


class TestLogical(unittest.TestCase):
    def test_and(self):
        self.assertEqual(values("SELECT 1 AND 1;"), ["1"])
        self.assertEqual(values("SELECT 1 AND 0;"), ["0"])
        self.assertEqual(values("SELECT 0 AND NULL;"), ["0"])
        self.assertEqual(values("SELECT 1 AND NULL;"), ["NULL"])
        self.assertEqual(values("SELECT NULL AND NULL;"), ["NULL"])
        self.assertEqual(values("SELECT NULL AND 0;"), ["0"])

    def test_or(self):
        self.assertEqual(values("SELECT 1 OR 0;"), ["1"])
        self.assertEqual(values("SELECT 0 OR 0;"), ["0"])
        self.assertEqual(values("SELECT NULL OR 1;"), ["1"])
        self.assertEqual(values("SELECT NULL OR 0;"), ["NULL"])
        self.assertEqual(values("SELECT NULL OR NULL;"), ["NULL"])

    def test_not(self):
        self.assertEqual(values("SELECT NOT 1;"), ["0"])
        self.assertEqual(values("SELECT NOT 0;"), ["1"])
        self.assertEqual(values("SELECT NOT NULL;"), ["NULL"])

    def test_mixed(self):
        self.assertEqual(values("SELECT 1 AND (2 OR 0);"), ["1"])
        self.assertEqual(values("SELECT NOT (1 AND 0);"), ["1"])


class TestCase(unittest.TestCase):
    def test_searched_form(self):
        self.assertEqual(values("SELECT CASE WHEN 1 THEN 2 ELSE 3 END;"), ["2"])
        self.assertEqual(values("SELECT CASE WHEN 0 THEN 1 ELSE 2 END;"), ["2"])
        self.assertEqual(values("SELECT CASE WHEN NULL THEN 1 ELSE 2 END;"), ["2"])
        self.assertEqual(values("SELECT CASE WHEN 0 THEN 1 END;"), ["NULL"])
        self.assertEqual(values("SELECT CASE WHEN 2>1 THEN 10 ELSE 20 END;"), ["10"])

    def test_first_matching_when_wins(self):
        self.assertEqual(values("SELECT CASE WHEN 1 THEN 2 WHEN 1 THEN 3 END;"), ["2"])

    def test_simple_form(self):
        self.assertEqual(values("SELECT CASE 2 WHEN 1 THEN 'a' WHEN 2 THEN 'b' END;"), ["b"])
        self.assertEqual(values("SELECT CASE 'x' WHEN 'x' THEN 1 ELSE 0 END;"), ["1"])
        # simple CASE matches with = semantics: NULL never matches
        self.assertEqual(values("SELECT CASE NULL WHEN NULL THEN 1 ELSE 0 END;"), ["0"])

    def test_else_missing_yields_null(self):
        self.assertEqual(values("SELECT CASE WHEN 0 THEN 1 END;"), ["NULL"])


class TestMultiColumn(unittest.TestCase):
    def test_two_columns(self):
        self.assertEqual(values("SELECT 1, 2;"), ["1", "2"])
        self.assertEqual(values("SELECT 1+1, 'x';"), ["2", "x"])
        self.assertEqual(values("SELECT 'a', NULL, 3.5;"), ["a", "NULL", "3.5"])


class TestErrors(unittest.TestCase):
    def assert_error(self, sql, fragment=None):
        res = run(sql)
        self.assertIsNotNone(res.error, f"expected error for {sql!r}")
        if fragment:
            self.assertIn(fragment, res.error)

    def test_syntax_errors(self):
        self.assert_error("SELECT 1 +;", "unexpected")
        self.assert_error("SELECT (1;", "expected")
        self.assert_error("SELECT 'oops;", "unterminated")
        self.assert_error("SELECT 1 2;", "unexpected")

    def test_unsupported_statements(self):
        self.assert_error("SELECT * FROM t;")
        self.assert_error("CREATE TABLE t1(x);")
        self.assert_error("INSERT INTO t VALUES (1);")
        self.assert_error("SELECT x;", "no FROM clause")

    def test_empty(self):
        self.assert_error("")
        self.assert_error("   ")


if __name__ == "__main__":
    unittest.main()
