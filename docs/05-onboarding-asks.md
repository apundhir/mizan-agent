# Onboarding asks

**Version 1.0 · 15 September 2026**

This is not a list of things that were denied. Every definition this POC runs on was chosen so the
work could proceed without waiting on the regulator's decision, and [the assumption
register](02-assumption-register.md) is the record of exactly which choices those were. This
document turns that record into the opposite of a complaint: the agenda a reviewer would work
through to move from a demonstration to a pilot, in the order that makes each later item answerable.

Read [the walkthrough](06-walkthrough.md) first if you have not. It says what the three demo
scenes prove and what they do not, and this list is the second half of that same honesty: the
things that would have to be true, and are not yet, for a real verification to be signed off by a
regulator rather than argued about after the fact.

## The floor

Six asks came out of the sprint that built this demonstration. Nothing below narrows this table;
everything after it adds detail underneath one row.

| Ask | What it unlocks | What stays unproven without it |
|---|---|---|
| Confirm the PMS PDF exports contain reservation-level rows | Everything in the metric library and the corpus design | Recomputation, and therefore the whole business case |
| Ratify the six definitional rules and the tolerance policy | Every number the system produces becomes defensible | Findings on real data are arguable |
| Name an owner for definitions and policy | Disputes get arbitrated rather than reopened | Nothing in the metric definitions is defensible |
| Release two or three real PDF exports, redacted | Extraction against a real layout | Whether extraction survives a real PMS export |
| Measure the manual verification baseline | The time-saving claim in the business case | No time saving can be quoted |
| Confirm hosting region and the reporting quarter basis | Deployment and corpus periods | Minor rework if either differs |

## Ask 1: confirm the PDFs carry reservation rows, not summaries

Every clause in `docs/01-definitions.md` assumes the PMS export a hotel would send is
reservation-level: one row per booking, with dates, room counts and a nationality field a parser
can read. If a real PMS export instead prints pre-aggregated monthly summaries, room-nights and
guest counts cannot be recomputed from it, and the whole approach narrows to comparing one
summary against another. That is the single largest design fork in this POC, and it is unresolved.
Nothing downstream of it, including the other five asks, is worth doing until this one has an
answer, which is why it is first rather than a footnote.

## Ask 2: ratify the six definitional rules

`docs/02-assumption-register.md` records eight assumptions. Two of them are not really open
questions: A-01 (cancellations and no-shows excluded) is near-universal hospitality practice, and
A-07 (one nationality per reservation) cannot be changed by a decision at all, because a
reservation-level export carries one nationality per booking and no more. The six that remain are
genuinely arguable, and each is a config change and a re-run away from a different answer, not a
code change:

| Ask | What it unlocks | What stays unproven without it |
|---|---|---|
| **A-02**: is a complimentary room a guest, is a house-use room excluded | Occupancy and guest counts move by roughly 1 to 2 percentage points either way; ratifying which side is correct closes that range | Whether a destination-fee use case would want `COMP` excluded too |
| **A-03**: do day-use guests count in the nationality table with zero room-nights | Affects about 0.8% of the synthetic corpus; small in aggregate, exactly large enough to explain one unexplained monthly variance | Whether a hotel's own reporting counts day-use guests at all |
| **A-04**: occupancy by occupied night, nationality by arrival month | The single most likely source of live definitional variance; ratifying it either confirms the system's mechanical detection of a month-basis mismatch or heads it off before a real hotel disputes a finding | Whether a real hotel apportions a month-spanning stay the same way this system does |
| **A-05**: are children counted in guest totals | Guest totals in a family-heavy leisure market move materially; already testable both ways without regenerating the corpus | Whether the regulator's destination reporting has its own convention here |
| **A-06**: does "guests by nationality" mean people, arrivals or room-nights | The broadest of the six: it does not shift a number, it changes what the number *is*. Flagged lowest confidence in the register because a hotel transcribing its own PMS report's column header may not mean what this project assumed | Every nationality finding is wrong in the same direction if this is wrong, which would look like a systematic hotel error and would not be one |
| **A-08**: are out-of-order rooms excluded from the occupancy denominator | Occupancy moves whenever rooms are out of order; ratifying this settles whether the metric measures commercial performance or asset utilisation, which are different questions with different right answers | Whether the regulator's interest in the number is commercial or operational |

A-01 and A-07 are worth naming even though neither is a ratification ask. A-01 should simply be
confirmed as expected rather than debated. A-07 is a hard limit on what the nationality figure can
ever mean from a reservation-level export: a family of four booked by one member shows as four
guests of that member's nationality, and no amount of engineering fixes that without a different
input file, which is its own conversation about personal data.

## Ask 3: name an owner for these definitions

The most consequential gap in the assumption register is not one of the eight entries. It is that
nobody is named as the owner of any of them. Six genuinely arguable rules with no named owner get
re-litigated every time a hotel disputes a finding, and the system's authority erodes one
conversation at a time. This is the cheapest ask on this list and it unblocks every other one:
Ask 2 has nothing to ratify against if nobody is authorised to ratify it.

## Ask 4: release two or three real PDF exports, redacted

The demo corpus proves the pipeline's mechanics: the positional PDF parser, the totals
reconciliation, the workbook mapping, the tolerance and permutation logic all run end to end and
agree with a known-correct answer, because this project built both sides of the comparison from
one ledger. It cannot prove the parser survives a real PMS's column layout, encoding or label
vocabulary, because it has never been asked to. A handful of real exports, redacted of guest
names, would let the parser be tested against something this project did not build, which is the
only way this specific gap closes.

## Ask 5: measure the manual verification baseline

No time-saving figure appears anywhere in this repository or the walkthrough, on purpose. A number
used before it is measured gets challenged, and the challenge lands on the whole result rather
than on the number. Measuring how long a verification officer currently takes to check one
quarter by hand is a small, bounded piece of work, and until it happens the business case has no
number to quote.

## Ask 6: confirm hosting region and the reporting quarter basis

The lowest-stakes ask on this list, and it is here for completeness rather than urgency. This
build runs against the Anthropic Console API rather than the Bedrock region the original PRD
named, because it is a personal reference build rather than a production deployment; the provider is an
interface specifically so the pinned-region question stays a config change rather than a
rebuild. Confirming the region, and confirming that a calendar quarter in the property's local
time is the reporting basis the regulator actually uses, is minor rework either way, not a redesign.

## The prize beyond verification

Everything above is about trusting a number the system checks. There is a second, larger
opportunity sitting one step past it, and it is named here, last, on purpose.

If the agent can recompute the aggregations from the PDFs, the hotel-authored Excel becomes
redundant. the regulator could ingest the PDFs and generate the return itself, removing a manual step at
every participating hotel and the transcription errors that step creates. That is not a bigger
version of verification; it is a different product, one that changes a hotel's reporting
obligation rather than checking it.

The sequencing is deliberate, not cosmetic. Verification helps the regulator's team and threatens nobody
at a participating hotel. Generation changes a hotel's process and its obligation, and raising
that possibility before verification has even been trusted would ask the regulator to buy the larger, riskier
idea on the strength of a demonstration that has not yet been adopted for the smaller one. The
prize is worth naming so the investment is judged on its full value, not worth leading with, and
worth asking for only once the verification result is trusted on its own merits, not just this
demonstration.
