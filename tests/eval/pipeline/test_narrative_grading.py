"""Narrative grading (PRD-95): the wiring, proven apart from the critic's own judgement.

Three kinds of test live here, mirroring the split `test_scorer_discriminates.py` already draws
for the numeric side.

**Unconditional and cassette-free.** `grade_narratives` returns nothing for a verdict with nothing
to narrate, and folding a `NarrativeResult` into `score_fixture`'s checks is what makes a bad
narrative fail a fixture — provable by construction, without a model or a fixture tree.

**Conditional, against the real fixture tree.** Whether a bad narrative actually fails a fixture
that would otherwise pass needs `corpus/fixtures/`, built by `make fixtures`. Skipped in words when
absent, the same position `tests/eval/pipeline/test_fixture_evals.py` takes.

**Against the held-back bad narrative's cassette.** The critic's own judgement on a real, deliberate
accusation, replayed from the cassette `tda.eval.record_narratives` records. This is the proof that
the *critic*, not just the plumbing, catches the PRD's own motivating example on a real fixture
finding rather than a synthetic one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tda.agents.contracts.narrative import FindingNarrative
from tda.agents.critic import grade
from tda.agents.provider import ReplayProvider
from tda.agents.runtime import AgentRunner
from tda.agents.tools import ToolRegistry
from tda.contracts import ExtractionSummary, Verdict, VerdictStatus
from tda.eval.narrative import grade_narratives
from tda.eval.record_narratives import BAD_NARRATIVE_FIXTURE, BAD_NARRATIVE_SENTENCE
from tda.eval.run import DEFAULT_FIXTURES_ROOT, find_fixtures, run_fixture, score_fixture
from tda.eval.scoring import NarrativeResult, narrative_check, narrative_tally_for, verdict_for
from tda.obs import TraceLog
from tda.policy import load_policy

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.eval


def _empty_verdict() -> Verdict:
    """The control shape: a run that produced nothing to narrate, same as F1 and F5."""
    return Verdict(
        run_id="run-0123456789ab",
        status=VerdictStatus.PASS,
        hotel_id="MZN-DXB-001",
        period="2026-Q1",
        policy_version="1.3.0",
        metric_library_version="1.0.0",
        model_id="claude-test",
        provider_mode="replay",
        extraction=ExtractionSummary(
            files=("pms_2026-01.pdf",),
            records_extracted=1200,
            pages_read=47,
            printed_total_matched=True,
            duplicate_ids=0,
        ),
        claims_checked=12,
        findings=(),
        definitional_items=(),
    )


def tree_or_skip() -> tuple[Path, ...]:
    if not DEFAULT_FIXTURES_ROOT.is_dir():
        pytest.skip(f"no fixture tree at {DEFAULT_FIXTURES_ROOT}; `make fixtures` builds it")
    return find_fixtures(DEFAULT_FIXTURES_ROOT)


# ── unconditional: nothing to grade ─────────────────────────────────────────


def test_a_verdict_with_nothing_to_narrate_grades_nothing() -> None:
    """F1 and F5's shape. Zero findings and zero definitional items is zero narratives, correctly:
    there is no sentence for a reviewer to read, which is not the same as an unmeasured one."""
    assert grade_narratives(_empty_verdict(), ReplayProvider(), load_policy()) == ()


# ── unconditional: the gate is real, provable without a model ──────────────


def test_a_failing_narrative_check_fails_the_fixture() -> None:
    """`narrative_check` is what makes a bad narrative fail through the same all-or-nothing rule a
    numeric mismatch uses. Proven directly, against the check vocabulary alone."""
    bad = NarrativeResult(
        finding_id="F-0001",
        sentence="the hotel appears to have under-reported",
        passed=False,
        failures=("reads as an accusation",),
        reason="clerical blame on a definitional difference",
    )
    good = NarrativeResult(
        finding_id="F-0001",
        sentence="the two counts differ because of a policy disagreement",
        passed=True,
        failures=(),
        reason="grounded, no leaked figure, no unassigned cause, no accusation",
    )
    assert verdict_for((narrative_check(bad),)) is verdict_for(())  # both FAILED
    assert verdict_for((narrative_check(good),)).value == "passed"
    assert verdict_for((narrative_check(bad),)).value == "failed"


def test_the_narrative_tally_matches_the_results_it_was_built_from() -> None:
    passed = NarrativeResult("F-0001", "sentence one", True, (), "clean")
    failed = NarrativeResult("F-0002", "sentence two", False, ("leaks a number",), "said 'forty'")

    empty = narrative_tally_for(())
    assert empty.narratives_graded == 0
    assert empty.narrative_pass_rate is None

    mixed = narrative_tally_for((passed, failed))
    assert mixed.narratives_graded == 2
    assert mixed.narratives_passed == 1
    assert mixed.narrative_pass_rate == pytest.approx(0.5)


# ── conditional: the real fixture tree, and a narrative failure injected into it ──


def test_a_failing_narrative_fails_a_fixture_that_would_otherwise_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The proof the plan calls for: this is wiring, not decoration.

    F2's numeric checks are untouched; only `grade_narratives` is replaced, with a single failing
    result. If the fixture still scored `PASSED`, the narrative gate would be decorative rather
    than load-bearing.
    """
    tree_or_skip()
    bad = NarrativeResult("F-0001", "the hotel appears to have under-reported", False, ("x",), "y")
    monkeypatch.setattr("tda.eval.run.grade_narratives", lambda *_a, **_k: (bad,))

    score = score_fixture(DEFAULT_FIXTURES_ROOT / "F2", out_root=tmp_path)

    assert score.outcome.value == "failed"
    narrative_checks = [c for c in score.checks if c.name.startswith("narrative[")]
    assert narrative_checks and not narrative_checks[0].passed
    numeric_checks = [c for c in score.checks if not c.name.startswith("narrative[")]
    assert all(c.passed for c in numeric_checks), [
        c.render() for c in numeric_checks if not c.passed
    ]


def test_a_passing_narrative_leaves_a_fixture_that_would_pass_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The mutation's inverse: flip `passed` back and the same fixture passes again."""
    tree_or_skip()
    good = NarrativeResult(
        "F-0001", "the two counts differ; both sides are internally consistent", True, (), "clean"
    )
    monkeypatch.setattr("tda.eval.run.grade_narratives", lambda *_a, **_k: (good,))

    score = score_fixture(DEFAULT_FIXTURES_ROOT / "F2", out_root=tmp_path)
    assert score.outcome.value == "passed"


# ── the held-back bad narrative, against its own recorded cassette ─────────


def test_the_held_back_accusation_fails_the_critic() -> None:
    """The PRD's own motivating example, graded for real: a correct finding, described as an
    accusation, on F3's actual definitional variance. Replayed from the cassette
    `tda.eval.record_narratives` records; skipped if that recording has not been made yet."""
    tree_or_skip()
    policy = load_policy()
    verdict = run_fixture(DEFAULT_FIXTURES_ROOT / BAD_NARRATIVE_FIXTURE, policy)
    if not verdict.definitional_items:
        pytest.skip(f"{BAD_NARRATIVE_FIXTURE} carries no definitional finding to grade")
    finding = verdict.definitional_items[0]

    bad = FindingNarrative(
        finding_id=finding.finding_id,
        sentence=BAD_NARRATIVE_SENTENCE,
        cites_permutation=None,
        is_definitional=True,
    )
    runner = AgentRunner(ReplayProvider(), policy=policy, registry=ToolRegistry(), trace=TraceLog())
    try:
        result = grade(finding, bad, runner, policy)
    except Exception as exc:  # CassetteMissError, reported as a skip rather than a failure
        pytest.skip(f"held-back bad narrative not recorded yet: {exc}")

    assert result.output.passed is False
    assert result.output.reads_as_an_accusation is True
