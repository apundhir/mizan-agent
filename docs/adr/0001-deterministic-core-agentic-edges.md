# ADR-0001 · Deterministic core, agentic edges

- **Status:** Accepted
- **Date:** 2026-09-13
- **Supersedes:** —
- **Related:** [ADR-0002 model layer](0002-model-layer.md) · [ADR-0004 agent runtime](0004-agent-runtime.md)
- **Issues:** PRD-80, PRD-81

## Context

The regulator receives a monthly Excel return from every hotel alongside the PDF reports the hotel exports from
its property management system. A coordinator checks the pack, an analyst verifies the numbers, and
the two go back and forth with the hotel until the data is confirmed. The output feeds **the
destination fee charged to hotels** and **the destination performance reporting given to leadership**.

So the numbers this system produces are not informational. One of them appears on an invoice, and the
other appears in front of leadership. A hotel that disputes a fee will ask how the figure was reached,
and the answer has to survive that question.

There are two plausible architectures.

**A — a reasoning agent.** Give a capable model the PDFs and the workbook, let it compare them and
report discrepancies. Fast to build, demonstrates impressively, and handles layout variation without
any parsing work.

**B — a reconciliation engine with agentic edges.** Extract reservation-level records
deterministically, recompute the aggregations in code, compare cell by cell. Use models only where
the problem is genuinely linguistic.

Architecture A fails a question nobody in the room can avoid asking: *run it again and show me the
same answer.* Model output is not reproducible, and a fee calculation that changes between runs is
not a fee calculation. It fails a second question too: *where did that number come from?* — because
the answer is "the model computed it", which is not an audit trail.

## Decision

**Build architecture B. Models read layouts, labels and language. Code performs every calculation.**

No path in the system lets a model output reach a metric function. Specifically:

1. `src/tda/metrics/` and `src/tda/reconcile/` contain **pure functions**: no I/O, no globals, no
   model client. Policy is a parameter on every function.
2. Those packages **may not import** `src/tda/agents/` or any model client, transitively.
3. Agent output contracts **may not carry numeric fields**, other than whitelisted reference integers
   (`page`, `row`).
4. Classification of a variance — including deciding that a difference is definitional rather than
   clerical — is performed **entirely in code**, driven by an enumerated, committed permutation set in
   `policy.yaml`. `classification.consults_model` is `false` and the schema will not accept `true`.
5. **Every contestable rule lives in `policy.yaml`, not in code**, so a definitional argument is
   settled by a configuration change and a re-run rather than by an engineer's edit.

The division of labour, stated as a sentence we are willing to be held to:

> **The code finds the cause. The model writes the sentence.**

## Enforcement

A rule that lives only in a document is a rule that will be broken by someone in a hurry who has a
good reason. All five points above are enforced mechanically:

| Rule | Enforced by | Proven by |
|---|---|---|
| 1, 2 | Import-graph guard in CI | A test that deliberately violates the guard and asserts the build fails |
| 3 | Agent schema lint in CI | Same pattern: a deliberately non-compliant schema must fail the lint |
| 4 | `policy.schema.json` — `consults_model` is `const: false` | `tools/policy/validate_policy.py`, adversarial case *"classification consults a model"* |
| 5 | `policy.schema.json` structural validation on load | 22 adversarial cases, each a loosening someone under deadline pressure would plausibly make |

**A guard nobody has watched fail is not a guard, it is a comment.** Every one of the above has a
negative test, and those negative tests are the load-bearing part of this ADR.

## Consequences

### Accepted costs

- **More code.** A parser, a column map, a metric library and a permutation runner, against roughly
  one prompt for architecture A.
- **Brittleness to layout change.** Deterministic extraction is tied to a known layout. A second PDF
  layout is real work, and this POC deliberately scopes to one. *(This is the honest weakness of the
  decision and it belongs in the walkthrough, not in a footnote.)*
- **Occupancy needs an input the PDFs cannot provide.** Rooms available is a property attribute, so a
  per-hotel inventory reference becomes a required input. Architecture A would have cheerfully
  estimated it — which is precisely the behaviour being rejected.
- **A less impressive demo.** The agentic surface is smaller. Addressed in [ADR-0004](0004-agent-runtime.md) by
  building the edges out properly rather than by moving the wall.

### What is bought

- **`make repro` can exist.** Two runs, identical verdict. This is the property the whole POC is
  asking to be trusted on, and architecture A cannot offer it at any price.
- **Every finding cites a PDF page and row range and an Excel cell** — because the value was derived
  from a specific row that the system can point at, not inferred from a document read as a whole.
- **Definitional variances are separable from clerical errors.** Re-running the metric under an
  alternative policy setting is a mechanical operation. Asking a model to intuit whether a difference
  reflects a rule disagreement is not, and getting it wrong turns a correct finding into a false
  accusation against a hotel.
- **A definitional argument is a config change.** When the regulator rules that complimentary rooms are
  excluded, that is one line in `policy.yaml`, a version bump and a re-run — not a code change, a PR
  and a regression risk.

### What this does not claim

This decision makes the system **reproducible**. It does not make it **right**. If `policy.yaml`
encodes the wrong definition of occupancy, the system will compute the wrong number identically every
time, and cite its evidence impeccably while doing so. Correctness rests on
[`01-definitions.md`](../01-definitions.md) and on the eight entries in
[`02-assumption-register.md`](../02-assumption-register.md) — six of which are genuinely arguable and
none of which the regulator has ratified.

Reproducibility is what makes the argument about correctness *possible*. It is not a substitute for
having it.

## Alternatives considered

**Model computes, code verifies.** Let the model do the aggregation and have code check its
arithmetic. Rejected: if code can verify the number, code can compute it, and the model adds only
latency, cost and a failure mode. The "verification" would also have to be a full reimplementation to
be meaningful — at which point it is the implementation.

**Model computes with a code-execution tool.** Reproducible in principle, since the arithmetic runs in
a sandbox. Rejected because *which* code the model writes is not reproducible: the tool output is
deterministic, the program that produced it is not. Auditing a run would mean auditing a
freshly-written program each time.

**Convention instead of enforcement** — a documented rule that metrics must not import the model
client, upheld in code review. Rejected on the strength of the counterfactual: the first time a
country label fails to map at 6pm before a demo, the fastest fix in the world is to ask the model, and
a reviewer looking at a three-line diff will approve it. The guard exists for that evening.
