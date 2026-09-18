# Mizan — an agentic reconciliation and verification system (POC): end-to-end build plan

> **Mizan** (ميزان) — the scales. The system weighs what a hotel *claims* against what its
> PMS reports *show*, and shows its working.

| | |
|---|---|
| **Source of truth** | A reconciliation-agent PRD and backlog, v1.0, 13 Sep 2026 |
| **Purpose here** | A reference implementation for the team: how an agentic system *should* be built |
| **Classification** | Green — synthetic data only. No real hotel or guest data ever enters this repo |
| **Not** | A production deployment. No hosting sign-off, no pen test, no real-file pilot |

---

## 1. The thesis, in one paragraph

Build a **reconciliation engine with agentic edges**, not a reasoning agent. Reservation-level
records are extracted from the PMS PDFs; the aggregations are **recomputed in code**; the result is
compared to the hotel's Excel cell by cell. Language models are used only where the problem is
genuinely linguistic — mapping a drifting spreadsheet to metrics, resolving a label variant,
writing the sentence that explains a finding, answering a reviewer's question. **No model performs
arithmetic, and no model output can reach a metric function.** That prohibition is enforced by an
import-graph check in CI, not by convention, because reproducibility is the thing the regulator is being asked to trust.

**Judge this POC on evidence quality, not autonomy.** Every finding cites a PDF page and row range
and an Excel cell. A verification officer who cannot check the agent in ten seconds will not sign
behind it, and the POC stalls regardless of its accuracy.

## 2. Why this is still a multi-agent showcase

The PRD's architecture is right, and it is also — read literally — only two model calls. That is a
thin demonstration of agentic capability. So the edges are built out properly: **six specialist
agents behind one supervisor**, each with a versioned prompt, a typed output contract, a narrow
tool allowlist, its own eval set, and a trace record. The deterministic wall does not move an inch.

The lesson for the team is exactly this shape: **agents at the edges, determinism in the core,
contracts at every boundary.** A system where agents do the arithmetic is easier to build, demos
well once, and cannot be signed off by anyone accountable for the number.

### The agent roster

| Agent | Job | Input | Output contract | Tool allowlist | Runs in |
|---|---|---|---|---|---|
| **Supervisor** | Routes work, enforces contracts, owns the budget, writes the trace | graph state | `RoutingDecision` | — (code router) | graph |
| **Mapping** | Which sheet/header block is which metric and axis | sheet names, header text, non-numeric cells | `SheetMapping[]` | `list_sheets`, `peek_headers` | `claim_parse` |
| **Resolution** | Resolve a label variant to a canonical code, **or abstain** | one unmapped label + code-supplied candidates | `LabelResolution` (`iso2 \| ABSTAIN`) | `iso_lookup`, `fuzzy_candidates` | `extract`, `claim_parse` |
| **Narrative** | Write the sentence that explains one finding | a `Variance` + its evidence refs | `FindingNarrative` | `read_finding`, `read_evidence` | `publish` |
| **Reviewer-assist** | Answer an officer's question, with citations | question + verdict | `CitedAnswer` | `query_verdict`, `get_evidence`, `get_policy_clause` | review UI |
| **Critic** | Grade narratives and abstentions; catch ungrounded prose | narrative + finding | `CriticVerdict` | — | `eval` |

**Patterns this demonstrates, deliberately:** supervisor/router; specialist agents with narrow tool
surfaces; *abstention as a first-class typed output* (the refusal scene); a critic/judge loop whose
result is scored deterministically; and handoffs that carry **keys and references only, never
numbers**.

### The hard constraint, stated as code

```
src/tda/metrics/    ── pure functions. no I/O, no globals, policy is a parameter.
src/tda/reconcile/  ── joins, tolerance, permutations, classification.
                       ↑ neither may import src.tda.agents.* or any model client.
                       ↑ CI fails the build on violation.
                       ↑ tests/arch/ proves the guard works by deliberately violating it.
```

A second guard: **no agent output schema may carry a numeric field** other than whitelisted
reference integers (`page`, `row`). A schema lint asserts it. This is what stops "the model
returned 1,204 room-nights" from ever becoming possible.

## 3. Determinism without `temperature=0`

The PRD specifies `temperature 0`. **That parameter has been removed from current Claude models and
returns HTTP 400** — and it never guaranteed determinism anyway. Reproducibility is therefore built
from three stronger mechanisms:

1. **Structured outputs on every call.** `client.messages.parse(..., output_format=<Pydantic>)`.
   A validation failure is an error, never a silent fall-back to free text. No free text enters the
   numeric path — it cannot, because the numeric path accepts only typed record objects.
2. **Cassette record/replay.** Every model call is keyed by a hash of
   `(model id, prompt version, rendered messages, output schema)`. Cassettes are committed.
   `make eval`, `make repro` and CI run in **replay** mode: offline, no key, byte-identical.
   `make record` refreshes cassettes against the live API; the diff is reviewed like any other diff.
3. **Pinned model id and prompt versions,** recorded in every run and stamped into `verdict.json`.

This is strictly stronger than the PRD's intent, and it is the reason CI can assert `make repro`
passes on every push.

**Model layer:** Anthropic Console API, `claude-opus-5`, adaptive thinking, effort tuned per agent
(`low` for resolution and mapping, `high` for narrative and reviewer-assist). Bedrock is out of
scope for this personal build. The provider is an interface with three adapters —
`anthropic` / `replay` / `stub` — so the pinned-region deployment question stays a config change.

## 4. Repository layout

```
Makefile                    datagen · run · review · eval · repro · demo · guard · ci
pyproject.toml              python 3.12 · ruff · mypy --strict · pytest
policy.yaml                 every contestable rule, schema-validated, versioned
docs/
  00-BUILD-PLAN.md          this file
  01-definitions.md         metric definitions (S1)
  02-assumption-register.md what we assumed, why, what moves if the regulator rules otherwise (S1)
  03-architecture.md        agent graph, the deterministic boundary, trace format
  04-runbook.md             a second engineer runs every target unaided (S12)
  05-onboarding-asks.md     the POC's real output: decisions the regulator would own (S12)
  06-walkthrough.md         what was proven, and what was not (S12)
  07-git-workflow.md        branches, PRs, commits, Definition of Done
  adr/                      ADR-0001 deterministic core · 0002 model layer · …
src/tda/
  contracts/                ReservationRecord · Claim · Variance · PdfRef · Finding · Verdict
  policy/                   loader, schema validation, permutation enumeration
  metrics/                  PURE — guarded
  extract/                  pdfplumber column map, normalisation, totals reconciliation
  excel/                    openpyxl claim reader, A1 refs, internal pre-checks
  reconcile/                join, tolerance, permutation runner, classification — guarded
  agents/                   supervisor · mapping · resolution · narrative · reviewer · critic
    provider/               LLMProvider: anthropic | replay | stub · cassettes · prompt registry
  graph/                    LangGraph: intake → extract → claim_parse → recompute_reconcile → publish
  outputs/                  verdict.json · annotated workbook · Word memo
  review/                   Streamlit findings screen (+ console fallback)
  obs/                      run ledger, agent trace, token and cost accounting, timing
tools/datagen/              ledger-first generator + INDEPENDENT aggregator → truth_metrics.json
tools/fixtures/             declarative mutation specs; expectations DERIVED from the spec
tools/guard/                import-graph guard + agent-schema lint
tests/{unit,arch,eval}/
corpus/demo/                frozen · never mutated · never scored
corpus/fixtures/F1..F6/
docker/                     Dockerfile · compose.yaml
.github/workflows/ci.yml
```

## 5. Data strategy — why the result will be believable

- **Ledger-first.** Reservations are generated first (~1,200 over one quarter); the three monthly
  PDFs *and* the hotel workbook are both rendered from that one ledger. Nothing is authored twice.
- **The generator's aggregator is an independent implementation** and may not import
  `src.tda.metrics`. If they shared code, the POC would prove only that the code equals itself.
  Guarded in CI.
- **The demo corpus is frozen, never mutated, and never scored.** A clean pass on data the system
  was rendered from is not evidence.
- **Fixtures are declarative mutations of a copy,** and expectations are **derived from the mutation
  spec**, never hand-written — so a mutation change cannot silently diverge from its expectation.
- **Two of six fixtures must produce zero findings.** A system that finds errors everywhere is not a
  verification system. Precision is what a reviewer actually feels.
- **The definitional edges are planted on purpose:** complimentary rooms, house-use rooms, day-use
  reservations, ≥25 month-spanning stays, and an out-of-order room window in the inventory
  reference. Without them the permutation engine has nothing to find.
- **Fixtures are never shown in a demonstration.** A demo built on visibly planted errors invites
  the one challenge that cannot be answered.

## 6. Quality gates — Definition of Done

Every PR must be green on all of these before it merges to `develop`:

| Gate | Mechanism |
|---|---|
| Lint + format | `ruff check`, `ruff format --check` |
| Types | `mypy --strict src/ tools/` |
| Unit tests | `pytest tests/unit` — every metric case cites the `definitions.md` clause it tests |
| **Architecture guard** | no model client under `metrics/` or `reconcile/`; generator ⊥ metric library |
| **Agent schema lint** | no numeric fields in agent output contracts beyond `page`/`row` |
| **Evidence assertion** | every `Finding` carries a `PdfRef` *and* an Excel cell — a type-level requirement |
| Eval | `make eval` — all six fixtures match their derived expectations |
| Reproducibility | `make repro` — two runs, identical verdict, timestamps excluded |
| Docs | ADR for every architectural decision; runbook updated when a target changes |

## 7. Delivery — 6 milestones, 20 issues

The PRD's twelve stories are carried verbatim (with their Jira refs) and eight are added for the
agent runtime, observability and delivery. Dependencies are encoded as Linear blocking links, so
the sequence enforces itself on the board rather than living only in a document.

| Milestone | Issues | What exists at the end of it |
|---|---|---|
| **M1 Contracts & Foundations** | S1, S2, A0 | Definitions, `policy.yaml`, typed contracts, model layer, the guard that makes the determinism claim real |
| **M2 Corpus & Truth** | S3, S4 | ~1,200-reservation synthetic quarter, three PDFs, a workbook, `truth_metrics.json`, and a metric library that reproduces it with **zero** deviation |
| **M3 Engine** | S5, S6, S7 | Extraction that refuses rather than guesses; claims with A1 refs; classification that separates hotel error from policy disagreement |
| **M4 Agents & Graph** | A1, S8, A4 | Supervisor, six agents, traces, token accounting, one orchestrated run |
| **M5 Surfaces & Outputs** | S9, S10, A2 | Review screen with side-by-side evidence, three artefacts an officer can file, a reviewer-assist agent |
| **M6 Evidence & Showcase** | S11, A3, S12, A5, A6 | Six fixtures scored, narratives graded, three demo scenes, Docker + CI + release bundle, published showcase |
| *Optional* | A7 | Next.js review frontend on Vercel — taken only if bandwidth allows |

**Sequencing rule:** nothing numeric is built before the definitions exist (S1), and the corpus is
built before any product code that reads it (S3), because ground truth must exist rather than be
re-derived by hand later.

## 8. Fallbacks, pre-agreed

Sixty points in a notional sprint is aggressive. Three fallbacks are written into the issues
themselves rather than improvised, and this is the order in which they are taken:

1. **S11** drops from six fixtures to three (F1, F2, F3 — control, transcription, definitional).
2. **S9** drops Streamlit for a console review flow with the same accept/reject/amend record.
3. **S10** drops the Word memo and keeps the annotated workbook and `verdict.json`.

A7 (Vercel frontend) is explicitly optional and is never traded against any of the above.

## 9. Risks carried openly

| Risk | Impact | What we do about it |
|---|---|---|
| **The PMS PDFs turn out to be pre-aggregated summaries, not reservation rows** | Recomputation is impossible; the approach narrows to summary-to-summary matching and the business case weakens materially | Stated as assumption #1, raised at onboarding, confirmed before any real-file work. **This is the single largest design fork.** |
| **This project authored both sides of the reconciliation** | A 100% result proves the pipeline, not field robustness | Said out loud in the walkthrough rather than left to be discovered by the audience |
| Definitional variances read as false positives | Correct findings discredit the system | The permutation engine classifies them mechanically and reports them in a visibly separate section from clerical errors |
| Real PMS exports carry names, nationalities, ages — personal data | The POC cannot lawfully touch real files as built | Synthetic data only. The pseudonymisation boundary is scoped but deferred until a real-file pilot is agreed |
| Non-deterministic model output leaks into a number | The reproducibility claim collapses | Import guard + schema lint + cassette replay, all three in CI |

**No time-saving figure appears anywhere in this repo or the walkthrough,** because the manual
baseline has not been measured. A number used before it is measured will be challenged, and the
challenge will land on the whole result rather than on the number.

## 10. Deviations from the PRD, stated plainly

| PRD says | This build does | Why |
|---|---|---|
| Amazon Bedrock, `me-central-1` | Anthropic Console API, `claude-opus-5`, behind a provider interface | This is a personal reference build, not a production deployment. Region pinning stays a config change. |
| `temperature 0` | Structured outputs + committed cassette replay | `temperature` is rejected (HTTP 400) on current models, and never guaranteed determinism |
| Two model touchpoints | Six agents behind a supervisor, same deterministic wall | The PRD architecture is right; the agentic surface was thin for a capability demonstration |
| Twelve stories | Twenty issues | Agent runtime, observability and delivery were implicit in the PRD and are made explicit |
| FR-04, FR-13 | *(absent from the PRD's own table)* | Numbering gap in the source document; not silently invented |

---

*Plan authored 13 Sep 2026. Story points are relative sizing, not days.*
