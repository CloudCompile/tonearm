#!/usr/bin/env python3
"""Convenience runner for the offline route harness (#135).

Runs the fixture-driven harness against a mock server via urllib (no
Chrome, no Apple). Equivalent to `python3 -m unittest tests.test_route_harness`.

    python3 scripts/harness_routes.py          # run harness
    python3 scripts/harness_routes.py -v       # verbose
"""

import os
import sys
import unittest

# running as a file (python3 scripts/harness_routes.py) puts scripts/ on
# sys.path, not the repo root — bootstrap it so `tests` imports (#208 soak).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    os.environ.setdefault("TONEARM_MOCK", "1")
    os.environ.setdefault("TONEARM_AUTO_CHROME", "0")
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_route_harness")
    verbosity = 2 if "-v" in sys.argv or "--verbose" in sys.argv else 1
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
