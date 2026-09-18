# ADR-0003 · The corpus is generated ledger-first, and the generator may not import the product

- **Status:** Accepted
- **Date:** 2026-09-13
- **Issues:** PRD-83

## Context

Every metric, extraction and reconciliation story needs data whose correct answers are known. There
are two ways to arrive at that, and only one of them survives contact with a disagreement.

The tempting way is to author the documents first — three plausible PMS reports and a workbook —
and then work out what the right answers are. The problem appears the first time the pipeline
disagrees with the expected number: the expectation was produced by a person reading a document, so
nobody can say which side is wrong without redoing the arithmetic by hand, and whoever redoes it is
the same person who got it wrong the first time. Worse, the expectation drifts silently. Change a
row in a PDF and the committed expectation no longer describes the document, with nothing to notice.

There is also a subtler trap, and it is the one this ADR is mostly about. Once the ledger exists,
computing `truth_metrics.json` from it is a few lines of aggregation — and the product already has a
metric library that does exactly that. Reusing it would be the obvious, DRY, apparently
professional choice. It would also destroy the entire value of the exercise: the POC would then
demonstrate that the metric library equals itself, and every eval score would be a tautology
wearing the costume of a test.

A narrower version of the same trap is easy to miss. `tda.contracts.ReservationRecord.occupied_nights()`
*is* D-RNS-03's month apportionment — the single clause the whole occupancy argument rests on, and
the one the PRD names as the most likely source of live definitional variance. A generator that
imported "just the contracts, not the metric library" would co-derive precisely the thing under
test, while looking entirely innocent in review.

## Decision

**1. Generate in one direction: spec → ledger → documents, with truth aggregated from the ledger.**

```
spec.py  →  ledger (reservations + inventory)  →  PDFs, workbook
                        ↓
                truth_metrics.json
```

Reservations are generated first from a fixed seed. The monthly PDFs and the claim workbook are
*renderings* of that ledger; `truth_metrics.json` is an *aggregation* of it. Nothing is authored
twice, so a document and the truth cannot disagree.

**2. `tools/datagen/` may not import any of `tda`** — not `tda.metrics`, not `tda.contracts`, not
anything. The generator re-derives month apportionment, the qualifying filter, the rounding and the
canonical key format from `docs/01-definitions.md`.

**3. Configuration is shared; implementation is not.** `aggregate.py` reads the qualifying rules and
month bases straight out of `policy.yaml`, because a truth value is only meaningful with respect to
a ruleset. A generator with its own hard-coded opinion of which rate codes qualify would disagree
with the pipeline the first time the policy was amended, and the disagreement would look like a
pipeline bug.

**4. The edges are quotas, not draws.** Month-spanning stays, complimentary and house-use rooms,
day-use reservations, pre-period arrivals and out-of-order windows are required minimums, asserted
after generation. Generation fails if any is unmet.

**5. The corpus is byte-reproducible and committed, and `corpus/demo/ground_truth/` is never read by
the pipeline.**

## Enforcement

| Decision | How it is held in place |
|---|---|
| No `tda` import in the generator | `tools/guard/import_guard.py` rule 2, in `make ci`. Two violation fixtures under `tests/arch/fixtures/` — one importing `tda.metrics`, one importing only `tda.contracts` — are asserted to fail it |
| The two apportionments agree | `test_the_two_apportionment_implementations_agree` runs both readings of D-RNS-03 over all 1,200 rows. This is what makes the duplication *worth* having rather than just duplicated |
| The generator's policy view matches the product's | `test_the_generators_policy_view_matches_the_product_loader` compares field by field. Two readers of one file can still disagree, and if they did, `truth_metrics.json` would describe a ruleset the pipeline never ran under |
| The key format is stated twice and correct once | Every emitted key is round-tripped through `MetricKey.parse` |
| The edges exist | `ledger.check_quotas`, plus a test that watches it reject a corpus stripped of its month-spanning stays |
| Byte-reproducibility | `make corpus` rebuilds into a temporary directory and diffs SHA-256 digests against the committed corpus. In `make ci`, so a stale or hand-edited corpus fails the build |
| Ground truth stays unread | `submission/` and `ground_truth/` are separate directories. A convention would be broken by accident; a directory split has to be broken on purpose |

## Consequences

### Accepted costs

- **The apportionment logic exists twice**, and both copies have to be maintained. This is a real
  cost and it is the point: the second copy is the check. Anybody tempted to remove it should read
  the cross-check test first.
- **A change to `policy.yaml`'s shape can break the generator.** Mitigated by failing loudly and
  naming the missing path rather than defaulting — a guessed default would emit ground truth for a
  policy nobody configured, which is the failure that cannot be detected from either side.
- **Byte-reproducibility took three separate fixes**, none of them obvious, all of them found by
  generating twice and diffing: reportlab's creation date and document id (`invariant=1`), every
  zip entry timestamp in the xlsx, and `dcterms:modified`, which openpyxl rewrites from the wall
  clock *during* the save. Each is pinned by a test, because a library upgrade could reintroduce
  any of them and the symptom would appear in `make repro` rather than here.
- **The committed corpus is ~200 KB of binary in the repository.** Accepted: a corpus regenerated
  on checkout is a corpus whose documents differ between machines, and the walkthrough cites page
  and row numbers.

### What is bought

- A disagreement between the pipeline and the expected number is **always the pipeline's**, and can
  be localised to a clause rather than argued about.
- Permutation expectations in S7 are derivable from the ledger rather than asserted by hand.
- `make repro` measures the pipeline's determinism rather than the generator's entropy.

### What this does not claim

**A clean pass on this corpus is not evidence that the system works.** It is the data the system was
rendered from; agreement is a tautology. `corpus/demo/` exists so the happy path is demonstrable and
so parsers have a realistic document to be built against. The **scored** fixtures — with planted
errors, where catching something means something — are built separately in S11 and are the only
ones `make eval` reports on.

It also does not claim the corpus is representative of a real property. The mix is plausible; it is
not measured against anything. Nothing in this repository should be used to estimate a real number.

## Alternatives considered

**Author the documents by hand and derive expectations from them.** Loses on the disagreement
argument above: an expectation produced by a person reading a PDF cannot adjudicate against code.

**Generate the ledger, then compute truth with `tda.metrics`.** The tautology. Rejected, and the
import guard exists because it is the shortcut somebody would reach for at speed with an entirely
reasonable justification.

**Import only `tda.contracts` for the typed records.** The narrower tautology, and much harder to
spot in review — `occupied_nights()` is the apportionment. Rejected, which is why the guard rule
covers all of `tda` rather than just `tda.metrics`, and why there is a fixture proving it.

**Keep the truth expectations in the test suite instead of a committed file.** Would work for the
metric library and break everything downstream: the eval report, the reviewer screen and the memo
all need the expected values at run time, not at test time.

**Let the edge cases fall out of the random draw.** Rejected because the failure is silent: a
permutation with nothing to permute returns the baseline, matches, and reports success. A missing
edge shows up as a *passing* test, which is the worst shape a defect can have.

**Skip the inventory reference and infer the denominator from `rooms_total × days`.** Would make
D-RNA-04 untestable and `P-OOO-INCLUDED` a no-op. The out-of-order windows exist so the denominator
cannot be inferred.
