"""Prepared demonstration scenes for the Run console: a known submission, a known outcome.

Five scenes, each a real submission a viewer can run through the real pipeline in replay, with an
outcome the fixture and demo machinery already proves the pipeline produces
(`tools/demo/run_demo.py`, `tools/fixtures`). This module does not re-verify that claim; it only
assembles the submission directories and states what each one is expected to show.

## Why this is not `tools.demo.run_demo.Scene`

That `Scene` carries an already-completed `RunResult` and exists to print one. This one describes
*how to build a submission* before any run happens - `prepare(run_dir) -> Path` stages the files and
returns the directory `verify_directory` should be pointed at. The two never collide because they
answer different questions, but importing both into one page is exactly the situation the different
names exist to keep straight.

## Why nothing here names a fixture by its internal label

`tests/unit/test_demo.py` already states the rule for `tools/demo/run_demo.py`'s own output, and
`tests/unit/test_console.py` checks it holds here too: a demo is not the place to tell a viewer
which row is a planted mutation. Titles and descriptions are written from what a viewer can see -
"a mistyped guest count" - never from how the corpus was built.

## What this module assumes about `fixtures_root`

That `F2/` and `F3/` already exist under it, built and verified. `catalogue()` does not build them
and does not check for them - the caller (`streamlit_app.py`'s `ensure_fixtures()`) guarantees the
directories exist before a `Scene` referencing them is ever constructed, the same way `make eval`
depends on `fixtures` rather than building them inline.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from tda.contracts import RejectionReason, VerdictStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path

# The demo corpus's own declaration (corpus/demo/manifest.json), reused rather than re-typed so a
# scene's declaration can never drift from the files it actually stages.
HOTEL_ID: Final = "MZN-DXB-001"
PERIOD: Final = "2026-Q1"

# The report scene 5 omits. February, not January or March, so the mutation sits in the middle of
# the quarter rather than at an edge a reader might mistake for an off-by-one in the scene itself.
MISSING_REPORT: Final = "pms_2026-02.pdf"

SUBMISSION_DIR: Final = "submission"


def copy_submission(source: Path, run_dir: Path, *, omit: frozenset[str] = frozenset()) -> Path:
    """Every file in `source` copied into `run_dir/submission/`, minus whatever `omit` names.

    A plain file copy rather than `shutil.copytree`, so a source directory holding more than a
    submission (a fixture's `README.md`, `expected.json`) does not silently become part of what the
    pipeline is handed - only files, and only the ones not named in `omit`.
    """
    destination = run_dir / SUBMISSION_DIR
    destination.mkdir(parents=True)
    for item in sorted(source.iterdir()):
        if item.is_file() and item.name not in omit:
            shutil.copyfile(item, destination / item.name)
    return destination


@dataclass(frozen=True, slots=True)
class Scene:
    """One demonstration: how to stage it, and what a viewer should see once it has run.

    `prepare` takes the run's own directory (`artifacts/<run_id>/`) and returns the submission
    directory it staged inside it - the same shape `verify_directory` expects, so the console calls
    `scene.prepare(run_dir)` and hands the result straight to the pipeline with nothing in between.
    """

    key: str
    title: str
    description: str
    expected_status: VerdictStatus
    expected_reason: RejectionReason | None
    hotel_id: str
    period: str
    makes_model_calls: bool
    prepare: Callable[[Path], Path]


class SceneCatalogue:
    """Every scene the console offers, in the order a viewer should see them: least to most
    surprising an outcome, ending on the one that makes no model call at all."""

    def __init__(self, scenes: Iterable[Scene]) -> None:
        self._scenes = tuple(scenes)

    @property
    def scenes(self) -> tuple[Scene, ...]:
        return self._scenes

    def __iter__(self) -> Iterator[Scene]:
        return iter(self._scenes)

    def __len__(self) -> int:
        return len(self._scenes)

    def by_key(self, key: str) -> Scene:
        for scene in self._scenes:
            if scene.key == key:
                return scene
        raise KeyError(f"no scene named {key!r}. Offered: {[s.key for s in self._scenes]}")

    def titles(self) -> tuple[str, ...]:
        return tuple(scene.title for scene in self._scenes)


def catalogue(
    *,
    demo_submission: Path,
    fixtures_root: Path,
    refusal_builder: Callable[[Path], Path] | None,
) -> SceneCatalogue:
    """Build the catalogue. `refusal_builder`, when given, is
    `tools.demo.run_demo.build_refusal_submission` - injected rather than imported here, so this
    module never reaches into `tools/`, which ships in no wheel (see `tools/guard/import_guard.py`
    on why `src/tda` may not import `datagen`, `fixtures` or `demo`).

    Omitted rather than raising when `refusal_builder` is `None`: reportlab, which the builder
    needs to re-render a report, is a `datagen` extra a minimal install can lack, and four working
    scenes beat a page that refuses to open over a fifth.
    """
    scenes = [
        Scene(
            key="clean-quarter",
            title="A clean quarter",
            description=(
                "The pipeline recomputes a full quarter from the PDFs and agrees with the "
                "workbook to the cell. Zero findings."
            ),
            expected_status=VerdictStatus.PASS,
            expected_reason=None,
            hotel_id=HOTEL_ID,
            period=PERIOD,
            makes_model_calls=True,
            prepare=lambda run_dir: copy_submission(demo_submission, run_dir),
        ),
        Scene(
            key="mistyped-count",
            title="A mistyped guest count",
            description=(
                "A transposition in one hand-keyed nationality count is caught at the cell it "
                "was typed in and, independently, at the quarter roll-up that sums it. Each "
                "finding carries a proposed correction and a citation back to the reservation "
                "rows behind it."
            ),
            expected_status=VerdictStatus.FAIL,
            expected_reason=None,
            hotel_id=HOTEL_ID,
            period=PERIOD,
            makes_model_calls=True,
            prepare=lambda run_dir: copy_submission(fixtures_root / "F2" / "submission", run_dir),
        ),
        Scene(
            key="different-definition",
            title="Occupancy under a different definition",
            description=(
                "The claimed figure is reproduced exactly by an alternative reading of the "
                "definitions - occupancy divided by room count rather than by room-nights. Not "
                "a hotel error: it goes to the policy owner, named as a definitional item, and "
                "is kept out of the hotel error count."
            ),
            expected_status=VerdictStatus.ESCALATED,
            expected_reason=None,
            hotel_id=HOTEL_ID,
            period=PERIOD,
            makes_model_calls=True,
            prepare=lambda run_dir: copy_submission(fixtures_root / "F3" / "submission", run_dir),
        ),
        Scene(
            key="missing-report",
            title="A missing monthly report",
            description=(
                "One month of the declared quarter has no report. Intake refuses the "
                "submission before any page is read and before the mapping agent is ever "
                "asked anything."
            ),
            expected_status=VerdictStatus.REJECTED,
            expected_reason=RejectionReason.INCOMPLETE_FILE_SET,
            hotel_id=HOTEL_ID,
            period=PERIOD,
            makes_model_calls=False,
            prepare=lambda run_dir: copy_submission(
                demo_submission, run_dir, omit=frozenset({MISSING_REPORT})
            ),
        ),
    ]
    if refusal_builder is not None:
        scenes.append(
            Scene(
                key="unknown-label",
                title="A nationality label nobody taught the system",
                description=(
                    "A label the committed lookup cannot resolve halts extraction before the "
                    "mapping agent is ever invoked. Zero model calls, and the blocking finding "
                    "names the exact string that failed rather than guessing at the nearest "
                    "country."
                ),
                expected_status=VerdictStatus.HALTED,
                expected_reason=None,
                hotel_id=HOTEL_ID,
                period=PERIOD,
                makes_model_calls=False,
                prepare=refusal_builder,
            )
        )
    return SceneCatalogue(scenes)
