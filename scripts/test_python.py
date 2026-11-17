#!/usr/bin/env python3
"""Run Python unit tests without importing standalone Go/IR suite launchers."""
from pathlib import Path
import sys
import unittest


def main():
    tests = unittest.TestSuite()
    root = Path(__file__).resolve().parent
    for path in sorted(root.glob("test_*.py")):
        if path.name in ("test_go.py", "test_saved_ir.py", "test_python.py"):
            continue
        tests.addTests(unittest.defaultTestLoader.loadTestsFromName(path.stem))
    result = unittest.TextTestRunner(verbosity=1).run(tests)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
