"""The result vocabulary for fixture scoring: an outcome, a check, and a report that says what it
did not measure.

Deliberately free of every contract in this system. Nothing here knows what a `Finding` is, which
is what lets `score.py` decide *whether* a fixture matched and this module decide only how that is
counted, rendered and serialised. The split matters because the report is the part somebody reads
in CI, and a renderer that could reach into a verdict would eventually start deciding things.

## No partial credit

`verdict_for` is all or nothing. A fixture that matched nine of ten properties has not partly
verified anything: it produced a verdict that differs from the derived expectation, and the only
useful question is which property moved. A percentage invites somebody to decide, in a spreadsheet
nobody reviews, which of the ten failures is acceptable.

`verdict_for(())` is therefore a failure and not a pass. An empty check set means the scorer
asserted nothing, and "nothing was asserted" reported as green is the one failure mode an eval
harness cannot survive.

## Durations are recorded, not targeted

Every duration here carries that phrase wherever it is rendered. This project quotes no time
figure as an achievement anywhere, because the manual baseline it would be compared against has
never been measured, and a number with no baseline becomes a claim the moment it is read aloud.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

TIMING_NOTE = (
    "Durations are recorded, not targeted. No manual baseline has been measured, so none of these "
    "figures is a performance claim."
)


class Outcome(StrEnum):
    """How one fixture came out.

    `NOT_RECORDED` is separate from `FAILED` because "we have not measured this" and "this is
    wrong" are different statements. It is not, however, a skip: a fixture whose cassettes are
    missing is unmeasured, unmeasured is not a pass, and the exit code says so.
    """

    PASSED = "passed"
    FAILED = "failed"
    NOT_RECORDED = "not_recorded"
    ERRORED = "errored"


@dataclass(frozen=True, slots=True)
class Check:
    """One property the verdict must have, and enough detail to act on when it does not.

    `detail` is filled on a pass as well as a failure. A passing check whose detail says which
    value it compared is what tells a reader the check was looking at the right thing, and a
    scorer that only explains itself when it fails is a scorer nobody can audit.
    """

    name: str
    passed: bool
    detail: str = ""

    def render(self) -> str:
        mark = "ok  " if self.passed else "FAIL"
        return f"    {mark} {self.name}{f' - {self.detail}' if self.detail else ''}"


@dataclass(frozen=True, slots=True)
class NarrativeResult:
    """One finding's narrative, graded by the critic.

    Free of `Finding` and `CriticVerdict` on purpose, matching the split this module's own
    docstring already draws: `tda.eval.narrative` knows what a `Finding` and a `CriticVerdict` are
    and converts one of the latter into this before it reaches here, so this file's claim to know
    no contract in the system stays true of the critic's contract as well as of `Finding`'s.
    """

    finding_id: str
    sentence: str
    passed: bool
    failures: tuple[str, ...]
    reason: str

    def render(self) -> str:
        mark = "ok  " if self.passed else "FAIL"
        detail = f" - {', '.join(self.failures)}: {self.reason}" if not self.passed else ""
        return f"    {mark} narrative[{self.finding_id}]{detail}"


def narrative_check(result: NarrativeResult) -> Check:
    """One narrative result, as the ordinary `Check` that makes it fail the fixture.

    Folded into the same `checks` tuple `verdict_for` already decides `Outcome` from, rather than
    into a second gate: a narrative that leaks a number or reads as an accusation should fail a
    fixture by the identical mechanism a numeric mismatch does, not by a rule that has to be kept in
    sync with it.
    """
    detail = f"{', '.join(result.failures)}: {result.reason}" if not result.passed else "clean"
    return Check(f"narrative[{result.finding_id}]", result.passed, detail)


@dataclass(frozen=True, slots=True)
class Tally:
    """The aggregate numbers, accumulated across fixtures.

    Two of these are counts rather than checks, and the distinction is deliberate.
    `absences_with_reason` and `absences_without_reason` measure the evidence promise: every
    finding cites something openable on at least one side, and every typed absence carries its
    reason. The `Finding` contract already makes the second unreachable, so asserting it would be a
    check that cannot fail while reading like one that can. Counted instead, so the scorecard
    carries the evidence that the property holds rather than an assertion that it must.

    The evidence target is emphatically **not** "both a page and a cell on every finding". That is
    unsatisfiable by construction: the contract permits one typed absence for a V5 with no claimed
    value and for a V7. Counting it as a coverage failure would report the contract working
    correctly as a defect.
    """

    material_expected: int = 0
    material_matched: int = 0
    class_expected: int = 0
    class_matched: int = 0
    controls: int = 0
    control_false_positives: int = 0
    false_positives: int = 0
    findings_seen: int = 0
    evidence_openable: int = 0
    absences_with_reason: int = 0
    absences_without_reason: int = 0
    narratives_graded: int = 0
    narratives_passed: int = 0

    def __add__(self, other: Tally) -> Tally:
        return Tally(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in Tally.__dataclass_fields__
            }
        )

    @property
    def recall(self) -> float | None:
        """Detection rate over the planted material errors. `None` when nothing was planted.

        `None` rather than 1.0, because a recall of 100% over zero opportunities is the number that
        makes a harness look best exactly when it measured least.
        """
        if not self.material_expected:
            return None
        return self.material_matched / self.material_expected

    @property
    def narrative_pass_rate(self) -> float | None:
        """The fraction of graded narratives the critic passed. `None` when nothing was graded.

        `None` rather than 1.0 for the same reason `recall` is: a pass rate of 100% over zero
        narratives is the number that makes the harness look best exactly when it measured least.
        """
        if not self.narratives_graded:
            return None
        return self.narratives_passed / self.narratives_graded

    def render(self) -> str:
        recall = "not measured (nothing planted)"
        if self.recall is not None:
            recall = (
                f"{self.material_matched}/{self.material_expected} "
                f"({self.recall * 100:.0f}%) planted material errors found"
            )
        narrative = "not measured (no findings to narrate)"
        if self.narrative_pass_rate is not None:
            narrative = (
                f"{self.narratives_passed}/{self.narratives_graded} "
                f"({self.narrative_pass_rate * 100:.0f}%) narratives passed the critic"
            )
        return "\n".join(
            [
                f"- Recall: {recall}",
                f"- False positives on controls: {self.control_false_positives} "
                f"across {self.controls} control fixture(s)",
                f"- Unexpected findings, all fixtures: {self.false_positives}",
                f"- Variance class matched: {self.class_matched}/{self.class_expected}",
                f"- Evidence openable on at least one side: "
                f"{self.evidence_openable}/{self.findings_seen} finding(s)",
                f"- Typed absences carrying a reason: {self.absences_with_reason} "
                f"({self.absences_without_reason} without)",
                f"- Narrative grading: {narrative}",
            ]
        )


@dataclass(frozen=True, slots=True)
class FixtureScore:
    """One fixture, scored, with everything the report needs and nothing it has to look up.

    `why` and `mutation` are carried rather than referenced so that a failing row can be read
    without opening the fixture spec. The reader of a red CI job is usually not the person who
    wrote the mutation.
    """

    fixture_id: str
    why: str
    mutation: str
    expected: str
    observed: str
    outcome: Outcome
    checks: tuple[Check, ...] = ()
    tally: Tally = field(default_factory=Tally)
    narrative: tuple[NarrativeResult, ...] = ()
    duration_ms: int = 0
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED

    def render(self) -> str:
        head = f"  {self.fixture_id}: {self.outcome.value}"
        if self.detail:
            head += f" ({self.detail})"
        return "\n".join(
            [
                head,
                f"    why: {self.why}",
                f"    mutation: {self.mutation}",
                f"    expected: {self.expected}",
                f"    observed: {self.observed}",
                *(check.render() for check in self.checks if not check.passed),
            ]
        )


def verdict_for(checks: tuple[Check, ...]) -> Outcome:
    """A fixture passes only if every check does, and an empty check set never passes.

    There is no partial credit: a partial score invites somebody to decide which failures are
    acceptable in a spreadsheet nobody reviews. An empty set is a failure because it means the
    scorer asserted nothing, and a scorer that asserts nothing reports green forever.
    """
    if not checks:
        return Outcome.FAILED
    return Outcome.PASSED if all(check.passed for check in checks) else Outcome.FAILED


def is_complete(results: tuple[FixtureScore, ...]) -> bool:
    """Whether every fixture was actually measured.

    An empty result set is incomplete, not complete-and-clean. A scorecard covering no fixtures is
    the shape a broken fixture tree produces, and reporting it as a pass is how a silently empty
    eval survives a release.
    """
    if not results:
        return False
    return all(result.outcome in (Outcome.PASSED, Outcome.FAILED) for result in results)


def tallies(results: tuple[FixtureScore, ...]) -> dict[Outcome, int]:
    return {outcome: sum(1 for r in results if r.outcome is outcome) for outcome in Outcome}


def narrative_tally_for(narrative: tuple[NarrativeResult, ...]) -> Tally:
    """One fixture's contribution to the narrative-grading aggregate.

    Mirrors `tda.eval.score.tally_for`'s shape for the numeric side: a `Tally` carrying only the
    fields this dimension measures, added into the fixture's tally the same way.
    """
    return Tally(
        narratives_graded=len(narrative),
        narratives_passed=sum(1 for n in narrative if n.passed),
    )


def aggregate(results: tuple[FixtureScore, ...]) -> Tally:
    total = Tally()
    for result in results:
        total = total + result.tally
    return total


def render_report(results: tuple[FixtureScore, ...]) -> str:
    """The report `make eval` writes and prints, in the order a reader needs it.

    The table first, because the question is always "which one is red". The aggregate second. What
    was *not* measured last and in words, never folded into a pass rate: a rate computed over the
    fixtures that happened to have cassettes is a number that improves when recordings are deleted.
    """
    counts = tallies(results)
    lines = [
        "# Fixture evals",
        "",
        f"{counts[Outcome.PASSED]} passed, {counts[Outcome.FAILED]} failed, "
        f"{counts[Outcome.NOT_RECORDED]} not recorded, {counts[Outcome.ERRORED]} errored"
        f"{'' if is_complete(results) else '  (INCOMPLETE)'}",
        "",
        "| Fixture | Mutation | Expected | Observed | Result | Why |",
        "|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {r.fixture_id} | {r.mutation} | {r.expected} | {r.observed} "
        f"| {r.outcome.value} | {r.why} |"
        for r in results
    )
    lines.extend(["", "## Aggregate", "", aggregate(results).render()])

    failures = tuple(r for r in results if not r.passed)
    if failures:
        lines.extend(["", "## What did not match", ""])
        lines.extend(r.render() for r in failures)

    graded = tuple(r for r in results if r.narrative)
    if graded:
        lines.extend(["", "## Narrative grading", ""])
        for r in graded:
            lines.append(f"  {r.fixture_id}:")
            lines.extend(n.render() for n in r.narrative)

    if counts[Outcome.NOT_RECORDED]:
        lines.extend(
            [
                "",
                "## Not recorded",
                "",
                "A fixture reported `not_recorded` has no cassette for a request the run made. It "
                "is unmeasured, which is not the same as passing, and it is counted as a failure "
                "here and in the exit code. Run `make record` with an API key and review the "
                "cassette diff.",
            ]
        )

    lines.extend(
        [
            "",
            "## Timing",
            "",
            TIMING_NOTE,
            "",
            *(f"- {r.fixture_id}: {r.duration_ms:,}ms (recorded, not targeted)" for r in results),
            "",
        ]
    )
    return "\n".join(lines)


def scorecard(results: tuple[FixtureScore, ...]) -> dict[str, Any]:
    """The machine-readable half. `complete` is first-class rather than inferred from the counts.

    A consumer that had to derive completeness by comparing a fixture count against a directory
    listing would get it wrong, and the way it gets it wrong is by treating an empty run as a
    clean one.
    """
    total = aggregate(results)
    return {
        "complete": is_complete(results),
        "fixtures_scored": len(results),
        "outcomes": {outcome.value: count for outcome, count in tallies(results).items()},
        "aggregate": {
            **{name: getattr(total, name) for name in Tally.__dataclass_fields__},
            "recall": total.recall,
        },
        "timing_note": TIMING_NOTE,
        "fixtures": [
            {
                "fixture_id": r.fixture_id,
                "why": r.why,
                "mutation": r.mutation,
                "expected": r.expected,
                "observed": r.observed,
                "outcome": r.outcome.value,
                "detail": r.detail,
                "duration_ms_recorded_not_targeted": r.duration_ms,
                "checks": [
                    {"name": c.name, "passed": c.passed, "detail": c.detail} for c in r.checks
                ],
                "narrative": [
                    {
                        "finding_id": n.finding_id,
                        "sentence": n.sentence,
                        "passed": n.passed,
                        "failures": list(n.failures),
                        "reason": n.reason,
                    }
                    for n in r.narrative
                ],
            }
            for r in results
        ],
    }


def write_scorecard(path: Path, results: tuple[FixtureScore, ...]) -> None:
    """Sorted keys and a trailing newline, matching every other artefact this repo writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(scorecard(results), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
