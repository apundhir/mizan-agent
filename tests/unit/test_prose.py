"""Grade the prose: the same sequence `make eval` runs, with both trace records kept.

**A real fixture, graded for real, carries both trace records** - the property this module exists
for, checked against the actual cassettes narrative grading's `make eval` already proves work.

**One missing cassette is one row, not a blank panel.** `grade_verdict` catches per finding, unlike
`tda.eval.narrative.grade_finding`, which is right for a fixture that must all grade and wrong for
a console showing five findings where one recording is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.test_outputs import definitional_finding, material_finding, verdict_with

from tda.agents.provider import ReplayProvider, StubProvider
from tda.eval.run import run_fixture
from tda.policy import load_policy
from tda.review.prose import grade_finding, grade_verdict

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "corpus" / "fixtures"


def fixtures_or_skip(fixture_id: str) -> Path:
    directory = FIXTURES_ROOT / fixture_id
    if not directory.is_dir():
        pytest.skip(
            f"no fixture at {directory}. `make fixtures` derives it from corpus/demo/; run that "
            "first if this test is being run on its own."
        )
    return directory


# ── coverage ─────────────────────────────────────────────────────────────────


def test_grading_a_verdict_with_no_findings_yields_no_rows() -> None:
    empty = verdict_with()
    assert grade_verdict(empty, StubProvider(), load_policy()) == ()


def test_grade_verdict_covers_both_findings_and_definitional_items() -> None:
    """Coverage, not success: neither finding has a recorded cassette (they are hand-built test
    fixtures, not real fixture findings), so both come back `not_recorded` - the point is that
    both were attempted, matching `tda.eval.narrative.grade_narratives`'s own coverage claim."""
    both = verdict_with(material_finding(), definitional_finding())

    rows = grade_verdict(both, ReplayProvider(cassette_dir=Path("/nonexistent")), load_policy())

    assert {row.finding_id for row in rows} == {
        material_finding().finding_id,
        definitional_finding().finding_id,
    }
    assert all(row.status == "not_recorded" for row in rows)


# ── one miss is one row ───────────────────────────────────────────────────────


def test_a_missing_cassette_is_one_row_not_a_blank_panel(tmp_path: Path) -> None:
    row = grade_finding(
        material_finding(), ReplayProvider(cassette_dir=tmp_path / "empty"), load_policy()
    )

    assert row.status == "not_recorded"
    assert row.finding_id == material_finding().finding_id
    assert row.sentence is None
    assert row.passed is None
    assert row.narrative_trace is None
    assert row.critic_trace is None
    assert "no cassette" in row.detail or "CassetteMissError" in row.detail


def test_a_provider_failure_is_a_failed_row_not_an_exception() -> None:
    """An unregistered stub raises `ProviderError`, not `CassetteMissError` - the other shape of
    failure `grade_finding` must also turn into a row rather than let propagate."""
    row = grade_finding(material_finding(), StubProvider(), load_policy())

    assert row.status == "failed"
    assert row.detail


def test_a_key_in_a_failure_detail_would_be_redacted() -> None:
    """`grade_finding`'s failure paths route every message through `redact()` - proven directly on
    the function rather than by planting a key in a real provider error, which nothing in this
    codebase's error messages would ever contain."""
    from tda.review.prose import _failed, _not_recorded

    planted = "sk-ant-api03-" + "EXAMPLE" * 4
    assert planted not in _failed("F-0001", f"boom {planted}").detail
    assert "[redacted:api_key]" in _failed("F-0001", f"boom {planted}").detail
    assert planted not in _not_recorded("F-0001", f"boom {planted}").detail


# ── against real, cassette-backed findings ────────────────────────────────────


def test_graded_rows_carry_both_trace_records() -> None:
    fixture = fixtures_or_skip("F2")
    policy = load_policy()
    verdict = run_fixture(fixture, policy)
    assert verdict.findings, "F2 is expected to carry findings; nothing to grade otherwise"

    rows = grade_verdict(verdict, ReplayProvider(), policy)

    assert len(rows) == len(verdict.findings) + len(verdict.definitional_items)
    for row in rows:
        assert row.status == "graded", row.detail
        assert row.sentence
        assert row.passed is True
        assert row.narrative_trace is not None
        assert row.narrative_trace.agent == "narrative"
        assert row.critic_trace is not None
        assert row.critic_trace.agent == "critic"


def test_a_definitional_items_narrative_is_graded_too() -> None:
    fixture = fixtures_or_skip("F3")
    policy = load_policy()
    verdict = run_fixture(fixture, policy)
    assert verdict.definitional_items, "F3 is expected to carry definitional items"

    rows = grade_verdict(verdict, ReplayProvider(), policy)

    definitional_ids = {item.finding_id for item in verdict.definitional_items}
    graded_definitional = [row for row in rows if row.finding_id in definitional_ids]
    assert graded_definitional
    assert all(row.status == "graded" for row in graded_definitional)
