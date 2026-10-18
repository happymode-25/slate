"""Run both suites and assert the CI count floors.

`unittest` exits 0 when it collects nothing, and a suite that quietly stopped
running is indistinguishable from one that passes. So CI asserts the numbers,
not just the exit codes. Written in Python so it behaves the same on Linux,
macOS and Windows.

    python ci/check_counts.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Floors. Raise these when the suites grow; never lower them silently.
FLOOR_SELFTEST = 52
FLOOR_E2E = 45


def run(args):
    proc = subprocess.run(args, capture_output=True, text=True,
                          cwd=str(ROOT), timeout=900)
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    rc, out = run([sys.executable, "slate.py", "selftest"])
    m = re.search(r"all checks passed:\s*(\d+)", out)
    if rc != 0 or not m:
        print("selftest did not pass:\n%s" % out)
        return 1
    n_self = int(m.group(1))
    if n_self < FLOOR_SELFTEST:
        print("selftest ran %d checks, floor is %d" % (n_self, FLOOR_SELFTEST))
        return 1

    rc, out = run([sys.executable, "-m", "unittest", "tests.test_slate"])
    m = re.search(r"Ran\s+(\d+)\s+tests", out)
    if rc != 0 or not m or "OK" not in out:
        print("e2e suite did not pass:\n%s" % out)
        return 1
    n_e2e = int(m.group(1))
    if n_e2e < FLOOR_E2E:
        print("e2e ran %d tests, floor is %d" % (n_e2e, FLOOR_E2E))
        return 1

    print("counts ok: selftest %d (floor %d), e2e %d (floor %d)"
          % (n_self, FLOOR_SELFTEST, n_e2e, FLOOR_E2E))
    return 0


if __name__ == "__main__":
    sys.exit(main())
