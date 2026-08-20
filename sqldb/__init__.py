"""sql-db-0820: a sqlite-compatible database engine (work in progress).

This slice provides the sqllogictest test runner and a minimal expression
engine that evaluates SELECT expression lists without a FROM clause.
"""

__version__ = "0.1.0"

from .engine import Engine, EngineError, StatementResult, render_value
from .runner import main as runner_main

__all__ = ["Engine", "EngineError", "StatementResult", "render_value", "runner_main"]
