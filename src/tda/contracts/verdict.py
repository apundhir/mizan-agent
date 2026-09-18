"""The verdict — the artefact a verification officer files, and the contract a downstream system
reads.

Two structural commitments live here.

**Definitional items are a separate array** (D-MAT-06). Not a flag on a shared list, not a
filter applied at render time — a different field, so a caller that iterates `findings` and
counts them as hotel errors gets the right answer by default rather than by remembering. The
validator refuses a verdict that puts a V2 in `findings` or anything else in
`definitional_items`.

**Every number carries its ruleset** (D-EV-04). `policy_version` and `metric_library_version`
are required. A verdict that cannot say which rules produced it is not defensible six months
later when a hotel disputes a fee.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tda.contracts.claim import NotVerifiable
from tda.contracts.metric_key import MetricKey
from tda.contracts.variance import Finding, Severity, VarianceClass


class VerdictStatus(StrEnum):
    """The five outcomes. There is no `PARTIAL` and no `UNKNOWN`: a verification that cannot
    say which of these applies has not finished."""

    PASS = "PASS"
    FAIL = "FAIL"
    ESCALATED = "ESCALATED"
    REJECTED = "REJECTED"
    HALTED = "HALTED"


class RejectionReason(StrEnum):
    """Why intake refused the submission, before any extraction was attempted.

    A stated reason code rather than a message, so the hotel gets the same answer every time
    and the coordinator can act on it without interpreting prose.
    """

    INCOMPLETE_FILE_SET = "incomplete_file_set"
    HOTEL_MISMATCH = "hotel_mismatch"
    PERIOD_MISMATCH = "period_mismatch"
    UNREADABLE_FILE = "unreadable_file"


class ReviewerDecision(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    AMEND = "amend"


class ReviewRecord(BaseModel):
    """A human decision against one finding (FR-11).

    The reviewer's name and the timestamp are required because the point of the review gate is
    that somebody accountable looked. An anonymous decision records that a button was pressed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(pattern=r"^F-[0-9]{4}$")
    decision: ReviewerDecision
    reviewer: str = Field(min_length=1)
    decided_at: datetime = Field(description="Timezone-aware. Excluded from the repro diff.")
    note: str | None = None
    amended_value: str | None = Field(
        default=None, description="Required when the decision is AMEND. Validated below."
    )

    @model_validator(mode="after")
    def _amend_carries_a_value(self) -> Self:
        if self.decision is ReviewerDecision.AMEND and self.amended_value is None:
            raise ValueError("an AMEND decision must carry the amended value")
        if self.decision is not ReviewerDecision.AMEND and self.amended_value is not None:
            raise ValueError(f"a {self.decision.value} decision must not carry an amended value")
        if self.decided_at.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware")
        return self


class ExtractionSummary(BaseModel):
    """What extraction managed, so a reader can judge the verdict's foundation.

    `printed_total_matched` is the one that matters: if the extracted totals did not reconcile
    against the totals printed on the PDF itself, nothing below it is trustworthy and the run
    should have halted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[str, ...] = Field(min_length=1)
    records_extracted: int = Field(ge=0)
    pages_read: int = Field(ge=0)
    printed_total_matched: bool
    duplicate_ids: int = Field(ge=0)
    unmapped_labels: tuple[str, ...] = ()


class Verdict(BaseModel):
    """The complete result of one verification run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(
        min_length=1,
        description=(
            "The run that produced this verdict. Minted per run, so two runs of one submission "
            "carry different ids. Excluded from the repro diff."
        ),
    )
    status: VerdictStatus
    rejection_reason: RejectionReason | None = None

    hotel_id: str = Field(min_length=1)
    period: str = Field(description="The quarter verified, e.g. 2026-Q1.")

    # Provenance. Required, because a number without its ruleset is not defensible (D-EV-04).
    policy_version: str = Field(min_length=1)
    metric_library_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    provider_mode: str = Field(min_length=1, examples=["replay", "anthropic", "stub"])
    prompt_versions: dict[str, str] = Field(
        default_factory=dict, description="agent name -> prompt version, per call site."
    )

    extraction: ExtractionSummary | None = Field(
        default=None, description="None only when intake rejected before extraction ran."
    )
    claims_checked: int = Field(ge=0)

    findings: tuple[Finding, ...] = Field(
        default=(), description="Clerical and blocking findings. Never contains a V2."
    )
    definitional_items: tuple[Finding, ...] = Field(
        default=(),
        description="V2 only, carried separately so they are never counted as hotel errors (D-MAT-06).",
    )
    not_verifiable: tuple[NotVerifiable, ...] = ()
    out_of_scope_claims: tuple[MetricKey, ...] = Field(
        default=(),
        description="Claims we did not verify, recorded so silence is not mistaken for approval "
        "(D-SCOPE-02).",
    )

    review_records: tuple[ReviewRecord, ...] = ()

    @model_validator(mode="after")
    def _verdict_is_coherent(self) -> Self:
        misfiled = [
            f.finding_id for f in self.findings if f.variance_class is VarianceClass.DEFINITIONAL
        ]
        if misfiled:
            raise ValueError(
                f"definitional findings in `findings`: {misfiled}. They belong in "
                "`definitional_items` - a hotel-error count that includes policy disagreements is "
                "a wrong number presented as a right one (D-MAT-06)"
            )

        not_definitional = [
            f.finding_id
            for f in self.definitional_items
            if f.variance_class is not VarianceClass.DEFINITIONAL
        ]
        if not_definitional:
            raise ValueError(f"non-V2 findings in `definitional_items`: {not_definitional}")

        ids = [f.finding_id for f in (*self.findings, *self.definitional_items)]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate finding ids: {duplicates}")

        unknown = sorted({r.finding_id for r in self.review_records} - set(ids))
        if unknown:
            raise ValueError(f"review records reference unknown findings: {unknown}")

        if self.status is VerdictStatus.REJECTED and self.rejection_reason is None:
            raise ValueError("a REJECTED verdict must carry a stated reason code")
        if self.status is not VerdictStatus.REJECTED and self.rejection_reason is not None:
            raise ValueError(f"a {self.status.value} verdict must not carry a rejection reason")

        # PASS means "zero findings". Not "zero findings I considered important".
        if self.status is VerdictStatus.PASS and (self.findings or self.definitional_items):
            raise ValueError(
                f"PASS with {len(self.findings)} findings and "
                f"{len(self.definitional_items)} definitional items - PASS means zero of both"
            )

        blocking = [f.finding_id for f in self.findings if f.severity is Severity.BLOCKING]
        if blocking and self.status not in (VerdictStatus.HALTED, VerdictStatus.ESCALATED):
            raise ValueError(
                f"blocking findings {blocking} but status is {self.status.value} - a run that "
                "could not read something is HALTED or ESCALATED, never FAIL or PASS"
            )
        return self

    # ── counts the memo's first block is built from ──────────────────────────

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for finding in (*self.findings, *self.definitional_items):
            counts[finding.severity.value] += 1
        return counts

    @property
    def hotel_error_count(self) -> int:
        """Only V1 and V5. Definitional items and extraction limits are excluded by construction."""
        return sum(1 for f in self.findings if f.is_hotel_error)

    @property
    def standing_decisions(self) -> dict[str, ReviewRecord]:
        """The decision currently in force for each finding.

        Decisions are **appended**, never overwritten: an officer who accepts a finding and later
        rejects it has done something an auditor needs to see, and replacing the first record would
        destroy exactly the evidence a review gate exists to produce. The last record for a finding
        is therefore the one standing, and every earlier one is still in `review_records`.

        It lives on the contract rather than in `tda.review` because the memo asks the same
        question, and two implementations of "what does this verdict say now" would eventually give
        a supervisor and a screen two different answers about the same run.
        """
        standing: dict[str, ReviewRecord] = {}
        for record in self.review_records:
            standing[record.finding_id] = record
        return standing

    @property
    def undecided_findings(self) -> tuple[str, ...]:
        """Findings with no recorded reviewer decision.

        The review gate exists so nothing material passes without a human decision (FR-10). This
        property is how the review screen knows it is not finished.
        """
        decided = {r.finding_id for r in self.review_records}
        return tuple(
            f.finding_id
            for f in (*self.findings, *self.definitional_items)
            if f.finding_id not in decided
        )
