# ADR-0006 · Redact rather than refuse, record runtime rather than target it

- **Status:** Accepted
- **Date:** 2026-09-14
- **Related:** [ADR-0001](0001-deterministic-core-agentic-edges.md) · [ADR-0002](0002-model-layer.md) · [ADR-0005](0005-sequential-graph-and-deferred-resilience.md)
- **Issues:** observability

## Context

An agentic system you cannot inspect is an agentic system you cannot defend. The pipeline ran
end to end after the orchestrated graph and left almost nothing behind: a verdict, and whatever scrolled past in
the terminal. The question an officer asks the day a hotel disputes a finding — *why did it say
that?* — had no artifact to answer it.

Three decisions inside that story were not obvious, and each had a plausible opposite.

**Personal data can reach an artifact, and it comes from outside this repository.** observability asked
for a test scanning the trace for name-shaped content from the corpus. The corpus has no names in
it: `tools/datagen/ledger.py` emits a salted `guest_ref` and says why, in M2. So that test would
have passed vacuously — the worst kind of green.

The exposure is real but elsewhere, and it is demonstrable rather than hypothetical.
`tda.excel.tools.digest` copies **every label cell** of the submitted workbook into the mapping
agent's prompt verbatim; its redaction is semantic and suppresses only cells that read as
*quantities*, because the agent needs the labels to do its job. A property that types

```
A9: Prepared by Ms. Jane Doe, jane.doe@hotel.ae, +971 50 123 4567
```

into a header cell has put that string in the prompt, therefore in the cassette's
`request_canonical`, and it can re-emerge through a free-prose contract field such as
`UnmappedBlock.reason` into `trace.jsonl`. No amount of care inside this repository prevents it:
the text comes from a file somebody else wrote.

**A trace viewer has to decide what it is willing to print.** `FindingNarrative.sentence` is
model-written prose about a named property's numbers. A viewer that pretty-printed contracts would
put it on a terminal, then in a screenshot, then in a ticket.

**Runtime is the number everybody wants and nobody has measured.** A POC that reports "a run takes
6 seconds" beside a claim about saving verification officers time invites exactly one question, and
the answer today is that nobody has timed the manual process.

## Decision

### 1 · Redact before writing, and record what was removed

Artifacts are redacted **on write**, not on read and not after. There is no unredacted copy: the
text is replaced by `[redacted:<kind>]` before `write_run` touches the disk, and `mizan trace`
reads the same redacted bytes everyone else does.

The obvious alternative — refuse to write an artifact containing personal data — is wrong. The
trace is the evidence that answers *"why did it say that?"*, and withholding it because one header
cell had a phone number in it **destroys the record in order to protect it**.

So the content goes and **the fact of it stays**: `run.json` carries `redactions` as kind and
count, never content. A reader learns something was removed and can ask the submitting property
about it, which is the correct next step and is not available if the artifact is simply missing. A
ledger field listing the phone numbers it redacted would be a ledger that leaks them.

The patterns are deliberately narrow — an `@`, an international dialling prefix, an honorific, a
`SURNAME/FORENAME` pair. Every one is a **structural** signal rather than "looks like a name",
because a pattern matching capitalised word pairs fires on `Room Nights`, `Rate Revenue` and half
of every workbook in the corpus, and a guard that cries wolf on correct data is a guard somebody
switches off. `tools/guard/secret_guard.py` makes the same argument about its own regexes.

### 2 · The tree prints short structural values and counts everything else

`mizan trace` shows a contract's scalar fields when they are shorter than 48 characters and elides
anything longer to `…(N chars)`. A metric code, an A1 range, a sheet name and an ISO country all
print. A narrative sentence does not.

The threshold is not about terminal width. It is the line between a value a reader needs in order
to follow the run and free prose that belongs in the verdict — which is the artifact meant to carry
it, and the one a human signs behind.

### 3 · Runtime is recorded, never targeted

`duration_ms` is written into the ledger and printed at the end of `make run`, labelled *recorded,
not targeted*. **There is no threshold, no budget and no assertion anywhere in this repository on
how long a run takes**, and there will not be one until somebody measures the manual baseline.

A number used before it is measured gets challenged, and the challenge lands on the whole result
rather than on the number.

### 4 · Every channel is redacted, and every run writes its artifacts

Not only the files. The console prints the ledger **as written** rather than the one in memory, and
the failure handler redacts the exception text and the node log before they reach stderr — a
pydantic error quotes the value it rejected, and a terminal is what gets screenshotted into a
ticket.

And a run that dies writes its artifacts too, under the id it was running as, with status `FAILED`.
That status is the one value `VerdictStatus` cannot express and should not: a verdict is a
statement about a submission, and a run that died made no statement. Writing only on success would
mean the trace exists for every run except the ones somebody needs it for.

### 5 · Calls are attributed to nodes arithmetically, not by a field on the record

Nothing in a `TraceRecord` names the node it ran under. Adding one was the obvious fix and was
rejected: a record of a model call has no business knowing about the pipeline that made it, and the
same reasoning keeps `tda.obs` from importing `tda.graph` at all — observability describes a run,
it does not depend on the thing being run.

Instead `NodeTiming.model_calls` says how many calls each node made, `TraceLog` preserves call
order, and the graph is strictly sequential (ADR-0005). Consuming the trace in order, `model_calls`
at a time, assigns every record to exactly one node, exactly.

That count has to include the calls that *failed*, and originally it did not — `AgentRunner` wrote
the trace record for a failure and left the usage ledger alone. One uncounted failure shifted every
later record onto the wrong node, and nothing on the page looked wrong. A failed call is still a
call; it is recorded with zero tokens, which is the honest figure when no request reached the model.

## Enforcement

| Decision | What holds it in place |
|---|---|
| Redaction happens on write | `write_run` is the only writer, and it redacts before `write_text`. `test_personal_data_is_redacted_before_it_is_written_and_the_count_is_kept` reads the bytes back off disk. |
| The exposure is real | `test_a_contact_detail_in_a_header_cell_really_does_reach_the_mapping_prompt` asserts `digest()` copies it. If that stops being true, the test fails and somebody finds out here rather than from a leak. |
| The patterns stay narrow | A parametrised test over ordinary workbook text (`Room Nights`, `RN/Mo`, `2026-01-31`, …) asserts none of them fire. |
| Prose never reaches a line | `test_free_model_prose_never_reaches_a_line_of_the_tree` asserts a narrative sentence's *content* is absent and its length is shown. |
| Runtime has no target | `grep` finds no threshold; the phrase "recorded, not targeted" is printed by `mizan run` itself. |
| Attribution stays honest | A leftover record is **reported in the output**, not dropped — which is what a concurrent graph will look like from here. |
| Repro exclusions cannot drift | `RunLedger.volatile_fields()` is read off the field descriptions, following the precedent `Verdict.decided_at` set in M1 — **nested models included**, as dotted paths. The first version stopped at the top level, missed `nodes.duration_ms` and `usage.duration_ms`, and would have failed the repro diff on every run. A test marks a field on a throwaway model to prove the answer is derived rather than remembered. |
| The console cannot leak what the file does not | `mizan run` prints `WrittenRun.as_written`, parsed back from the redacted bytes. There is no code path that prints the in-memory ledger. |

## Consequences

### Accepted costs

- **A bare name is not caught.** `A9: Jane Doe` passes. Nothing structural distinguishes it from
  `A9: Deluxe King`, and a rule catching the first flags the second on every run. This is asserted
  in a test rather than left implied, so the limit cannot be quietly forgotten or quietly claimed
  as closed. What covers it today is the corpus design — there are no names to leak — and, for a
  real pilot, the pseudonymisation boundary the build plan defers until a real-file pilot is agreed.
- **Redaction is lossy and irreversible.** A workbook label legitimately containing an `@` would be
  redacted in the trace, and the original is not recoverable from the artifact. A viewer that could
  recover it would be a second place the data lives.
- **The tree cannot show what an agent was asked, only what it was asked *for*.** The prompt text
  is not in the trace — the cassette key ties a record to its recording, and that is the
  indirection. Showing the rendered prompt would mean storing every prompt twice.
- **Attribution is exact only while the graph is sequential.** The day a node runs two agents
  concurrently, a `node` field on the record becomes the right answer. Both directions of an
  imbalance are reported in the output — the dangerous one is a node claiming *more* calls than the
  trace has left, because it silently pushes every later node's calls one place up.
- **A failed call's token cost is lost.** When the model answered and the answer failed validation,
  the tokens were spent and are not in the ledger. Recording them would mean reaching into the
  provider's response from the failure path, and the failure path is the one place that must not
  acquire new ways to fail.
- **`titled_name` takes at most three words after the honorific.** `Dr. Jane Marie Doe` goes;
  a fourth given name would leave a word behind. Unbounded would eat `Dr. Smith Room Nights Q1`.

### What is bought

A run leaves `artifacts/<run_id>/` with three files: what it looked at and under which rules
(`run.json`, inputs by SHA-256), what each agent said (`trace.jsonl`), and what the pipeline did in
order and where it stopped (`nodes.jsonl`). `mizan trace` joins them for a human. Cost is printed
per agent with the rate card version that produced it, because prices are a configured input rather
than a fact.

### What this does not claim

Not a pseudonymisation boundary, not DLP, and not a guarantee about real guest data — the corpus is
synthetic and the patterns are narrow by choice. Not a performance measurement: the durations are
observations of a POC on one machine, and nothing here says what a verification *should* take.

## Alternatives considered

| Alternative | Why it lost |
|---|---|
| Refuse to write an artifact containing personal data | Destroys the record in order to protect it. The trace is the evidence, and a missing trace is not a safer trace. |
| Redact on read, keep the original on disk | Then the artifact is the leak and the viewer is decoration. |
| Broad "looks like a name" patterns | Fires on `Room Nights` and `Rate Revenue`. A guard that cries wolf on correct data gets switched off, and then nothing is guarded. |
| Scan the trace for corpus guest names, as observability wrote it | The corpus has no names; the test would pass vacuously. Reframed to the exposure that is demonstrable, with the reasoning recorded here and in `tda/obs/redact.py`. |
| A `node` field on `TraceRecord` | Makes a record of a model call depend on the pipeline that made it. The arithmetic join is exact for a sequential graph, and says so out loud when it stops being. |
| Put token counts on the agent contracts | They are integers, and `tools/guard/agent_schema_lint.py` forbids an integer on an `AgentOutput`. Weakening that guard to carry telemetry would trade the deterministic-core claim for a convenience. |
| Print a time-saving figure | Nobody has measured the manual baseline. A number used before it is measured gets challenged, and the challenge lands on the whole result. |
