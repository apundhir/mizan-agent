"""The collaborators a run carries: policy, the model layer, and the three things it records into.

Separate from `RunState` because they are different kinds of thing, and merging them would cost the
distinction. `RunState` is **what the run has established** — records, claims, findings — and it is
what a node reads to decide what to do. `RunContext` is **what the run was given** — the ruleset,
the provider, the ledgers — and no node ever changes it.

LangGraph merges each node's returned dict into the state schema, so anything in `RunState` has to
be mergeable. A `TraceLog` is not: it is append-only and shared, and two nodes returning it would
race to overwrite each other with divergent copies. So the recorders live here, are passed
alongside, and are appended to rather than returned.

## Why the usage checkpoint exists

`UsageLedger` accumulates across a whole run. PRD-89 asks for per-node token counts, which is a
different question: a node's figures are what *it* spent, not what the run had spent by the time it
finished. `checkpoint()` remembers the totals on entry and `since_checkpoint()` subtracts, so
`claim_parse` reports the mapping call and `publish` reports the narrative calls, rather than both
reporting a running total that only makes sense if you read them in order and do the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tda.agents.runtime import AgentRunner
from tda.agents.tools import ToolRegistry
from tda.contracts import FindingIds
from tda.obs.nodes import NodeLog
from tda.obs.trace import TraceLog
from tda.obs.usage import UsageLedger

if TYPE_CHECKING:
    from tda.agents.provider.base import LLMProvider
    from tda.contracts import Period
    from tda.policy import Policy


@dataclass(frozen=True, slots=True)
class UsageDelta:
    """What one node spent. Zeroes for a node that called no model, which is four of the five."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class RunContext:
    """One run's ruleset, model layer and recorders.

    Not frozen, because `_checkpoint` moves as the run proceeds. Everything a node might mistake
    for state is read-only in practice: no node assigns to `policy`, `runner` or `period`, and a
    node that wanted to would be changing the rules half way through a verification.
    """

    policy: Policy
    period: Period
    provider: LLMProvider
    runner: AgentRunner
    trace: TraceLog = field(default_factory=TraceLog)
    usage: UsageLedger = field(default_factory=UsageLedger)
    nodes: NodeLog = field(default_factory=NodeLog)
    finding_ids: FindingIds = field(default_factory=FindingIds)
    _checkpoint: UsageDelta = field(default_factory=UsageDelta)
    _trace_mark: int = 0

    @property
    def provider_mode(self) -> str:
        """Recorded in the verdict, because a number produced against a live model and one
        replayed from a cassette are different kinds of evidence."""
        return self.provider.mode.value

    def _totals(self) -> UsageDelta:
        per_agent = self.usage.per_agent()
        return UsageDelta(
            calls=sum(u.calls for u in per_agent),
            input_tokens=sum(u.input_tokens for u in per_agent),
            output_tokens=sum(u.output_tokens for u in per_agent),
        )

    def checkpoint(self) -> None:
        """Remember what the ledger and the trace say now. Called on every node entry."""
        self._checkpoint = self._totals()
        self._trace_mark = len(self.trace)

    def prompt_versions_since_checkpoint(self) -> dict[str, str]:
        """The prompt versions used by **this node**, not by the run so far.

        `TraceLog.prompt_versions()` folds the whole trace, so reading it at a node's exit gives
        every node after `claim_parse` the mapping agent's version — a record that says the
        reconciliation node ran a prompt it has no way of running. These records are persisted to
        `artifacts/<run_id>/`, so that misattribution would outlive the run.
        """
        return {
            record.agent: record.prompt_version for record in self.trace.records[self._trace_mark :]
        }

    def since_checkpoint(self) -> UsageDelta:
        """What has been spent since the last checkpoint — this node's own figures."""
        now = self._totals()
        return UsageDelta(
            calls=now.calls - self._checkpoint.calls,
            input_tokens=now.input_tokens - self._checkpoint.input_tokens,
            output_tokens=now.output_tokens - self._checkpoint.output_tokens,
        )

    def runner_for(self, registry: ToolRegistry) -> AgentRunner:
        """A runner over this run's provider and recorders, with a registry bound to one agent's data.

        Needed because a tool registry is **per workbook, per finding** — `tda.agents.tools` refuses
        to hold state across runs for exactly this reason, and the mapping agent's tools cannot be
        built until the node that opens the workbook has opened it. What must *not* be per node is
        the trace and the ledger: those are the run's, and a node that quietly made its own would
        report its own calls into a log nobody reads.
        """
        return AgentRunner(
            self.provider,
            policy=self.policy,
            registry=registry,
            trace=self.trace,
            usage=self.usage,
        )

    @classmethod
    def build(cls, policy: Policy, period: Period, provider: LLMProvider) -> RunContext:
        """Assemble a context around a provider, wiring the default runner to the same recorders.

        The runner and the context must share one `TraceLog` and one `UsageLedger`, or the per-node
        figures would be computed from a ledger nobody was writing to — reporting zero for every
        node and being wrong quietly rather than loudly.

        The default runner carries an empty registry, which is right for an agent whose allowlist is
        empty (the critic) and wrong for every other one. Those get `runner_for`.
        """
        trace = TraceLog()
        usage = UsageLedger()
        return cls(
            policy=policy,
            period=period,
            provider=provider,
            runner=AgentRunner(
                provider, policy=policy, registry=ToolRegistry(), trace=trace, usage=usage
            ),
            trace=trace,
            usage=usage,
        )
