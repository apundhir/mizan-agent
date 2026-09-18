# ADR-0009 · The reviewer's assistant cites or declines, and computes nothing

- **Status:** Accepted
- **Date:** 2026-09-15
- **Related:** [ADR-0001](0001-deterministic-core-agentic-edges.md) · [ADR-0004](0004-agent-runtime.md) · [ADR-0008](0008-the-review-gate-is-headless-and-the-evidence-is-cropped.md)
- **Issues:** PRD-93

## Context

ADR-0008 built the review screen around a stopwatch: an officer must be able to judge one finding
without opening the PDF in another window. It closed the gap for the questions a *card* can answer —
what was claimed, what the records support, which rows, which cell.

It does not close the gap for the questions an officer actually asks out loud. *Which rule makes
this definitional rather than an error? How many of these are the hotel's own mistakes? Where did
that figure come from?* Today those have two answers: open the PDF, or ask a colleague. Both are the
failure the stopwatch measures, arriving a minute later.

An assistant that answers them is obvious. It is also the single most dangerous component this
system could grow, and for a reason specific to where it sits:

**Everywhere else in this repository, a model's output is checked by something.** A mapping is
validated against the workbook's geometry. A narrative sits beside figures the officer can compare
it to. A resolution is a suggestion attached to a blocking finding a human must clear. Here, the
model's output *is* what the officer is checking with. There is nothing behind it, and a confident
wrong answer is indistinguishable from a right one until somebody goes and looks — which is the work
the assistant exists to save them.

Three specific failures follow from that position:

**Fluent prose with nothing behind it.** *"Always cite your sources"* is advice a model follows most
of the time. On this surface, most of the time is not good enough.

**A citation that leads nowhere.** `pms_2026-01.pdf p.9 rows 1-4` is trivial for a model to write.
The file exists, the page exists, and the rows have nothing to do with the finding. An officer who
follows it finds *a* page and concludes the tool is approximately right.

**A number.** The officer is reading the answer on the same screen as figures recomputed from
reservation records. A figure in prose is indistinguishable from one of them, and if the two ever
disagree, every number on the page becomes suspect.

## Decision

### 1 · An uncited answer is not a weaker answer; it is unconstructible

`Answered.citations` has `min_length=1`. A model that returns prose with nothing behind it fails
schema validation in the provider, and `tda.review.assist` renders *"it cannot tell you anything it
cannot show you"* rather than the prose.

Not a check after the fact, because a check can be skipped, moved, or made a warning by somebody in
a hurry. The contract is the narrowest possible place to put this, and it is the only place where
"an answer without a citation" stops being a thing that can exist in the program at all.

A citation is a **pointer**, never a quotation the agent composed: rows of a report, a cell of the
workbook, rows of the inventory reference, or a clause of the definitions. A quotation is a thing a
model can write; a pointer is a thing a reader can follow.

### 2 · Every citation is checked against the run before the answer is shown

`check_citations` refuses a `PdfRef` no finding carries, an `ExcelRef` no finding carries, an
`InventoryRef` no finding carries, a clause the definitions do not define, and a clause cited under
a policy version this verdict was not produced under.

This is the same check `resolve_label` applies to a matched candidate, for the same reason: code
bounds the space of acceptable answers, and anything outside it was invented. It raises rather than
downgrading to a decline — an abstention is a considered refusal, and quietly relabelling a
fabrication as one would put the two in the same bucket in every eval and every trace that follows.

On the screen, a fabricated citation withholds the prose entirely. A warning beside an answer the
officer can still read is a warning nobody acts on, and it is the wrong reading of the event: an
assistant that invented a reference once has told the officer something about every other answer it
has given today.

### 3 · Three read-only tools, no arithmetic, no file access — and they are actually called

`query_verdict`, `get_evidence`, `get_policy_clause`, bound to one verdict. That is the agent's
entire world. It cannot open the submission, cannot re-derive a figure, and cannot reach a document
the run did not already read.

`ModelRequest` carries text, not tool calls (see ADR-0004 and `tda.excel.tools`), so the tools are
called by **code** in `gather()` and their output is rendered into the message, as the mapping and
narrative agents' are. This is worth stating in an ADR because the first implementation did not do
it: it built a tool session and never called through it, which handed the model three tool
*descriptions* and no verdict. Every honest answer it could then give would cite something it had
to invent, §2's check would correctly refuse it, and the officer would read *"the assistant cited
evidence this run does not contain"* on every question — a fabrication check that had inverted into
a fabrication generator while looking exactly like a working one.

The tools return classifications, periods, clauses, causes and references, and **not** `claimed`,
`computed` or `difference` — the same omission `tda.agents.narrative` makes and for the same reason.

**Nor any count.** `claims_checked` and `hotel_error_count` are metric-function outputs that do not
look like figures in a listing, and a model shown `hotel_errors: 1` will restate it. So the
listings name findings by id and carry no totals, and the agent answers *which* rather than *how
many*. The officer has the counts on screen already.

The cost is real and worth writing down: **the assistant genuinely cannot answer "by how much?"**
It is expected to say so and point at the finding that holds the figure. We accept that, because the
officer already has the number on screen. What they lack is which rule made it a finding and where
each side came from, and that is what this agent supplies.

### 4 · Declining is a typed outcome, and the eval set requires it

`CitedAnswer.answer` is `Answered | Declined`, discriminated. Three of the seven committed eval
cases cannot be answered from the verdict — a period the run did not cover, a figure that would have
to be recomputed, and a question about a person's intent — and the agent must decline all three.

An assistant that answers everything is indistinguishable from one that makes things up. Building
the ability to refuse into the contract, and then requiring it in almost half the eval set, is how
that stays true after the prompt is next edited.

### 5 · The question and its answer are appended to the run's trace

Question, citations and tokens, in the `TraceRecord` the runtime already writes, appended to the
run's own `trace.jsonl` beside the calls that produced the verdict — and **redacted** on the way,
through the same function `write_run` uses. An officer's question is free text a human typed, which
makes the question box the only path by which such text enters an artifact after the run has
finished.

Be exact about what the redaction buys. `redact` matches structure — an email address, a phone
number, a booking reference — and its own docstring is clear that it does not catch a bare name. A
guest's name typed into the box reaches the trace verbatim, and a test asserts that it does, so the
limit is recorded rather than implied away. The question also reaches the provider unredacted,
because the agent has to read it.

`mizan trace` renders these under an **after the run** heading rather than counting them as calls
it could not attribute to a node. They have no node and never will — the agent `runs_in="review"` —
and a viewer that fired its mis-attribution warning on every reviewed run would teach readers to
ignore the one line that catches real mis-attribution.

Failed questions are recorded too. A trace carrying only the questions that were answered cannot
show that the assistant was asked something it could not do, which is the half an auditor reading a
signed verdict most wants.

The consequence is that **a question changes the trace**: a run reviewed twice has a longer trace
than a run reviewed once. That is correct. The questions asked about a verdict are part of how it
came to be signed.

### 6 · The ask path is headless, as the recorder is

`tda.review.assist.put_question` takes paths, strings and a provider, and returns `Answer` or
`NoAnswer` — never raises, including when `docs/01-definitions.md` is missing, which it is beside
an installed wheel. `tda.review.app` renders what it returns. ADR-0008's argument applies
unchanged: everything that can go wrong lives somewhere a test can reach without a browser, and the
ways an answer can fail to arrive are distinguishable values rather than one exception.

The screen holds the last answer in `st.session_state`, not `st.cache_data`. The cache is
process-global: two officers asking the same question on one run would have shared a single call,
and the second officer's question would have been appended to no trace at all — losing exactly the
record §5 says the questions are.

## Consequences

**Good.** The one component with no check behind it has three, and all three are wiring rather than
instruction. A fabricated citation is caught by the run's own contents rather than by the officer's
diligence. The assistant's cost is printed under every answer. The refusal path is exercised by
almost half the eval set rather than assumed.

**Bad.** The assistant is less useful than a naive one. It will decline questions a less careful
tool would answer, and an officer meeting three declines in a row may stop asking. We think a tool
that is trusted and narrow beats one that is broad and second-guessed, but the reverse argument is
coherent and this ADR is where to reopen it.

**Also bad, and still true in practice.** Replay answers the questions it holds recordings for and
nothing else. The eight eval-case questions are recorded; anything an officer actually types misses,
so the box says so and answers nothing. That is honest and it is not useful. What closes it is a key
at review time, or a recording set far wider than an eval set has any business being.

**Watch for.** The decline rate. A prompt edit that makes the agent decline *less* is a prompt edit
that has traded the property this ADR is about for apparent helpfulness, and the eval set is where
that shows up first.
