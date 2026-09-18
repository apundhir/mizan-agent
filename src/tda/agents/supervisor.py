"""The supervisor: a code router that decides which agent runs, and refuses when the budget is spent.

**The supervisor is code, and that is the architectural decision this file exists to hold.**
Routing a verification pipeline is not a judgement call. The order is fixed — extract, then map,
then read, then reconcile, then publish — and which agent handles an unmapped country label is
decided by what kind of thing it is, not by a model's read of the situation. A model in this seat
would add a non-deterministic branch to the one part of the system that has no reason to have one,
and it would do it at the point where a wrong turn is hardest to notice: nothing downstream can
tell that the resolution agent was never asked.

So the supervisor is a `match` statement with a budget, and every decision it makes is recorded.
See `docs/adr/0004-agent-runtime.md`.

## The budget

A per-run cap on model calls, from `policy.yaml`. Two numbers: a total for the run and a cap per
agent. The per-agent cap is the one that matters, because the failure it catches is a loop — an
agent whose answer fails a check, is retried, fails again — and a total-only budget lets one
runaway agent consume the allowance of every other before anything notices.

**Exhaustion is a refusal, not a truncation.** The supervisor raises, the run fails, and the trace
says which agent asked for the call that could not be granted. The alternative is a verdict
produced from partial agent work, which looks exactly like a complete one.

## What a decision records

Every call to `route()` produces a `RoutingDecision`, granted or refused, whether or not a model is
subsequently called. A router that records only its approvals cannot answer why an agent did not
run, which — with abstention as a first-class outcome — is a question a reviewer will actually ask.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from tda.agents.roster import entry_for

if TYPE_CHECKING:
    from tda.policy import Policy

# Used when `policy.yaml` predates the budget block. Generous enough that no correct run reaches
# it, tight enough that a loop stops inside one coffee break. A missing budget is not an excuse for
# an unbounded run.
DEFAULT_MAX_CALLS_PER_RUN = 60
DEFAULT_MAX_CALLS_PER_AGENT = 20


class Disposition(StrEnum):
    """What the supervisor decided.

    `SKIPPED` is not a failure: a run whose workbook has no unmappable labels should not call the
    resolution agent, and recording that as a decision is how the trace shows the agent was
    considered rather than forgotten.
    """

    GRANTED = "granted"
    SKIPPED = "skipped"
    REFUSED_BUDGET = "refused_budget"
    REFUSED_UNKNOWN_AGENT = "refused_unknown_agent"


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """One routing decision, with the reason in prose.

    Deliberately **not** an `AgentOutput`: no model produced it. Putting it in
    `agents/contracts/` would mark a code decision as a model answer, and the schema lint would
    then be policing the wrong thing — the interesting rule about this record is that it must never
    have come from a model at all.
    """

    node: str
    agent: str
    disposition: Disposition
    reason: str

    @property
    def granted(self) -> bool:
        return self.disposition is Disposition.GRANTED

    def render(self) -> str:
        return f"{self.node} -> {self.agent}: {self.disposition.value} ({self.reason})"


class BudgetExceededError(Exception):
    """The run asked for more model calls than its budget allows.

    Raised rather than returned, because there is no sensible partial answer. A verdict built from
    a pipeline that stopped calling agents halfway through is indistinguishable from a complete
    one, and it is the reviewer who would pay for the difference.
    """

    def __init__(self, agent: str, *, limit: int, scope: str) -> None:
        super().__init__(
            f"model-call budget exhausted: {scope} limit of {limit} reached, and agent {agent!r} "
            "asked for another. Raising rather than continuing without the call - a verdict built "
            "from a pipeline that quietly stopped calling agents looks exactly like a complete one."
        )
        self.agent = agent
        self.limit = limit


@dataclass(frozen=True, slots=True)
class Budget:
    """The per-run caps, read from policy.

    Frozen, and read once at the start of a run. A budget that could be raised mid-run by the code
    it constrains is not a budget.
    """

    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN
    max_calls_per_agent: int = DEFAULT_MAX_CALLS_PER_AGENT

    @classmethod
    def from_policy(cls, policy: Policy) -> Budget:
        budget = policy.model.budget
        if budget is None:
            return cls()
        return cls(
            max_calls_per_run=budget.max_calls_per_run,
            max_calls_per_agent=budget.max_calls_per_agent,
        )


@dataclass(slots=True)
class Supervisor:
    """Routes work, enforces the budget, and records every decision.

    Mutable, because it is a tally of what a run has spent. The immutable artefacts are the
    `RoutingDecision`s it produces, which are what reach the run ledger.
    """

    budget: Budget = field(default_factory=Budget)
    _decisions: list[RoutingDecision] = field(default_factory=list)
    _spent: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_policy(cls, policy: Policy) -> Supervisor:
        return cls(budget=Budget.from_policy(policy))

    # ── the tally ────────────────────────────────────────────────────────────

    @property
    def calls_made(self) -> int:
        return sum(self._spent.values())

    def calls_for(self, agent: str) -> int:
        return self._spent.get(agent, 0)

    @property
    def decisions(self) -> tuple[RoutingDecision, ...]:
        """Every decision in order, refusals and skips included."""
        return tuple(self._decisions)

    # ── routing ──────────────────────────────────────────────────────────────

    def route(self, node: str, agent: str, *, needed: bool, reason: str) -> RoutingDecision:
        """Decide whether `agent` runs at `node`, and record the decision either way.

        `needed` is the caller's answer to "is there work for this agent?", and it is a *code*
        question with a code answer: are there unmappable labels, are there findings to narrate.
        The supervisor does not second-guess it. What the supervisor owns is the budget and the
        record, which is the whole of a router's job when the route itself is fixed.

        Raises `BudgetExceededError` when the work is needed and the allowance is spent. Returns a
        refused decision without raising for an unknown agent, because that is a wiring defect the
        caller should see in full rather than as an exception from three frames down — the
        subsequent `AgentSpec` construction will fail on it anyway.
        """
        try:
            entry_for(agent)
        except KeyError:
            return self._record(
                node,
                agent,
                Disposition.REFUSED_UNKNOWN_AGENT,
                f"{agent!r} is not in the roster, so its tool allowlist is undeclared",
            )

        if not needed:
            return self._record(node, agent, Disposition.SKIPPED, reason)

        spent_by_agent = self._spent.get(agent, 0)
        if spent_by_agent >= self.budget.max_calls_per_agent:
            self._record(
                node,
                agent,
                Disposition.REFUSED_BUDGET,
                f"{agent} has made {spent_by_agent} calls, its per-agent cap",
            )
            raise BudgetExceededError(
                agent, limit=self.budget.max_calls_per_agent, scope="per-agent"
            )

        if self.calls_made >= self.budget.max_calls_per_run:
            self._record(
                node,
                agent,
                Disposition.REFUSED_BUDGET,
                f"the run has made {self.calls_made} calls, its per-run cap",
            )
            raise BudgetExceededError(agent, limit=self.budget.max_calls_per_run, scope="per-run")

        self._spent[agent] = spent_by_agent + 1
        return self._record(node, agent, Disposition.GRANTED, reason)

    def _record(
        self, node: str, agent: str, disposition: Disposition, reason: str
    ) -> RoutingDecision:
        decision = RoutingDecision(node=node, agent=agent, disposition=disposition, reason=reason)
        self._decisions.append(decision)
        return decision

    def render(self) -> str:
        return "\n".join(decision.render() for decision in self._decisions)
