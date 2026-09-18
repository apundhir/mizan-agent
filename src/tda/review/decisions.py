"""Recording what a human decided, into the verdict, with their name on it.

FR-10 and FR-11: nothing material passes without a human decision, and the decision says who made
it and when. `ReviewRecord` has required that since M1 — a reviewer name, a timezone-aware
timestamp, and an amended value whenever the decision is AMEND. This module is what finally writes
one.

## Why the screen is a shell over this, and not the other way round

Everything here is headless and takes paths and strings. The Streamlit app calls it; so could a
console flow, which is the fallback PRD-91 names — *"the console flow must record the identical
decision structure so the verdict schema does not change with the fallback."* That guarantee is
free if there is one recorder and expensive if there are two, so there is one.

It also means the review gate is testable without driving a browser, which is the difference
between a gate that is asserted and a gate that is hoped for.

## Decisions are appended, never overwritten

An officer who accepts a finding and later rejects it has done something an auditor needs to see.
Overwriting the first record would destroy exactly the evidence a review gate exists to produce —
so both are kept, in order, and `latest_by_finding` answers "what does this verdict say now".

## The verdict is re-read immediately before every write

Not cached from when the screen loaded. Two officers reviewing one run would otherwise have the
second write silently drop the first's decisions, and neither would ever know. Re-reading and
appending to what is actually on disk *is* the merge.

## Every rule about a decision lives here, not in the screen

"Amend only applies to a transcription error" was a `disabled=` on a Streamlit button, which meant
the rule held for the surface that happened to implement it and not for the gate. A console flow —
the fallback PRD-91 names — would have recorded a corrected figure against a definitional variance,
which is telling a hotel to change a number that is not wrong. A rule enforced in one of two front
ends is a rule with a hole in it.

## The write is undone if the memo cannot follow

The verdict is written first and the memo second, so a failure in between used to leave exactly the
state the paragraph below calls impossible: a verdict recording a decision beside a memo saying
nobody had looked. The verdict's previous bytes are kept and restored if the memo raises, so a
decision is either in both artefacts or in neither.

## The memo is re-issued with every decision

`memo.docx`'s signature block is derived from `review_records`: leaving it alone would mean a
verdict recording three decisions sitting next to a memo that says *"No human review has been
recorded against this verdict"*. Both are handed to the same supervisor. One of them would be
lying, and it is the one written in Word that gets forwarded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from tda.contracts import ReviewerDecision, ReviewRecord, VarianceClass
from tda.outputs.memo import write_memo
from tda.outputs.verdict import VERDICT_FILE, read_verdict, write_verdict

if TYPE_CHECKING:
    from decimal import Decimal
    from pathlib import Path

    from tda.contracts import Finding, Verdict
    from tda.outputs.verdict import VerdictDocument


# A signature block and a JSON field both have to hold the reviewer's name, so it is one line
# and bounded. Unbounded, a paste of an entire email thread ends up in the memo.
MAX_NAME: Final = 120


class ReviewError(ValueError):
    """A decision that cannot be recorded, with the reason a reviewer needs to fix it.

    A `ValueError` subclass because every case is a bad argument — an empty name, an unknown
    finding, an amendment with no value — and a distinct type so a UI can tell "you left the name
    blank" apart from a failure to write the file.
    """


@dataclass(frozen=True, slots=True)
class Recorded:
    """What a recorded decision changed on disk."""

    verdict: VerdictDocument
    verdict_path: Path
    memo_path: Path
    record: ReviewRecord

    def render(self) -> str:
        return (
            f"  {self.record.finding_id}: {self.record.decision.value} by {self.record.reviewer}\n"
            f"  {self.verdict_path}\n  {self.memo_path}"
        )


def record_decision(
    run: Path,
    *,
    finding_id: str,
    decision: ReviewerDecision,
    reviewer: str,
    note: str | None = None,
    amended_value: Decimal | None = None,
    at: datetime | None = None,
) -> Recorded:
    """Record one decision into a run's verdict, and re-issue its memo.

    `at` is injectable so a test can assert on a timestamp; it defaults to now, in UTC, because
    `ReviewRecord` refuses a naive one. A decision whose timezone is whatever the reviewer's laptop
    thought is not a timestamp anybody can reconcile against a submission deadline.
    """
    verdict_path = run / VERDICT_FILE
    if not verdict_path.is_file():
        raise ReviewError(
            f"no {VERDICT_FILE} in {run}. A review is a decision about a verdict, and this run "
            "has not produced one - check the run id, or the run's status."
        )

    name = reviewer.strip()
    if not name:
        raise ReviewError(
            "a decision must name the person who made it. The point of the review gate is that "
            "somebody accountable looked; an anonymous decision records that a button was pressed."
        )
    if len(name) > MAX_NAME or "\n" in name or "\r" in name:
        raise ReviewError(
            f"{name[:40]!r}… is not a name. A signature block and a JSON field both have to hold "
            f"it, so it is one line and at most {MAX_NAME} characters."
        )

    # Re-read rather than take a caller's copy. See the module docstring on two reviewers.
    current = read_verdict(verdict_path)
    known = {f.finding_id for f in (*current.findings, *current.definitional_items)}
    if finding_id not in known:
        raise ReviewError(
            f"{finding_id} is not in this verdict. It has {sorted(known) or 'no findings'}."
        )
    finding = next(
        f for f in (*current.findings, *current.definitional_items) if f.finding_id == finding_id
    )
    _check_amendment(finding, decision, amended_value)

    record = ReviewRecord(
        finding_id=finding_id,
        decision=decision,
        reviewer=name,
        decided_at=at or datetime.now(UTC),
        note=(note.strip() or None) if note else None,
        amended_value=str(amended_value) if amended_value is not None else None,
    )
    updated = current.model_copy(update={"review_records": (*current.review_records, record)})

    previous = verdict_path.read_bytes()
    written, _ = write_verdict(run, updated)
    try:
        # Re-read so the memo is rendered from the bytes on disk, as ADR-0007 requires of every
        # document downstream of the verdict.
        memo = write_memo(run, read_verdict(written))
    except Exception as exc:
        # Put the verdict back. A decision recorded in `verdict.json` with a memo still saying "No
        # human review has been recorded" is the disagreement this module exists to prevent, and
        # the officer - who just saw an error - would reasonably believe nothing was recorded.
        verdict_path.write_bytes(previous)
        raise ReviewError(
            f"{finding_id} was not recorded: the memo could not be re-issued "
            f"({type(exc).__name__}: {exc}). The verdict is unchanged."
        ) from exc
    return Recorded(
        verdict=read_verdict(written), verdict_path=written, memo_path=memo, record=record
    )


def _check_amendment(
    finding: Finding, decision: ReviewerDecision, amended_value: Decimal | None
) -> None:
    """The three rules about a corrected figure, in the gate rather than in a button.

    `Finding` already refuses `proposed_correction` on anything but a V1, for the reason restated
    below. This is the same rule on the review side, where a human is the one proposing.
    """
    if decision is not ReviewerDecision.AMEND:
        if amended_value is not None:
            raise ReviewError(
                f"a {decision.value} decision must not carry an amended value. Only an amendment "
                "changes a figure."
            )
        return
    if amended_value is None:
        raise ReviewError(
            f"amending {finding.finding_id} requires the amended value. An AMEND with no figure "
            "says the number is wrong without saying what it should be, which leaves the hotel "
            "nothing to act on."
        )
    if not amended_value.is_finite():
        raise ReviewError(f"{amended_value} is not a figure a hotel can restate a return to.")
    if finding.variance_class is not VarianceClass.TRANSCRIPTION:
        raise ReviewError(
            f"{finding.finding_id} is a {finding.variance_class.value} and cannot be amended. A "
            "corrected figure is only meaningful for a transcription error - proposing one for a "
            "definitional variance tells a hotel to change a number that is not wrong (D-MAT-06)."
        )


def superseded(verdict: Verdict, finding_id: str) -> tuple[ReviewRecord, ...]:
    """Earlier decisions on one finding, oldest first. Empty unless somebody changed their mind."""
    for_finding = [r for r in verdict.review_records if r.finding_id == finding_id]
    return tuple(for_finding[:-1])


def undecided(verdict: Verdict) -> tuple[Finding, ...]:
    """Findings nobody has decided on yet, in the order the verdict lists them.

    `Verdict.undecided_findings` gives the ids; the screen needs the findings themselves, and
    deriving them twice in two places is how two surfaces come to disagree about what is left.
    """
    outstanding = set(verdict.undecided_findings)
    return tuple(
        finding
        for finding in (*verdict.findings, *verdict.definitional_items)
        if finding.finding_id in outstanding
    )
