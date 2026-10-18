"""Prove the selftest and the e2e suite can actually fail.

Break the code on purpose and require the suites to notice (named check
failures), not just non-zero exits. A suite that cannot fail is not evidence.

    python ci/prove_suites_can_fail.py
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

TOOL = pathlib.Path("slate.py")


def read_source() -> str:
    with open(TOOL, encoding="utf-8", newline="") as fh:
        return fh.read()


def write_source(text: str) -> None:
    with open(TOOL, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def run_selftest():
    proc = subprocess.run([sys.executable, str(TOOL), "selftest"],
                          capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout + proc.stderr


def run_e2e():
    proc = subprocess.run([sys.executable, "-m", "unittest", "tests.test_slate"],
                          capture_output=True, text=True, timeout=900)
    out = proc.stdout + proc.stderr
    if proc.returncode and "FAILED" not in out:
        return 1
    return proc.returncode


def named_check_failure(output: str):
    for line in output.splitlines():
        if line.startswith("  - "):
            return line[4:].strip()
    return None


def attempt(label, old, new, expect_check=True):
    src = read_source()
    if old not in src:
        print("%s: pattern not found" % label)
        return False
    try:
        write_source(src.replace(old, new, 1))
        rc, out = run_selftest()
        if rc == 0:
            print("%s: selftest did not fail as expected (rc=0)" % label)
            return False
        if expect_check and not named_check_failure(out):
            print("%s: no named check failure found in selftest" % label)
            return False
        rc2 = run_e2e()
        if rc2 == 0:
            print("%s: e2e suite did not fail" % label)
            return False
        return True
    finally:
        write_source(src)


def main() -> int:
    ok = True
    ok &= attempt(
        "behaviour: unbalanced transactions are never reported",
        "    if bad:",
        "    if False:")
    ok &= attempt(
        "reporting: negative control injected",
        "    return c.report()",
        '    c.ok(False, "negative control injected by CI")\n    return c.report()')
    if not ok:
        sys.exit(1)
    print("ok: both suites can fail as named checks")
    sys.exit(0)


if __name__ == "__main__":
    main()
