"""End-to-end tests for slate.

These drive the real CLI as a subprocess against temporary journals and stores,
so they cover the class of bug unit-level checks cannot see: a flag that never
reaches the code, a wrong exit code, a store that cannot be written, a report
that emits a broken document.

    python tests/test_slate.py
    python -m unittest tests.test_slate
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOL = ROOT / "slate.py"

CLEAN = """\
2026-01-01 * "Opening"
  Assets:Bank       1000.00 USD
  Equity:Opening   -1000.00 USD

2026-01-05 * "Groceries"
  Expenses:Food       42.50 USD
  Assets:Bank        -42.50 USD

2026-02-01 balance Assets:Bank = 957.50 USD
"""

UNBALANCED = """\
2026-01-01 * "Broken"
  Assets:Bank       100.00 USD
  Equity:Opening    -90.00 USD
"""


def run(sub, *args, store=None, stdin=None):
    cmd = [sys.executable, str(TOOL), sub] + [str(a) for a in args]
    if store is not None:
        cmd += ["--store", str(store)]
    env = dict(os.environ)
    env.pop("SLATE_STORE", None)
    return subprocess.run(cmd, capture_output=True, text=True, input=stdin,
                          env=env, timeout=300)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.store = self.dir / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def journal(self, text, name="a.journal"):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return p


class TestCheck(Base):
    def test_clean_exits_zero(self):
        p = self.journal(CLEAN)
        proc = run("check", p)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no findings", proc.stdout)

    def test_unbalanced_exits_one(self):
        p = self.journal(UNBALANCED)
        proc = run("check", p)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unbalanced-transaction", proc.stdout)

    def test_parse_error_exits_two(self):
        p = self.journal("not a journal")
        proc = run("check", p)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("expected a date", proc.stderr)

    def test_missing_file_exits_two(self):
        self.assertEqual(run("check", self.dir / "nope").returncode, 2)

    def test_json_shape(self):
        p = self.journal(CLEAN)
        data = json.loads(run("check", p, "--json").stdout)
        self.assertEqual(sorted(data), ["document", "findings", "summary"])
        self.assertEqual(data["summary"]["transactions"], 2)

    def test_unknown_account_type_is_an_error(self):
        p = self.journal("2026-01-01 *\n  Widgets:Thing 1 USD\n  Equity:X -1 USD\n")
        self.assertEqual(run("check", p).returncode, 1)
        self.assertIn("unknown-account-type", run("check", p).stdout)

    def test_empty_transaction_is_an_error(self):
        p = self.journal('2026-01-01 * "nothing"\n')
        self.assertEqual(run("check", p).returncode, 1)
        self.assertIn("empty-transaction", run("check", p).stdout)

    def test_out_of_order_is_an_error(self):
        p = self.journal("2026-01-05 *\n  Assets:A 1 USD\n  Equity:X -1 USD\n"
                         "2026-01-01 *\n  Assets:A 1 USD\n  Equity:X -1 USD\n")
        self.assertEqual(run("check", p).returncode, 1)
        self.assertIn("out-of-order", run("check", p).stdout)

    def test_balance_assertion_holds(self):
        p = self.journal(CLEAN)
        self.assertEqual(run("check", p).returncode, 0)

    def test_balance_assertion_fails(self):
        p = self.journal("2026-01-01 *\n  Assets:Bank 1 USD\n  Equity:X -1 USD\n"
                         "2026-01-02 balance Assets:Bank = 5 USD\n")
        proc = run("check", p)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("balance-assertion", proc.stdout)

    def test_elided_posting_is_inferred(self):
        p = self.journal("2026-01-01 *\n  Assets:Bank 100 USD\n  Income:Salary\n")
        self.assertEqual(run("check", p).returncode, 0)

    def test_two_elided_postings_are_refused(self):
        p = self.journal("2026-01-01 *\n  Assets:Bank 100 USD\n  Income:A\n  Equity:B\n")
        self.assertEqual(run("check", p).returncode, 1)

    def test_reads_stdin(self):
        proc = run("check", "-", stdin=CLEAN)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_tolerates_utf8_bom(self):
        p = self.dir / "bom.journal"
        p.write_bytes("\ufeff".encode("utf-8") + CLEAN.encode("utf-8"))
        self.assertEqual(run("check", p).returncode, 0)

    def test_identity_holds_in_summary(self):
        p = self.journal(CLEAN)
        data = json.loads(run("check", p, "--json").stdout)
        self.assertEqual(Decimal(data["summary"]["identity_error"]), Decimal("0"))


class TestBalance(Base):
    def test_balance_lists_accounts(self):
        p = self.journal(CLEAN)
        out = run("balance", p).stdout
        self.assertIn("Assets:Bank", out)
        self.assertIn("Expenses:Food", out)

    def test_balance_json(self):
        p = self.journal(CLEAN)
        data = json.loads(run("balance", p, "--json").stdout)
        accounts = {b["account"]: b["balance"] for b in data["balances"]}
        self.assertEqual(accounts["Assets:Bank"], "957.50")

    def test_balance_reports_net_worth(self):
        p = self.journal(CLEAN)
        self.assertIn("net worth", run("balance", p).stdout)


class TestRegister(Base):
    def test_register_lists_postings(self):
        p = self.journal(CLEAN)
        out = run("register", p).stdout
        self.assertIn("Expenses:Food", out)
        self.assertIn("42.50", out)

    def test_register_account_filter(self):
        p = self.journal(CLEAN)
        out = run("register", p, "--account", "Expenses").stdout
        self.assertIn("Expenses:Food", out)
        self.assertNotIn("Assets:Bank", out)

    def test_register_json(self):
        p = self.journal(CLEAN)
        data = json.loads(run("register", p, "--json").stdout)
        self.assertTrue(any(r["account"] == "Assets:Bank" for r in data))


class TestRender(Base):
    def test_render_writes_file(self):
        p = self.journal(CLEAN)
        out = self.dir / "report.html"
        proc = run("render", p, "--out", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(out.exists())

    def test_render_is_self_contained(self):
        p = self.journal(CLEAN)
        out = self.dir / "report.html"
        run("render", p, "--out", out)
        html = out.read_text(encoding="utf-8")
        self.assertIn("<svg", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script", html)

    def test_render_shows_net_worth(self):
        p = self.journal(CLEAN)
        out = self.dir / "report.html"
        run("render", p, "--out", out)
        self.assertIn("957.50", out.read_text(encoding="utf-8"))

    def test_render_empty_journal(self):
        p = self.journal("; nothing here\n")
        out = self.dir / "report.html"
        run("render", p, "--out", out)
        self.assertIn("Nothing recorded", out.read_text(encoding="utf-8"))

    def test_render_broken_journal_still_exits_zero(self):
        p = self.journal(UNBALANCED)
        out = self.dir / "report.html"
        proc = run("render", p, "--out", out)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("unbalanced-transaction", out.read_text(encoding="utf-8"))


class TestSealLedger(Base):
    def test_seal_clean(self):
        p = self.journal(CLEAN)
        proc = run("seal", p, store=self.store)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("recorded as seq 0", proc.stdout)

    def test_seal_refuses_broken_books(self):
        p = self.journal(UNBALANCED)
        proc = run("seal", p, store=self.store)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refusing to seal", proc.stdout)
        self.assertFalse(self.store.exists())

    def test_seal_records_journal_hash(self):
        p = self.journal(CLEAN)
        run("seal", p, store=self.store)
        rec = json.loads(self.store.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(len(rec["journal_hash"]), 64)
        self.assertEqual(rec["kind"], "seal")

    def test_second_seal_is_seq_one(self):
        p = self.journal(CLEAN)
        run("seal", p, store=self.store)
        proc = run("seal", p, store=self.store)
        self.assertIn("recorded as seq 1", proc.stdout)

    def test_history_lists_seals(self):
        p = self.journal(CLEAN)
        run("seal", p, "--note", "January close", store=self.store)
        self.assertIn("January close", run("history", store=self.store).stdout)

    def test_history_json(self):
        p = self.journal(CLEAN)
        run("seal", p, store=self.store)
        data = json.loads(run("history", "--json", store=self.store).stdout)
        self.assertEqual(len(data), 1)

    def test_history_empty(self):
        proc = run("history", store=self.store)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("no seals", proc.stdout)

    def test_seal_deterministic_timestamp(self):
        p = self.journal(CLEAN)
        run("seal", p, "--now", "2026-01-01T00:00:00", store=self.store)
        rec = json.loads(self.store.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["ts"], "2026-01-01T00:00:00")


class TestVerify(Base):
    def _seed(self, n=2):
        p = self.journal(CLEAN)
        for _ in range(n):
            run("seal", p, store=self.store)

    def test_verify_clean(self):
        self._seed()
        proc = run("verify", store=self.store)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("chain intact", proc.stdout)

    def test_verify_empty(self):
        self.assertEqual(run("verify", store=self.store).returncode, 0)

    def test_verify_detects_edit(self):
        self._seed()
        text = self.store.read_text(encoding="utf-8").replace('"note":""', '"note":"forged"')
        self.store.write_text(text, encoding="utf-8")
        proc = run("verify", store=self.store)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hash", proc.stdout.lower())

    def test_verify_detects_deleted_middle(self):
        self._seed(3)
        lines = self.store.read_text(encoding="utf-8").splitlines()
        del lines[1]
        self.store.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(run("verify", store=self.store).returncode, 1)

    def test_verify_json(self):
        self._seed()
        data = json.loads(run("verify", "--json", store=self.store).stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["problems"], [])


class TestCLI(Base):
    def test_version(self):
        proc = run("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("slate", proc.stdout)

    def test_subcommand_version(self):
        proc = run("check", "--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("1.0.0", proc.stdout)

    def test_help(self):
        self.assertEqual(run("--help").returncode, 0)

    def test_unknown_command_exits_two(self):
        proc = subprocess.run([sys.executable, str(TOOL), "frobnicate"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_no_command_exits_two(self):
        proc = subprocess.run([sys.executable, str(TOOL)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)

    def test_selftest_passes(self):
        proc = run("selftest")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("all checks passed", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
