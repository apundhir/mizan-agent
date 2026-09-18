# Architecture — the agent roster, the handoff rules, and the trace format

> The lesson this repository is meant to teach is one sentence: **agents at the edges, determinism
> in the core, contracts at every boundary.** Everything below is that sentence, made checkable.

| | |
|---|---|
| **Issues** | PRD-88 (agent runtime) · PRD-82 (model layer) · PRD-81 (contracts and guards) |
| **Decisions** | [ADR-0001](adr/0001-deterministic-core-agentic-edges.md) · [ADR-0002](adr/0002-model-layer.md) · [ADR-0004](adr/0004-agent-runtime.md) · [ADR-0006](adr/0006-observability-redaction-and-recorded-runtime.md) |
| **Also here** | the graph's five nodes and their wiring (§8, PRD-89) and what a run leaves behind (§9, PRD-90) |

---

## 1 · The boundary, first

Everything else in this document is subordinate to this diagram, so it comes first.

```
                        the agentic edge                    the deterministic core
      ┌──────────────────────────────────────────┐   ┌──────────────────────────────────┐
      │  mapping · resolution · narrative        │   │  tda.metrics    PURE functions   │
      │  reviewer-assist · critic                │   │  tda.reconcile  joins, tolerance │
      │                                          │   │                                  │
      │  reads layouts, labels, language         │   │  performs every calculation      │
      │  returns keys and references             │   │  accepts only typed records      │
      └──────────────────────────────────────────┘   └──────────────────────────────────┘
                         │                                          ▲
                         │   keys · references · codes · ranges     │
                         └──────────────────────────────────────────┘
                                    never a number
                    ▲                                          ▲
      tools/guard/import_guard.py                tools/guard/agent_schema_lint.py
      neither core package may import            no AgentOutput may declare a numeric
      tda.agents or any model client             field beyond page / row / row_start / row_end
```

Both arrows are CI failures, not conventions. `tests/arch/` proves the guards work by deliberately
violating them — a guard nobody has seen fail is a guard nobody knows the state of.

---

## 2 · What an agent is

**Five things, and nothing else.** Anything with fewer is a prompt with ambitions.

| # | The thing | Where it lives | What is lost without it |
|---|---|---|---|
| 1 | a **versioned prompt** | `src/tda/agents/prompts/<agent>/v<n>.md` | nobody can say which instructions produced a number |
| 2 | a **typed output contract** | a subclass of `AgentOutput` | free text reaches the numeric path |
| 3 | a **narrow tool allowlist** | `src/tda/agents/roster.py` | nobody can answer "what can it reach?" |
| 4 | an **eval set** | `tests/eval/agents/cases/<agent>/` | "it works" is a sentence somebody typed once |
| 5 | a **trace record** | `tda.obs.TraceRecord` | nobody can answer "why did it say that?" |

Four are required arguments to `AgentSpec` with no defaults, so an agent missing one does not
type-check. The fifth is asserted by `tests/eval/agents/test_agent_evals.py`.

---

## 3 · The roster

Declared as data in `src/tda/agents/roster.py`, in one table, so the question *what can this agent
reach?* has one place to look rather than every call site.

| Agent | Job | Output contract | Tool allowlist | Effort | Runs in |
|---|---|---|---|---|---|
| **Supervisor** | routes work, owns the budget, records every decision | `RoutingDecision` | — *(code, no prompt, no model)* | — | the graph |
| **Mapping** | which sheet and header block is which metric and axis | `WorkbookMapping` | `list_sheets`, `peek_headers` | low | `claim_parse` |
| **Resolution** | resolve a label variant to a canonical code, **or abstain** | `LabelResolution` | `iso_lookup`, `fuzzy_candidates` | low | `extract`, `claim_parse` |
| **Narrative** | write the sentence that explains one finding | `FindingNarrative` | `read_finding`, `read_evidence` | high | `publish` |
| **Reviewer-assist** | answer an officer's question, with citations | `CitedAnswer` | `query_verdict`, `get_evidence`, `get_policy_clause` | high | the review screen |
| **Critic** | grade narratives; catch ungrounded prose | `CriticVerdict` | **none** | high | `eval` |

Four entries are worth reading twice.

**The supervisor has no prompt and no contract**, because it is code. See ADR-0004 §2.

**The critic has no tools**, and the emptiness is deliberate. An agent that can fetch its own
evidence can find something that makes an ungrounded sentence look grounded, and it will, because
that is what looking for supporting evidence does.

**The mapping agent's two tools cannot return a value.** Not "are instructed not to" — `tda.excel.
tools` has no code path that emits a cell's contents. The allowlist is the second lock on that
door; redaction is the first.

**Reviewer-assist has three tools and none of them computes.** It is the only agent whose output an
officer reads *as an answer* rather than as a field in a document, and therefore the only one with
nothing behind it to check it against. So its answer cannot be constructed without a citation
(`Answered.citations` is `min_length=1`), every citation is checked against the run before the
answer is shown, and the tools withhold the figures exactly as the narrative agent's do. It answers
*which rule* and *where from*; it declines *by how much*. See ADR-0009.

### Effort, and where it comes from

`low` for mapping and resolution: classification tasks with a small answer space, checked against a
closed vocabulary afterwards anyway. `high` for narrative, reviewer-assist and critic: writing and
judgement, where quality is visible to the officer.

Effort and prompt version come from `policy.yaml`, so a verification's cost profile is legible in
the same file as its rules. **The tool allowlist does not**, and the split is the point: effort is
a tuning decision, an allowlist is a boundary, and a boundary editable from a YAML file that ships
alongside the data it governs is not one.

---

## 4 · The handoff rules

### Rule 1 — agents pass keys and references, never numbers

| Agent | Hands on | Never hands on |
|---|---|---|
| Mapping | `Nationality!D4:D26`, `guests_by_nationality` | the values in those cells |
| Resolution | `CI`, `matched_candidate` | a guest count |
| Narrative | `F-0007`, `P-COMP-EXCLUDED` | the claimed or computed figure |
| Critic | four booleans | a score |

Enforced by `tools/guard/agent_schema_lint.py`: no `AgentOutput` subclass may declare a numeric
field. The whitelist is `page`, `row`, `row_start`, `row_end` — citations, not quantities. Nothing
downstream does arithmetic on a page number.

Booleans are fine. `refused: bool`, `is_definitional: bool` are classifications, and classification
is what agents are for.

### Rule 2 — the output is validated before it is believed, and checked before it is used

Two distinct stages, in two distinct places:

1. **The provider validates the contract.** A response that is not the declared type raises
   `OutputValidationError`. There is no fall-back to free text.
2. **The caller checks the answer against reality.** A `matched_candidate` that was never offered
   is a fabrication; a `finding_id` the agent was not asked about is a misroute; a
   `cites_permutation` the classifier did not assign is a plausible sentence about a cause nobody
   found.

The second is the caller's job, never the runner's. A module that both makes a call and decides
what to believe about the answer is a module where the second half softens under pressure from the
first.

### Rule 3 — an agent reaches only through its session

`ToolRegistry.session(agent, allowlist)` is the only route to a tool. The allowlist is checked at
the call, the call is recorded whether allowed or refused, and a refused call raises rather than
returning an empty result — which would convert a contract violation into a retry loop that
succeeds with no record.

### Rule 4 — abstention is an outcome, not an error

`LabelResolution.answer` is `Resolved | Abstained`, discriminated. An abstention is falsy, so
`if resolution:` reads correctly; it carries a reason, because that reason is what the human reads;
and it produces the same blocking finding the label would have produced anyway. **The agent
proposes, a human disposes.** Nothing an agent returns reaches a metric.

---

## 5 · The trace format

One JSON object per model call, in call order, at `artifacts/<run_id>/trace.jsonl`. JSON Lines rather than an array, so a trace is readable when a run dies halfway — which is
exactly when somebody wants to read it.

```json
{
  "agent": "resolution",
  "prompt_version": "v1",
  "model_id": "claude-sonnet-5",
  "effort": "low",
  "provider_mode": "replay",
  "cassette_key": "9f2c1d40e8ab77315cc0…",
  "output_contract": "LabelResolution",
  "tool_calls": [
    { "name": "fuzzy_candidates", "allowed": true },
    { "name": "read_values", "allowed": false }
  ],
  "output_json": "{\"raw_label\":\"Austrlia\",\"answer\":{\"outcome\":\"abstained\",…}}",
  "error": null,
  "input_tokens": 1840,
  "output_tokens": 96,
  "cache_read_tokens": 0,
  "duration_ms": 1
}
```

| Field | The question it answers |
|---|---|
| `agent`, `prompt_version` | which instructions produced this |
| `model_id`, `effort`, `provider_mode` | which model, thinking how hard, live or replayed |
| `cassette_key` | which recording — the identifier tying a replayed answer to the live call behind it |
| `tool_calls` | what the agent reached for, **refusals included** |
| `output_json` | the validated contract, serialised |
| `error` | why there is no output, when there is none |
| token counts, `duration_ms` | what it cost |

Four properties, each of which the format would be worthless without:

**A record has an output or an error, never neither.** A record with both empty is the shape a
swallowed exception takes, and it reads as a successful call that returned nothing.
`TraceRecord.model_post_init` rejects it.

**A failed call is still recorded.** `AgentRunner` writes the record and re-raises, including for
a failure that happens before a request exists. A trace written only on success is silent about
the one call anybody will want to read.

**Refusals are kept.** `"allowed": false` entries are the interesting half. A trace listing only
successful calls cannot answer what the agent *tried* to do.

**Order is the content.** `TraceLog` does not sort and does not deduplicate. Two identical calls
are two entries, because they were two calls, and collapsing them hides a retry loop.

Telemetry lives in `tda.obs` and not on any contract, because token counts are integers and an
integer on an `AgentOutput` is what the schema lint forbids. **A record of what a call cost is not
an agent's answer.**

---

## 6 · The supervisor and the budget

Code. A router with a budget and a log, in `src/tda/agents/supervisor.py`.

```
route(node, agent, needed=…, reason=…) ──► RoutingDecision
                                              granted                → one call may be made
                                              skipped                → no work for this agent
                                              refused_budget         → raises BudgetExceededError
                                              refused_unknown_agent  → not in the roster
```

`needed` is a **code** question with a code answer: are there unmappable labels, are there findings
to narrate. The supervisor does not second-guess it; what it owns is the budget and the record.

Every decision is recorded, skips included. A router that logs only its approvals cannot answer why
an agent did not run — and with abstention as a first-class outcome, that is a question a reviewer
will actually ask.

The budget is two numbers in `policy.yaml`:

```yaml
model:
  budget:
    max_calls_per_run: 60
    max_calls_per_agent: 20
```

The per-agent cap is the load-bearing one. The failure it catches is a loop, and a total-only
budget lets one runaway agent spend every other agent's allowance before anything notices.

**Exhaustion raises. It is never a truncation.** A verdict produced from a pipeline that quietly
stopped calling agents looks exactly like a complete one.

---

## 7 · The eval sets, and what they currently measure

`tests/eval/agents/cases/<agent>/<name>.json`. Each case carries the input, the expectations as
**property checks** rather than a golden answer, a sentence saying what it defends against, and two
fixtures: an answer that must score as a pass and one that must score as a fail.

Property checks rather than golden answers because a golden `FindingNarrative` compares prose to
one recorded sentence — which measures similarity to a recording, not quality, and fails on every
improvement. *This must abstain*, *this must state no figure*, *this must name the assigned
permutation* survive a better answer and still catch a wrong one.

Three assertions, and all three run today:

| Assertion | Status |
|---|---|
| every implemented agent has an eval set | runs on every push |
| the scorer accepts a right answer and **rejects a wrong one** | runs on every push, no model involved |
| the agents score against their cases | runs on every push, against 20 committed cassettes |

The third was blocked on `make record`, which makes live calls and needs an API key, until that key
arrived. A case with no cassette is reported as `NOT_RECORDED` in words rather than skipped, because
a skip reads as a pass in a CI summary and this is not a pass.

The second assertion is the one that makes every number this harness will ever report mean
something. A scorer that passes everything is the classic way an eval harness becomes decorative,
and it is the one property provable without a key — so it is proved on every push.

---

## 8 · The run: five nodes, one verdict

```
START → intake → extract → claim_parse → recompute_reconcile → publish → END
           │         │           │                  │
           └─────────┴───────────┴──────────────────┴────────────► publish (finished)
```

Sequential, and three pieces of resilience are deliberately absent — see
[ADR-0005](adr/0005-sequential-graph-and-deferred-resilience.md). **A submission that halts simply
halts.** Every path still reaches `publish`, because a rejected run's verdict is what answers the
officer's question about why.

| Node | Type | On failure |
|---|---|---|
| `intake` | code | reject with a stated reason code, before anything is read |
| `extract` | code | blocking findings for row-level defects. **Never infers a value** |
| `claim_parse` | model + code | unmapped sheets become findings for human mapping |
| `recompute_reconcile` | code | hard fail — a metric over a record set the library refuses has no correct partial answer |
| `publish` | code | the status, which `Verdict`'s own invariants then enforce |

### One typed state object

`RunState` carries the run's whole knowledge: records, claims, computed values, findings, and the
status. One object rather than five bespoke signatures, because the thing a reader wants to know is
*what was established by the time it got here*, and five signatures spread that answer across five
call sites.

What is **not** in it is as deliberate: policy, the provider and the three recorders live in
`RunContext`. A `TraceLog` is append-only and shared, and two nodes returning one would race to
overwrite each other with divergent copies.

> **A trap worth knowing.** LangGraph resolves the state schema's annotations at runtime, so every
> name in a `RunState` field annotation must exist at runtime. Moving one into a `TYPE_CHECKING`
> block turns `StateGraph(RunState)` into a `NameError`. `pyproject.toml` carries a per-file lint
> exemption saying so.

### Node records: entry and exit

Every node writes two records (`tda.obs.nodes`), and the asymmetry is the point: **a node that
halts mid-way leaves an entry with no exit**, which names exactly where the run stopped.
`NodeLog.unfinished()` answers that question and `mizan run` prints it on the failure path.

`TraceRecord` could not do this job. It requires a `prompt_version` matching `^v\d+$`, a `model_id`
and an `output_contract`, because it describes one model call — and four of the five nodes make no
model call at all. Inventing a prompt version for `recompute_reconcile` would be the dishonest way
to reuse a type.

### What the run produces

```
run-a1b2c3d4e5f6  PASS
  hotel MZN-DXB-001  period 2026-Q1
  policy 1.3.0  metrics 1.0.0  model claude-sonnet-5 (replay)
  claims checked 94  findings 0  definitional 0  not verifiable 0
```

Every stamp comes from the one place that owns it. `metric_library_version` had existed since M2
with no reader; the graph is its first.

---

## 9 · What a run leaves behind

`artifacts/<run_id>/`, three files, written by `mizan run` and read back by `mizan trace`. Each
answers a different question, and the split is deliberate — see
[ADR-0006](adr/0006-observability-redaction-and-recorded-runtime.md).

| File | The question |
|---|---|
| `run.json` | what was this run looking at, under which rules, and what did it cost? |
| `trace.jsonl` | what did each agent get asked and what did it answer? (§5) |
| `nodes.jsonl` | what did the pipeline do, in order, and where did it stop? |

They are written whether or not the run finished. A run that dies writes the same three files
under the id it was running as, with `status: FAILED` — the one value `VerdictStatus` cannot
express, because a verdict is a statement about a submission and a run that died made no statement.
Writing only on success would mean the record exists for every run except the ones anybody needs it
for.

`verdict.json` is **not** among them, and will not be written by this code. The verdict is the
thing an officer signs behind; these are its working. One writer for both would make the evidence
and the conclusion move together whenever either changed.

### The inputs are recorded by digest

`run.json` carries a SHA-256 per submitted file — the difference between *"the run says occupancy
was 71.2%"* and *"the run says occupancy was 71.2% for **these exact bytes**"*. Without it a
verdict and a file set can drift apart silently: somebody re-exports the workbook, the numbers
change, and nothing in the record says the two verdicts were about different documents. It is also
what makes a challenge answerable, because a hotel disputing a finding can be shown the digest of
the file the finding was computed from.

The convention is `sha256:` plus 64 hex characters, the same one `corpus/demo/manifest.json` uses,
asserted against that file by a test so a reader never meets two.

### Everything is redacted on the way out

Not on read, and there is no unredacted copy. The submitted workbook's label cells reach the
mapping prompt verbatim — `tda.excel.tools.digest` must pass them through for the agent to do its
job — so a property that types contact details into a header cell has put them in the prompt, and
they can re-emerge through a free-prose contract field into the trace.

The answer is redaction rather than refusal: withholding a trace because one header cell had a
phone number in it destroys the record in order to protect it. So the content is replaced and the
**fact** of it is recorded — `run.json` carries kind and count, never content. `tda/obs/redact.py`
states the limit this does not cover, and ADR-0006 states why the patterns are narrow.

### `mizan trace`

```
run-a5e8fb89466a  PASS  MZN-DXB-001  2026-Q1
  policy 1.3.0 · metrics 1.0.0 · claude-sonnet-5 (stub)
  5 input file(s) · $0.0000 (rates 2026-09-13) · 6,665ms
  redacted: nothing
├─ intake               ok            47ms
├─ extract              ok         5,224ms
├─ claim_parse          ok            23ms  1 call(s)
│  └─ mapping/v1  [stub f0a0e6f0…]  0/0 tok  $0.0000  0ms
│     asked for: WorkbookMapping, with list_sheets, peek_headers x4
│     returned: blocks [7] · cover {3} · unmapped [0]
├─ recompute_reconcile  ok         1,359ms
└─ publish              ok             0ms
```

Two properties are worth knowing before reading one.

**A call is attributed to a node by arithmetic, not by a field.** Nothing in a `TraceRecord` names
the node it ran under, because a record of a model call has no business knowing about the pipeline
that made it. `NodeTiming.model_calls` says how many calls each node made and the graph is
sequential (ADR-0005), so consuming the trace in order assigns every record exactly. A record the
timings cannot account for is **reported in the output** rather than dropped.

**Short structural values print; prose is counted.** A metric code, an A1 range or a sheet name
appears; a `FindingNarrative.sentence` becomes `…(147 chars)`. That line is not about terminal
width — it separates a value a reader needs to follow the run from prose that belongs in the
verdict.

### Runtime is recorded, never targeted

`run.json` carries `duration_ms` per node and per run, and `mizan run` prints the total labelled
*recorded, not targeted*. **No threshold, budget or assertion on run time exists anywhere in this
repository**, and none will until somebody measures the manual baseline. A number used before it is
measured gets challenged, and the challenge lands on the whole result rather than on the number.

---

## 10 · The three artefacts an officer files

Written by `tda.outputs` into the same `artifacts/<run_id>/` directory, and each aimed at a
different reader.

| File | Reader | The thing it must get right |
|---|---|---|
| `verdict.json` | a downstream system, an auditor | it cannot misreport its own contents |
| `annotated_<workbook>.xlsx` | the verification officer | the original is not modified, and "not checked" is visibly not "checked and correct" |
| `memo.docx` | the supervisor who reads one page | the verdict is in the first block |

### One redaction point

`write_outputs` writes `verdict.json`, **reads it back**, and renders the memo and the workbook
comments from what came off the disk. No downstream document can then say more than the verdict it
accompanies — which is the defect PRD-90's review found in the console output, and which would be
considerably worse in a Word file that gets forwarded.

The annotated workbook's **cells** are exempt: that file is a copy of the hotel's own submission
going back to the hotel, and removing content from it destroys evidence without withholding
anything the recipient does not already have.

### The summary block, and why it is validated rather than computed

`VerdictDocument` extends `Verdict` with a summary — the counts a reader needs before reading
anything else — and **recomputes it on load, refusing a document whose summary disagrees with its
own arrays**. The failure that prevents is quiet: somebody edits the findings, the summary keeps
saying five, and every reader believes the summary because reading it is cheaper than counting.

It is a subclass with a real field rather than a `computed_field`, because a dump carrying computed
fields fails to re-validate under `extra="forbid"` — the artefact would be write-only.

### The original is never modified, and the code proves it

`annotate` digests the submitted workbook before and after and raises if the two differ. It is the
hotel's evidence: the comparison must stay re-runnable against it, and nobody should have to ask
whether the tool changed what it was judging. See
[ADR-0007](adr/0007-outputs-render-from-the-written-verdict.md).

---

## 11 · The review gate

`make review` opens a Streamlit screen over one run's `verdict.json`. **Its acceptance criterion is
a stopwatch** — PRD-91: *if judging one finding requires opening the PDF in another window, the
screen has failed, regardless of how correct the finding is.*

So each finding is one card carrying, without a click: the figures, the cause, **the report rows
the computed value came from** and **the workbook cell the claim was read from**, then accept,
reject or amend.

```
   tda.review.evidence     the two pictures        ─┐
   tda.review.present      the words                ├─  tda.review.app  (a thin shell)
   tda.review.decisions    the record               ─┘
```

### The gate is headless, and that is what makes it testable

`record_decision` takes paths and strings. The screen calls it; PRD-91's fallback console flow
would call the same function, which is how *"the verdict schema does not change with the fallback"*
becomes a property rather than a promise. It also means the review gate is exercised in `make ci`
without a browser.

### The crop and the citation share a generator

`tda.extract.pdf.row_bands` reuses the function that assigns `PdfRef.row_start` during extraction.
A second implementation of "which row is row 12" would drift the first time a layout changed, and
its failure mode is the worst an evidence screen has: a highlighted row that is not the row the
finding is about, under a caption that is correct.

### Decisions are appended, and the memo follows

`Verdict.standing_decisions` gives the decision in force; every superseded one stays in the file.
Each decision is written immediately and re-issues `memo.docx`, whose signature block is derived
from `review_records` — otherwise a verdict recording three decisions sits beside a memo saying no
human has looked. See [ADR-0008](adr/0008-the-review-gate-is-headless-and-the-evidence-is-cropped.md).

---

## 12 · Where to read next

| Question | File |
|---|---|
| why the boundary exists at all | [ADR-0001](adr/0001-deterministic-core-agentic-edges.md) |
| why there is no `temperature` | [ADR-0002](adr/0002-model-layer.md) |
| why the supervisor is code | [ADR-0004](adr/0004-agent-runtime.md) |
| why the graph has no retries | [ADR-0005](adr/0005-sequential-graph-and-deferred-resilience.md) |
| why a trace is redacted rather than withheld | [ADR-0006](adr/0006-observability-redaction-and-recorded-runtime.md) |
| why the memo is rendered from the file rather than from memory | [ADR-0007](adr/0007-outputs-render-from-the-written-verdict.md) |
| why the review gate is headless | [ADR-0008](adr/0008-the-review-gate-is-headless-and-the-evidence-is-cropped.md) |
| what a model can and cannot see of a workbook | `src/tda/excel/tools.py` |
| why an unmapped label is never guessed | `src/tda/extract/normalise.py` |
| what a cassette is and how to review its diff | `tests/cassettes/README.md` |
| how to run any of it | [04-runbook.md](04-runbook.md) |
