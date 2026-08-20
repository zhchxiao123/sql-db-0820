#!/usr/bin/env python3
"""Root-level convenience entry point for the sqllogictest runner.

Invoke as ``python sqllogictest_runner.py <file.test> ...`` from the repo
root, or use the installed console script / ``python -m sqldb.runner``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqldb.runner import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
