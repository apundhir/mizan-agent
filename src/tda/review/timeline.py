"""One run, as a sequence of events a page can animate - built from exactly what a run writes.

Four kinds of thing happen during a run, in the order they happened: a node is entered, an agent is
routed (granted, skipped or refused), an agent is called, a node is exited. `build_timeline` walks
`nodes.jsonl`, `trace.jsonl` and `routing.jsonl` - or the live equivalents, `RunContext.nodes`,
`.trace` and `.supervisor.decisions`, while a run is still in progress - and produces one ordered
`TimelineEvent` sequence that a live view and a replay view both render through the same code.

## The join is the same arithmetic `tda.obs.viewer` uses, walked differently

`NodeRecord.model_calls` (on the exit record) says how many trace records a node made; the graph is
strictly sequential (ADR-0005), so consuming the trace in that count, in order, assigns every
record to exactly one node - see `tda.obs.viewer`'s own module docstring for the argument and its
one stated limit. This module keeps that arithmetic rather than importing it, because the tree
viewer groups a node with all of its calls under one line and this one needs each call and each
routing decision as its own event, in between an enter and an exit that are themselves events.

## Two clocks: `at_ms` is true, `display_ms` is for the eye

`at_ms` is the real offset from the run's own start, in milliseconds, as recorded. A replay run
completes in about a second - too fast to watch a stage light up - so `display_ms` stretches every
node and call to at least a floor (`NODE_MIN_MS`, `CALL_MIN_MS`) for the player to animate against,
while `at_ms` stays what actually happened. A card showing a call's cost shows `duration_ms` off the
trace record, never `display_ms`; the stretch is a presentation device, not a claim about timing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from tda.agents.roster import ROSTER
from tda.graph.nodes import NODE_ORDER
from tda.obs.nodes import NodeOutcome, Phase

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from tda.obs.nodes import NodeRecord
    from tda.obs.routing import RoutingRecord
    from tda.obs.trace import TraceRecord
    from tda.review.runner import RunJob

# Floors so a fast replay call is still visible to animate against. Real for a live run - a call
# that actually took 4 seconds is never sped up, only a call that took 4 milliseconds is stretched.
NODE_MIN_MS: Final = 400
CALL_MIN_MS: Final = 250

REVIEW_NODE: Final = "review"
UNATTRIBUTED_NODE: Final = "unattributed"

_OUTCOME_TO_CHIP: Final[dict[NodeOutcome, str]] = {
    NodeOutcome.OK: "ok",
    NodeOutcome.HALTED: "halted",
    NodeOutcome.REJECTED: "rejected",
    NodeOutcome.FAILED: "failed",
}


class EventKind(StrEnum):
    NODE_ENTER = "node_enter"
    NODE_EXIT = "node_exit"
    ROUTE_DECISION = "route_decision"
    AGENT_CALL = "agent_call"
    VERDICT = "verdict"


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One thing that happened, in the order it happened.

    `node`, `agent`, `outcome` and `detail` are filled in as each kind needs them and left at their
    defaults otherwise - a `VERDICT` event has no `agent`, a `NODE_ENTER` has no `call`. A renderer
    switches on `kind` and reads only the fields that kind promises.
    """

    sequence: int
    kind: EventKind
    at_ms: int
    display_ms: int
    node: str
    agent: str | None = None
    outcome: str | None = None
    detail: str | None = None
    duration_ms: int = 0
    call: TraceRecord | None = None
    routing: RoutingRecord | None = None


@dataclass(frozen=True, slots=True)
class Timeline:
    events: tuple[TimelineEvent, ...]
    total_ms: int
    total_display_ms: int
    finished: bool

    def upto(self, display_ms: int | None) -> tuple[TimelineEvent, ...]:
        """Every event whose `display_ms` has arrived by `display_ms`. `None` means all of them -
        the final state, not a point mid-animation."""
        if display_ms is None:
            return self.events
        return tuple(event for event in self.events if event.display_ms <= display_ms)

    def chip_states(self, display_ms: int | None) -> dict[str, str]:
        """One of `pending`, `running`, `ok`, `halted`, `rejected`, `failed`, `skipped` for every
        node in `NODE_ORDER`, as of `display_ms` (`None` for the final state)."""
        visible = self.upto(display_ms)
        entered = {e.node for e in visible if e.kind is EventKind.NODE_ENTER}
        exits = {e.node: e for e in visible if e.kind is EventKind.NODE_EXIT}
        at_the_end = display_ms is None or display_ms >= self.total_display_ms

        states: dict[str, str] = {}
        for node in NODE_ORDER:
            if node in exits:
                states[node] = exits[node].outcome or "ok"
            elif node in entered:
                states[node] = "running"
            elif self.finished and at_the_end:
                states[node] = "skipped"
            else:
                states[node] = "pending"
        return states

    def calls_for(self, agent: str, display_ms: int | None = None) -> tuple[TimelineEvent, ...]:
        return tuple(
            e for e in self.upto(display_ms) if e.kind is EventKind.AGENT_CALL and e.agent == agent
        )

    def decisions(self, display_ms: int | None = None) -> tuple[TimelineEvent, ...]:
        return tuple(e for e in self.upto(display_ms) if e.kind is EventKind.ROUTE_DECISION)


def _stretch(real_ms: int, floor: int) -> int:
    return max(real_ms, floor)


def _runs_in_review(agent: str) -> bool:
    entry = ROSTER.get(agent)
    return entry is not None and entry.runs_in == REVIEW_NODE


def build_timeline(
    nodes: Sequence[NodeRecord],
    trace: Sequence[TraceRecord],
    routing: Sequence[RoutingRecord],
    *,
    status: str | None = None,
) -> Timeline:
    """Assemble one run's timeline from its three logs (or their live, in-progress equivalents).

    `status` is the verdict's status once there is one - `None` while the run is still going, so
    `chip_states` can tell "not reached yet" apart from "the run is over and this was skipped".
    """
    events: list[TimelineEvent] = []
    sequence = 0
    clock = 0
    display_clock = 0
    trace_cursor = 0
    entered_at: dict[str, tuple[int, int]] = {}  # node -> (real clock, display clock) at entry

    def emit(**kwargs: object) -> None:
        nonlocal sequence
        events.append(TimelineEvent(sequence=sequence, **kwargs))  # type: ignore[arg-type]
        sequence += 1

    def emit_calls_and_decisions(
        node: str, count: int, *, real_start: int, display_start: int
    ) -> None:
        nonlocal trace_cursor, clock, display_clock
        for decision in [r for r in routing if r.node == node]:
            emit(
                kind=EventKind.ROUTE_DECISION,
                at_ms=real_start,
                display_ms=display_start,
                node=node,
                agent=decision.agent,
                outcome=decision.disposition,
                detail=decision.reason,
                routing=decision,
            )
        real_offset = real_start
        display_offset = display_start
        for _ in range(count):
            if trace_cursor >= len(trace):
                break
            call = trace[trace_cursor]
            trace_cursor += 1
            emit(
                kind=EventKind.AGENT_CALL,
                at_ms=real_offset,
                display_ms=display_offset,
                node=node,
                agent=call.agent,
                outcome="failed" if call.failed else "ok",
                detail=call.error,
                duration_ms=call.duration_ms,
                call=call,
            )
            real_offset += call.duration_ms
            display_offset += _stretch(call.duration_ms, CALL_MIN_MS)
        clock = max(clock, real_offset)
        display_clock = max(display_clock, display_offset)

    for record in nodes:
        if record.phase is Phase.ENTER:
            entered_at[record.node] = (clock, display_clock)
            emit(
                kind=EventKind.NODE_ENTER,
                at_ms=clock,
                display_ms=display_clock,
                node=record.node,
            )
            continue

        # EXIT. A node with no matching ENTER cannot happen (every writer pairs them), so this
        # trusts the log rather than re-deriving entry from nothing.
        real_start, display_start = entered_at.get(record.node, (clock, display_clock))
        emit_calls_and_decisions(
            record.node, record.model_calls, real_start=real_start, display_start=display_start
        )
        real_exit = real_start + record.duration_ms
        display_exit = display_start + _stretch(record.duration_ms, NODE_MIN_MS)
        emit(
            kind=EventKind.NODE_EXIT,
            at_ms=real_exit,
            display_ms=display_exit,
            node=record.node,
            outcome=_OUTCOME_TO_CHIP.get(record.outcome, record.outcome.value),
            detail=record.detail,
            duration_ms=record.duration_ms,
        )
        clock = real_exit
        display_clock = display_exit

    # A node still open (entered, never exited - a run still in progress) absorbs whatever calls
    # have been made since it opened; nothing else in a sequential graph could have made them.
    unfinished = [
        node
        for node in entered_at
        if node not in {e.node for e in events if e.kind is EventKind.NODE_EXIT}
    ]
    if unfinished and trace_cursor < len(trace):
        node = unfinished[-1]
        real_start, display_start = entered_at[node]
        emit_calls_and_decisions(
            node, len(trace) - trace_cursor, real_start=real_start, display_start=display_start
        )

    # Calls made after the graph finished - the review screen's assistant, asked once a verdict
    # exists. Never lost: shown as their own kind of aftermath rather than folded into a node that
    # did not make them.
    while trace_cursor < len(trace):
        call = trace[trace_cursor]
        trace_cursor += 1
        node = REVIEW_NODE if _runs_in_review(call.agent) else UNATTRIBUTED_NODE
        emit(
            kind=EventKind.AGENT_CALL,
            at_ms=clock,
            display_ms=display_clock,
            node=node,
            agent=call.agent,
            outcome="failed" if call.failed else "ok",
            detail=call.error,
            duration_ms=call.duration_ms,
            call=call,
        )
        clock += call.duration_ms
        display_clock += _stretch(call.duration_ms, CALL_MIN_MS)

    finished = status is not None
    if finished:
        emit(kind=EventKind.VERDICT, at_ms=clock, display_ms=display_clock, node="", outcome=status)

    return Timeline(
        events=tuple(events), total_ms=clock, total_display_ms=display_clock, finished=finished
    )


def timeline_from_run(run_dir: Path) -> Timeline:
    """Rebuild a past run's timeline from its four artifacts on disk."""
    from tda.obs.artifacts import read_routing, read_run

    ledger, trace, nodes = read_run(run_dir)
    routing = read_routing(run_dir)
    return build_timeline(nodes.records, trace.records, routing.records, status=ledger.status)


def timeline_from_job(job: RunJob) -> Timeline:
    """A live or just-finished job's timeline, read off its own `RunContext` - the same lists the
    worker thread is appending to, snapshotted at the moment this is called.

    `status` is `None` while `job` is still queued or running, and `job.status` once it is done -
    the same distinction `build_timeline` uses everywhere else, between "not reached yet" and "the
    run is over and this was skipped".
    """
    from tda.obs.routing import routing_records

    return build_timeline(
        job.context.nodes.records,
        job.context.trace.records,
        routing_records(job.context.supervisor.decisions).records,
        status=job.status if job.done else None,
    )
