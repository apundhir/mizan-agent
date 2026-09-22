# ADR-0004 · The agent runtime: a code supervisor, call-time allowlists, abstention as a type

- **Status:** Accepted
- **Date:** 2026-09-13
- **Related:** [ADR-0001](0001-deterministic-core-agentic-edges.md) · [ADR-0002](0002-model-layer.md)
- **Issues:** the agent runtime

> **On the number.** the agent runtime's description names this file `0003-agent-runtime.md`. ADR-0003 was
> taken by the ledger-first corpus decision before this story started, and `adr/README.md` already
> listed the agent runtime as 0004. Numbers are allocated on write, not reserved in advance; the
> issue text is the stale half.

## Context

ADR-0001 drew the deterministic boundary and ADR-0002 built the model layer under it. Neither says
what an **agent** is. That gap is not academic: this POC is as much a reference implementation for
the team as it is a verification tool, and "we built six agents" is a claim whose meaning depends
entirely on what the word carries.

The industry answer is that an agent is a prompt plus a model call, occasionally with tools. That
definition is unfalsifiable — anything is an agent — and it is useless to somebody who has to be
accountable for what comes out. When a verification officer asks *"why did it say that?"*, a prompt
and a model call have no answer. When someone asks *"what can it reach?"*, there is no list. When
the prompt changes, nothing records that the answer was produced under the old one.

Three specific pressures forced the decisions below.

**The pipeline is fixed and somebody will want a model to route it.** Extract, map, read,
reconcile, publish, in that order, with the resolution agent reached when and only when a label
fails the committed lookup. That is a `match` statement. A supervisor model would be the
demonstration people expect and would add a non-deterministic branch to the one part of the system
that has no reason to have one.

**Prompts describing restrictions do not restrict.** ADR-0001 made this argument about values —
`tda.excel.tools` has no code path that can return a cell's contents, which is a guarantee rather
than a request. The same argument applies to *reach*: an agent told not to use a tool is an agent
that might.

**Ambiguity has no correct answer and code must still do something.** `tda.extract.normalise`
refuses to guess an unmapped country label, and is right to. But a refusal produced by an exception
and a refusal produced by deliberation are different events, and a system that renders both as
"error" has thrown away the more useful one.

## Decision

**An agent is exactly five things: a versioned prompt, a typed output contract, a narrow tool
allowlist, an eval set, and a trace record. The supervisor is code. Allowlists are enforced at the
call. Abstention is a typed output.**

### 1 · Five things, four of them constructor arguments

`AgentSpec` takes `name`, `prompt_version`, `output_type`, `tools` and `effort`, with **no
defaults**. An agent missing any of them does not type-check under `mypy --strict`. The fifth — the
eval set — cannot be a constructor argument, so `tests/eval/agents/test_agent_evals.py` asserts
that every agent in the roster has one, and fails when a new agent arrives without cases.

`output_type` is bound to `AgentOutput`, not to `BaseModel`. The bound is what makes "an agent
returns a contract the schema lint has checked" a statement the type checker enforces rather than a
convention that holds until someone is in a hurry.

### 2 · The supervisor is code

`tda.agents.supervisor.Supervisor` is a router with a budget and a log. It decides nothing a model
could decide better, because there is nothing here to decide: the caller answers "is there work for
this agent?" from the data — are there unmappable labels, are there findings to narrate — and the
supervisor owns the budget and the record.

Every `route()` call produces a `RoutingDecision`, **including the skips and the refusals**. A
router that logs only its approvals cannot answer why an agent did not run, and with abstention as
a first-class outcome that is a question a reviewer will actually ask.

`RoutingDecision` is deliberately **not** an `AgentOutput` and does not live in
`agents/contracts/`. No model produced it, and marking a code decision as a model answer would
point the schema lint at the wrong thing.

### 3 · The budget refuses; it never truncates

`policy.yaml` carries `model.budget` with two numbers. The per-agent cap is the load-bearing one:
the failure it catches is a loop — an answer fails a check, is retried, fails again — and a
total-only budget lets one runaway agent spend every other agent's allowance before anything
notices.

Exhaustion raises `BudgetExceededError` and the run fails. It does not continue with the calls it
has left, because **a verdict produced from a pipeline that quietly stopped calling agents looks
exactly like a complete one**, and the reviewer is the one who would pay for the difference.

### 4 · Allowlists are enforced at the call, and refusals are recorded

`ToolRegistry.session(agent, allowed)` returns a `ToolSession` bound to one agent's allowlist. It
is the only route to a tool. Two checks, at two different moments, for two different failures:

| When | What it catches | Why there |
|---|---|---|
| session construction | an allowlist naming an unregistered tool | a silently-ignored entry grants nothing while looking like it granted something |
| the call | an agent reaching outside its allowlist | the only moment the decision is real |

A refused call **raises**. Returning "no such tool" and letting the model try something else
converts a contract violation into a retry loop, and the run ends up succeeding with no record that
an agent reached outside its surface. Refusals are recorded in the session and copied into the
trace: a log of only the successful calls cannot answer what the agent *tried* to do.

The allowlist lives in `roster.py`, in code, and **not** in `policy.yaml` — unlike effort and
prompt version, which do. Effort is a tuning decision a reader may reasonably change. A tool
allowlist is a boundary, and a boundary editable from a YAML file that ships alongside the data it
governs is not one.

Granting a tool changes the rendered system prompt, so it changes the cassette key. That is
occasionally surprising and it is correct: an agent that could have asked something new is not the
agent that was recorded.

### 5 · Abstention is a typed output

`LabelResolution.answer` is a discriminated union of `Resolved` and `Abstained`. Not an exception,
which says the machinery broke. Not a sentinel string, which the caller has to remember. Not a
confident guess, which is the failure the whole normalisation design exists to prevent.

The union is nested inside one contract because the provider takes one output schema per call and a
bare union is not a class. The nesting is what keeps the two outcomes structurally distinct instead
of collapsing them into a `resolved: bool` flag beside an `iso2` that is sometimes meaningful.

### 6 · The trace lives in `tda.obs`, and is written even when the call fails

`TraceRecord` carries token counts and a duration. Those are integers, and an integer on an
`AgentOutput` is exactly what the schema lint forbids — so the trace lives next to `UsageLedger`,
because **a record of what a call cost is not an agent's answer**.

`AgentRunner` writes the record and re-raises on every failure path, including one that dies before
a request exists. A trace written only on success is silent about the one call anybody will ever
want to read about.

## Enforcement

| Decision | Held in place by |
|---|---|
| five things, four required | `AgentSpec` has no defaults; `mypy --strict` in `make ci` |
| every agent has an eval set | `test_every_implemented_agent_has_an_eval_set` |
| no numeric field on any contract | `tools/guard/agent_schema_lint.py`, in `make guard` |
| allowlists enforced, not described | `ToolSession.call` raises; tests call a denied tool and assert |
| refusals recorded | `TraceRecord.refused_tools`, asserted in `tests/unit/test_agents.py` |
| the budget refuses | `BudgetExceededError`; two adversarial policies in `tools/policy/validate_policy.py` |
| the scorer actually discriminates | every eval case carries a counterexample that must fail |

The last one deserves naming separately. A scorer that passes everything reports a number that
looks like evidence, and it is the most common way an eval harness becomes decorative. Every case
under `tests/eval/agents/cases/` carries both an answer that must score as a pass and one that must
score as a fail, and both assertions run on every push.

## Consequences

### Accepted costs

**The supervisor is unimpressive.** "Our router is a `match` statement" is a worse demo than an
agent deciding what to do next, and this POC is partly a capability demonstration. Taken anyway:
the routing is genuinely fixed, and a model making a decision that has one right answer adds
variance and nothing else.

**No tool-use loop anywhere.** Tools are called by code, before the request, with their output
rendered into the message. This is a real limitation — an agent cannot decide it needs to look at a
second sheet — and it is the same trade `tda.excel.tools` already took: a model with no channel is
a stronger guarantee than a model with a filtered one. If a future agent genuinely needs
iteration, the session already records every call and the allowlist already gates every call; what
would change is the provider, not this design.

**Granting a tool re-records the cassettes.** A one-word allowlist change invalidates every
recording for that agent. Correct, and expensive.

**The eval scores did not exist at this decision.** The harness ran, the scorer was proven and the
cases were committed, but scoring the *agents* needed cassettes, cassettes needed `make record`, and
that needed an API key. The gap was stated in three places and closed the first time anybody ran the
target: 20 of 20 cases recorded and passing. A case with no cassette is still reported as
`NOT_RECORDED` in words rather than skipped, because a skip reads as a pass in a CI summary.

### What is bought

A reviewer can answer *"why did it say that?"* from `trace.jsonl` without a debugger. A reader can
answer *"what can the narrative agent reach?"* from one table. A run cannot cost more than its
budget. An ambiguous country label produces a considered refusal that a human confirms in seconds,
rather than a guess nothing downstream can detect. And the six-agent system does not move the
deterministic boundary by an inch, because no agent has a field that can carry a number.

### What this does not claim

That the agents are *good*. Nothing here measures answer quality; that is the eval set's job, and
twenty passing cases are a floor rather than a verdict. That two prompts make two blind spots
independent — the critic and the narrative agent share a model, and the critic catches ungrounded
prose rather than subtle inferential error. That the allowlist protects against a malicious model;
it protects against a mistaken one, which is the threat this system actually has.

## Alternatives considered

**A model supervisor.** Rejected for the reason in the context: the route is fixed, so the only
thing a model adds is a branch that can go wrong silently. Nothing downstream can tell that the
resolution agent was never asked.

**Allowlists described in the prompt.** The same mistake ADR-0001 rejected for values, one level
up. An agent told not to use a tool is an agent that might.

**Allowlists in `policy.yaml`.** Tempting for consistency with effort and prompt version, and
wrong: policy ships with the data it governs, and a boundary editable from there is not a boundary.

**Abstention as an exception.** Simpler, and it loses the distinction between "the machinery broke"
and "the agent considered this and declined" — which is exactly the distinction demo scene three is
built on.

**A single `AgentOutput` with `resolved: bool`.** One less type, and it makes the invalid state
representable: `resolved=True` with a null code, or `resolved=False` with a populated one. The
discriminated union makes both unconstructible.

**Golden answers in the eval cases.** Compares prose to one recorded sentence, which measures
similarity to a recording rather than quality, and fails on every improvement. The cases hold
property checks instead — *this must abstain*, *this must state no figure* — which survive a better
answer and still catch a wrong one.

**A model grading the eval harness.** It would close the cassette gap today. It would also report a
number produced by the thing being measured, which is not a number.
