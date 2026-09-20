"""One record per supervisor decision: which agent was asked to run, and what was decided.

`tda.agents.supervisor.RoutingDecision` is the in-memory record `Supervisor.route()` produces. This
module is its serialised form, `routing.jsonl`, the fourth file a run writes alongside `run.json`,
`trace.jsonl` and `nodes.jsonl`.

## Why this is not `tda.agents.supervisor.RoutingDecision` written straight to disk

`tda.obs` must not import `tda.agents`. `tda.agents.runtime` already imports `tda.obs.trace`, and
the reverse import would be a cycle. So this module carries its own record, `RoutingRecord`, shaped
like `RoutingDecision` but independent of it, and `routing_records()` converts by duck typing
(`.node`, `.agent`, `.disposition`, `.reason`) rather than by importing the class it converts from.

## Why a run without this file is not a broken run

`routing.jsonl` did not exist before v0.6.0. `read_routing` reads a missing file as an empty log,
not as an error, unlike `trace.jsonl` and `nodes.jsonl`, which `read_run` requires (see
`tda.obs.artifacts`). Those two describe *what happened*; an absent one means the directory was
truncated. This one describes *why*, added after the fact, and an older run simply has nothing to
say about it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

ROUTING_LOG = "routing.jsonl"


class HasRoutingDecision(Protocol):
    """What `routing_records()` needs from a decision, without naming its class.

    `tda.agents.supervisor.RoutingDecision` satisfies this today. So would anything else with the
    same four attributes: the point of a duck-typed boundary is that this module never has to
    know which one it was handed.

    Read-only properties rather than plain attributes, so mypy checks structural compatibility
    covariantly: `RoutingDecision.disposition` is a `Disposition` (a `StrEnum`, a subtype of
    `object`), and a mutable attribute typed `object` would reject that under strict mode's
    invariance rule for writable fields.
    """

    @property
    def node(self) -> str: ...
    @property
    def agent(self) -> str: ...
    @property
    def disposition(self) -> object: ...
    @property
    def reason(self) -> str: ...


class RoutingRecord(BaseModel):
    """One decision, redacted and ready to write.

    Frozen, like `TraceRecord` and `NodeRecord`: a statement about something that already happened.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    disposition: str = Field(min_length=1)
    reason: str

    def render(self) -> str:
        return f"{self.node} -> {self.agent}: {self.disposition} ({self.reason})"


class RoutingLog:
    """Every routing record from one run, in the order the supervisor produced them.

    Shaped like `tda.obs.nodes.NodeLog`, deliberately: the three logs are read the same way, and a
    fourth with a different shape would be a fourth thing to learn rather than the same thing again.
    """

    def __init__(self, records: Iterable[RoutingRecord] = ()) -> None:
        self._records: list[RoutingRecord] = list(records)

    def append(self, record: RoutingRecord) -> None:
        self._records.append(record)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[RoutingRecord]:
        return iter(self._records)

    @property
    def records(self) -> tuple[RoutingRecord, ...]:
        return tuple(self._records)

    def for_node(self, node: str) -> tuple[RoutingRecord, ...]:
        return tuple(r for r in self._records if r.node == node)

    def refusals(self) -> tuple[RoutingRecord, ...]:
        return tuple(r for r in self._records if r.disposition.startswith("refused"))

    def to_jsonl(self) -> str:
        return "".join(record.model_dump_json() + "\n" for record in self._records)

    @classmethod
    def from_jsonl(cls, text: str) -> RoutingLog:
        return cls(
            RoutingRecord.model_validate_json(line) for line in text.splitlines() if line.strip()
        )

    def render(self) -> str:
        return "\n".join(record.render() for record in self._records)


def routing_records(decisions: Iterable[HasRoutingDecision]) -> RoutingLog:
    """`Supervisor.decisions` (or anything shaped like it), converted without importing `tda.agents`.

    `disposition` is read as `.value` when it has one (a `StrEnum`, which is what
    `RoutingDecision.disposition` actually is) and as `str(...)` otherwise, so this does not require
    the caller's disposition type to be any particular enum. It only has to print sensibly.
    """
    return RoutingLog(
        RoutingRecord(
            node=d.node,
            agent=d.agent,
            disposition=getattr(d.disposition, "value", str(d.disposition)),
            reason=d.reason,
        )
        for d in decisions
    )
