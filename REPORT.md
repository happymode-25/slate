# slate — engineering report

What was built, what was verified, and what broke on the way.

Version 1.0.0. One file, `slate.py`, Python 3.9+, standard library only.

## What it does

Provable, plain-text double-entry bookkeeping. You keep a journal of dated
transactions — each a set of postings that must sum to zero — and slate parses
it, checks the invariants that make bookkeeping trustworthy, reports balances and
a net-worth summary, renders a self-contained HTML report, and keeps a
hash-chained log of every *seal* (a recorded hash and total of a closed set of
books).

Amounts are exact `Decimal`, never `float`. Commands: `check`, `balance`,
`register`, `render`, `seal`, `history`, `verify`, `selftest`.

## Verification

Everything below was run locally on Windows (Python 3.12) and is enforced in CI
on Python 3.9, 3.11 and 3.13 across Linux, macOS and Windows.

| Gate | Command | Result |
|---|---|---|
| Import gate | `python ci/import_gate.py` | no non-stdlib imports |
| Selftest | `python slate.py selftest` | 52 checks, all pass |
| End-to-end | `python tests/test_slate.py` | 45 tests, all pass |
| Count floors | `python ci/check_counts.py` | selftest ≥ 52, e2e ≥ 45 |
| Fail-proof | `python ci/prove_suites_can_fail.py` | both suites fail as named checks, then restored |
| Mutation sweep | `python ci/mutation.py` | 11/11 mutants killed, canary survived |

### The mutation sweep

Each mutant is a one-line change that disables one invariant, corrupts the
parser, or breaks the summary. The sweep applies it to a scratch copy, compiles
it, and requires the **selftest to fail**. A semantically-identical canary must
survive.

| # | Mutant | Killed by |
|---|---|---|
| 1 | unbalanced transactions never reported | `an unbalanced transaction is reported` |
| 2 | the elided amount is filled with zero | `a single omitted amount is inferred` |
| 3 | empty transactions never reported | `an empty transaction is reported` |
| 4 | out-of-order entries never reported | `out-of-order entries are reported` |
| 5 | balance assertions never checked | `a broken balance assertion is reported` |
| 6 | a leading BOM no longer tolerated | `a leading byte-order mark is tolerated` |
| 7 | income keeps the wrong sign | `income is presented as earned` |
| 8 | invalid dates accepted | `bad date is refused` |
| 9 | invalid currencies accepted | `bad currency is refused` |
| 10 | invalid account names accepted | `bad account is refused` |
| 11 | postings no longer counted | `posting count is right` |
| — | canary: drop a return annotation | survived (as required) |

The live `slate.py` is hashed before and after the sweep; the harness fails if it
changed.

### The fail-proof gate

`ci/prove_suites_can_fail.py` breaks the tool two ways and requires the suites to
notice, then restores the source in a `finally`:

1. **Behaviour.** Disable the unbalanced-transaction rule. The selftest must fail
   with the named check `an unbalanced transaction is reported`, and the e2e
   suite must fail too.
2. **Reporting.** Inject `c.ok(False, ...)` at the end of `selftest`. The named
   check `negative control injected by CI` must appear.

This gate is also what caught defect 1 below: the difference between "the suite
failed" and "the suite failed as a *named test*" is exactly where a broken check
hides.

> Honest limit: the restore happens in a `finally`, so an *exception* restores the
> file, but a hard kill (SIGKILL, or the machine going down) mid-run can leave the
> injected line behind. CI always checks out clean; if you abort a local run,
> re-run `selftest` to confirm the baseline before trusting anything.

## Defects found while building

Both were found during bring-up, by the checks themselves.

### 1. The selftest crashed instead of reporting

Seven checks were written as

```python
c.eq(proc(doc)["findings"][0]["rule"], "unbalanced-transaction",
     "an unbalanced transaction is reported")
```

This assumes the finding exists. When the fail-proof gate disabled the rule, the
list was empty and Python raised `IndexError` — so the *whole selftest aborted*
with a traceback instead of reporting which invariant had stopped holding. The
remaining checks never ran. A crash is not a report: it tells you something is
wrong but not what, and it hides every other failure behind the first one.

Fixed with a small helper and predicate checks:

```python
def has_rule(text, rule):
    return rule in [f["rule"] for f in proc(text)["findings"]]
...
c.ok(has_rule(UNBALANCED, "unbalanced-transaction"),
     "an unbalanced transaction is reported")
```

Now a missing rule is a normal, named failure and the suite runs to the end. The
same reasoning applies to the BOM check, which was made to catch `SlateError`
and report rather than propagate.

### 2. An e2e test pinned a decimal rendering, not an invariant

`test_identity_holds_in_summary` asserted the JSON `identity_error` was the
string `"0"`. It is `"0.00"`: the summary is computed in exact decimals, and
`summary_json` renders a `Decimal` with `format(d, "f")`, so a computed identity
of `Decimal("0.00")` keeps its scale. The tool was right; the test had pinned an
accidental formatting detail instead of the invariant.

Fixed by comparing as decimals, so trailing zeros are irrelevant:

```python
self.assertEqual(Decimal(data["summary"]["identity_error"]), Decimal("0"))
```

## Design notes

**Exact decimal accounting.** `Decimal` throughout. `0.1 + 0.2` is `0.3`, and a
balance assertion is an exact equality, not an epsilon comparison. This is the
whole reason the tool is trustworthy for money.

**Per-currency balancing.** Every transaction's postings must sum to zero *for
each currency it touches*. There is no implicit conversion.

**One inferred amount, and only one.** A single posting may omit its amount, and
only when every other posting shares one currency. Then and only then is it
filled in. Two omissions, or an omission across currencies, is refused — the
amount would be underdetermined, and guessing it would be the tool inventing
numbers.

**Determinism.** Parser, validation order and render layout depend only on the
input, so the same journal always yields the same findings in the same order.

**Fail-closed sealing.** `seal` refuses to write a record for books that have any
finding. A ledger that could attest to broken books would be worse than none.

**The report is one file.** `render` produces a single HTML document with an
inline SVG — no scripts, no fonts, no network. It always exits 0, even for a
broken journal, so you can look at what is wrong; `check` is the gate.

**Family hash discipline.** The seal ledger is
`hash[n] = sha256(canonical(record minus hash))` with `prev = hash[n-1]`, genesis
= 64 zeros — the same chain construction as the rest of the family.

## Limitations

- **Arithmetic, not reality.** slate proves a journal is internally consistent;
  it cannot prove the numbers match the world.
- **An inferred amount is a convenience, not a control.** If you meant a
  different number, the inference will silently agree with the other side.
- **No foreign exchange.** Each currency balances separately; there is no
  conversion and no cross-currency net worth.
- **Accounts are flat strings.** Balances are per exact account; `register
  --account` filters by prefix, but there is no rollup reporting.
- **No external anchor.** Tail-truncation of the ledger is not detectable
  in-band.

## Files

| Path | What |
|---|---|
| `slate.py` | The tool. Parser, rules, summary, chain ledger, HTML report, selftest. |
| `examples/personal.journal` | A worked, clean set of books. |
| `tests/test_slate.py` | 45 end-to-end tests over subprocesses. |
| `ci/import_gate.py` | Rejects any non-stdlib import. |
| `ci/check_counts.py` | Asserts the suite counts meet the CI floors. |
| `ci/prove_suites_can_fail.py` | Negative control for both suites. |
| `ci/mutation.py` | Canary-guarded mutation sweep. |
| `.github/workflows/ci.yml` | 5-cell matrix plus the gate job. |
