"""`make demo`: three real runs of the pipeline, and the guarantees each scene has to keep.

Three properties are checked, one per scene, plus the seam between them and the screen:

- **The refusal scene halts extraction with a blocking finding, in replay mode, with no cassette
  needed for it.** Not "eventually reports an error" - it must never reach the mapping agent, so
  the model call count is asserted to be exactly zero rather than inferred from the absence of a
  `CassetteMissError`.
- **Each scene reports the outcome `run_demo` asserts for it.** Pass, material catch, blocking
  refusal - the same comparison `main()` uses to decide its own exit code, exercised directly so a
  regression here fails a unit test rather than only a manual read of `make demo`'s output.
- **Nothing a scene renders names a fixture by its internal label.** `corpus/fixtures/F1..F3`'s own
  READMEs use those labels; a demo audience never sees them.

The catch scene needs `corpus/fixtures/F2/`, which `make fixtures` derives from `corpus/demo/` and
never commits. Where it is absent the affected tests skip with the reason in words - the same
position `tests/unit/test_fixtures.py` takes about the fixture tree generally.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from demo import run_demo
from tda.contracts import Severity, VarianceClass, VerdictStatus
from tda.extract.normalise import UnmappableLabelError, nationality
from tda.policy import load_policy

CATCH_SUBMISSION_MISSING = (
    f"no submission at {run_demo.CATCH_SUBMISSION}. `make fixtures` derives it from "
    "corpus/demo/; run that first if this test is being run on its own."
)


def catch_submission_or_skip() -> None:
    if not run_demo.CATCH_SUBMISSION.is_dir():
        pytest.skip(CATCH_SUBMISSION_MISSING)


def test_unresolvable_nationality_is_not_in_the_committed_lookup() -> None:
    """The refusal scene's whole mechanism rests on this one fact about the label it plants."""
    with pytest.raises(UnmappableLabelError):
        nationality(run_demo.UNRESOLVABLE_NATIONALITY)


def test_refusal_scene_halts_before_claim_parse_with_no_model_calls() -> None:
    """The scene the PRD asks for: a PDF-side label the lookup cannot resolve, and a halt that
    costs zero model calls because extraction stops the run before the mapping agent is invoked."""
    policy = load_policy()
    with tempfile.TemporaryDirectory(prefix="mizan-demo-test-") as tmp:
        scene = run_demo.run_refusal_scene(policy, Path(tmp))

    verdict = scene.result.verdict
    assert verdict.status is VerdictStatus.HALTED
    assert len(scene.result.context.trace.records) == 0

    blocking = [f for f in verdict.findings if f.severity is Severity.BLOCKING]
    assert blocking, "a halted run must carry at least one blocking finding"
    assert all(f.severity is Severity.BLOCKING for f in verdict.findings), (
        "D-MAT-01: blocking always outranks every other severity, so a halted run's findings are "
        "all blocking or the status contradicts its own findings"
    )
    assert any(
        f.variance_class is VarianceClass.EXTRACTION_LIMIT and f.clause == "D-NAT-12"
        for f in blocking
    ), "the row-level defect this scene plants must be among the findings, not just a side effect"


def test_pick_refusal_target_is_deterministic_and_ordinary() -> None:
    """The chosen reservation is picked by rule, not read off a literal id, and the rule is
    stable across calls - a demo that silently mutated a different row each run would be a
    different demo each run."""
    ledger = run_demo._read_ledger(run_demo.GROUND_TRUTH / "reservations.csv")
    first = run_demo.pick_refusal_target(ledger)
    second = run_demo.pick_refusal_target(ledger)
    assert first.reservation_id == second.reservation_id
    assert first.rooms == 1
    assert first.status == "CHECKED_OUT"
    assert first.arrival_date.month == 1
    assert first.departure_date.month == 1


def test_pass_scene_reports_pass() -> None:
    """Scene 1 against the real, unmutated demo corpus."""
    policy = load_policy()
    scene = run_demo.run_pass_scene(policy)
    assert scene.matched_expectation
    assert scene.result.verdict.status is VerdictStatus.PASS
    assert scene.result.verdict.findings == ()


def test_catch_scene_reports_fail_with_the_two_findings_the_mutation_produces() -> None:
    """Scene 2: the transposition is caught at the cell and at the roll-up it feeds."""
    catch_submission_or_skip()
    policy = load_policy()
    scene = run_demo.run_catch_scene(policy)
    assert scene.matched_expectation
    assert scene.result.verdict.status is VerdictStatus.FAIL
    assert len(scene.result.verdict.findings) == 2
    assert all(
        f.variance_class is VarianceClass.TRANSCRIPTION for f in scene.result.verdict.findings
    )


def test_all_three_scenes_report_their_expected_outcome() -> None:
    """The property `main()` checks before deciding its exit code, exercised directly."""
    catch_submission_or_skip()
    policy = load_policy()
    with tempfile.TemporaryDirectory(prefix="mizan-demo-test-") as tmp:
        scenes = [
            run_demo.run_pass_scene(policy),
            run_demo.run_catch_scene(policy),
            run_demo.run_refusal_scene(policy, Path(tmp)),
        ]
    unmatched = [scene.title for scene in scenes if not scene.matched_expectation]
    assert not unmatched, f"scene(s) did not report their expected outcome: {unmatched}"


def test_no_scene_names_a_fixture_by_its_internal_label(capsys: pytest.CaptureFixture[str]) -> None:
    """A viewer sees what the pipeline found, never the internal name of the case that produced
    it - the labels `corpus/fixtures/F1..F3/README.md` use for exactly that reason."""
    catch_submission_or_skip()
    exit_code = run_demo.main()
    assert exit_code == 0

    output = capsys.readouterr().out
    for label in ("F1", "F2", "F3", "fixture"):
        assert label not in output, f"{label!r} leaked into demo output: a viewer must never see it"
