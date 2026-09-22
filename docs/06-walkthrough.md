# Walkthrough — what was proven, and what was not

**Version 1.0 · 15 September 2026**

This is the record of `make demo`: three real runs of the actual pipeline, and what each one is
entitled to claim. It carries the same weight in both directions. A capability that works is
stated as plainly as a limit that has not been closed.

**This project authored both sides of the demo corpus.** The PDFs and the workbook both come from
one ledger (`tools/datagen/`). Extraction against that corpus proves the pipeline works. It does
**not** prove the pipeline survives a real property management system's actual export
formatting, because nothing in this corpus has ever met a real PMS's column drift, encoding
quirks or inconsistent labelling. That gap is real, it is open, and [the onboarding
asks](05-onboarding-asks.md) name what closes it.

---

## The three scenes

`make demo` runs `tools/demo/run_demo.py` against the real graph (`tda.graph.verify_directory`),
in replay mode, with no `ANTHROPIC_API_KEY` set anywhere. Nothing about the pipeline is mocked or
shortened for the demo. The only thing that differs from `mizan run` is which submission directory
it points at.

### Scene 1, a clean quarter

The unmutated demo corpus. Reservation-level records are extracted from three months of PDFs,
every metric is recomputed in code, and the result is compared against the hotel's workbook cell
by cell. The verdict is **PASS**, zero findings.

This is the scene to open with because a system that finds nothing on a clean input is not a
weaker demonstration than one that finds an error. It is the harder property to earn, and the one
a reviewer checks first. A tool that reports something on every run teaches its reviewer to skim,
and skimming is exactly the failure mode a real finding needs its reader not to have.

### Scene 2, a mistyped guest count

A hand-keyed transposition in one nationality cell: the commonest real error a verification
system meets, someone typed `38` for `83`. The verdict is **FAIL**, with two findings: the
mistyped cell, and the quarter roll-up that sums it. A spreadsheet adds its own column up, so a
system that only caught the cell that was typed would leave the roll-up standing uncorrected. Each
finding carries a proposed correction and a citation back to the PDF rows and the Excel cell it
disagrees with.

Two fixtures were available for this scene beyond the control used in Scene 1. The occupancy
denominator error is the more architecturally interesting of the two, a policy disagreement
escalated rather than filed against the hotel, but it needs a sentence of setup about permutations
before an audience can follow why the system is not accusing anyone. The transposition needs no
setup: a number is wrong, the system says which one and what it should be, and a reviewer can
check the arithmetic in their head. Fixtures are used internally for both scenes; neither is
presented as one, per the issue's own instruction. A demo built on a visibly planted error invites
the one challenge it cannot answer, "of course it found it, you put it there," and that challenge
is answered by not raising the subject, not by a rebuttal.

### Scene 3, a nationality label nobody taught the system

A reservation whose nationality field carries a code the committed ISO lookup has never seen.
Extraction raises a blocking finding (D-NAT-12) before the claim parser or the mapping agent is
ever invoked, so this scene makes **zero model calls**. The verdict is **HALTED**.

This is the refusal the issue's acceptance criteria ask for: the system declining to assert a
finding it cannot evidence, and escalating instead. It is built without a model in the loop at
all, a stronger claim than the issue asked for. `src/tda/graph/nodes.py`'s `_skip` guard means a
blocking finding raised at `extract` routes the run straight to `publish`, and `claim_parse_node`
(the only node that calls a model) never runs. That is confirmed by asserting the run's trace
carries zero records, not by the absence of an error message.

The scene is a fourth corpus variant, distinct from the F4 to F6 fixtures already specified (those
are Excel-side and need a model recording nobody has taken yet). It is built at runtime by
`tools/demo/run_demo.py`: one reservation, chosen by rule from the demo corpus's own committed
ledger, has its nationality field replaced with a code that resolves nowhere, and January's report
is re-rendered from that mutated ledger with the same generator the committed corpus was built
from. It is never committed to `corpus/`, because it exists to demonstrate D-NAT-12, not to be
scored against it. There is no mutation spec and no derived expectation to keep in sync with it.

A single unreadable field taints every total that depended on it, and the system says so more than
once. The mutated row fails to parse, so it drops out of the month's record set; the report's own
totals block, rendered from the full ledger including that row, still counts it. Row count, the
printed room-night sum, the qualifying room-night sum and the nationality breakdown each disagree
independently as a result, so the run halts with several blocking findings rather than one. That is
not noise. It is defense in depth doing what it is there for: five checks exist because losing a
row and misreading a column are different failures, and this scene happens to trip more than one
of them at once.

---

## What the deterministic core actually guarantees

Three mechanisms, checked in CI on every push, not asserted in a document.

**The import guard** (`tools/guard/import_guard.py`) proves, by parsing the AST of every file
under `src/tda/metrics/` and `src/tda/reconcile/`, that neither package imports a model client or
does filesystem I/O, directly or transitively. `tests/arch/` proves the guard itself works by
feeding it code that deliberately violates each rule and checking that it is rejected.

**The reconciliation engine** (`src/tda/reconcile/engine.py`) turns a join between a claim and a
computed value into a `Finding`. Classification runs a fixed, committed order (D-CLS) and never
consults a model, and definitional variances are kept in a separate array from clerical ones, so a
hotel-error count can never silently include a policy disagreement (D-MAT-06).

**The evidence-citation validator** is not a convention. It is enforced at construction.
`Finding` (`src/tda/contracts/variance.py`) refuses to build without both a source reference and
an Excel reference, and permits a typed absence (`NotReached`, never an empty string) on at most
one side, for the two cases the definitions name as legitimate (D-EV-01, D-EV-02, D-EV-05). A
finding with nothing behind it on either side cannot exist, because the type does not allow it.

## Where a model is in the loop, and what happens when it declines

Two agents can run inside a verification: mapping (which sheet and header block is which metric)
in the pipeline itself, and reviewer-assist and label resolution on the review screen. None of
them can write a number that reaches a metric. `tools/guard/agent_schema_lint.py` enforces the
same guarantee from the other side: no `AgentOutput` subclass may declare a numeric field beyond a
citation integer (`page`, `row`).

Abstention is a typed, first-class outcome, not an error path. The label-resolution agent's
`LabelResolution.answer` is `Resolved | Abstained`, and an abstention produces the same D-NAT-12
blocking finding a human-authored refusal would. The model proposes, a human disposes, and nothing
it returns is trusted until somebody accepts it. The reviewer-assist agent takes the same stance
one step further (ADR-0009): its tools cannot return a figure at all, so it can answer which rule
applied and where a number came from, and it must decline to say by how much. There is nothing
behind a model's own arithmetic on that screen to check it against, so the design keeps arithmetic
out of its reach entirely rather than trusting a citation to catch a wrong one.

## The corpus is synthetic, and that is a real limit, stated once and held to

Every PDF and every workbook cell in this demonstration was generated from one ledger this project
built. A
100% extraction rate against this corpus is real evidence that the pipeline's mechanics work: the
positional PDF parser, the totals reconciliation, the workbook mapping, the tolerance and
permutation logic all run end to end and agree with a known-correct answer. It is not evidence
that a real PMS export would extract cleanly. Real exports come from vendors this project has never
seen a page from, with column layouts, encodings and label vocabularies nobody has tested against.
The single largest open question in the whole design, whether a real PMS export even carries
reservation-level rows rather than pre-aggregated monthly summaries, is unresolved and is the first
onboarding ask for exactly this reason.

## The numbers, as they stand today

Verified by re-running rather than copied from an earlier tag.

- **890 unit and architecture tests pass.** (A prior figure of 875 was true when this document was
  drafted and stopped being true within the same session, because a concurrent, in-flight change
  on this branch added tests of its own. 890 is what a fresh run reported just now, and this
  document was updated to match rather than left to quote the older number.)
- The eval scorecard scores **3 of 6 fixtures** (F1, F2, F3): **100% recall** on every planted
  material error, **zero false positives** on the control fixture. F4, F5 and F6 are specified but
  not yet built. Each needs a model recording (`make record`, which needs `ANTHROPIC_API_KEY`)
  that has not been taken for those three cases.
- **`make ci` is green**, in 2 minutes 37 seconds on the machine this was written on. Earlier in
  the same session it was red, because a concurrent, in-flight change on this branch (the delivery pipeline,
  container packaging) failed `make types` on a file outside this issue's scope
  (`tests/unit/test_bundle.py`); that change was fixed before this document was finished, and the
  number above is the state it left behind rather than the state along the way. The two minutes
  quoted elsewhere in this project's history was true of a smaller test suite; 890 tests and the
  full corpus and fixture rebuild now take a little over that, which is recorded here rather than
  rounded down to match an older claim.
- No time-saving figure appears anywhere in this document, because the manual verification
  baseline has not been measured. It is the fifth onboarding ask.

## Limits, stated with the same directness as the strengths above

- **Whether this pipeline survives a real PMS export is unproven.** The positional parser is built
  and tested against a layout this project controls. A real export's column drift, encoding or
  label
  vocabulary could break it in ways this corpus cannot surface, because it was never asked to
  survive them.
- **Only three of six specified fixtures are scored.** The eval code's own report says this in
  words (`NOT_RECORDED`, never a silent skip), and this document repeats it rather than letting a
  partial number stand for a complete one.
- **The refusal scene is one deliberately planted case, not a sweep across many.** It proves the
  mechanism, halting before a model is consulted, works for the case it was built to demonstrate.
  It does not establish how many distinct label variants, encodings or malformed rows a real
  export could throw at the same code path, or whether every one of them halts as cleanly as this
  one does.
- **No hosting region, pilot timeline or manual baseline is decided.** Every one of those is an
  onboarding ask, not a gap in the engineering.
- **These are this project's own definitions, not a regulator's ratified ones.** Six of the eight
  entries in the assumption register are contestable, and nothing in this walkthrough should be
  read as any regulator having ratified them.

## Where to read next

| Question | File |
|---|---|
| Every assumption this POC rests on, and what it costs to change each one | [02-assumption-register.md](02-assumption-register.md) |
| The decisions the regulator would own to move from demonstration to pilot | [05-onboarding-asks.md](05-onboarding-asks.md) |
| The agent graph, the deterministic boundary, the trace format | [03-architecture.md](03-architecture.md) |
| Why the assistant cites or declines | [ADR-0009](adr/0009-the-assistant-cites-or-declines.md) |
| Every `make` target | [04-runbook.md](04-runbook.md) |
