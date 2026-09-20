"""The demonstration catalogue: five staged submissions, each with a stated outcome.

Two properties matter more than the others.

**A scene's outcome is not asserted, it is checked against the real pipeline.** `run_demo.py`
already proves the corpus and fixtures produce these outcomes; the tests below re-derive that
through `tda.review.scenes.catalogue`'s own `prepare()` functions rather than trusting that the
wiring here calls the right thing.

**Nothing a scene shows names a fixture by its internal label**, the same rule
`tests/unit/test_demo.py` states for `tools/demo/run_demo.py`'s own output.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from demo.run_demo import CATCH_SUBMISSION, build_refusal_submission
from tda.contracts import Period, RejectionReason, VerdictStatus
from tda.graph import verify_directory
from tda.policy import load_policy
from tda.review.scenes import (
    MISSING_REPORT,
    Scene,
    SceneCatalogue,
    catalogue,
    copy_submission,
)

if TYPE_CHECKING:
    from tda.graph import RunResult

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SUBMISSION = REPO_ROOT / "corpus" / "demo" / "submission"
FIXTURES_ROOT = REPO_ROOT / "corpus" / "fixtures"


def fixtures_or_skip() -> None:
    if not CATCH_SUBMISSION.is_dir():
        pytest.skip(
            f"no submission at {CATCH_SUBMISSION}. `make fixtures` derives it from "
            "corpus/demo/; run that first if this test is being run on its own."
        )


def full_catalogue() -> SceneCatalogue:
    return catalogue(
        demo_submission=DEMO_SUBMISSION,
        fixtures_root=FIXTURES_ROOT,
        refusal_builder=build_refusal_submission,
    )


def test_the_catalogue_names_five_scenes_with_expected_outcomes() -> None:
    scenes = full_catalogue()
    assert len(scenes) == 5
    outcomes = {scene.key: scene.expected_status for scene in scenes}
    assert outcomes == {
        "clean-quarter": VerdictStatus.PASS,
        "mistyped-count": VerdictStatus.FAIL,
        "different-definition": VerdictStatus.ESCALATED,
        "missing-report": VerdictStatus.REJECTED,
        "unknown-label": VerdictStatus.HALTED,
    }
    assert scenes.by_key("clean-quarter").expected_reason is None
    assert scenes.by_key("missing-report").expected_reason is RejectionReason.INCOMPLETE_FILE_SET


def test_the_refusal_scene_is_omitted_without_its_builder() -> None:
    """Four scenes rather than a page that refuses to open, when the demo/datagen extra (and
    therefore reportlab) is not installed."""
    scenes = catalogue(
        demo_submission=DEMO_SUBMISSION, fixtures_root=FIXTURES_ROOT, refusal_builder=None
    )
    assert len(scenes) == 4
    assert "unknown-label" not in {scene.key for scene in scenes}


def test_by_key_names_what_it_could_not_find() -> None:
    scenes = catalogue(
        demo_submission=DEMO_SUBMISSION, fixtures_root=FIXTURES_ROOT, refusal_builder=None
    )
    with pytest.raises(KeyError, match="no scene named 'invented'"):
        scenes.by_key("invented")


def test_no_scene_text_names_an_internal_label() -> None:
    """A viewer sees what the pipeline found, never the internal name of the case that produced
    it. Checked over every scene's title, description and key - a demo is not the place to say
    which row is a planted mutation."""
    scenes = full_catalogue()
    for scene in scenes:
        for text in (scene.key, scene.title, scene.description):
            for label in ("F1", "F2", "F3", "F4", "F5", "F6", "fixture"):
                assert label not in text, f"{label!r} leaked into {scene.key}: {text!r}"


def test_copy_submission_copies_files_only_and_skips_what_is_named(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.pdf").write_bytes(b"a")
    (source / "b.pdf").write_bytes(b"b")
    (source / "README.md").write_text("not part of a submission")
    (source / "subdir").mkdir()

    destination = copy_submission(source, tmp_path / "run", omit=frozenset({"b.pdf"}))

    assert destination == tmp_path / "run" / "submission"
    assert sorted(p.name for p in destination.iterdir()) == ["README.md", "a.pdf"]


@pytest.mark.parametrize(
    "key",
    ["clean-quarter", "mistyped-count", "different-definition", "missing-report"],
)
def test_each_scene_prepares_a_role_named_submission_under_the_run_dir(
    key: str, tmp_path: Path
) -> None:
    fixtures_or_skip()
    scene = full_catalogue().by_key(key)

    submission = scene.prepare(tmp_path)

    assert submission == tmp_path / "submission"
    assert submission.is_dir()
    names = {p.name for p in submission.iterdir()}
    if key == "missing-report":
        assert MISSING_REPORT not in names
        assert "pms_2026-01.pdf" in names
        assert "pms_2026-03.pdf" in names
    else:
        assert "claims_2026-Q1.xlsx" in names
        assert any(name.startswith("pms_") for name in names)


def test_the_refusal_scene_stages_its_own_submission_directory(tmp_path: Path) -> None:
    scene = full_catalogue().by_key("unknown-label")

    submission = scene.prepare(tmp_path)

    assert submission == tmp_path / "submission"
    assert submission.is_dir()


def _run(scene: Scene, submission: Path) -> RunResult:
    from tda.agents.provider import ReplayProvider

    return verify_directory(
        submission,
        scene.hotel_id,
        Period.parse(scene.period),
        load_policy(),
        ReplayProvider(),
    )


def test_the_clean_quarter_scene_passes_through_the_real_pipeline(tmp_path: Path) -> None:
    scene = full_catalogue().by_key("clean-quarter")
    submission = scene.prepare(tmp_path)

    assert _run(scene, submission).verdict.status is scene.expected_status


def test_the_missing_report_scene_is_rejected_through_the_real_pipeline(tmp_path: Path) -> None:
    scene = full_catalogue().by_key("missing-report")
    submission = scene.prepare(tmp_path)

    result = _run(scene, submission)

    assert result.verdict.status is scene.expected_status
    assert result.verdict.rejection_reason is scene.expected_reason
    assert len(result.context.trace) == 0


def test_the_mistyped_count_scene_fails_through_the_real_pipeline(tmp_path: Path) -> None:
    fixtures_or_skip()
    scene = full_catalogue().by_key("mistyped-count")
    submission = scene.prepare(tmp_path)

    assert _run(scene, submission).verdict.status is scene.expected_status


def test_the_different_definition_scene_escalates_through_the_real_pipeline(
    tmp_path: Path,
) -> None:
    fixtures_or_skip()
    scene = full_catalogue().by_key("different-definition")
    submission = scene.prepare(tmp_path)

    assert _run(scene, submission).verdict.status is scene.expected_status


def test_the_unknown_label_scene_halts_with_no_model_calls() -> None:
    scene = full_catalogue().by_key("unknown-label")

    with tempfile.TemporaryDirectory(prefix="mizan-scene-test-") as tmp:
        submission = scene.prepare(Path(tmp))
        result = _run(scene, submission)

    assert result.verdict.status is scene.expected_status
    assert len(result.context.trace) == 0
