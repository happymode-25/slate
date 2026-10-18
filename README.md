# slate

Provable, plain-text double-entry bookkeeping.

You keep a journal — dated transactions, each a set of postings that must sum to
zero — and slate parses it, enforces the invariants that make bookkeeping
trustworthy, reports balances and a net-worth summary, draws a self-contained
report, and can *seal* the journal: recording its hash and totals in a
hash-chained log so a set of books cannot be quietly rewritten after the fact.

Amounts are exact decimals, never floats. Single file, Python 3.9+, standard
library only. No install step.

```
python slate.py check   books/journal
python slate.py balance books/journal
python slate.py render  books/journal --out report.html
python slate.py seal    books/journal --note "January close"
python slate.py verify
```

## Why write the books down this way

Double-entry bookkeeping is a bet that structure catches lies. Every transaction
must sum to zero; a balance you assert on one day must equal what the postings
say; dates must run forwards. Those are arithmetic facts, and arithmetic is
exactly what a program can check without being told what the numbers mean.

A journal in a spreadsheet hides its own errors: the formula that points at the
wrong cell, the row inserted after the total was computed, the currency changed
without the offset. slate makes those structural, and then checks them — and
refuses to seal books that fail.

slate does **not** guess anything. An amount you omit is inferred only when it is
unambiguous; a line it does not fully understand is an error, not a skipped line.

## The format

One entry per line. Blank lines and lines starting with `;` or `#` are ignored.

```
; a transaction:  DATE [*|!] "description"   then indented postings
2026-01-01 * "Opening balances"
  Assets:Bank:Checking       2500.00 USD
  Assets:Cash                 120.00 USD
  Equity:Opening            -2620.00 USD

2026-01-15 * "Salary"
  Assets:Bank:Checking       3200.00 USD
  Income:Salary             -3200.00 USD

; a balance assertion:  DATE balance ACCOUNT = AMOUNT CURRENCY
2026-01-31 balance Assets:Bank:Checking = 3200.00 USD
```

- A transaction starts with a date (`YYYY-MM-DD`), an optional flag (`*` cleared,
  `!` pending) and an optional quoted description.
- Each posting is indented: `ACCOUNT AMOUNT CURRENCY`. **At most one posting per
  transaction may omit its amount**, and only when every other posting is in the
  same currency; the omitted amount is inferred so the transaction sums to zero.
- An account is `Type:Name[:Sub...]` where `Type` is one of `Assets`,
  `Liabilities`, `Equity`, `Income`, `Expenses`. Names are letters, digits, `_`
  and `-`.
- Amounts are signed decimals. Credits and debits are just signs; the books
  balance when every transaction's postings sum to zero **per currency**.

The worked example ships in [`examples/personal.journal`](examples/personal.journal).

## What it checks

Every finding is an error, so `check` exits non-zero on any of them.

| Rule | Meaning |
|---|---|
| `empty-transaction` | a transaction with no postings |
| `unbalanced-transaction` | postings do not sum to zero, or an omission cannot be inferred |
| `unknown-account-type` | an account's first segment is not one of the five types |
| `balance-assertion` | a `balance` directive does not match the running balance |
| `out-of-order` | an entry's date sorts before the previous entry's |

Findings are reported in line order, each with the line number and a plain
message. Nothing is silently repaired.

## Commands

| Command | What it gives you |
|---|---|
| `selftest` | 52 checks: the parser, all five rules, the exit codes, the chain, the render |
| `check FILE [--json]` | Findings. Exit 1 if there are any |
| `balance FILE [--json]` | Accounts grouped by type, with balances and net worth |
| `register FILE [--account A] [--json]` | Every posting, or those under an account prefix |
| `render FILE --out report.html [--title T]` | A self-contained HTML report: totals, bars, account table, findings |
| `seal FILE [--note N]` | Analyse and append a hash-chained record of the books. Refuses broken books |
| `history [--json]` | Every recorded seal |
| `verify [--json]` | Rebuild the seal hash chain; exit 1 if anything is wrong |

`FILE` may be `-` to read from stdin. `--version` works everywhere. `--now ISO`
pins the timestamp so a seal is reproducible.

### Net worth

`net_worth = assets + liabilities`. A liability you owe is stored as a negative
balance, so owing money reduces net worth. `income` is presented as earned
(`-` the raw `Income` total), so a salary shows as a positive number.

## Exit codes

- **0** — the journal is clean, or the command succeeded.
- **1** — at least one finding; or `seal` refused a journal with errors; or
  `verify` found the chain broken.
- **2** — the input could not be read or parsed (bad syntax, missing file, bad
  store), or the command is unknown. Nothing is guessed.

`render` always exits **0**: it writes a report even for a broken journal, so you
can see what is wrong. `check` is the gate.

## The seal ledger

`seal` records what you closed and when, in a hash-chained JSONL log at
`~/.slate/ledger.jsonl` (or `$SLATE_STORE`, or `--store`). Each record stores
the SHA-256 of the journal *bytes*, its counts, its net worth, and its note,
chained so a seal cannot be quietly rewritten afterwards:

```
hash[n] = sha256(canonical(record[n] minus hash))     # including prev = hash[n-1]
```

Record 0's `prev` is 64 zeros. `verify` checks sequence contiguity, prev linkage
and content hash per record and reports *all* problems, each named. Editing a
record and deleting a record from the middle are both caught.

Honest limit: trimming the *tail* of the file is not detectable from inside the
file. Anchor the last hash elsewhere if that matters.

## Design constraints

**No dependencies.** Python 3.9+ standard library. The parser, the validation
order and the render layout are all deterministic, so the same journal always
produces the same findings in the same order.

**Exact decimals.** Amounts are `Decimal`, never `float`. `0.1 + 0.2` is `0.3`,
and a balance assertion is an exact comparison, not an epsilon.

**Explicit over inferred.** The parser rejects anything it does not fully
understand (a malformed date, a bad amount, a posting outside a transaction,
an unknown account shape) with exit 2. Silently skipping a line the author wrote
would be the worst possible behaviour for a bookkeeping tool.

**Fail-closed sealing.** `seal` refuses to record books that have any finding.
A ledger that would attest to broken books is worse than no ledger.

**The report is one file.** `render` produces a single HTML document with an
inline SVG — no scripts, no fonts, no network.

## Verification

| Gate | What it proves | Floor |
|---|---|---|
| `ci/import_gate.py` | zero third-party imports anywhere | — |
| `selftest` | parser, all five rules, exit codes, chain, render | 52 checks |
| `tests/test_slate.py` | real CLI behaviour over subprocesses | 45 tests |
| `ci/prove_suites_can_fail.py` | the suites fail when the code breaks and pass when restored | — |
| `ci/mutation.py` | 11 real mutants killed, semantically-identical canary survives | 11/11 |

Run them locally from the repo root:

```powershell
python ci/import_gate.py
python slate.py selftest
python tests/test_slate.py
python ci/check_counts.py
python ci/prove_suites_can_fail.py
python ci/mutation.py
```

The mutation sweep works on a scratch copy; the live `slate.py` is hashed before
and after, so a sweep can never leave a mutant behind.

## Limitations

**It checks the arithmetic, not reality.** slate can prove a journal is
internally consistent; it cannot prove the numbers correspond to the world.

**An inferred amount is a convenience, not a control.** Omitting one posting's
amount is safe only because slate recomputes it; if you meant a different number,
the inference will quietly agree with the other side.

**No foreign exchange.** Each currency is summed and balanced separately. There
is no conversion and no cross-currency net worth.

**Accounts are flat strings.** Balances are per exact account; `register
--account` filters by prefix, but there is no rollup reporting.

**The ledger has no external anchor.** Tail-truncation is not detectable in-band.

## Files

| Path | What |
|---|---|
| `slate.py` | The whole tool. Parser, rules, summary, chain ledger, HTML report, selftest. |
| `examples/personal.journal` | A worked, clean set of books. |
| `tests/test_slate.py` | 45 end-to-end tests over subprocesses. |
| `ci/import_gate.py` | Fails if anything imports outside the standard library. |
| `ci/check_counts.py` | Asserts the suite counts meet the CI floors. |
| `ci/prove_suites_can_fail.py` | Breaks the tool on purpose; requires the suites to notice. |
| `ci/mutation.py` | Mutates a throwaway copy and requires the selftest to notice. Canary-guarded. |
| `REPORT.md` | Engineering report: features, verification, and the defects found while building. |
| `.github/workflows/ci.yml` | Both suites, both gates, on Linux, macOS and Windows. |

## Licence

MIT. See [LICENSE](LICENSE).
