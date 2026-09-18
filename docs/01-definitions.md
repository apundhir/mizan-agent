# Metric definitions

**Version 1.0 · 13 September 2026**

This document is the contract the whole build is measured against. It is written **before any metric
code exists**, and every number the system produces traces back to a clause here.

Two rules govern how it is read:

1. **Precision over brevity.** A definition is correct here only if two engineers working from it
   independently reach the same number. Where that required a worked example, there is one.
2. **Assumptions are labelled.** Where this project has chosen a rule that has not been ratified,
   the clause says so and points at [`02-assumption-register.md`](02-assumption-register.md).
   Nothing in this document is presented as an established regulatory rule unless it is one.

Every clause has an **id** (`D-OCC-04`, and so on). Ids are referenced by unit tests, by findings,
and by the reviewer-assist agent when it cites a rule. **Ids are stable**: a clause is superseded,
never renumbered.

---

## 1 Scope

| Id | Clause |
|---|---|
| **D-SCOPE-01** | Two metric families are in scope, and no others: **monthly occupancy** (§3) and **guests by nationality** (§4). |
| **D-SCOPE-02** | Out of scope for this POC: age band, average length of stay, revenue, rate, fee and tax figures. A claim in the workbook addressing any of these is **not** verified and **not** reported as a finding — it is recorded as `out_of_scope` in the verdict so that silence is not mistaken for approval. |
| **D-SCOPE-03** | The reporting period is one calendar quarter, resolved from the submission. Months are **calendar months** in the property's local civil time (Asia/Dubai, UTC+04:00, no daylight saving). No fiscal-month or 4-4-5 basis is used. |
| **D-SCOPE-04** | All dates in the canonical record are **date-only** values. No time-of-day is carried, and none is needed: every rule below resolves on calendar dates. |

### 1.1 Canonical metric keys

Every computed value and every parsed claim is keyed identically, so the join in reconciliation is
exact rather than fuzzy. The key is a string:

```
<metric>:<period>[:<dimension>=<value>]

occupancy_pct:2026-01
room_nights_sold:2026-02
room_nights_available:2026-03
guests_by_nationality:2026-01:nationality_iso2=DE
guests_by_nationality:2026-02:nationality_iso2=GB
```

| Id | Clause |
|---|---|
| **D-KEY-01** | `period` is always `YYYY-MM`. A quarter-level or year-level claim is a **separate key** with period `YYYY-Qn` or `YYYY`, never a month key. |
| **D-KEY-02** | Dimension values use the canonical code, not the label found in the source. `guests_by_nationality:2026-01:nationality_iso2=CZ` regardless of whether the document said *Czechia*, *Czech Republic* or *CZE*. |
| **D-KEY-03** | Keys are lower-snake-case and contain no whitespace. A key is never constructed from a model output. |

---

## 2 Qualifying reservations

Every metric below is computed over a **qualifying set** of reservations. This section defines that
set once, so the two metric families cannot drift apart on inclusion rules.

| Id | Clause | Assumption? |
|---|---|---|
| **D-QUAL-01** | A reservation qualifies only if `status ∈ {CHECKED_OUT, IN_HOUSE}`. | — |
| **D-QUAL-02** | `CANCELLED` and `NO_SHOW` reservations are **excluded** from every metric. | **Yes** — [A-01](02-assumption-register.md#a-01) |
| **D-QUAL-03** | A `status` value that is not one of the four enumerated in Appendix A is **blocking**. It is never coerced, mapped by similarity, or treated as `CHECKED_OUT`. | — |
| **D-QUAL-04** | `rate_code = HOUSE` (house-use: rooms occupied by staff, maintenance or the property's own operations) is **excluded** from every metric — occupancy and guest counts alike. | **Yes** — [A-02](02-assumption-register.md#a-02) |
| **D-QUAL-05** | `rate_code = COMP` (complimentary: a genuine guest paying nothing) is **included** in every metric. The room was occupied by a visitor; that it was not paid for is a commercial fact, not an occupancy one. | **Yes** — [A-02](02-assumption-register.md#a-02) |
| **D-QUAL-06** | All other rate codes (`BAR`, `CORP`, `GOV`, `OTA`, `GROUP`) are included. A `rate_code` outside the enumerated set is **blocking**, per D-QUAL-03's reasoning. |  — |
| **D-QUAL-07** | A reservation appearing more than once with the same `reservation_id` is **blocking**, not de-duplicated. A duplicate row in a PMS export means the export is wrong, and silently collapsing it hides that. | — |

### 2.1 Day-use

| Id | Clause | Assumption? |
|---|---|---|
| **D-QUAL-08** | A **day-use** reservation is one where `departure_date = arrival_date`, giving `nights = 0`. The guest occupied a room during the day and did not stay overnight. | — |
| **D-QUAL-09** | Day-use reservations contribute **zero room-nights** and are therefore invisible to occupancy. | **Yes** — [A-03](02-assumption-register.md#a-03) |
| **D-QUAL-10** | Day-use reservations **are** counted in guests by nationality, on the arrival month. A day-use visitor is a guest of the destination. | **Yes** — [A-03](02-assumption-register.md#a-03) |

> D-QUAL-09 and D-QUAL-10 disagree deliberately, and the disagreement is the point: occupancy
> measures rooms sold overnight, and the nationality table counts people who came. A day-use visitor
> is one but not the other. A test asserts both behaviours on the same reservation.

---

## 3 Monthly occupancy

### 3.1 Room-nights sold

| Id | Clause |
|---|---|
| **D-RNS-01** | `nights = departure_date − arrival_date`, in whole days. For a stay arriving 2026-01-10 and departing 2026-01-13, `nights = 3`. |
| **D-RNS-02** | `room_nights = nights × rooms`. This value is **always derived**. A `room_nights` column printed in a PMS export is compared as a cross-check and **never adopted as an input**; a mismatch is a blocking finding on that row. |
| **D-RNS-03** | A reservation's room-nights are **apportioned to the month of each occupied night**. The occupied nights of a stay are the nights of `arrival_date` through `departure_date − 1` inclusive. The night of the departure date is not occupied. |
| **D-RNS-04** | `room_nights_sold(month)` = the sum, over the qualifying set, of `rooms × (occupied nights of that reservation falling in that month)`. |

#### Worked example — a month-spanning stay

A reservation of **2 rooms**, arriving **2026-02-28**, departing **2026-03-03**.

```
occupied nights:  night of 28 Feb   night of 1 Mar   night of 2 Mar     → nights = 3
                  └── February ──┘  └──────── March ────────────┘
room_nights total          = 3 nights × 2 rooms = 6
room_nights_sold(2026-02)  = 1 night  × 2 rooms = 2
room_nights_sold(2026-03)  = 2 nights × 2 rooms = 4
```

The night of 3 March is **not** occupied — the guest departed that day. The same reservation
contributes to two months' occupancy, and (per §4) contributes its **guests to February only**.

> **This is the single most likely source of live definitional variance.** A hotel that apportions
> the whole stay to the arrival month will report February room-nights 4 too high and March 4 too
> low. The system detects this mechanically via the `month_basis` permutation (S7) and reports it as
> **definitional**, escalated to the policy owner — never as a hotel error.

### 3.2 Room-nights available

| Id | Clause |
|---|---|
| **D-RNA-01** | Rooms available is a **property attribute, not a reservation attribute**, and does not appear in a reservation export. It comes from a per-hotel **inventory reference**: one row per calendar date, carrying `rooms_total` and `rooms_out_of_order`. |
| **D-RNA-02** | `room_nights_available(month)` = Σ over the dates in that month of `(rooms_total − rooms_out_of_order)`. |
| **D-RNA-03** | Out-of-order rooms are **excluded from the denominator**. A room that cannot be sold was not available, and counting it depresses occupancy for a reason that has nothing to do with demand. *(Assumption [A-08](02-assumption-register.md#a-08).)* |
| **D-RNA-04** | The inventory reference is a **required input**. Without it, occupancy verification is **not possible** and the verdict says so — the run reports occupancy as `not_verifiable` with reason `missing_inventory_reference`. It is never approximated from a room count, a maximum observed room number, or any value inferred from the reservations. |
| **D-RNA-05** | A month for which the inventory reference has **no rows** is a **closed month**: `room_nights_available = 0`. This is distinct from D-RNA-04 — the reference exists, and it says the property was shut. |

### 3.3 Occupancy percentage

| Id | Clause |
|---|---|
| **D-OCC-01** | `occupancy_pct(month) = 100 × room_nights_sold(month) ÷ room_nights_available(month)`. |
| **D-OCC-02** | The denominator is **room-nights** available, not **rooms** available. Dividing by a room count rather than a room-night count inflates occupancy by roughly the number of days in the month, and is the canonical definitional error this POC is built to catch (fixture F3). |
| **D-OCC-03** | **A zero denominator returns zero, and is a closed month, not an error.** `room_nights_available = 0` ⇒ `occupancy_pct = 0.0`. No exception is raised and no division is attempted. |
| **D-OCC-04** | Occupancy is carried internally at **full float precision** and rounded **only at the presentation boundary**, to **two decimal places**, half-up. Comparison against a claim uses the rounded value (see D-TOL-02). Rounding mid-computation is never done. |
| **D-OCC-05** | Occupancy is **not capped at 100%**. A value above 100 is arithmetically possible when rooms are oversold or the inventory reference is wrong, and it is a finding worth surfacing rather than a number worth hiding. |

---

## 4 Guests by nationality

| Id | Clause | Assumption? |
|---|---|---|
| **D-NAT-01** | The metric counts **guests** — people — not arrivals, not reservations, and not room-nights. | **Yes** — [A-06](02-assumption-register.md#a-06) |
| **D-NAT-02** | `guests(reservation) = adults + children`. | — |
| **D-NAT-03** | **Children are included** in guest totals. | **Yes** — [A-05](02-assumption-register.md#a-05) |
| **D-NAT-04** | `adults` and `children` are carried as separate fields in the canonical record so an alternative definition can be tested without regenerating the corpus. | — |
| **D-NAT-05** | Every guest on a reservation is attributed to that reservation's **single** `nationality_iso2`. A PMS reservation export carries one nationality per booking, not one per occupant; attributing a party of four to four nationalities would be inventing data. | **Yes** — [A-07](02-assumption-register.md#a-07) |
| **D-NAT-06** | Guests are counted in the month of the reservation's **`arrival_date`** — the **arrival-month basis**, not the occupied-night basis used for occupancy (D-RNS-03). A guest arrives once and is counted once. | **Yes** — [A-04](02-assumption-register.md#a-04) |
| **D-NAT-07** | `guests_by_nationality(month, iso2)` = Σ `(adults + children)` over qualifying reservations whose `arrival_date` falls in that month and whose `nationality_iso2` equals `iso2`. |  — |
| **D-NAT-08** | A reservation arriving **before** the reporting period and departing within it contributes **room-nights** to the period (D-RNS-03) but **no guests** to it (D-NAT-06). The two metric families are not expected to reconcile with each other, and a workbook in which they do is the suspicious case. | — |

### 4.1 Nationality normalisation

| Id | Clause |
|---|---|
| **D-NAT-09** | Nationality is normalised to **ISO 3166-1 alpha-2** from a **committed lookup table**, which covers alpha-2 codes, alpha-3 codes, ISO official short names, and common variants and short forms in documented use. |
| **D-NAT-10** | Matching is **case-insensitive and punctuation-insensitive**, and collapses internal whitespace. `czech republic`, `Czech Republic`, `CZECH  REPUBLIC` and `Czech Rep.` all resolve identically. |
| **D-NAT-11** | Known variant pairs resolve to one code. `Czechia` and `Czech Republic` both → **`CZ`**. `Korea, Republic of`, `South Korea` and `Republic of Korea` all → **`KR`**. `UK`, `United Kingdom` and `Great Britain` all → **`GB`**. |
| **D-NAT-12** | **An unmappable label is blocking and is never guessed.** Not by edit distance, not by substring match, not by a model's best effort. The run raises a **V7 extraction-limit** finding naming the exact unmatched string, and escalates for human mapping. |
| **D-NAT-13** | The label-resolution agent may be consulted for an unmappable label, and its output contract permits **`ABSTAIN`**. An abstention produces D-NAT-12's blocking finding. A resolution it *does* return is a **proposal recorded in the finding for a human to accept** — it does not silently enter the metric. |
| **D-NAT-14** | Nationality is the guest's **nationality as recorded by the property**, not a country of residence and not a country of booking origin. No cross-field inference is performed. |
| **D-NAT-15** | `nationality_iso2` is the only nationality representation that reaches a metric function. The original label is retained on the record **for evidence display only**. |

---

## 5 Tolerances

| Id | Clause |
|---|---|
| **D-TOL-01** | **Counts are exact.** Guest counts, room-night counts, reservation counts: zero tolerance. A difference of one guest is a finding. |
| **D-TOL-02** | **Percentages: ± 0.10 percentage points**, applied to the presentation-rounded value (D-OCC-04). A claimed 71.43% against a computed 71.38% is within tolerance; against 71.30% it is not. |
| **D-TOL-03** | **Totals and subtotals are exact.** A total row is a count or a sum of counts, so D-TOL-01 governs it. |
| **D-TOL-04** | **Anything inside tolerance is logged, never raised.** It appears in the run ledger and in the "claims checked" count, and does not appear in the findings list. A verification report whose findings include differences the policy says are acceptable teaches the reviewer to skim. |
| **D-TOL-05** | Tolerance is applied **symmetrically** — a claim below the computed value is treated exactly as one above it. |

---

## 6 Materiality and severity

Severity is a property of the **variance**, assigned mechanically from the cause and the size. It is
never assigned by a model.

| Id | Clause |
|---|---|
| **D-MAT-01** | **Blocking** — the system could not read or could not map something, so no judgement about the submission is possible. Always outranks every other severity. Cause class **V7**. |
| **D-MAT-02** | **Material** — a difference outside tolerance that a reviewer must decide on. Cause classes **V1** (transcription), **V2** (definitional), **V5** (completeness). |
| **D-MAT-03** | **Informational** — a difference inside tolerance, or a presentational rounding artefact. Cause class **V6**. Logged, never raised (D-TOL-04). |
| **D-MAT-04** | A **missing** claim (a computed value with no counterpart in the workbook) is **material, V5**. The workbook is incomplete. |
| **D-MAT-05** | An **orphan** claim (a claim with no computed counterpart) is **material, V5**. The workbook asserts something the source data cannot support, which is the more serious of the two directions. |
| **D-MAT-06** | **Definitional variances (V2) are carried in a separate array from clerical errors and are never counted as hotel errors.** They are escalated to the policy owner. A count of "hotel errors" that includes policy disagreements is a wrong number presented as a right one. |

### 6.1 Classification order

Tested in this order, first match wins. The order is a correctness requirement, not a performance
optimisation.

| Id | Order | Clause |
|---|---|---|
| **D-CLS-01** | 1 · **Extraction limit (V7)** | Tested first, so the system **never accuses a hotel of an error it could not actually see**. |
| **D-CLS-02** | 2 · **Definitional (V2)** | Tested before transcription, so a **policy disagreement is never reported as a clerical mistake**. |
| **D-CLS-03** | 3 · **Completeness (V5)** | A missing or orphan claim is structural, and outranks a value difference. |
| **D-CLS-04** | 4 · **Transcription (V1)** | Only reached once the variance is known to be readable, not definitional, and not structural. |
| **D-CLS-05** | 5 · **Rounding (V6)** | The residual: inside tolerance, or explained by presentation rounding. |
| **D-CLS-06** | A variance that reaches the end of the ladder **unclassified is blocking**. There is no "other" bucket, because an unexplained difference in a verification report is exactly the thing a reviewer cannot act on. |

### 6.2 How a definitional cause is established

| Id | Clause |
|---|---|
| **D-CLS-07** | The engine re-runs the metric under a **small enumerated set of alternative policy settings** — status inclusion, complimentary treatment, day-use treatment, month basis, occupancy denominator. If one permutation reproduces the claimed value **within tolerance**, the variance is **definitional**, and the permutation that explains it is **named in the finding**. |
| **D-CLS-08** | The permutation set is enumerated in `policy.yaml` under `permutations`. It is **finite, ordered and committed** — never generated at run time and never searched heuristically. |
| **D-CLS-09** | Where more than one permutation reproduces the claim, the **first in the committed order** is named, and the finding records that others also explain it. Reporting one cause as certain when two are consistent with the evidence would be a false precision. |
| **D-CLS-10** | **The code finds the cause. The model only writes the sentence.** No part of classification consults a model. |

---

## 7 Evidence

| Id | Clause |
|---|---|
| **D-EV-01** | Every finding carries **both** a source reference **and** an Excel reference (sheet, A1 cell). The source reference is a PDF reference (file, page, row range) for every reservation-derived figure, and the **inventory reference** (file, row range) for `room_nights_available`, which has no page anywhere in the system because rooms available is a property attribute rather than a reservation one (D-RNA-01). A finding missing either side fails an assertion and **never reaches the output**. |
| **D-EV-02** | A blocking finding raised **before** claims are parsed (an unreadable page, a failed totals reconciliation) carries the PDF reference and records the Excel reference as `not_reached` — an explicit, typed absence, not an empty string. |
| **D-EV-05** | And the mirror: a finding raised from the **workbook alone**, before it is compared to any source (§8), carries the Excel reference and records the source reference as `not_reached`. A finding may carry **one** typed absence and never two — one citing nothing on either side is refused at construction. |
| **D-EV-03** | **No guest name enters state, output or logs.** The canonical record carries an opaque `guest_ref` only. |
| **D-EV-04** | Every output records the **`policy.yaml` version** and the **metric library version** that produced it. A number without its ruleset is not defensible. |

---

## 8 The submitted workbook

The claims side of the comparison. These clauses govern how a figure gets **out of the hotel's
spreadsheet** and what is checked about the spreadsheet before any figure of ours is put beside it.

| Id | Clause |
|---|---|
| **D-XLS-01** | A claim is read by code from a cell reference, and **every claim carries that reference** (`Nationality!D14`). A model may decide which range holds which metric; it never reads, reports or adjusts a value. The restriction is a property of the tool surface, not an instruction in a prompt. |
| **D-XLS-02** | An **empty cell is not a zero.** It is the absence of a claim, and no claim is recorded for it. Whether the absence conceals an omission is settled by D-XLS-04, never by assuming a value. |
| **D-XLS-03** | A cell holding a **formula with no cached result is a refusal**, not a computation. Evaluating it would mean this system deciding what the hotel claimed. A figure stored as text (`81.70%`, `1,285`) *is* read, and the literal text is kept alongside the parsed value. |
| **D-XLS-04** | **The workbook is checked against itself first**, before any comparison to the PDFs: every stated total equals the sum of its stated components, every period roll-up equals the sum of its months, and a stated occupancy equals the stated room-nights sold over the stated room-nights available. Percentages are excluded from the additive checks — a quarter's occupancy is a ratio, not a sum. |
| **D-XLS-05** | A self-consistency failure is attributed to the **hotel** and reported **ahead of every finding**, because a workbook whose own arithmetic disagrees produces a cascade of variances that all trace to one clerical error, and naming the clerical error first is the difference between one actionable item and twelve confusing ones. It is carried as a workbook inconsistency rather than forced into a finding: a finding is a variance on a metric key, and a total *across* a dimension has no key the grammar can express (D-KEY-02). Where a key does identify the figure, reconciliation raises it as a transcription variance (V1). |
| **D-XLS-06** | A sheet or header block that cannot be mapped is **flagged for human mapping and never silently skipped**. Exhaustiveness is established by comparing the mapping against the workbook's own sheet list, not by trusting the mapper to have mentioned everything. A block naming a metric the configuration does not declare (§1, and `policy.scope`) is treated the same way — an unrecognised name never becomes a new metric (D-KEY-03). |

---

## 9 What this document does not settle

Honest limits, so nobody mistakes silence for coverage:

- **Whether these are a regulator's ratified definitions.** They are this project's own, chosen so
  the POC can proceed. Six of them are flagged assumptions. Ratifying them is the second onboarding
  ask.
- **Whether real PMS exports carry reservation-level rows at all.** If they carry pre-aggregated
  monthly summaries, §3.1 and §4 cannot be computed from them, and the approach narrows to
  summary-to-summary matching. Every clause above assumes reservation-level input.
- **Multi-property and multi-currency.** One hotel, one quarter. No consolidation rules exist here.
- **Revisions and resubmissions.** A corrected submission is treated as a new submission. There is no
  concept of a diff against a previous verdict.

---

*Superseding a clause: mark the old id `superseded by <new id>` and leave it in place. Ids are cited
by tests, findings and the reviewer-assist agent; renumbering one silently invalidates that citation.*
