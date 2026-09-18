"""Scoring the real fixture tree, and proving that its absence is not a pass.

Two kinds of test live here, and the split is the point.

**Unconditional.** `make eval` must exit 2 when it cannot score anything: no fixture tree, an empty
one, or one where a fixture carries no `expected.json`. These need no fixtures to run, and they are
what stops the honest gap below from becoming a dishonest green. An eval that exits 0 having
measured nothing is worse than no eval, because the release gate reads the exit code.

**Conditional.** Scoring the tree needs the tree, and `tools/fixtures/` builds it. Where it is
absent the tests skip with the reason in words, and a skip is visible in pytest's own summary
(`-ra` is on in `addopts`) rather than being counted among the passes. That is the same position
`tests/eval/agents/` takes about a missing cassette: unmeasured and passing are different
statements, and a harness that conflates them teaches people to ignore its output.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tda.agents.provider import CassetteMissError
from tda.eval.__main__ import COULD_NOT_RUN, SCORECARD_FILE, main
from tda.eval.expectation import EXPECTED_FILE, SUPPORTED_FIXTURE_SET_VERSION
from tda.eval.run import (
    DEFAULT_FIXTURES_ROOT,
    MANIFEST_FILE,
    FixtureError,
    find_fixtures,
    score_fixtures,
)
from tda.eval.scoring import Outcome, is_complete, render_report

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.eval

MISSING_TREE = (
    f"no fixture tree at {DEFAULT_FIXTURES_ROOT}. `make datagen` builds it; this suite scores what "
    "exists and materialises nothing. Nothing here was measured, which is not a pass."
)


def tree_or_skip() -> tuple[Path, ...]:
    if not DEFAULT_FIXTURES_ROOT.is_dir():
        pytest.skip(MISSING_TREE)
    return find_fixtures(DEFAULT_FIXTURES_ROOT)


def test_every_fixture_matches_its_derived_expectation(tmp_path: Path) -> None:
    """The gate `make eval` enforces. The report is printed on failure, so a red CI job carries
    the mutation, the expectation and what came out rather than a bare count."""
    tree_or_skip()
    results = score_fixtures(DEFAULT_FIXTURES_ROOT, out_root=tmp_path)
    failed = [result.fixture_id for result in results if not result.passed]
    assert not failed, render_report(results)


def test_the_scorecard_covers_every_fixture(tmp_path: Path) -> None:
    """Completeness is asserted separately from correctness.

    A sweep where one fixture errored and the rest matched is not a pass, and the aggregate would
    otherwise read as one: five of five green over a tree of six.
    """
    directories = tree_or_skip()
    results = score_fixtures(DEFAULT_FIXTURES_ROOT, out_root=tmp_path)
    assert len(results) == len(directories)
    assert is_complete(results), render_report(results)


def test_an_absent_fixture_tree_could_not_run(tmp_path: Path) -> None:
    """Absence exits 2, never 0. The machine-checkable half of the skip above."""
    assert main(["--fixtures", str(tmp_path / "nothing"), "--out", str(tmp_path / "out")]) == (
        COULD_NOT_RUN
    )


def test_an_empty_fixture_tree_could_not_run(tmp_path: Path) -> None:
    """A directory that exists and holds nothing is the shape a half-run build leaves behind.

    Both defences are asserted, because either alone would let this test pass while the other was
    removed: `find_fixtures` refuses the empty tree, and `is_complete` refuses to call a sweep over
    zero fixtures a clean one.
    """
    empty = tmp_path / "fixtures"
    empty.mkdir()
    with pytest.raises(FixtureError, match="holds no fixture directories"):
        find_fixtures(empty)
    assert not is_complete(())
    assert main(["--fixtures", str(empty), "--out", str(tmp_path / "out")]) == COULD_NOT_RUN


def test_a_fixture_without_an_expectation_is_refused(tmp_path: Path) -> None:
    """A fixture nobody derived an expectation for is a hard error rather than a quiet omission.

    Skipping it would let a tree that half built report a clean sweep over the half that did, which
    is the failure mode this whole file is arranged against.
    """
    root = tmp_path / "fixtures"
    (root / "F1").mkdir(parents=True)
    with pytest.raises(FixtureError, match=EXPECTED_FILE):
        find_fixtures(root)


def minimal_fixture(root: Path, fixture_id: str = "F1") -> Path:
    """A fixture tree with a valid declaration and expectation, and nothing to extract.

    Enough for the paths below, which never reach extraction: they are about what happens to a
    fixture that produced no verdict at all.
    """
    directory = root / fixture_id
    (directory / "submission").mkdir(parents=True)
    (directory / MANIFEST_FILE).write_text(
        json.dumps({"hotel_id": "MZN-DXB-001", "period": "2026-Q1"}), encoding="utf-8"
    )
    (directory / EXPECTED_FILE).write_text(
        json.dumps(
            {
                "fixture_set_version": SUPPORTED_FIXTURE_SET_VERSION,
                "fixture_id": fixture_id,
                "why": "A control fixture that plants nothing must still come back empty.",
                "mutation": {"kind": "none"},
                "status": "PASS",
                "exhaustive": True,
                "findings": [],
                "definitional_items": [],
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_a_cassette_miss_is_not_recorded_and_never_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour the whole package is arranged around, proven without any cassettes.

    A replay miss means the fixture was not measured. It is reported in words, counted as a
    failure, and it exits 2 rather than 0: an eval that reports a clean sweep over fixtures it
    never ran is worse than no eval at all.
    """

    def miss(*_args: object, **_kwargs: object) -> None:
        raise CassetteMissError(key="abc123", agent="mapping", available=1)

    monkeypatch.setattr("tda.eval.run.verify_directory", miss)
    root = tmp_path / "fixtures"
    minimal_fixture(root)
    out = tmp_path / "out"

    results = score_fixtures(root, out_root=out)
    assert [result.outcome for result in results] == [Outcome.NOT_RECORDED]
    assert not results[0].passed
    assert not is_complete(results)
    assert "make record" in results[0].detail

    assert main(["--fixtures", str(root), "--out", str(out)]) == COULD_NOT_RUN
    scorecard = json.loads((out / SCORECARD_FILE).read_text(encoding="utf-8"))
    assert scorecard["complete"] is False
    assert scorecard["outcomes"]["not_recorded"] == 1


def test_a_run_that_died_is_an_error_and_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception from the pipeline is `ERRORED`, which is distinct from a miss and equally not
    a pass. A fixture that blew up has told us something real, and folding it into the same bucket
    as an unrecorded one would hide it."""

    def die(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the graph stopped inside extract")

    monkeypatch.setattr("tda.eval.run.verify_directory", die)
    root = tmp_path / "fixtures"
    minimal_fixture(root)

    results = score_fixtures(root, out_root=tmp_path / "out")
    assert [result.outcome for result in results] == [Outcome.ERRORED]
    assert "the graph stopped inside extract" in results[0].detail
    assert main(["--fixtures", str(root), "--out", str(tmp_path / "out")]) == COULD_NOT_RUN
