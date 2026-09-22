"""Token and cost accounting, per call and per agent.

Lives in `obs` rather than in the agents package on purpose. Token counts are integers, and an
integer on an agent output contract is exactly what the schema lint forbids - so putting telemetry
there would force a choice between weakening the guard and mislabelling the data. A count of what
a call cost is not an agent's answer, and keeping the two apart is what lets the guard stay strict.

Prices are per million tokens and are a **configured input, not a fact**. No rate card ships with
this repository. Rates go stale, they differ per account and per provider, and a number committed
here would be quietly wrong for most readers within a quarter. The operator supplies them through
the environment, the same way they supply a key, and whatever they supply is stamped into the
ledger alongside the figures it produced: a cost without the rate card that generated it cannot be
reconciled later.

With no rates configured, token accounting still runs and cost is reported as **absent**. Absent is
not zero. Zero is a real and common cost, because every replay run costs nothing, so a zero standing
in for "unknown" would be indistinguishable from the truth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from tda.obs.repro import REPRO_EXCLUDED

_MILLION = Decimal(1_000_000)

_RATE_VARS = (
    "MIZAN_RATE_VERSION",
    "MIZAN_RATE_INPUT_PER_MTOK",
    "MIZAN_RATE_OUTPUT_PER_MTOK",
    "MIZAN_RATE_CACHE_READ_PER_MTOK",
)

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


class RateCard(BaseModel):
    """What one operator's tokens cost, per million, and the version that says when.

    Not committed. Built by `from_env` from whatever the operator exported, so this repository
    never ships a price that will be wrong for the reader.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cache_read_per_mtok: Decimal = Field(ge=0)

    @classmethod
    def from_env(cls) -> RateCard | None:
        """All four variables or none of them.

        A partial rate card raises rather than filling the gap with a zero. Three rates and a
        missing fourth would silently under-report every run that used the missing one, which is
        the kind of wrong number that survives a review because it looks plausible.
        """
        present = {name: os.environ.get(name, "").strip() for name in _RATE_VARS}
        supplied = {name: value for name, value in present.items() if value}
        if not supplied:
            return None
        missing = [name for name in _RATE_VARS if name not in supplied]
        if missing:
            raise ValueError(
                f"Incomplete rate card: {', '.join(missing)} not set. Set all of "
                f"{', '.join(_RATE_VARS)} or none of them."
            )
        try:
            return cls(
                version=supplied["MIZAN_RATE_VERSION"],
                input_per_mtok=Decimal(supplied["MIZAN_RATE_INPUT_PER_MTOK"]),
                output_per_mtok=Decimal(supplied["MIZAN_RATE_OUTPUT_PER_MTOK"]),
                cache_read_per_mtok=Decimal(supplied["MIZAN_RATE_CACHE_READ_PER_MTOK"]),
            )
        except InvalidOperation as exc:
            raise ValueError(f"Rate card values must be decimal numbers: {exc}") from exc

    def cost_of(self, usage: AgentUsage) -> Decimal:
        """What one agent cost, rounded to the cent-fraction everything else reports.

        Decimal, not float: a cost rolled up in binary floating point drifts in the cents, and a
        figure that does not reconcile is worse than no figure.

        **Rounded here rather than at the point of printing**, and that is the whole reason this
        method exists in this shape. An earlier version returned the exact quotient and let each
        caller format it, so seven calls each displayed a rounded 0.0001 under a total of 0.0009, which
        is precisely the non-reconciling figure the paragraph above says must not happen. A reader
        who adds the column up and gets a different answer stops trusting every number on the page,
        including the ones that were right.
        """
        exact = (
            Decimal(usage.input_tokens) * self.input_per_mtok
            + Decimal(usage.output_tokens) * self.output_per_mtok
            + Decimal(usage.cache_read_tokens) * self.cache_read_per_mtok
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

    def total_cost_usd(self, rates: RateCard | None) -> Decimal | None:
        """The sum of the per-agent figures **as displayed**, not of the exact quotients.

        Summing the unrounded values would produce a total that does not equal the column above it,
        and a reader who adds the column up and gets a different answer has found a reason to
        distrust every figure on the page.

        `None` when no rate card is configured, never `Decimal(0)`. A run that made no calls really
        did cost zero, and a run whose price is unknown did not, so the two cannot share a value.
        """
        if rates is None:
            return None
        return sum((rates.cost_of(u) for u in self.per_agent()), start=Decimal(0))

    def total_calls(self) -> int:
        return sum(len(calls) for calls in self._calls.values())


def spend_line(usage: UsageLedger, rates: RateCard | None) -> str:
    """One line for a recording script's summary: what this cost, or why it cannot say.

    `make record` is the one target that spends money, so it is also the one place a reader most
    wants a figure. When no rate card is exported it says so plainly rather than printing a zero
    that would read as "this was free".
    """
    total = usage.total_cost_usd(rates)
    if rates is None or total is None:
        return f"Cost unknown: set {', '.join(_RATE_VARS)} to price this run."
    return f"Cost ${total:.4f} at the rates you exported (version {rates.version})."
