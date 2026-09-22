# Assumption register

**Version 1.0 · 13 September 2026**

Every definition in this POC is **this project's own, not a regulator's**. That is deliberate:
nothing here waits on a regulatory decision, so the POC can be built and shown. But it means a
reader needs to know exactly which rules are established and which are choices made so the work
could proceed.

This register is the answer. **Eight assumptions**, each with the value chosen, why, what moves if the regulator rules otherwise, and — critically — **how much work it is to change**.

> **This register is not a list of things we were denied.** It is the source of
> `05-onboarding-asks.md` *(written alongside the demo scenes)*: each entry becomes a decision the regulator would own to
> move from demonstration to pilot. The ask list is the POC's **output**, not its input.

## How to read the "cost to change" column

Because every contestable rule lives in `policy.yaml` rather than in code, most of these are a
config change and a re-run:

| Cost | Meaning |
|---|---|
| **Config** | Edit `policy.yaml`, bump its version, re-run. No code change. Regenerate the corpus if the rule affects what the generator plants. |
| **Config + corpus** | As above, plus `make datagen` — the synthetic corpus embeds the rule, so ground truth has to be re-derived. |
| **Code** | A metric function or contract changes. Needs a PR, tests and a review. |

The whole point of writing S1 before any metric code is that the **Code** row is almost empty.

---

## A-01 · Cancellations and no-shows are excluded

| | |
|---|---|
| **Clause** | [D-QUAL-02](01-definitions.md#2-qualifying-reservations) |
| **Question** | Are `CANCELLED` and `NO_SHOW` reservations counted? |
| **Value chosen** | **Excluded** from every metric |
| **Confidence** | High. This is near-universal hospitality practice |
| **Cost to change** | Config — `qualifying.status.included` |
| **Permutation** | `P-STATUS-INCLUDE-CANCELLED`, `P-STATUS-INCLUDE-NOSHOW` |

**Why.** A cancelled booking never occupied a room and a no-show guest never arrived. Counting either
inflates both occupancy and guest counts with people who were not there.

**What moves if the regulator rules otherwise.** Occupancy and **every count** shift upward. The magnitude
depends on the property's cancellation rate, which for a resort market is typically material — this
is not a rounding-level change.

**Worth noting:** a no-show is sometimes *billed* like an occupied room. If the regulator's interest is
commercial rather than physical occupancy, this assumption is the one most likely to be overturned —
and the permutation engine will detect it automatically the first time a hotel reports on the other
basis.

---

## A-02 · Complimentary rooms count; house-use rooms do not

| | |
|---|---|
| **Clause** | [D-QUAL-04](01-definitions.md#2-qualifying-reservations), [D-QUAL-05](01-definitions.md#2-qualifying-reservations) |
| **Question** | Are complimentary and house-use rooms counted in occupancy? |
| **Value chosen** | `COMP` **included**; `HOUSE` **excluded** |
| **Confidence** | Medium. Both halves are defensible the other way |
| **Cost to change** | Config — `qualifying.rate_code.excluded` |
| **Permutation** | `P-COMP-EXCLUDED`, `P-HOUSE-INCLUDED` |

**Why.** The distinction is **who was in the room**. A complimentary guest is a visitor to the
destination who paid nothing — a commercial fact, not an occupancy one. A house-use room is the
property consuming its own inventory: staff accommodation, a room held for maintenance. The first is
a guest; the second is not.

**What moves if the regulator rules otherwise.** Roughly **1 to 2 percentage points** of occupancy, in either
direction. Guest counts move by the complimentary guest count.

**The sharper question this hides.** If the regulator's occupancy figure feeds a **destination fee charged per
occupied room**, then "was the room paid for" may matter more than "was someone in it", and `COMP`
should probably be excluded. The right answer depends on what the number is *for* — which is a
question for the policy owner — see [What has no owner yet](#what-has-no-owner-yet) — not for an
engineer.

---

## A-03 · Day-use rooms are not room-nights, but day-use guests are guests

| | |
|---|---|
| **Clause** | [D-QUAL-08](01-definitions.md#21-day-use) – [D-QUAL-10](01-definitions.md#21-day-use) |
| **Question** | Do day-use reservations count as room-nights? As guests? |
| **Value chosen** | **Zero room-nights**; **counted as guests** on the arrival month |
| **Confidence** | High on room-nights; medium on guests |
| **Cost to change** | Config — `qualifying.day_use.*` |
| **Permutation** | `P-DAYUSE-COUNTS-RN` |

**Why.** A day-use stay has `nights = 0` by definition, so `nights × rooms` is zero and occupancy
cannot see it. But the person came to Ras Al Khaimah, and the nationality table counts people.

**The two rules disagree on purpose.** Occupancy measures rooms sold overnight; the nationality table
counts visitors. A day-use guest is one and not the other. A test asserts both behaviours on the
same reservation so the disagreement is visible in the test suite rather than surprising someone later.

**What moves if the regulator rules otherwise.** Affects roughly **0.8% of reservations** in the synthetic
corpus. Small in aggregate, but it is exactly the kind of difference that produces a single
unexplained variance in one month and costs an afternoon.

---

## A-04 · Month basis: occupancy by occupied night, nationality by arrival month

| | |
|---|---|
| **Clause** | [D-RNS-03](01-definitions.md#31-room-nights-sold), [D-NAT-06](01-definitions.md#4-guests-by-nationality) |
| **Question** | How is a stay that spans a month boundary apportioned? |
| **Value chosen** | Occupancy **by occupied night**; nationality **by arrival month** |
| **Confidence** | High that this is correct. **Low that a hotel will do the same** |
| **Cost to change** | Config — `metrics.*.month_basis` |
| **Permutation** | `P-MONTH-ARRIVAL`, `P-MONTH-DEPARTURE`, `P-NAT-MONTH-OCCUPIED` |

> ### This is the single most likely source of live definitional variance.

**Why.** A room occupied on the night of 28 February was occupied in February, whatever month the
guest checked out in. Occupancy is a nightly measure, so it apportions nightly. A guest, by contrast,
arrives once — counting them in two months would double-count a person.

**Worked example.** 2 rooms, arriving 2026-02-28, departing 2026-03-03:

```
room_nights total         = 6     (3 nights × 2 rooms)
room_nights_sold(2026-02) = 2     (the night of 28 Feb)
room_nights_sold(2026-03) = 4     (the nights of 1 and 2 Mar)
guests counted in         = February only
```

**What moves if the regulator rules otherwise.** A hotel apportioning the whole stay to the arrival month
reports February 4 room-nights too high and March 4 too low **on this one reservation**. The
synthetic corpus carries **at least 25 month-spanning stays** precisely so this effect is measurable
rather than theoretical.

**Why this matters more than its size.** A variance from this cause looks exactly like a clerical
error in the workbook. Reported as a transcription mistake it is an accusation; reported as a
definitional difference it is a conversation. The permutation engine is what makes the difference,
and this assumption is the reason the permutation engine exists at all.

---

## A-05 · Children are counted in guest totals

| | |
|---|---|
| **Clause** | [D-NAT-03](01-definitions.md#4-guests-by-nationality) |
| **Question** | Are children included in guest counts? |
| **Value chosen** | **Included** |
| **Confidence** | Medium |
| **Cost to change** | Config — `metrics.guests_by_nationality.include_children` |
| **Permutation** | `P-CHILDREN-EXCLUDED` |

**Why.** A child is a visitor to the destination. For destination performance reporting — which is
what this figure feeds — a family of four is four visitors.

**What moves if the regulator rules otherwise.** Guest totals fall, materially in a leisure market with a high
family mix. Nationality *shares* also shift, because family travel is unevenly distributed across
source markets: excluding children does not scale every row down by the same factor.

**Mitigated by design.** `adults` and `children` are carried as **separate fields** in the canonical
record (D-NAT-04), so this can be tested both ways **without regenerating the corpus**. That was a
deliberate choice made because this assumption looked likely to be contested.

---

## A-06 · The nationality table counts guests — not arrivals, not room-nights

| | |
|---|---|
| **Clause** | [D-NAT-01](01-definitions.md#4-guests-by-nationality) |
| **Question** | Is the nationality table counting guests, arrivals or room-nights? |
| **Value chosen** | **Guests** (people) |
| **Confidence** | Medium-low. The workbook's column header does not say |
| **Cost to change** | Config — `metrics.guests_by_nationality.counts` |
| **Permutation** | `P-NAT-COUNTS-ARRIVALS`, `P-NAT-COUNTS-ROOMNIGHTS` |

**Why.** "Guests by nationality" reads as a count of people, and destination performance reporting is
normally interested in visitor numbers.

**What moves if the regulator rules otherwise.** **Every nationality claim in the workbook.** This is the
broadest of the eight assumptions: it does not shift a number, it changes what the number *is*. A
party of 3 in 1 room for 4 nights is 3 guests, 1 arrival, or 4 room-nights — three different answers
to the same cell.

**Why this is flagged low-confidence.** A hotel filling in a spreadsheet column labelled "Guests"
may well be transcribing whatever its PMS report calls "Guests", and PMS vendors are not consistent
about it. Of the eight, **this is the one most worth asking about before the first real file**, because
a wrong answer here makes every nationality finding wrong in the same direction — which looks like a
systematic hotel error and is not one.

---

## A-07 · One nationality per reservation, applied to every guest on it

| | |
|---|---|
| **Clause** | [D-NAT-05](01-definitions.md#4-guests-by-nationality) |
| **Question** | How is nationality attributed when a reservation has several occupants? |
| **Value chosen** | Every guest on the reservation takes the reservation's **single** `nationality_iso2` |
| **Confidence** | High — as a description of what the data supports |
| **Cost to change** | **Code**, and it needs richer source data |
| **Permutation** | none — this cannot be permuted from the available fields |

**Why.** A PMS reservation export carries **one nationality per booking**, not one per occupant.
Attributing a party of four to four nationalities would be inventing data.

**What moves if the regulator rules otherwise.** Nothing can be recomputed — the information is not in the
export. Per-occupant nationality would require a **guest-level** export rather than a
reservation-level one, which is a different input file and a different conversation about personal
data.

**Why it is in the register even though it cannot be changed.** Because it is a **real limit on what
the number means**, and the walkthrough should say so rather than let a reader assume the figure is
per-person-accurate. A party of four Germans booked by a British colleague shows as four British
guests, and no amount of engineering on this input fixes that.

---

## A-08 · Out-of-order rooms are excluded from the occupancy denominator

| | |
|---|---|
| **Clause** | [D-RNA-03](01-definitions.md#32-room-nights-available) |
| **Question** | Do rooms out of order count as available? |
| **Value chosen** | **Excluded** from the denominator |
| **Confidence** | Medium |
| **Cost to change** | Config — `metrics.occupancy_pct.exclude_out_of_order` |
| **Permutation** | `P-OOO-INCLUDED` |

**Why.** A room that cannot be sold was not available. Counting it depresses occupancy for a reason
that has nothing to do with demand, which is the thing the metric is supposed to measure.

**What moves if the regulator rules otherwise.** Occupancy falls whenever rooms are out of order. The synthetic
corpus carries a deliberate **out-of-order window** so the effect is present and measurable; without
it, this assumption would be untestable and the denominator would be indistinguishable from a plain
room count.

**The competing view, stated fairly.** If the regulator's interest is **asset utilisation** rather than
commercial performance, out-of-order rooms *should* count — a hotel that lets a wing fall into
disrepair is genuinely underusing its asset, and excluding those rooms flatters it. Both definitions
are defensible; they answer different questions. Which is exactly why this needs a named owner rather
than an engineer's preference.

---

## What has no owner yet

The most consequential gap in this register is not an assumption at all:

> **Nobody is named as the owner of these definitions.**

Six of the eight above are genuinely arguable. Without a named policy owner, each one gets
re-litigated every time a hotel disputes a finding, and the system's authority erodes one
conversation at a time. **Naming an owner is a cheaper ask than any of the others on the list, and it
unblocks all of them** — which is why it appears in the onboarding asks in its own right rather than
as a footnote to the definitional questions.

## The assumption that is not ours to make

Everything in this register assumes the PMS PDF exports contain **reservation-level rows**.

If real exports turn out to be **pre-aggregated monthly summaries**, then room-nights cannot be
derived, occupancy cannot be recomputed, nationality cannot be re-aggregated, and **none of the eight
assumptions above matters** — because there would be nothing to apply them to. The approach would
narrow to summary-to-summary matching, and the business case would weaken materially.

**This is the single largest design fork in the POC**, it is unresolved, and confirming it is the
**first** onboarding ask. It is recorded here rather than only in the risk table because it is the
premise the other eight entries rest on.
