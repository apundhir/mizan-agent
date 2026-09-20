# Architecture decision records

One file per decision, numbered, immutable once accepted. A decision that turns out to be wrong is
**superseded by a new ADR**, not edited — the reasoning that led to the wrong call is the most useful
thing in the file, and rewriting history to look prescient teaches nobody anything.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-deterministic-core-agentic-edges.md) | Deterministic core, agentic edges — and enforce it in CI | Accepted |
| [0002](0002-model-layer.md) | Anthropic provider, cassette replay, and why `temperature` is gone | Accepted |
| [0003](0003-ledger-first-synthetic-corpus.md) | Ledger-first corpus, and the generator may not import the product | Accepted |
| [0004](0004-agent-runtime.md) | Agent runtime: code supervisor, tool allowlists, abstention as a typed output | Accepted |
| [0005](0005-sequential-graph-and-deferred-resilience.md) | A sequential graph, and three pieces of resilience deliberately not built | Accepted |
| [0006](0006-observability-redaction-and-recorded-runtime.md) | Redact rather than refuse, record runtime rather than target it | Accepted |
| [0007](0007-outputs-render-from-the-written-verdict.md) | Outputs render from the written verdict, and the submission is never touched | Accepted |
| [0008](0008-the-review-gate-is-headless-and-the-evidence-is-cropped.md) | The review gate is headless, and the evidence is cropped to what was cited | Accepted |
| [0009](0009-the-assistant-cites-or-declines.md) | The reviewer's assistant cites or declines, and computes nothing | Accepted |
| [0010](0010-the-console-runs-in-process-and-replays-the-record.md) | The Run console runs in process, and replays the same record a finished run would | Accepted |

## Template

```markdown
# ADR-NNNN · <decision, as a statement not a question>

- **Status:** Proposed | Accepted | Superseded by ADR-NNNN
- **Date:** YYYY-MM-DD
- **Deciders:**
- **Issues:** PRD-nn

## Context
What forced a decision. Include the constraint that made the obvious option wrong.

## Decision
What was decided, in the imperative. Specific enough to be checkable.

## Enforcement
How the decision is held in place mechanically. A rule that lives only in this file
will be broken by someone in a hurry who has a good reason.

## Consequences
### Accepted costs
The honest downsides. If this section is empty, the decision was not a decision.
### What is bought
### What this does not claim

## Alternatives considered
Each with the reason it lost. "We didn't think of it" is a valid entry.
```
