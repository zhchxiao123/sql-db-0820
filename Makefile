# Standard test entry point for sql-db-0820.
# Uses stdlib unittest so the suite runs without installing any dependency.
test:
	python -m unittest discover -s tests -v

# Run the sqllogictest runner over the bundled corpus (all should pass).
test-runner:
	python sqllogictest_runner.py tests/data/expressions.test tests/data/hash.test tests/data/statements.test
