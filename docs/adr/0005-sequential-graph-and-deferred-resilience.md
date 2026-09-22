# ADR-0005 · A sequential graph, and three pieces of resilience deliberately not built

- **Status:** Accepted
- **Date:** 2026-09-14
- **Related:** [ADR-0001](0001-deterministic-core-agentic-edges.md) · [ADR-0004](0004-agent-runtime.md)
- **Issues:** the orchestrated graph

## Context

Every piece of the verification existed and none of them had ever run together. Extraction was
tested against PDFs, the claim parser against a workbook, reconciliation against claims a test
handed it — each correct about inputs another test constructed, and no evidence that the three
agreed with each other about anything.

Wiring them up is the obvious part. The decision worth recording is what *not* to wire.

LangGraph offers three things that any production pipeline would eventually want, and all three
were on the table for this story:

- **`Send` fan-out** — extract the three monthly PDFs in parallel rather than in sequence.
- **A bounded retry ladder** — retry a node that failed transiently, up to some limit.
- **Checkpointed interrupt-and-resume** — persist state so a run that dies can be continued.

The pull towards building them is real, and it is mostly about how the system *looks*. A graph with
fan-out and retries reads as engineered; a five-node line reads as a script with extra steps. This
POC is partly a capability demonstration, so that pull deserves an answer rather than a shrug.

## Decision

**Sequential, no retries, no checkpointing. A submission that halts simply halts.**

### 1 · The nodes run in order, one at a time

`intake → extract → claim_parse → recompute_reconcile → publish`, with one conditional edge after
each: go to the next node, or go straight to `publish` if the run is already over.

Every path reaches `publish`, including a rejected one. A rejected run still produces a verdict —
`REJECTED`, carrying its reason code — because the officer's question after a failed run is "what
happened?", and that is answered by a verdict rather than by an absent one.

### 2 · No retry ladder

**A retry ladder that hides a transient extraction failure is worse than a halt.** Not slower,
not less elegant: *worse*, and the reason is the one thing this POC is asking to be trusted on.

If a run can silently survive a failure, then a clean run and a run that failed twice and succeeded
on the third attempt produce the same verdict, and nothing in the output distinguishes them. The
officer signing behind the number cannot tell which they are holding. Reproducibility — the claim
`make repro` exists to demonstrate — quietly stops meaning anything, because the thing being
repeated is no longer deterministic and no longer says so.

For a POC, honest beats resilient. A halt is legible: the node log names the last node entered, the
run exits non-zero, and somebody looks.

### 3 · No `Send` fan-out across the PDFs

Extraction of three monthly reports takes about five seconds in sequence. Parallelising it buys a
few seconds on a POC that nobody is running at volume, and costs the property that makes a failure
readable: with fan-out, three reports fail into one merged state and the node record describes the
aggregate rather than the file.

This is the cheapest of the three to add later and the least urgent.

### 4 · No checkpointed resume

Resume requires persisted state, and persisted state requires deciding what a *partially verified*
submission means — whether a resumed run may reuse extraction from before a policy change, what
happens when the files moved. Those are real questions and none of them are answered by writing a
checkpointer. Re-running takes seven seconds.

### 5 · What replaced them: a record of where the run stopped

Every node writes an **entry record and an exit record** (`tda.obs.nodes`). A node that halts
mid-way leaves an entry with no exit, and `NodeLog.unfinished()` names it. `mizan run` prints that
log on the failure path, so the question a halt raises is answered by the output of the command
that halted.

That is the honest substitute for resilience: not "the run survived", but "the run stopped here,
and here is what it had established by then".

## Enforcement

| Decision | Held in place by |
|---|---|
| sequential, five nodes | `SEQUENCE` in `tda.graph.build`; `test_every_node_runs_in_order_on_a_clean_submission` |
| a halt halts | `tda.graph.run.verify` catches nothing; `test_a_node_that_fails_still_writes_its_exit_record` |
| every path reaches publish | `_route_from`; `test_a_rejected_verdict_still_says_why` |
| the run says where it stopped | `NodeLog.unfinished()`; `test_an_entered_node_with_no_exit_names_where_the_run_stopped` |
| no retries creep in | there is no retry parameter to set; adding one is a diff against this ADR |

## Consequences

### Accepted costs

**A transient failure ruins a run.** A flaky read, a file locked by another process, and the whole
seven seconds is wasted. Accepted, because the alternative is a verdict that cannot say whether it
was clean.

**The graph is unimpressive.** Five nodes in a line is not the demo anybody hopes for from a
multi-agent system, and the agentic surface that *is* interesting lives in ADR-0004 rather than
here.

**Nothing resumes.** A run that dies at `recompute_reconcile` re-extracts from scratch.

**One acceptance criterion moved while it was being built.** the orchestrated graph's table says `extract` halts
with a blocking finding. A whole document that will not open cannot produce one: `Finding` requires
a citation on at least one side (D-EV-01), and a file that will not open has no page to cite —
inventing `page=1` would put a false citation in front of a reviewer, which `tda.contracts.refs`
warns against explicitly. So an unopenable report is an **intake rejection** (`UNREADABLE_FILE`,
declared in M1 and until now produced by nothing), and extraction deals only with files already
known to open. Row-level defects inside a readable document are still blocking findings and still
cite the page they failed on. The criterion is met; the boundary sits one node earlier than the
issue assumed.

### What is bought

The pipeline runs. Three PDFs, 1,200 reservation records, a workbook, 94 claims, 94 recomputed
values, joined on the canonical metric key — and **zero findings** on the committed corpus. That
claim existed for the reconciliation layer before this story; it now covers intake, extraction, the
claim parser and the metric library agreeing with one another, which is a different and much
stronger statement.

`RejectionReason` acquires its first producer. `METRIC_LIBRARY_VERSION` acquires its first reader.
`check_cover` becomes reachable for the first time, because the graph is the first caller that has
a declaration to check the workbook against.

### What this does not claim

That the pipeline is production-shaped. It is not, and the three things above are the three
reasons. That a halt is a good user experience — it is an honest one. That sequential is faster; it
is slower and that was not the trade.

## Alternatives considered

**Build the retry ladder anyway, and record retries in the verdict.** The steelman: resilience plus
honesty, since the verdict would say "extraction attempt 3 of 3". Rejected because it puts a number
in the verdict that nobody has decided how to read — is a two-retry run acceptable to sign behind?
— and this POC has no answer to that question yet. A rule nobody can state is a rule nobody can
enforce.

**Fan out across the PDFs and keep everything else sequential.** The cheapest of the three, and
still rejected for this story: the whole point of the node records is that a failure names a file,
and merged parallel state names an aggregate. Worth revisiting once the run is slow enough to care.

**Skip LangGraph and write a five-function pipeline.** Genuinely tempting — the graph adds a
dependency and one real trap (it resolves the state schema's annotations at runtime, so a
`TYPE_CHECKING`-only import turns `StateGraph(RunState)` into a `NameError`). Rejected because the
PRD names LangGraph and because the conditional-edge routing is the thing that will carry fan-out
and retries when they are built. A plain pipeline would have to be rewritten to get there.

**A model supervisor routing the nodes.** Already rejected in ADR-0004, for the same reason it is
rejected here: the route is fixed, and a model in that seat adds a non-deterministic branch where
nothing downstream could detect a wrong turn.
