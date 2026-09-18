# `corpus/demo` — the synthetic demonstration corpus

**Classification: Green. Synthetic data only.** No real property, no real export, no real guest.
The hotel does not exist. There is no guest name anywhere in this corpus, and none was ever
generated: `guest_ref` is a digest under a published salt, so there is nothing to leak rather than
something withheld (D-EV-03).

Regenerate with `make datagen`. Byte-identical every time, from seed `20260913`.

## Layout, and the one rule

```
submission/     exactly what a hotel submits — the pipeline reads ONLY this
ground_truth/   what the generator knows and the hotel does not
manifest.json   sha256 of every file above
```

**Nothing in the pipeline may read `ground_truth/`.** It holds `truth_metrics.json` and the
reservation ledger both documents were rendered from. A verification that had seen the answers
would not be a verification. The split is two directories rather than a naming convention because
this is the easiest rule in the repository to break by accident and the hardest to notice
afterwards.

## This corpus is frozen, and it is never scored

A clean pass on the data the system was rendered from is a tautology, not evidence. `corpus/demo/`
exists so the happy path is demonstrable and so parsers have a realistic document to be built
against. The **scored** fixtures — the ones with planted errors, where catching something means
something — are built separately in S11 and are the only ones `make eval` reports on.

## What is in it, and why each edge is there

| Edge | Why it exists |
|---|---|
| ≥ 25 month-spanning stays | D-RNS-03 apportionment, and the `P-MONTH-ARRIVAL` / `P-MONTH-DEPARTURE` permutations |
| ≥ 25 COMP and ≥ 25 HOUSE | so `P-COMP-EXCLUDED` and `P-HOUSE-INCLUDED` move a number instead of silently returning the baseline |
| ≥ 30 day-use reservations | D-QUAL-08..10: zero room-nights, but a real guest of the destination |
| 12 stays arriving before 1 Jan | D-NAT-08: room-nights in January, no guests in January — the case the two metric families are *expected* not to reconcile on |
| 8 stays open at 31 March | `IN_HOUSE` with dates that agree with the status |
| Two out-of-order windows | D-RNA-03/04: the occupancy denominator cannot be inferred from a room count |
| One nationality in one month only | S8's completeness path needs a row a workbook can omit |
| Country **labels**, not ISO codes, in the workbook | D-NAT-09..11 normalisation, including the `Czech Republic` → `CZ` variant pair |
| An out-of-scope sheet | D-SCOPE-02: silence on an unverified claim reads as approval |

The claim workbook asserts the **correct** figures. See "never scored", above.

## Files

| File | What it is |
|---|---|
| `submission/pms_2026-01..03.pdf` | Monthly reservation detail reports. Text layer, 30 rows per page, per-page subtotals, and a grand-total block on the last page |
| `submission/inventory_2026-Q1.csv` | The room inventory reference — one row per day, with the out-of-order windows |
| `submission/claims_2026-Q1.xlsx` | The claim workbook: Summary, Occupancy, Nationality, Rate & Revenue |
| `ground_truth/truth_metrics.json` | Every metric this corpus establishes, keyed canonically |
| `ground_truth/reservations.csv` | The reservation ledger everything was rendered from |

A month-spanning stay appears on **both** months' reports, with `RN Total` (the whole stay) and
`RN Month` (the part in this month) printed separately. The grand total is the **qualifying** total
and the report names what it excluded, so the reconciliation is a check a reader can do by hand.
