"""One record per node entry and per node exit — including the nodes no model ever touches.

`TraceRecord` is the wrong shape for this and deliberately so. It requires a `prompt_version`
matching `^v\\d+$`, a non-empty `model_id`, an `effort` and an `output_contract`, because it
describes **one model call**. Four of the graph's five nodes make no model call at all, and the
honest way to record `recompute_reconcile` is not to invent a prompt version for it.

So: `TraceRecord` answers *"what did this agent say, and why?"*, and `NodeRecord` answers *"what
did this run do, in what order, and where did it stop?"*. A run produces both, and the two are
joined by nothing more clever than time order.

## Why two records per node rather than one

A node that halts mid-way leaves an ENTER with no EXIT. That asymmetry is the entire point: a
single record written on completion cannot describe a node that did not complete, and the run would
go quiet exactly where a reader most needs it to speak. Pairing them also makes the arithmetic
checkable — a reader can add the node durations and compare against the run, rather than trusting a
number this module computed.

## Why the model figures are carried here too

`NodeRecord` carries token counts and prompt versions for the node's *whole* turn, which is the
sum over whatever model calls it made. That is a different question from the per-call one
`TraceRecord` answers, and the orchestrated graph asks for it explicitly: an officer reading a run wants to know
which **stage** cost what, without reconstructing it from a call log. For a code node both are
empty, and empty is the correct answer rather than a missing one.

## What this is not

Not a span, not a tracer, not a context manager that swallows exceptions. `record_exit` is called
on the failure path as well as the success path, and the exception keeps propagating — see
`tda.graph.nodes`. A recorder that caught what it observed would be the last thing to report a
problem and the first to hide one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from tda.obs.repro import REPRO_EXCLUDED

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


class Phase(StrEnum):
    """Which half of a node's turn this record describes."""

    ENTER = "enter"
    EXIT = "exit"


class NodeOutcome(StrEnum):
    """How a node's turn ended.

    `HALTED` and `REJECTED` are distinct from `FAILED` because they are *decisions* rather than
    defects: a submission that cannot be read is halted on purpose, and one that fails intake is
    rejected on purpose. Collapsing all three into "failed" would make a correct refusal
    indistinguishable from a crash in every report that followed.
    """

    RUNNING = "running"
    OK = "ok"
    HALTED = "halted"
    REJECTED = "rejected"
    FAILED = "failed"


class NodeRecord(BaseModel):
    """One node entry or exit.

    Frozen, like `TraceRecord` and for the same reason: it is a statement about something that
    already happened, and a mutable one invites a later stage to tidy it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    phase: Phase
    outcome: NodeOutcome = NodeOutcome.RUNNING
    duration_ms: int = Field(
        default=0,
        ge=0,
        description=f"Zero on entry; the node's total on exit. {REPRO_EXCLUDED}",
    )
    prompt_versions: dict[str, str] = Field(
        default_factory=dict,
        description="Agent name -> prompt version, for the agents this node called. Empty for a "
        "code node, and empty is the right answer there rather than a missing one.",
    )
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    detail: str | None = Field(
        default=None,
        description="Why a node ended the way it did. Required for anything but OK and RUNNING - "
        "a halt with no stated reason is the thing this record exists to prevent.",
    )

    def model_post_init(self, _context: object, /) -> None:
        """A node that did not simply succeed must say why.

        The failure this closes is a run that halts, records the halt, and leaves the reader to
        work out which of a dozen blocking conditions fired.
        """
        if self.outcome in (NodeOutcome.HALTED, NodeOutcome.REJECTED, NodeOutcome.FAILED) and not (
            self.detail
        ):
            raise ValueError(
                f"node {self.node!r} ended {self.outcome.value} with no stated reason. A halt "
                "nobody can explain is indistinguishable from a crash in every report that "
                "follows it."
            )

    @property
    def finished(self) -> bool:
        return self.phase is Phase.EXIT

    def render(self) -> str:
        tokens = (
            f" tok={self.input_tokens}/{self.output_tokens} calls={self.model_calls}"
            if self.model_calls
            else ""
        )
        suffix = f" - {self.detail}" if self.detail else ""
        if self.phase is Phase.ENTER:
            return f"{self.node} enter"
        return f"{self.node} exit {self.outcome.value} {self.duration_ms}ms{tokens}{suffix}"


class NodeLog:
    """Every node record from one run, in the order the run produced them.

    Order is the content, as in `TraceLog`: it is the sequence that tells a reader where a run got
    to. Nothing here sorts or deduplicates.
    """

    def __init__(self, records: Iterable[NodeRecord] = ()) -> None:
        self._records: list[NodeRecord] = list(records)

    def append(self, record: NodeRecord) -> None:
        self._records.append(record)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[NodeRecord]:
        return iter(self._records)

    @property
    def records(self) -> tuple[NodeRecord, ...]:
        return tuple(self._records)

    def entered(self) -> tuple[str, ...]:
        """Every node the run entered, in order."""
        return tuple(r.node for r in self._records if r.phase is Phase.ENTER)

    def completed(self) -> tuple[str, ...]:
        return tuple(r.node for r in self._records if r.phase is Phase.EXIT)

    def unfinished(self) -> tuple[str, ...]:
        """Nodes that were entered and never left.

        In a healthy run this is empty. When it is not, it names exactly where the run stopped,
        which is the question a reader of a broken run opens the file to answer.
        """
        exits = {r.node for r in self._records if r.phase is Phase.EXIT}
        return tuple(
            r.node for r in self._records if r.phase is Phase.ENTER and r.node not in exits
        )

    def total_duration_ms(self) -> int:
        return sum(r.duration_ms for r in self._records if r.phase is Phase.EXIT)

    def to_jsonl(self) -> str:
        """One JSON object per line, in order. Readable when a run dies halfway, which is when
        somebody reads it."""
        return "".join(record.model_dump_json() + "\n" for record in self._records)

    @classmethod
    def from_jsonl(cls, text: str) -> NodeLog:
        return cls(
            NodeRecord.model_validate_json(line) for line in text.splitlines() if line.strip()
        )

    def render(self) -> str:
        return "\n".join(record.render() for record in self._records)
