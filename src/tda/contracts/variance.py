"""Variance, classification and the `Finding` type.

`Finding` is where this POC's central promise is made unbreakable. Four rules that would
otherwise be review conventions are validators here, so the *only* way to produce a
non-compliant finding is to fail construction:

1. **A finding always cites something** (D-EV-01). `source_ref` is where the computed side came
   from — PDF rows, or the inventory reference for `room_nights_available`, which has no page at
   all because rooms available is a property attribute rather than a reservation one (D-RNA-01).
   Each reference may be a typed absence in the one case that warrants it, and never both at once:
   `excel_ref` accepts `NotReached` only on a V7, where extraction halted before the workbook was
   parsed (D-EV-02), and `source_ref` accepts it only where no source was consulted, which is the
   workbook self-consistency check the claim parser runs before any comparison (D-EV-05). Two
   absences would be a finding with nothing behind it on either side, and that is refused.
2. **A definitional finding must name the permutation that explains it** (D-CLS-07). A V2
   without `explaining_permutation` cannot exist, so "this is a policy difference, trust us"
   is not expressible.
3. **A correction is proposed only for transcription** (V1). Proposing a corrected value for a
   definitional variance would be telling a hotel to change a number that is not wrong.
4. **V7 is always blocking** (D-MAT-01). A finding that says "I could not read this" cannot be
   filed as informational — and it is the one class permitted to carry neither a claimed nor a
   computed value, because a refusal reports that no value could be established rather than a
   difference between two.

The `narrative` field is the only place free model text lives, and it is deliberately the
last field: everything a reviewer acts on is already determined before it is written. The
model writes the sentence; the code found the cause.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tda.contracts.metric_key import MetricKey
from tda.contracts.refs import ExcelCitation, NotReached, SourceCitation


class VarianceClass(StrEnum):
    """The cause of a variance. The gap in numbering (no V3, V4) is the PRD's own and is
    preserved rather than tidied: renumbering would break every citation written against it."""

    TRANSCRIPTION = "V1"
    DEFINITIONAL = "V2"
    COMPLETENESS = "V5"
    ROUNDING = "V6"
    EXTRACTION_LIMIT = "V7"


class Severity(StrEnum):
    BLOCKING = "blocking"
    MATERIAL = "material"
    INFORMATIONAL = "informational"


class EscalationTarget(StrEnum):
    """Who a finding goes to. Getting this wrong is how a correct finding becomes an insult:
    a definitional variance sent to the hotel reads as an accusation of carelessness about a
    rule nobody ever agreed."""

    HOTEL = "hotel"
    POLICY_OWNER = "policy_owner"
    HUMAN_REVIEW = "human_review"
    NONE = "none"


class FindingIds:
    """`F-0001`, `F-0002`, … in the order findings were raised.

    Sequential and stable within a run, because a reviewer's recorded decision references the id
    (`ReviewRecord.finding_id`). Ids derived from content would change when a message was reworded,
    silently orphaning every decision already made against the previous wording.

    Lives here, beside `Finding`, because three layers now issue ids - PDF extraction, the
    Excel claim parser and reconciliation - and the alternative was `tda.reconcile` importing
    `tda.extract` to borrow a counter. That import would couple the deterministic core to the
    extraction layer for the sake of eighteen lines, and ADR-0001's whole claim is that the
    core depends on nothing that reads the world.
    """

    def __init__(self) -> None:
        self._issued = 0

    def take(self) -> str:
        self._issued += 1
        if self._issued > 9999:
            raise RuntimeError(
                "more than 9999 findings in one run. The id format is F-nnnn, and a run producing "
                "this many has a systematic failure that should have halted long before here."
            )
        return f"F-{self._issued:04d}"


class Finding(BaseModel):
    """One variance a reviewer must decide on, with everything needed to decide it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(
        pattern=r"^F-[0-9]{4}$",
        description="Stable within a run. Referenced by the reviewer's recorded decision.",
    )
    key: MetricKey
    variance_class: VarianceClass
    severity: Severity
    escalates_to: EscalationTarget

    claimed: Decimal | None = Field(
        default=None, description="None when the workbook omitted the figure entirely (V5)."
    )
    computed: Decimal | None = Field(
        default=None,
        description="None when the source could not support any value - a V7, or an orphan claim.",
    )
    difference: Decimal | None = Field(
        default=None, description="claimed - computed. None when either side is absent."
    )

    proposed_correction: Decimal | None = Field(
        default=None, description="Transcription only. Validated below."
    )
    explaining_permutation: str | None = Field(
        default=None,
        pattern=r"^P-[A-Z0-9]+(-[A-Z0-9]+)*$",
        description="Required for V2, forbidden otherwise. Validated below (D-CLS-07).",
    )
    also_explained_by: tuple[str, ...] = Field(
        default=(),
        description=(
            "Other permutations that also reproduce the claim. Recorded because naming one cause "
            "as certain when several fit the evidence is a false precision (D-CLS-09)."
        ),
    )

    source_ref: SourceCitation = Field(
        description=(
            "Where the computed side came from: PDF rows, or the inventory reference for "
            "room_nights_available, which has no page because rooms available is a property "
            "attribute (D-RNA-01). Or an explained absence where no source was consulted at all - a "
            "workbook self-consistency check (D-EV-05). Never None, never empty."
        )
    )
    excel_ref: ExcelCitation = Field(
        description="A real cell, or an explained absence on a V7. Never None, never empty (D-EV-02)."
    )

    clause: str = Field(
        pattern=r"^D-[A-Z]+-[0-9]{2}$",
        description="The definitions clause this finding rests on. Cited to the reviewer.",
    )
    narrative: str | None = Field(
        default=None,
        description=(
            "Written by the narrative agent, last, from the fields above. The only free model "
            "text in a finding, and it changes nothing a reviewer acts on."
        ),
    )

    @model_validator(mode="after")
    def _promises_hold(self) -> Self:
        cls_ = self.variance_class

        # 1. A NotReached Excel citation is only defensible where no cell exists to point at, and
        #    there are exactly two such cases.
        #
        #    V7: extraction halted before the workbook was parsed (D-EV-02).
        #
        #    V5 with no claimed value: a *missing claim* - the source supports a figure and the
        #    workbook is silent about it (D-MAT-04). There is no cell because the hotel wrote
        #    nothing, which is the finding. This was found by reconciliation and classification, and it is the same
        #    over-broad-rule correction validator 6 needed in PDF extraction: "only V7" was written when V7
        #    was the only absence anyone had met. Note how narrow the exemption is - a V5 *orphan*
        #    claim (D-MAT-05) has a cell and is not exempt, so the rule still bites on the direction
        #    where a citation genuinely exists.
        no_cell_is_defensible = cls_ is VarianceClass.EXTRACTION_LIMIT or (
            cls_ is VarianceClass.COMPLETENESS and self.claimed is None
        )
        if isinstance(self.excel_ref, NotReached) and not no_cell_is_defensible:
            raise ValueError(
                f"{cls_} carries a NotReached Excel citation. Only a V7, or a V5 with no claimed "
                "value, may: every other finding is raised about a figure the workbook states, so "
                "a cell reference exists - D-EV-01"
            )

        # 1b. …and the mirror. A finding may carry one typed absence; never two. Two would make it
        #     a statement with nothing behind it on either side, which is the one thing D-EV-01
        #     exists to prevent - it does not care *which* reference is missing, it cares that a
        #     reviewer can always get from a finding to something they can look at.
        if isinstance(self.source_ref, NotReached) and isinstance(self.excel_ref, NotReached):
            raise ValueError(
                "a finding must cite something. Both references are a typed absence, which leaves "
                "a reviewer nothing to look at - D-EV-01"
            )

        # 1c. A definitional variance is established by a permutation *reproducing the claim* from
        #     the source records, and those records are read from the PDFs. One asserted without a
        #     page to point at is an assertion about a computation nobody can check.
        if isinstance(self.source_ref, NotReached) and cls_ is VarianceClass.DEFINITIONAL:
            raise ValueError(
                "V2 requires a real source_ref: a definitional variance is established by recomputing "
                "the claim from the source records, so the rows it was recomputed from exist and "
                "must be cited - D-CLS-07"
            )

        # 2. A definitional finding that cannot name its cause is an unsupported assertion.
        if cls_ is VarianceClass.DEFINITIONAL and self.explaining_permutation is None:
            raise ValueError(
                "V2 requires explaining_permutation: a definitional variance is established by a "
                "permutation reproducing the claim, and the finding must name it - D-CLS-07"
            )
        if cls_ is not VarianceClass.DEFINITIONAL and self.explaining_permutation is not None:
            raise ValueError(
                f"{cls_} must not carry explaining_permutation - only V2 is established that way"
            )
        if self.also_explained_by and cls_ is not VarianceClass.DEFINITIONAL:
            raise ValueError(f"{cls_} must not carry also_explained_by")
        if self.explaining_permutation in self.also_explained_by:
            raise ValueError("explaining_permutation must not repeat inside also_explained_by")

        # 3. Proposing a correction for anything but a clerical error tells a hotel to change a
        #    number that is not wrong.
        if self.proposed_correction is not None and cls_ is not VarianceClass.TRANSCRIPTION:
            raise ValueError(
                f"{cls_} must not propose a correction - only V1 (transcription) does. A "
                "definitional variance goes to the policy owner, not back to the hotel"
            )

        # 4. "I could not read this" is never informational.
        if cls_ is VarianceClass.EXTRACTION_LIMIT and self.severity is not Severity.BLOCKING:
            raise ValueError("V7 is always blocking - D-MAT-01")

        # 5. A definitional variance must never be routed to the hotel (D-MAT-06).
        if (
            cls_ is VarianceClass.DEFINITIONAL
            and self.escalates_to is not EscalationTarget.POLICY_OWNER
        ):
            raise ValueError(
                "V2 escalates to the policy owner, never to the hotel - reporting a rule "
                "disagreement as a hotel error is how correct findings discredit the system"
            )

        # 6. A *variance* with neither side has nothing to report. A refusal is not a variance.
        #
        # This rule was written as "a finding needs at least one of claimed or computed" and was too
        # broad, which only became visible when PDF extraction raised the first real V7. An extraction limit
        # raised before the workbook is parsed has no claimed value (nothing was read) and no computed
        # value (that is what it is reporting) — "I could not read row 14 of page 3" is a finding a
        # reviewer must act on and carries no numbers by nature. The `computed` field's own description
        # already said "None … a V7", so the model contradicted itself.
        #
        # V7 is therefore exempt, and only V7. Every other class is a statement about a difference
        # between two numbers, and one with neither is an empty assertion.
        if (
            self.claimed is None
            and self.computed is None
            and cls_ is not VarianceClass.EXTRACTION_LIMIT
        ):
            raise ValueError(
                f"{cls_} needs at least one of claimed or computed - a variance with neither is an "
                "empty assertion. Only V7 may carry neither, because an extraction limit reports "
                "that no value could be established rather than a difference between two."
            )

        # 7. The difference must actually be the difference.
        if self.claimed is not None and self.computed is not None:
            expected = self.claimed - self.computed
            if self.difference is None:
                raise ValueError(
                    "difference is required when both claimed and computed are present"
                )
            if self.difference != expected:
                raise ValueError(
                    f"difference={self.difference} is not claimed - computed "
                    f"({self.claimed} - {self.computed} = {expected})"
                )
        elif self.difference is not None:
            raise ValueError("difference must be None when either side is absent")

        return self

    @property
    def is_hotel_error(self) -> bool:
        """Whether this counts towards "hotel errors" in the memo.

        Definitional items never do (D-MAT-06), and neither do extraction limits — the system
        could not see, which is not the hotel's fault. Getting this property wrong produces a
        wrong number presented as a right one.
        """
        return self.variance_class in (VarianceClass.TRANSCRIPTION, VarianceClass.COMPLETENESS)

    @property
    def citation(self) -> str:
        """Both halves of the evidence, as a reviewer reads it."""
        return f"{self.source_ref.citation} | {self.excel_ref.citation}"
