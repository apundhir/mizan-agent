"""The timeline: nodes, routing decisions and calls, merged into one ordered sequence.

**The join is exact, not a guess.** `tda.obs.viewer`'s own claim - a node's `model_calls` count,
consumed against the trace in order, assigns every record to exactly one node because the graph is
strictly sequential - is exercised here the same way, over a shape built for a page rather than a
tree.

**A past run and the live view agree**, checked by building a timeline both ways from one real
pipeline run: once from the artifacts it wrote, once from the `RunContext` still in memory.
"""

from __future__ import annotations

from pathlib import Path

from tda.agents.provider import ReplayProvider
from tda.contracts import Period
from tda.obs.nodes import NodeOutcome, NodeRecord, Phase
from tda.obs.routing import RoutingRecord
from tda.obs.trace import TraceRecord
from tda.policy import load_policy
from tda.review.runner import execute, prepare_job
from tda.review.timeline import (
    CALL_MIN_MS,
    NODE_MIN_MS,
    REVIEW_NODE,
    UNATTRIBUTED_NODE,
    EventKind,
    build_timeline,
    timeline_from_job,
    timeline_from_run,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SUBMISSION = REPO_ROOT / "corpus" / "demo" / "submission"


def node(
    name: str,
    phase: Phase,
    *,
    duration_ms: int = 0,
    outcome: NodeOutcome = NodeOutcome.OK,
    model_calls: int = 0,
    detail: str | None = None,
) -> NodeRecord:
    return NodeRecord(
        node=name,
        phase=phase,
        outcome=outcome if phase is Phase.EXIT else NodeOutcome.RUNNING,
        duration_ms=duration_ms,
        model_calls=model_calls,
        detail=detail,
    )


def call(agent: str, *, duration_ms: int = 100, failed: bool = False) -> TraceRecord:
    return TraceRecord(
        agent=agent,
        prompt_version="v1",
        model_id="claude-sonnet-5",
        effort="low",
        provider_mode="replay",
        cassette_key="abc123",
        output_contract="WorkbookMapping",
        output_json=None if failed else '{"blocks": []}',
        error="boom" if failed else None,
        duration_ms=duration_ms,
    )


def decision(node_name: str, agent: str, *, disposition: str = "granted") -> RoutingRecord:
    return RoutingRecord(node=node_name, agent=agent, disposition=disposition, reason="requested")


# ── the join ───────────────────────────────────────────────────────────────────


def test_events_follow_append_order_and_offsets_add_up() -> None:
    nodes = [
        node("intake", Phase.ENTER),
        node("intake", Phase.EXIT, duration_ms=10),
        node("claim_parse", Phase.ENTER),
        node("claim_parse", Phase.EXIT, duration_ms=50, model_calls=1),
    ]
    trace = [call("mapping", duration_ms=40)]
    routing = [decision("claim_parse", "mapping")]

    timeline = build_timeline(nodes, trace, routing, status="PASS")

    kinds = [e.kind for e in timeline.events]
    assert kinds == [
        EventKind.NODE_ENTER,
        EventKind.NODE_EXIT,
        EventKind.NODE_ENTER,
        EventKind.ROUTE_DECISION,
        EventKind.AGENT_CALL,
        EventKind.NODE_EXIT,
        EventKind.VERDICT,
    ]
    assert [e.sequence for e in timeline.events] == list(range(len(timeline.events)))

    intake_enter, intake_exit, claim_enter, route, agent_call, claim_exit, verdict = timeline.events
    assert intake_enter.at_ms == 0
    assert intake_exit.at_ms == 10
    assert claim_enter.at_ms == 10
    assert route.at_ms == 10
    assert agent_call.at_ms == 10
    assert claim_exit.at_ms == 60  # entered at 10, node duration 50
    assert verdict.at_ms == 60
    assert timeline.total_ms == 60


def test_calls_are_attributed_to_the_node_that_made_them() -> None:
    nodes = [
        node("claim_parse", Phase.ENTER),
        node("claim_parse", Phase.EXIT, duration_ms=10, model_calls=1),
        node("publish", Phase.ENTER),
        node("publish", Phase.EXIT, duration_ms=10, model_calls=2),
    ]
    trace = [call("mapping"), call("narrative"), call("critic")]

    timeline = build_timeline(nodes, trace, [], status="PASS")

    calls = [e for e in timeline.events if e.kind is EventKind.AGENT_CALL]
    assert [(c.node, c.agent) for c in calls] == [
        ("claim_parse", "mapping"),
        ("publish", "narrative"),
        ("publish", "critic"),
    ]


def test_a_decision_sits_before_the_call_it_granted() -> None:
    nodes = [
        node("claim_parse", Phase.ENTER),
        node("claim_parse", Phase.EXIT, duration_ms=10, model_calls=1),
    ]
    trace = [call("mapping")]
    routing = [decision("claim_parse", "mapping")]

    timeline = build_timeline(nodes, trace, routing, status="PASS")

    route_seq = next(e.sequence for e in timeline.events if e.kind is EventKind.ROUTE_DECISION)
    call_seq = next(e.sequence for e in timeline.events if e.kind is EventKind.AGENT_CALL)
    assert route_seq < call_seq


def test_a_skipped_decision_with_no_call_still_appears() -> None:
    """`resolution` skipped because nothing was unresolved - no call follows, and the decision is
    still on the timeline naming why."""
    nodes = [
        node("extract", Phase.ENTER),
        node("extract", Phase.EXIT, duration_ms=5, model_calls=0),
    ]
    routing = [decision("extract", "resolution", disposition="skipped")]

    timeline = build_timeline(nodes, [], routing, status="PASS")

    decisions = timeline.decisions()
    assert len(decisions) == 1
    assert decisions[0].outcome == "skipped"
    assert decisions[0].agent == "resolution"


# ── chip states ────────────────────────────────────────────────────────────────


def test_an_unfinished_node_shows_as_running_and_the_rest_as_pending() -> None:
    nodes = [
        node("intake", Phase.ENTER),
        node("intake", Phase.EXIT, duration_ms=5),
        node("extract", Phase.ENTER),
    ]

    timeline = build_timeline(nodes, [], [], status=None)

    states = timeline.chip_states(None)
    assert states["intake"] == "ok"
    assert states["extract"] == "running"
    assert states["claim_parse"] == "pending"
    assert states["recompute_reconcile"] == "pending"
    assert states["publish"] == "pending"


def test_a_rejected_run_marks_unreached_nodes_skipped() -> None:
    nodes = [
        node("intake", Phase.ENTER),
        node(
            "intake",
            Phase.EXIT,
            duration_ms=5,
            outcome=NodeOutcome.REJECTED,
            detail="incomplete file set",
        ),
    ]

    timeline = build_timeline(nodes, [], [], status="REJECTED")

    states = timeline.chip_states(None)
    assert states["intake"] == "rejected"
    for later in ("extract", "claim_parse", "recompute_reconcile", "publish"):
        assert states[later] == "skipped"


def test_a_halted_run_marks_the_node_it_halted_in_and_skips_the_rest() -> None:
    nodes = [
        node("intake", Phase.ENTER),
        node("intake", Phase.EXIT, duration_ms=1),
        node("extract", Phase.ENTER),
        node("extract", Phase.EXIT, duration_ms=5, outcome=NodeOutcome.HALTED, detail="D-NAT-12"),
    ]

    timeline = build_timeline(nodes, [], [], status="HALTED")

    states = timeline.chip_states(None)
    assert states["intake"] == "ok"
    assert states["extract"] == "halted"
    assert states["claim_parse"] == "skipped"


def test_chip_states_mid_animation_only_sees_what_has_arrived() -> None:
    nodes = [
        node("intake", Phase.ENTER),
        node("intake", Phase.EXIT, duration_ms=NODE_MIN_MS),
        node("extract", Phase.ENTER),
        node("extract", Phase.EXIT, duration_ms=NODE_MIN_MS),
    ]

    timeline = build_timeline(nodes, [], [], status="PASS")

    early = timeline.chip_states(1)
    assert early["intake"] == "running"
    assert early["extract"] == "pending"

    later = timeline.chip_states(timeline.total_display_ms)
    assert later["intake"] == "ok"
    assert later["extract"] == "ok"


# ── the display clock never collapses a fast step ────────────────────────────


def test_display_offsets_never_collapse_a_fast_node() -> None:
    nodes = [node("intake", Phase.ENTER), node("intake", Phase.EXIT, duration_ms=1)]

    timeline = build_timeline(nodes, [], [], status="PASS")

    exit_event = next(e for e in timeline.events if e.kind is EventKind.NODE_EXIT)
    assert exit_event.at_ms == 1  # the true offset is untouched
    assert exit_event.display_ms >= NODE_MIN_MS  # the shown offset has a floor


def test_a_fast_call_also_has_a_display_floor() -> None:
    nodes = [
        node("claim_parse", Phase.ENTER),
        node("claim_parse", Phase.EXIT, duration_ms=1000, model_calls=1),
    ]
    trace = [call("mapping", duration_ms=1)]

    timeline = build_timeline(nodes, trace, [], status="PASS")

    call_event = next(e for e in timeline.events if e.kind is EventKind.AGENT_CALL)
    assert call_event.at_ms == 0
    assert call_event.duration_ms == 1  # the card's own figure is never stretched
    assert call_event.display_ms >= 0
    exit_event = next(e for e in timeline.events if e.kind is EventKind.NODE_EXIT)
    assert exit_event.display_ms >= call_event.display_ms + CALL_MIN_MS


# ── calls after the graph: the review screen's assistant ─────────────────────


def test_review_time_questions_land_after_the_run() -> None:
    nodes = [node("intake", Phase.ENTER), node("intake", Phase.EXIT, duration_ms=1)]
    trace = [call("reviewer_assist")]

    timeline = build_timeline(nodes, trace, [], status="REJECTED")

    review_calls = [e for e in timeline.events if e.node == REVIEW_NODE]
    assert len(review_calls) == 1
    assert review_calls[0].agent == "reviewer_assist"


def test_a_call_from_no_known_agent_is_marked_unattributed_rather_than_dropped() -> None:
    nodes = [node("intake", Phase.ENTER), node("intake", Phase.EXIT, duration_ms=1)]
    trace = [call("invented-agent")]

    timeline = build_timeline(nodes, trace, [], status="REJECTED")

    stray = [e for e in timeline.events if e.node == UNATTRIBUTED_NODE]
    assert len(stray) == 1
    assert stray[0].agent == "invented-agent"


# ── against the real pipeline ─────────────────────────────────────────────────


def test_a_past_run_and_the_live_view_build_the_same_timeline(tmp_path: Path) -> None:
    job = prepare_job(
        artifacts_root=tmp_path,
        submission=DEMO_SUBMISSION,
        hotel_id="MZN-DXB-001",
        period=Period.parse("2026-Q1"),
        policy=load_policy(),
        provider=ReplayProvider(),
        provider_name="replay",
    )
    execute(job)
    assert job.written is not None

    from_job = timeline_from_job(job)
    from_disk = timeline_from_run(job.written.directory)

    shape = [(e.kind, e.node, e.agent, e.outcome) for e in from_job.events]
    assert shape == [(e.kind, e.node, e.agent, e.outcome) for e in from_disk.events]
    assert from_job.finished
    assert from_disk.finished


def test_build_timeline_with_no_status_is_not_finished() -> None:
    timeline = build_timeline([], [], [], status=None)
    assert not timeline.finished
    assert timeline.events == ()
