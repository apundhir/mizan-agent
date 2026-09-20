"""What an agent was shown, recovered honestly or not at all.

**The recomputed mapping request is provably the one that was sent** - checked by running the real
mapping agent against the demo workbook in replay, then rebuilding the request independently and
comparing it to the cassette the run actually matched. The two come out byte-identical.

**A replayed call's cassette is read back exactly**, redacted the same way an artifact is.

**Nothing is guessed.** A live call for any agent other than mapping, or a request with no cassette
to read, returns `None` rather than a fabricated line.
"""

from __future__ import annotations

from pathlib import Path

from tda.agents.provider import ReplayProvider
from tda.contracts import Period
from tda.obs.trace import TraceRecord
from tda.policy import load_policy
from tda.review.shown import from_cassette, mapping_recomputed, shown_for

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SUBMISSION = REPO_ROOT / "corpus" / "demo" / "submission"
DEMO_WORKBOOK = DEMO_SUBMISSION / "claims_2026-Q1.xlsx"

CONTACT = "Prepared by " + "Ms" + ". Jane Doe, jane.doe" + "@" + "hotel.ae"


def trace_record(**overrides: object) -> TraceRecord:
    fields: dict[str, object] = {
        "agent": "mapping",
        "prompt_version": "v1",
        "model_id": "claude-sonnet-5",
        "effort": "low",
        "provider_mode": "replay",
        "cassette_key": "",
        "output_contract": "WorkbookMapping",
        "output_json": "{}",
    }
    fields.update(overrides)
    return TraceRecord.model_validate(fields)


# ── from_cassette ────────────────────────────────────────────────────────────


def test_a_replay_call_shows_the_cassettes_request() -> None:
    """The mapping call the demo submission actually made in replay, read back."""
    from tda.graph import Declaration, RunContext, verify
    from tda.graph.run import discover

    policy = load_policy()
    period = Period.parse("2026-Q1")
    context = RunContext.build(policy, period, ReplayProvider())
    verify(
        discover(DEMO_SUBMISSION, period),
        Declaration(hotel_id="MZN-DXB-001", period=period),
        policy,
        ReplayProvider(),
        context=context,
    )
    mapping_call = next(r for r in context.trace.records if r.agent == "mapping")

    shown = from_cassette("mapping", mapping_call.cassette_key)

    assert shown is not None
    assert shown.source == "cassette"
    assert shown.agent == "mapping"
    assert shown.messages


def test_from_cassette_with_an_empty_key_is_none() -> None:
    assert from_cassette("mapping", "") is None


def test_from_cassette_with_no_matching_file_is_none() -> None:
    assert from_cassette("mapping", "0" * 32) is None


# ── mapping_recomputed ────────────────────────────────────────────────────────


def test_the_recomputed_mapping_input_equals_the_recorded_request() -> None:
    """Runs the real mapping call against the demo workbook, rebuilds the request independently,
    and diffs the two against the cassette that matched the real run's own cassette key."""
    from tda.graph import Declaration, RunContext, verify
    from tda.graph.run import discover

    policy = load_policy()
    period = Period.parse("2026-Q1")
    context = RunContext.build(policy, period, ReplayProvider())
    verify(
        discover(DEMO_SUBMISSION, period),
        Declaration(hotel_id="MZN-DXB-001", period=period),
        policy,
        ReplayProvider(),
        context=context,
    )
    mapping_call = next(r for r in context.trace.records if r.agent == "mapping")
    recorded = from_cassette("mapping", mapping_call.cassette_key)
    assert recorded is not None

    recomputed = mapping_recomputed(DEMO_WORKBOOK, policy)

    assert recomputed.system == recorded.system
    assert recomputed.messages == recorded.messages
    assert recomputed.model_id == recorded.model_id
    assert recomputed.effort == recorded.effort
    assert recomputed.source == "recomputed"
    assert "identical to what was sent" in recomputed.label


def test_a_contact_detail_in_a_header_cell_is_redacted_on_the_card(tmp_path: Path) -> None:
    from openpyxl import Workbook as XlWorkbook

    workbook = XlWorkbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Occupancy"
    sheet["A1"] = CONTACT
    sheet["A2"] = "Room Nights"
    sheet["B2"] = 100
    path = tmp_path / "claims.xlsx"
    workbook.save(path)

    shown = mapping_recomputed(path, load_policy())

    joined = shown.system + " ".join(content for _, content in shown.messages)
    assert "jane.doe" not in joined
    assert "[redacted:email]" in joined


# ── shown_for ────────────────────────────────────────────────────────────────


def test_shown_for_a_replay_call_reads_its_cassette() -> None:
    from tda.graph import Declaration, RunContext, verify
    from tda.graph.run import discover

    policy = load_policy()
    period = Period.parse("2026-Q1")
    context = RunContext.build(policy, period, ReplayProvider())
    verify(
        discover(DEMO_SUBMISSION, period),
        Declaration(hotel_id="MZN-DXB-001", period=period),
        policy,
        ReplayProvider(),
        context=context,
    )
    mapping_call = next(r for r in context.trace.records if r.agent == "mapping")

    shown = shown_for(mapping_call)

    assert shown is not None
    assert shown.source == "cassette"


def test_shown_for_a_live_call_by_an_agent_other_than_mapping_is_none() -> None:
    record = trace_record(agent="narrative", provider_mode="anthropic", cassette_key="")
    assert shown_for(record) is None


def test_shown_for_a_live_mapping_call_without_a_workbook_is_none() -> None:
    record = trace_record(agent="mapping", provider_mode="anthropic", cassette_key="")
    assert shown_for(record) is None


def test_shown_for_a_live_mapping_call_with_a_workbook_recomputes() -> None:
    record = trace_record(agent="mapping", provider_mode="anthropic", cassette_key="")
    shown = shown_for(record, workbook=DEMO_WORKBOOK, policy=load_policy())
    assert shown is not None
    assert shown.source == "recomputed"


def test_shown_for_a_replay_call_with_no_cassette_is_none() -> None:
    record = trace_record(agent="mapping", provider_mode="replay", cassette_key="0" * 32)
    assert shown_for(record) is None
