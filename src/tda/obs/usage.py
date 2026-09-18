"""Token and cost accounting, per call and per agent.

Lives in `obs` rather than in the agents package on purpose. Token counts are integers, and an
integer on an agent output contract is exactly what the schema lint forbids - so putting telemetry
there would force a choice between weakening the guard and mislabelling the data. A count of what
a call cost is not an agent's answer, and keeping the two apart is what lets the guard stay strict.

Prices are per million tokens and are a **configured input, not a fact**. They go stale, so they
are stamped into the ledger alongside the numbers they produced: a cost figure without the rate
card that generated it cannot be reconciled later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from tda.obs.repro import REPRO_EXCLUDED

# Per million tokens, Anthropic Console API, recorded 2026-09-13. Stamped into every ledger.
PRICING_VERSION = "2026-09-13"
INPUT_PER_MTOK = Decimal("5.00")
OUTPUT_PER_MTOK = Decimal("25.00")
CACHE_READ_PER_MTOK = Decimal("0.50")

_MILLION = Decimal(1_000_000)

# Every cost in this system is reported to four decimal places - the ledger, the console
# summary and the trace tree alike. Rounding once, here, is what makes the rows add up to
# the total wherever they are displayed.
CENT_FRACTION = Decimal("0.0001")


class AgentUsage(BaseModel):
    """Rolled-up usage for one agent across a run.

    `calls`, `input_tokens` and the rest are integers, which is why this type is deliberately not
    an `AgentOutput`: see the module docstring.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str = Field(min_length=1)
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    duration_ms: int = Field(
        ge=0, description=f"Wall clock across this agent's calls. {REPRO_EXCLUDED}"
    )

    @property
    def cost_usd(self) -> Decimal:
        """What this agent cost, rounded to the cent-fraction everything else reports.

        Decimal, not float: a cost rolled up in binary floating point drifts in the cents, and a
        figure that does not reconcile is worse than no figure.

        **Rounded here rather than at the point of printing**, and that is the whole reason this
        property exists in this shape. An earlier version returned the exact quotient and let each
        caller format it, so seven calls displayed as $0.0001 each under a total of $0.0009 - which
        is precisely the non-reconciling figure the paragraph above says must not happen. A reader
        who adds the column up and gets a different answer stops trusting every number on the page,
        including the ones that were right.
        """
        exact = (
            Decimal(self.input_tokens) * INPUT_PER_MTOK
            + Decimal(self.output_tokens) * OUTPUT_PER_MTOK
            + Decimal(self.cache_read_tokens) * CACHE_READ_PER_MTOK
        ) / _MILLION
        return exact.quantize(CENT_FRACTION)


@dataclass(slots=True)
class UsageLedger:
    """Accumulates usage as a run proceeds.

    Mutable by design - it is a tally, not a record of a decision. The immutable artefact is the
    `AgentUsage` set it produces at the end, which is what reaches `run.json`.
    """

    _calls: dict[str, list[tuple[int, int, int, int]]] = field(default_factory=dict)

    def record(
        self,
        agent: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        duration_ms: int = 0,
    ) -> None:
        self._calls.setdefault(agent, []).append(
            (input_tokens, output_tokens, cache_read_tokens, duration_ms)
        )

    def per_agent(self) -> list[AgentUsage]:
        """Sorted by agent name, so `run.json` is byte-stable across runs and `make repro` is
        comparing the pipeline rather than dict ordering."""
        return [
            AgentUsage(
                agent=agent,
                calls=len(calls),
                input_tokens=sum(c[0] for c in calls),
                output_tokens=sum(c[1] for c in calls),
                cache_read_tokens=sum(c[2] for c in calls),
                duration_ms=sum(c[3] for c in calls),
            )
            for agent, calls in sorted(self._calls.items())
        ]

    def total_cost_usd(self) -> Decimal:
        """The sum of the per-agent figures **as displayed**, not of the exact quotients.

        Summing the unrounded values would produce a total that does not equal the column above it,
        and a reader who adds the column up and gets a different answer has found a reason to
        distrust every figure on the page.
        """
        return sum((u.cost_usd for u in self.per_agent()), start=Decimal(0))

    def total_calls(self) -> int:
        return sum(len(calls) for calls in self._calls.values())
