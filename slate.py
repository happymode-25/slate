#!/usr/bin/env python3
"""slate -- provable, plain-text double-entry bookkeeping.

You keep a journal: dated transactions, each a set of postings that must sum to
zero. slate parses it, enforces the invariants that make bookkeeping trustworthy
(every transaction balances; balance assertions hold; dates run forwards), reports
balances and a net-worth summary, draws a self-contained report, and can *seal*
the journal -- recording its hash and totals in a hash-chained log so a set of
books cannot be quietly rewritten after the fact.

Amounts are exact decimals, never floats.

Single file, Python 3.9+, standard library only.

    python slate.py selftest
    python slate.py check books/journal
    python slate.py balance books/journal
    python slate.py render books/journal --out report.html
    python slate.py seal books/journal --note "January close"
    python slate.py verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation

TOOL = "slate"
VERSION = "1.0.0"
GENESIS = "0" * 64

ACCOUNT_TYPES = ("Assets", "Liabilities", "Equity", "Income", "Expenses")

ACCOUNT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(:[A-Za-z0-9][A-Za-z0-9_-]*)+$")
CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")

RULES = ("empty-transaction", "unbalanced-transaction", "unknown-account-type",
         "balance-assertion", "out-of-order")


class SlateError(Exception):
    """A refusal: bad syntax, bad store, bad record. Always exits non-zero."""


def fail(message: str):
    raise SlateError(message)


def fmt(d: Decimal) -> str:
    return format(d, "f")


# --------------------------------------------------------------------------
# canonical hashing (the family discipline)
# --------------------------------------------------------------------------

def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return sha256_hex(canonical(body))


def now_iso(now=None) -> str:
    if now:
        return now
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def parse_date(text: str, lineno: int) -> str:
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        fail("line %d: not a valid date: %r" % (lineno, text))
    return text


def parse_amount(text: str, lineno: int) -> Decimal:
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        fail("line %d: not a valid amount: %r" % (lineno, text))
    if not value.is_finite():
        fail("line %d: amount must be finite: %r" % (lineno, text))
    return value


def parse_currency(text: str, lineno: int) -> str:
    if not CURRENCY_RE.match(text):
        fail("line %d: not a valid currency: %r" % (lineno, text))
    return text


def parse_account(text: str, lineno: int) -> str:
    if not ACCOUNT_RE.match(text):
        fail("line %d: not a valid account name: %r" % (lineno, text))
    return text


def parse_posting(s: str, lineno: int) -> dict:
    parts = s.split()
    if not parts:
        fail("line %d: empty posting" % lineno)
    account = parse_account(parts[0], lineno)
    if len(parts) == 1:
        return {"account": account, "amount": None, "currency": None, "line": lineno}
    if len(parts) == 3:
        return {"account": account,
                "amount": parse_amount(parts[1], lineno),
                "currency": parse_currency(parts[2], lineno),
                "line": lineno}
    fail("line %d: posting must be 'ACCOUNT [AMOUNT CURRENCY]'" % lineno)


def parse_journal(text: str) -> list:
    """Parse the journal into an ordered list of transactions and directives."""
    # Tolerate a UTF-8 byte-order mark; many editors write one.
    if text.startswith("\ufeff"):
        text = text[1:]
    items = []
    current = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        s = raw.rstrip()
        stripped = s.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        indented = s[:1] in (" ", "\t")
        if indented:
            if current is None:
                fail("line %d: posting is not inside a transaction" % lineno)
            current["postings"].append(parse_posting(stripped, lineno))
            continue
        current = None
        if not re.match(r"^\d{4}-\d{2}-\d{2}(?:\s|$)", stripped):
            fail("line %d: expected a date to start a line, got %r" % (lineno, stripped))
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+balance\b", stripped):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+balance\s+(\S+)\s*=\s*(\S+)\s+(\S+)$",
                         stripped)
            if not m:
                fail("line %d: malformed balance assertion: %r" % (lineno, stripped))
            items.append({
                "kind": "balance",
                "date": parse_date(m.group(1), lineno),
                "account": parse_account(m.group(2), lineno),
                "amount": parse_amount(m.group(3), lineno),
                "currency": parse_currency(m.group(4), lineno),
                "line": lineno,
            })
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+([*!]))?\s*(.*)$", stripped)
        date = parse_date(m.group(1), lineno)
        flag = m.group(2) or ""
        desc = m.group(3).strip()
        if len(desc) >= 2 and desc.startswith('"') and desc.endswith('"'):
            desc = desc[1:-1]
        current = {"kind": "txn", "date": date, "flag": flag, "desc": desc,
                   "postings": [], "line": lineno}
        items.append(current)
    return items


# --------------------------------------------------------------------------
# validation and reporting model
# --------------------------------------------------------------------------

def F(rule: str, line: int, message: str) -> dict:
    return {"rule": rule, "severity": "error", "line": line, "message": message}


def resolve_postings(postings: list, lineno: int):
    explicit = [p for p in postings if p["amount"] is not None]
    elided = [p for p in postings if p["amount"] is None]

    if len(elided) > 1:
        return None, F("unbalanced-transaction", lineno,
                       "%d postings have no amount; at most one may be omitted"
                       % len(elided))

    resolved = list(explicit)
    if len(elided) == 1:
        currencies = set(p["currency"] for p in explicit)
        if len(currencies) != 1:
            return None, F("unbalanced-transaction", lineno,
                           "cannot infer the omitted amount across %d currencies"
                           % len(currencies))
        currency = next(iter(currencies))
        total = sum((p["amount"] for p in explicit), Decimal(0))
        resolved.append({"account": elided[0]["account"], "amount": -total,
                         "currency": currency, "line": elided[0]["line"]})

    sums = {}
    for p in resolved:
        sums[p["currency"]] = sums.get(p["currency"], Decimal(0)) + p["amount"]
    bad = {c: v for c, v in sums.items() if v != 0}
    if bad:
        parts = ", ".join("%s %s" % (fmt(v), c) for c, v in sorted(bad.items()))
        return None, F("unbalanced-transaction", lineno,
                       "postings do not sum to zero: %s" % parts)
    return resolved, None


def process(items: list) -> dict:
    findings = []
    balances = {}
    accounts = set()
    txns = []
    postings_count = 0
    prev_date = None

    for item in items:
        if prev_date is not None and item["date"] < prev_date:
            findings.append(F("out-of-order", item["line"],
                              "date %s sorts before the previous entry %s"
                              % (item["date"], prev_date)))
        prev_date = item["date"]

        if item["kind"] == "balance":
            accounts.add(item["account"])
            top = item["account"].split(":")[0]
            if top not in ACCOUNT_TYPES:
                findings.append(F("unknown-account-type", item["line"],
                                  "account %r has unknown type %r"
                                  % (item["account"], top)))
            current = balances.get((item["account"], item["currency"]), Decimal(0))
            if current != item["amount"]:
                findings.append(F("balance-assertion", item["line"],
                                  "assertion failed for %s: expected %s %s, found %s %s"
                                  % (item["account"], fmt(item["amount"]),
                                     item["currency"], fmt(current), item["currency"])))
            continue

        if not item["postings"]:
            findings.append(F("empty-transaction", item["line"],
                              "transaction has no postings"))
            txns.append(dict(item, resolved=[]))
            continue

        for p in item["postings"]:
            accounts.add(p["account"])
            top = p["account"].split(":")[0]
            if top not in ACCOUNT_TYPES:
                findings.append(F("unknown-account-type", p["line"],
                                  "account %r has unknown type %r"
                                  % (p["account"], top)))

        resolved, err = resolve_postings(item["postings"], item["line"])
        if err:
            findings.append(err)
            txns.append(dict(item, resolved=[]))
            continue

        for p in resolved:
            postings_count += 1
            key = (p["account"], p["currency"])
            balances[key] = balances.get(key, Decimal(0)) + p["amount"]
        txns.append(dict(item, resolved=resolved))

    findings.sort(key=lambda f: f["line"])
    summary = summarize(balances, items, txns, postings_count)
    return {"findings": findings, "balances": balances, "accounts": accounts,
            "txns": txns, "summary": summary}


def summarize(balances: dict, items: list, txns: list, postings_count: int) -> dict:
    totals = {t: Decimal(0) for t in ACCOUNT_TYPES}
    for (account, _currency), value in balances.items():
        top = account.split(":")[0]
        if top in totals:
            totals[top] += value
    assets = totals["Assets"]
    liabilities = totals["Liabilities"]
    equity = totals["Equity"]
    income = -totals["Income"]
    expenses = totals["Expenses"]
    identity = assets + liabilities + equity + totals["Income"] + expenses
    return {
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "income": income,
        "expenses": expenses,
        "net_worth": assets + liabilities,
        "identity_error": identity,
        "accounts": len({a for (a, _c) in balances}),
        "transactions": sum(1 for it in items if it["kind"] == "txn"),
        "postings": postings_count,
    }


def summary_json(summary: dict) -> dict:
    out = {}
    for k, v in summary.items():
        out[k] = fmt(v) if isinstance(v, Decimal) else v
    return out


# --------------------------------------------------------------------------
# the seal ledger (hash-chained)
# --------------------------------------------------------------------------

def resolve_store(explicit):
    if explicit:
        return pathlib.Path(explicit)
    env = os.environ.get("SLATE_STORE")
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / ".slate" / "ledger.jsonl"


def load_records(store: pathlib.Path) -> list:
    if not store.exists():
        return []
    records = []
    with open(store, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                fail("store %s: line %d is not valid JSON" % (store, lineno))
    return records


def append_record(store: pathlib.Path, rec: dict) -> dict:
    records = load_records(store)
    prev = records[-1]["hash"] if records else GENESIS
    rec = dict(rec)
    rec["seq"] = len(records)
    rec["prev"] = prev
    rec["hash"] = record_hash(rec)
    store.parent.mkdir(parents=True, exist_ok=True)
    with open(store, "a", encoding="utf-8") as fh:
        fh.write(canonical(rec) + "\n")
    return rec


def verify_chain(records: list) -> list:
    problems = []
    expected_prev = GENESIS
    for i, rec in enumerate(records):
        if rec.get("seq") != i:
            problems.append("record %d: seq is %r, expected %d" % (i, rec.get("seq"), i))
        if rec.get("prev") != expected_prev:
            problems.append("record %d: prev link is broken" % i)
        if rec.get("hash") != record_hash(rec):
            problems.append("record %d: content hash mismatch" % i)
        expected_prev = rec.get("hash")
    return problems


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


BAR_COLOR = {
    "Assets": "#7aa2f7", "Liabilities": "#f7768e", "Equity": "#bb9af7",
    "Income": "#9ece6a", "Expenses": "#e0af68",
}


def render_html(name: str, data: dict, title: str) -> str:
    summary = data["summary"]
    findings = data["findings"]
    balances = data["balances"]

    cards = "".join(
        "<div class='card'><div class='n'>%s</div><div class='l'>%s</div></div>"
        % (_esc(fmt(summary[k])), label)
        for k, label in [("net_worth", "net worth"), ("assets", "assets"),
                         ("liabilities", "liabilities"), ("income", "income"),
                         ("expenses", "expenses")])

    if balances:
        bar_values = [("Assets", summary["assets"]), ("Liabilities", summary["liabilities"]),
                      ("Equity", summary["equity"]), ("Income", summary["income"]),
                      ("Expenses", summary["expenses"])]
        peak = max([abs(v) for _n, v in bar_values] + [Decimal(1)])
        bars = []
        y = 20
        for label, value in bar_values:
            width = int(abs(value) / peak * 360)
            bars.append("<text x='0' y='%d' fill='#c0caf5' font-size='12'>%s</text>"
                        % (y + 12, _esc(label)))
            bars.append("<rect x='90' y='%d' width='%d' height='16' rx='3' fill='%s'/>"
                        % (y, max(width, 1), BAR_COLOR[label]))
            bars.append("<text x='%d' y='%d' fill='#c0caf5' font-size='12'>%s</text>"
                        % (100 + width, y + 12, _esc(fmt(value))))
            y += 30
        chart = ("<svg viewBox='0 0 520 %d' width='100%%' style='max-width:520px'>%s</svg>"
                 % (y + 10, "".join(bars)))
    else:
        chart = "<p class='empty'>Nothing recorded yet.</p>"

    rows = []
    for (account, currency) in sorted(balances):
        top = account.split(":")[0]
        color = BAR_COLOR.get(top, "#c0caf5")
        rows.append("<tr><td><span class='dot' style='background:%s'></span>%s</td>"
                    "<td class='num'>%s</td><td class='cur'>%s</td></tr>"
                    % (color, _esc(account), _esc(fmt(balances[(account, currency)])),
                       _esc(currency)))
    table = ("<table><thead><tr><th>account</th><th class='num'>balance</th>"
             "<th>cur</th></tr></thead><tbody>%s</tbody></table>" % "".join(rows)) \
        if rows else ""

    if findings:
        items_html = "".join(
            "<li><span class='sev'>%s</span> line %d: %s</li>"
            % (_esc(f["rule"]), f["line"], _esc(f["message"])) for f in findings)
        finding_html = "<h2>Findings</h2><ul class='findings'>%s</ul>" % items_html
    else:
        finding_html = ("<p class='ok'>Every transaction balances, and every balance "
                        "assertion holds.</p>")

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>
:root { color-scheme: dark; }
body { margin:0; background:#1a1b26; color:#c0caf5;
  font-family:system-ui,-apple-system,Segoe UI,sans-serif; }
header { padding:28px 32px 8px; }
h1 { margin:0 0 6px; font-size:22px; }
.sub { color:#565f89; font-size:14px; }
main { padding:16px 32px 40px; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0; }
.card { background:#1f2335; border:1px solid #3b4261; border-radius:8px; padding:10px 14px; min-width:110px; }
.card .n { font-size:20px; font-weight:600; }
.card .l { color:#565f89; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
h2 { font-size:15px; color:#565f89; text-transform:uppercase; letter-spacing:.05em; margin:26px 0 8px; }
table { border-collapse:collapse; width:100%%; max-width:560px; }
th, td { text-align:left; padding:7px 10px; border-bottom:1px solid #292e42; font-size:14px; }
th { color:#565f89; font-weight:600; font-size:12px; text-transform:uppercase; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
td.cur, th.cur { color:#565f89; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%%; margin-right:8px; }
.findings { list-style:none; padding:0; }
.findings li { background:#1f2335; border:1px solid #3b4261; border-radius:8px; padding:10px 14px; margin:8px 0; font-size:14px; }
.sev { color:#f7768e; font-weight:700; font-size:11px; text-transform:uppercase; margin-right:8px; }
.ok { color:#9ece6a; }
.empty { color:#565f89; }
</style></head><body>
<header>
  <h1>%s</h1>
  <div class="sub">%d transactions &middot; %d postings &middot; %d accounts &middot; %d errors</div>
</header>
<main>
  <div class="cards">%s</div>
  <h2>Totals</h2>
  %s
  <h2>Accounts</h2>
  %s
  %s
</main></body></html>
""" % (_esc(title), _esc(title), summary["transactions"], summary["postings"],
       summary["accounts"], len(findings), cards, chart, table, finding_html)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def read_file(path: str):
    if path == "-":
        data = sys.stdin.buffer.read()
        return data.decode("utf-8"), "<stdin>", sha256_bytes(data)
    p = pathlib.Path(path)
    if not p.exists():
        fail("no such file: %s" % path)
    data = p.read_bytes()
    return data.decode("utf-8"), p.name, sha256_bytes(data)


def report_text(name, data) -> str:
    summary = data["summary"]
    out = ["journal: %s" % name,
           "%d transactions, %d postings, %d accounts"
           % (summary["transactions"], summary["postings"], summary["accounts"]),
           ""]
    out.append("net worth:   %s" % fmt(summary["net_worth"]))
    out.append("  assets:      %s" % fmt(summary["assets"]))
    out.append("  liabilities: %s" % fmt(summary["liabilities"]))
    out.append("  income:      %s" % fmt(summary["income"]))
    out.append("  expenses:    %s" % fmt(summary["expenses"]))
    findings = data["findings"]
    if findings:
        out.append("")
        out.append("errors (%d)" % len(findings))
        for f in findings:
            out.append("  [%s] line %d: %s" % (f["rule"], f["line"], f["message"]))
    else:
        out.append("")
        out.append("no findings")
    return "\n".join(out)


def cmd_check(path, as_json):
    text, name, _ = read_file(path)
    items = parse_journal(text)
    data = process(items)
    if as_json:
        print(json.dumps({
            "document": name,
            "findings": data["findings"],
            "summary": summary_json(data["summary"]),
        }, indent=2, sort_keys=True))
    else:
        print(report_text(name, data))
    return 1 if data["findings"] else 0


def cmd_balance(path, as_json):
    text, name, _ = read_file(path)
    items = parse_journal(text)
    data = process(items)
    balances = data["balances"]
    if as_json:
        print(json.dumps({
            "document": name,
            "balances": [{"account": a, "currency": c, "balance": fmt(balances[(a, c)])}
                         for (a, c) in sorted(balances)],
            "summary": summary_json(data["summary"]),
        }, indent=2, sort_keys=True))
        return 0
    print("journal: %s" % name)
    current_type = None
    for (account, currency) in sorted(balances):
        top = account.split(":")[0]
        if top != current_type:
            current_type = top
            print("")
            print("%s" % top)
        print("  %-34s %14s %s" % (account, fmt(balances[(account, currency)]), currency))
    print("")
    print("net worth: %s" % fmt(data["summary"]["net_worth"]))
    return 0


def cmd_register(path, account_filter, as_json):
    text, name, _ = read_file(path)
    items = parse_journal(text)
    data = process(items)
    rows = []
    for txn in data["txns"]:
        for p in txn.get("resolved", []):
            if account_filter and not p["account"].startswith(account_filter):
                continue
            rows.append({"date": txn["date"], "flag": txn["flag"], "desc": txn["desc"],
                         "account": p["account"], "amount": fmt(p["amount"]),
                         "currency": p["currency"]})
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for r in rows:
        print("%s %s %-22s %14s %s" % (r["date"], r["flag"] or " ",
                                       r["account"], r["amount"], r["currency"]))
    return 0


def cmd_render(path, out, title):
    text, name, _ = read_file(path)
    items = parse_journal(text)
    data = process(items)
    html = render_html(name, data, title or name)
    out_path = pathlib.Path(out)
    if out_path.parent and str(out_path.parent) not in ("", "."):
        out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print("wrote report to %s" % out)
    return 0


def cmd_seal(path, note, store, now):
    text, name, journal_hash = read_file(path)
    items = parse_journal(text)
    data = process(items)
    if data["findings"]:
        n = len(data["findings"])
        print("refusing to seal: the journal has %d error%s" % (n, "" if n == 1 else "s"))
        for f in data["findings"]:
            print("  [%s] line %d: %s" % (f["rule"], f["line"], f["message"]))
        return 1
    summary = data["summary"]
    rec = append_record(store, {
        "ts": now_iso(now), "tool": TOOL, "version": VERSION, "kind": "seal",
        "journal": name, "journal_hash": journal_hash,
        "transactions": summary["transactions"], "postings": summary["postings"],
        "accounts": summary["accounts"], "net_worth": fmt(summary["net_worth"]),
        "note": note or "",
    })
    print("sealed %s" % name)
    print("  journal hash %s" % journal_hash[:12])
    print("  net worth    %s" % fmt(summary["net_worth"]))
    print("  recorded as seq %d" % rec["seq"])
    return 0


def cmd_history(as_json, store):
    records = load_records(store)
    if as_json:
        print(json.dumps(records, indent=2, sort_keys=True))
        return 0
    if not records:
        print("no seals recorded")
        return 0
    for r in records:
        print("%4d  %s  %s  net %s  %s"
              % (r["seq"], r["ts"], r["journal"], r.get("net_worth", "?"),
                 r.get("note", "")))
    return 0


def cmd_verify(as_json, store):
    records = load_records(store)
    problems = verify_chain(records)
    if as_json:
        print(json.dumps({"ok": not problems, "records": len(records),
                          "problems": problems}, indent=2, sort_keys=True))
        return 0 if not problems else 1
    if not problems:
        print("chain intact: %d seals" % len(records))
        return 0
    print("chain broken: %d problems" % len(problems))
    for p in problems:
        print("  - %s" % p)
    return 1


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

class Checks:
    def __init__(self):
        self.total = 0
        self.failed = []

    def ok(self, condition, name):
        self.total += 1
        if not condition:
            self.failed.append(name)

    def eq(self, got, want, name):
        self.total += 1
        if got != want:
            self.failed.append("%s (got %r, want %r)" % (name, got, want))

    def report(self):
        if not self.failed:
            print("all checks passed: %d" % self.total)
            return 0
        print("FAIL: %d of %d checks failed" % (len(self.failed), self.total))
        for name in self.failed:
            print("  - %s" % name)
        return 1


def _j(*lines) -> str:
    return "\n".join(lines) + "\n"


CLEAN = _j(
    "2026-01-01 * \"Opening balance\"",
    "  Assets:Bank        1000.00 USD",
    "  Equity:Opening    -1000.00 USD",
    "",
    "2026-01-05 * \"Groceries\"",
    "  Expenses:Food       42.50 USD",
    "  Assets:Bank        -42.50 USD",
    "",
    "2026-01-31 * \"Salary\"",
    "  Assets:Bank        500.00 USD",
    "  Income:Salary     -500.00 USD",
    "",
    "2026-02-01 balance Assets:Bank = 1457.50 USD",
)

UNBALANCED = _j(
    "2026-01-01 * \"Broken\"",
    "  Assets:Bank        100.00 USD",
    "  Equity:Opening     -90.00 USD",
)


def cmd_selftest():
    c = Checks()

    # --- parsing -----------------------------------------------------------
    items = parse_journal(_j(
        "2026-01-01 * \"Opening\"",
        "  Assets:Bank   1000.00 USD",
        "  Equity:Opening  -1000.00 USD"))
    c.eq(items[0]["kind"], "txn", "a transaction parses")
    c.eq(items[0]["date"], "2026-01-01", "transaction date parses")
    c.eq(items[0]["flag"], "*", "the cleared flag is captured")
    c.eq(items[0]["desc"], "Opening", "a quoted description is unquoted")
    c.eq(len(items[0]["postings"]), 2, "postings are collected")

    p = parse_posting("Assets:Bank   1000.00 USD", 1)
    c.eq((fmt(p["amount"]), p["currency"]), ("1000.00", "USD"),
         "a posting keeps its exact amount and currency")

    el = parse_posting("Assets:Bank", 1)
    c.ok(el["amount"] is None, "a posting may omit its amount")

    c.eq(len(parse_journal("; a comment\n# another\n\n")), 0,
         "comment and blank lines are skipped")

    try:
        bom_items = parse_journal(
            "\ufeff2026-01-01 * x\n  Assets:Bank 1 USD\n  Equity:X -1 USD")
        c.eq(len(bom_items), 1, "a leading byte-order mark is tolerated")
    except SlateError:
        c.ok(False, "a leading byte-order mark is tolerated")

    bal = parse_journal("2026-02-01 balance Assets:Bank = 10.00 USD")[0]
    c.eq((bal["kind"], bal["account"], fmt(bal["amount"])), ("balance", "Assets:Bank", "10.00"),
         "a balance assertion parses")

    c.eq(parse_journal("2026-01-01 ! \"Pending\"\n  Assets:Bank 1 USD\n  Equity:X -1 USD")[0]["flag"],
         "!", "the pending flag is captured")

    for bad, name in [
        (_j("2026-13-40 * \"nope\"", "  A:B 1 USD", "  C:D -1 USD"), "bad date is refused"),
        (_j("2026-01-01 *", "  NoColon 1 USD", "  C:D -1 USD"), "bad account is refused"),
        (_j("2026-01-01 *", "  A:B notanumber USD", "  C:D -1 USD"), "bad amount is refused"),
        (_j("2026-01-01 *", "  A:B 1 usd", "  C:D -1 USD"), "bad currency is refused"),
        (_j("  A:B 1 USD"), "a posting outside a transaction is refused"),
        (_j("this is not a date"), "an unparseable line is refused"),
        ("2026-02-01 balance Assets:Bank 10.00 USD", "a malformed assertion is refused"),
    ]:
        try:
            parse_journal(bad)
            c.ok(False, name)
        except SlateError:
            c.ok(True, name)

    # --- validation --------------------------------------------------------
    def proc(text):
        return process(parse_journal(text))

    def has_rule(text, rule):
        return rule in [f["rule"] for f in proc(text)["findings"]]

    d = proc(CLEAN)
    c.eq(d["findings"], [], "a clean journal has no findings")
    c.eq(d["summary"]["transactions"], 3, "transaction count is right")
    c.eq(d["summary"]["postings"], 6, "posting count is right")
    c.eq(d["summary"]["assets"], Decimal("1457.50"), "assets accumulate")
    c.eq(d["summary"]["income"], Decimal("500"), "income is presented as earned")
    c.eq(d["summary"]["expenses"], Decimal("42.50"), "expenses accumulate")
    c.eq(d["summary"]["net_worth"], Decimal("1457.50"), "net worth is assets plus liabilities")
    c.eq(d["summary"]["identity_error"], Decimal("0"), "the accounting identity holds")
    c.eq(d["summary"]["accounts"], 4, "accounts are counted distinctly")

    c.ok(has_rule(UNBALANCED, "unbalanced-transaction"),
         "an unbalanced transaction is reported")

    elided = _j("2026-01-01 * \"one elided\"",
                "  Assets:Bank   100.00 USD",
                "  Income:Salary")
    c.eq(proc(elided)["findings"], [], "a single omitted amount is inferred")
    c.eq(proc(elided)["summary"]["income"], Decimal("100"), "the inferred amount balances")

    two_elided = _j("2026-01-01 *",
                    "  Assets:Bank   100.00 USD",
                    "  Income:Salary",
                    "  Equity:Opening")
    c.ok(has_rule(two_elided, "unbalanced-transaction"),
         "two omitted amounts are refused")

    mixed = _j("2026-01-01 *",
               "  Assets:Bank   100.00 USD",
               "  Assets:Cash   50.00 EUR",
               "  Equity:Opening")
    c.ok(has_rule(mixed, "unbalanced-transaction"),
         "an omission across currencies is refused")

    c.eq(proc(_j("2026-01-01 * \"x\"", "  Assets:Bank  1 USD", "  Equity:X -1 USD",
                 "2026-01-02 balance Assets:Bank = 1 USD"))["findings"], [],
         "a satisfied balance assertion passes")
    bad_assert = _j("2026-01-01 * \"x\"", "  Assets:Bank  1 USD", "  Equity:X -1 USD",
                    "2026-01-02 balance Assets:Bank = 5 USD")
    c.ok(has_rule(bad_assert, "balance-assertion"),
         "a broken balance assertion is reported")

    c.ok(has_rule(_j("2026-01-05 *", "  Assets:A 1 USD", "  Equity:X -1 USD",
                     "2026-01-01 *", "  Assets:A 1 USD", "  Equity:X -1 USD"), "out-of-order"),
         "out-of-order entries are reported")

    c.ok(has_rule(_j("2026-01-01 *", "  Widgets:Thing 1 USD", "  Equity:X -1 USD"),
                  "unknown-account-type"),
         "an unknown account type is reported")

    c.ok(has_rule(_j("2026-01-01 * \"empty\""), "empty-transaction"),
         "an empty transaction is reported")

    multi = _j("2026-01-01 *", "  Assets:Bank  10.00 USD", "  Equity:X -10.00 USD",
               "2026-01-02 *", "  Assets:Bank  5.00 USD", "  Income:Y -5.00 USD")
    c.eq(proc(multi)["summary"]["net_worth"], Decimal("15"), "net worth sums across transactions")

    # --- exit codes --------------------------------------------------------
    def safe_code(text):
        try:
            return 1 if process(parse_journal(text))["findings"] else 0
        except SlateError:
            return 2

    c.eq(safe_code(CLEAN), 0, "a clean journal exits 0")
    c.eq(safe_code(UNBALANCED), 1, "an unbalanced journal exits 1")
    c.eq(safe_code("garbage"), 2, "an unparseable journal exits 2")

    # --- chain -------------------------------------------------------------
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="slate-selftest-"))
    try:
        store = tmp / "ledger.jsonl"
        first = append_record(store, {"ts": "2026-01-01T00:00:00", "kind": "seal",
                                      "journal": "a", "journal_hash": "x"})
        c.eq(first["seq"], 0, "the first seal has seq 0")
        c.eq(first["prev"], GENESIS, "the first seal links to genesis")
        second = append_record(store, {"ts": "2026-01-02T00:00:00", "kind": "seal",
                                       "journal": "b", "journal_hash": "y"})
        c.eq(second["prev"], first["hash"], "the second seal links to the first hash")
        c.eq(verify_chain(load_records(store)), [], "a clean seal chain verifies")

        tampered = load_records(store)
        tampered[0]["journal"] = "forged"
        c.ok(any("hash" in p for p in verify_chain(tampered)),
             "editing a seal is caught")
        deleted = load_records(store)
        del deleted[0]
        c.ok(len(verify_chain(deleted)) > 0, "deleting the oldest seal is caught")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # --- render ------------------------------------------------------------
    html = render_html("demo", d, "demo")
    c.ok("<svg" in html, "render emits an svg chart")
    c.ok("1457.5" in html, "render shows the net worth")
    c.ok("http://" not in html and "https://" not in html,
         "render contains no external URLs")
    c.ok("<script" not in html, "render contains no script")
    empty_html = render_html("empty", proc(""), "empty")
    c.ok("Nothing recorded" in empty_html, "render handles an empty journal")

    return c.report()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def extract_now(argv):
    now = None
    out = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--now":
            if i + 1 >= len(argv):
                fail("--now needs a value")
            now = argv[i + 1]
            i += 2
            continue
        if a.startswith("--now="):
            now = a.split("=", 1)[1]
            i += 1
            continue
        out.append(a)
        i += 1
    return now, out


def build_parser():
    p = argparse.ArgumentParser(prog="slate", description="Plain-text double-entry bookkeeping.")
    p.add_argument("--version", action="version", version="%s %s" % (TOOL, VERSION))
    sub = p.add_subparsers(dest="command")

    def add(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--store", default=None)
        sp.add_argument("--version", action="version",
                        version="%s %s" % (TOOL, VERSION))
        return sp

    sp = add("check", "Validate the journal's invariants.")
    sp.add_argument("journal")
    sp.add_argument("--json", action="store_true")

    sp = add("balance", "Show account balances and net worth.")
    sp.add_argument("journal")
    sp.add_argument("--json", action="store_true")

    sp = add("register", "List postings, optionally for one account.")
    sp.add_argument("journal")
    sp.add_argument("--account", default=None)
    sp.add_argument("--json", action="store_true")

    sp = add("render", "Write a self-contained HTML report.")
    sp.add_argument("journal")
    sp.add_argument("--out", required=True)
    sp.add_argument("--title", default=None)

    sp = add("seal", "Record the journal's hash and totals in the seal ledger.")
    sp.add_argument("journal")
    sp.add_argument("--note", default="")

    sp = add("history", "List recorded seals.")
    sp.add_argument("--json", action="store_true")

    sp = add("verify", "Verify the seal ledger hash chain.")
    sp.add_argument("--json", action="store_true")

    add("selftest", "Run the built-in checks.")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    now, argv = extract_now(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_usage(sys.stderr)
        return 2
    store = resolve_store(getattr(args, "store", None))
    try:
        if args.command == "check":
            return cmd_check(args.journal, args.json)
        if args.command == "balance":
            return cmd_balance(args.journal, args.json)
        if args.command == "register":
            return cmd_register(args.journal, args.account, args.json)
        if args.command == "render":
            return cmd_render(args.journal, args.out, args.title)
        if args.command == "seal":
            return cmd_seal(args.journal, args.note, store, now)
        if args.command == "history":
            return cmd_history(args.json, store)
        if args.command == "verify":
            return cmd_verify(args.json, store)
        if args.command == "selftest":
            return cmd_selftest()
        fail("unknown command %r" % args.command)
    except SlateError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
