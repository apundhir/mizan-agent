"""Running the fixtures: one pipeline invocation per fixture, in replay, scored against its file.

This module materialises nothing. `tools/fixtures/` builds `corpus/fixtures/F1..F6/` and derives
each `expected.json`; this takes a directory that already exists and runs the ordinary product path
over it. A scorer that built its own inputs would be scoring the system against a submission only
it knows how to make.

## Replay, and why a miss is not a skip

The provider is `ReplayProvider` unconditionally rather than whatever `policy.yaml` names. An eval
that could reach the network would produce a different number on every run and cost money to
disagree with itself. A replay miss is a hard error by design, and it arrives here as
`Outcome.NOT_RECORDED` reported in words: the fixture was not measured, unmeasured is not a pass,
and it counts as a failure in the aggregate and in the exit code.

## The verdict is written and read back before it is scored

Rather than scoring the object `verify_directory` returns. `tda.outputs.write_outputs` already
takes this position for the memo and the annotated workbook, and the reason is the same: the
artefact on disk is what a reviewer opens, `write_verdict` redacts on the way out, and
`read_verdict` refuses a document whose summary block disagrees with its own arrays. Scoring the
in-memory verdict would pass a run whose published file cannot be loaded.

## Durations are recorded, not targeted

Every duration this module records carries that phrase wherever it is rendered. No manual baseline
has ever been measured, so a time figure here is an observation and never an achievement.

## The narrative is graded here too, and it can fail a fixture

`tda.eval.narrative.grade_narratives` runs after the verdict is read back, over the same findings
`score` scores numerically. Its results join `score`'s checks in one tuple, so a narrative that
leaks a number or reads as an accusation fails the fixture through the same all-or-nothing rule a
numeric mismatch does (PRD-95). A cassette miss inside it is indistinguishable from one inside the
pipeline run: both are caught by the `try` below and reported as `Outcome.NOT_RECORDED`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tda.agents.provider import CassetteMissError, ReplayProvider
from tda.contracts import Period
from tda.eval.expectation import EXPECTED_FILE, Expectation, load_expectation
from tda.eval.narrative import grade_narratives
from tda.eval.score import observed, score, tally_for
from tda.eval.scoring import (
    Check,
    FixtureScore,
    Outcome,
    Tally,
    narrative_check,
    narrative_tally_for,
    verdict_for,
)
from tda.graph import RunContext, new_run_id, verify_directory
from tda.outputs.verdict import read_verdict, write_verdict
from tda.policy import load_policy

if TYPE_CHECKING:
    from tda.contracts import Verdict
    from tda.policy import Policy

# `src/tda/eval/run.py` -> repository root.
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURES_ROOT: Final = REPO_ROOT / "corpus" / "fixtures"
DEFAULT_OUT_ROOT: Final = REPO_ROOT / "artifacts" / "eval"

SUBMISSION_DIR: Final = "submission"
MANIFEST_FILE: Final = "manifest.json"


class FixtureError(Exception):
    """A fixture that cannot be run: no tree, no submission, or no declaration to check it against.

    Distinct from a fixture that ran and mismatched, because the two demand different exit codes.
    A mismatch is a result; this is the absence of one.
    """


def find_fixtures(root: Path) -> tuple[Path, ...]:
    """Every fixture directory under `root`, in name order.

    A directory without an `expected.json` is a hard error rather than a quiet omission. The
    failure it would otherwise produce is the worst one available to an eval: a tree that half
    built reports a clean sweep over the half that did.
    """
    if not root.is_dir():
        raise FixtureError(
            f"no fixture tree at {root}. `make datagen` builds it from `corpus/demo/`, and this "
            "scorer materialises nothing: it runs fixtures that already exist."
        )
    directories = sorted(path for path in root.iterdir() if path.is_dir())
    if not directories:
        raise FixtureError(f"{root} holds no fixture directories")
    missing = [d.name for d in directories if not (d / EXPECTED_FILE).is_file()]
    if missing:
        raise FixtureError(
            f"fixture(s) {missing} under {root} carry no {EXPECTED_FILE}. An unscored fixture "
            "reported as absent would let a half-built tree score as a clean sweep."
        )
    return tuple(directories)


def declaration_for(directory: Path) -> tuple[str, Period]:
    """The hotel and period the fixture's case record states.

    Read from `manifest.json` beside `submission/` rather than from anything inside it, which is
    the placement `mizan run` relies on for the same reason: intake exists to check the files
    against a declaration made somewhere other than the files, and reading it out of the submission
    would be asking the evidence to vouch for itself.
    """
    manifest = directory / MANIFEST_FILE
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FixtureError(
            f"{directory.name} has no readable {MANIFEST_FILE}: {exc}. Without a declaration there "
            "is nothing for intake to check the submission against."
        ) from exc
    except json.JSONDecodeError as exc:
        raise FixtureError(f"{manifest} is not valid JSON: {exc}") from exc

    try:
        hotel, period_text = str(payload["hotel_id"]), str(payload["period"])
    except (KeyError, TypeError) as exc:
        raise FixtureError(f"{manifest} states no hotel_id and period") from exc
    try:
        return hotel, Period.parse(period_text)
    except ValueError as exc:
        raise FixtureError(f"{manifest}: {period_text!r} is not a period: {exc}") from exc


def score_fixture(
    directory: Path,
    *,
    policy: Policy | None = None,
    out_root: Path | None = None,
) -> FixtureScore:
    """Run one fixture in replay and score the verdict it published.

    `ExpectationError` and `FixtureError` propagate: a fixture that cannot be read has not produced
    a result, and inventing a failing one would put "the pipeline got this wrong" in a report where
    the truth is "nobody asked it".
    """
    expectation = load_expectation(directory / EXPECTED_FILE)
    submission = directory / SUBMISSION_DIR
    if not submission.is_dir():
        raise FixtureError(
            f"{directory} has no {SUBMISSION_DIR}/ directory. A fixture is a copy of the demo "
            "submission with one declared mutation applied; without it there is nothing to verify."
        )
    hotel, period = declaration_for(directory)
    policy = policy or load_policy()
    out = (out_root or DEFAULT_OUT_ROOT) / expectation.fixture_id

    started = time.perf_counter()
    try:
        verdict = _run(submission, hotel, period, policy, out)
        narrative = grade_narratives(verdict, ReplayProvider(), policy)
    except Exception as exc:
        return _not_measured(expectation, exc, _elapsed(started))
    duration_ms = _elapsed(started)

    checks = (*score(verdict, expectation), *(narrative_check(n) for n in narrative))
    return FixtureScore(
        fixture_id=expectation.fixture_id,
        why=expectation.why,
        mutation=expectation.mutation.render(),
        expected=expectation.rendered,
        observed=observed(verdict),
        outcome=verdict_for(checks),
        checks=checks,
        tally=tally_for(verdict, expectation) + narrative_tally_for(narrative),
        narrative=narrative,
        duration_ms=duration_ms,
    )


def score_fixtures(
    root: Path | None = None,
    *,
    policy: Policy | None = None,
    out_root: Path | None = None,
) -> tuple[FixtureScore, ...]:
    """Every fixture under `root`, scored. The policy is loaded once and shared.

    One `Policy` for the whole sweep rather than one per fixture, because the ruleset is what the
    figures are defensible against: six fixtures scored under six loads of the same file would
    agree today and hide the day they stopped agreeing.
    """
    policy = policy or load_policy()
    return tuple(
        score_fixture(directory, policy=policy, out_root=out_root)
        for directory in find_fixtures(root or DEFAULT_FIXTURES_ROOT)
    )


def run_fixture(directory: Path, policy: Policy, *, out_root: Path | None = None) -> Verdict:
    """One fixture's pipeline run, published and read back, with no scoring attached.

    The half of `score_fixture` that recording the narrative/critic cassettes also needs: a real
    `Verdict` for a fixture that already exists, run in replay against its already-recorded
    mapping cassettes. Kept separate from `score_fixture` rather than factored into it, so a change
    here cannot alter what `score_fixture` measures.
    """
    submission = directory / SUBMISSION_DIR
    if not submission.is_dir():
        raise FixtureError(
            f"{directory} has no {SUBMISSION_DIR}/ directory. A fixture is a copy of the demo "
            "submission with one declared mutation applied; without it there is nothing to verify."
        )
    hotel, period = declaration_for(directory)
    out = (out_root or DEFAULT_OUT_ROOT) / directory.name
    return _run(submission, hotel, period, policy, out)


def _run(submission: Path, hotel: str, period: Period, policy: Policy, out: Path) -> Verdict:
    """One pipeline invocation, published and read back. See the module docstring on the round trip."""
    provider = ReplayProvider()
    context = RunContext.build(policy, period, provider)
    result = verify_directory(
        submission, hotel, period, policy, provider, run_id=new_run_id(), context=context
    )
    path, _ = write_verdict(out, result.verdict)
    return read_verdict(path)


def _not_measured(expectation: Expectation, exc: Exception, duration_ms: int) -> FixtureScore:
    """A fixture that did not produce a verdict, reported as unmeasured or as an error.

    A cassette miss is `NOT_RECORDED` and everything else is `ERRORED`, and neither is a pass. The
    cause chain is walked because a miss that reached here inside another exception is still a
    miss, and classifying it as an error would send somebody looking for a defect in the pipeline
    when the answer is `make record`.
    """
    miss = _cassette_miss(exc)
    if miss is not None:
        return FixtureScore(
            fixture_id=expectation.fixture_id,
            why=expectation.why,
            mutation=expectation.mutation.render(),
            expected=expectation.rendered,
            observed="not measured",
            outcome=Outcome.NOT_RECORDED,
            checks=(Check("run.replayed", False, str(miss)),),
            tally=Tally(material_expected=len(expectation.material_keys)),
            duration_ms=duration_ms,
            detail="no cassette for a request this run made; run `make record`",
        )
    return FixtureScore(
        fixture_id=expectation.fixture_id,
        why=expectation.why,
        mutation=expectation.mutation.render(),
        expected=expectation.rendered,
        observed="no verdict",
        outcome=Outcome.ERRORED,
        checks=(Check("run.completed", False, f"{type(exc).__name__}: {exc}"),),
        tally=Tally(material_expected=len(expectation.material_keys)),
        duration_ms=duration_ms,
        detail=f"{type(exc).__name__}: {exc}",
    )


def _cassette_miss(exc: BaseException) -> CassetteMissError | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, CassetteMissError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
