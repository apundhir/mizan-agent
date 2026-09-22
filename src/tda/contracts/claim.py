"""What the hotel asserted, and what the source data actually supports.

Two mirror-image types. A `Claim` is a number the workbook states, carrying the cell it was
read from. A `ComputedValue` is a number the metric library derived, carrying the source rows it
was derived from — PDF reservation rows for the three reservation-derived metrics, and inventory
days for `room_nights_available`, which is a property attribute and appears in no export
(D-RNA-01). Reconciliation joins them on `MetricKey`, and the pair of references is what makes
every finding citable (D-EV-01).

Values are `Decimal`, not `float`. Occupancy is compared against a ±0.10 percentage-point
tolerance and counts are compared exactly; doing either in binary floating point means
`71.43` is not reliably `71.43`, and a verification system that reports a variance caused by
its own representation error has no business reporting variances at all.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from tda.contracts.metric_key import MetricKey
from tda.contracts.refs import ExcelRef, SourceRef

if TYPE_CHECKING:
    from collections.abc import Sequence


class Claim(BaseModel):
    """One figure asserted by the hotel's workbook, with the cell it came from.

    Produced by the Excel claim parser in its **second** stage: a model maps sheets
    to metrics, then `openpyxl` reads the values. A model never populates `value`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: MetricKey
    value: Decimal
    excel_ref: ExcelRef = Field(description="Required. There is no such thing as an uncited claim.")
    raw_text: str | None = Field(
        default=None,
        description=(
            "The cell's literal content when it was not already numeric - '71.4%' stored as "
            "text, say. Kept for evidence display so a reviewer sees what the hotel typed, not "
            "only what we parsed it into."
        ),
    )


class ComputedValue(BaseModel):
    """One figure derived from the extracted records by the metric library.

    `source_rows` is what turns "we computed 14" into "we computed 14 from these rows on these
    pages". A reviewer who cannot get from a number back to its rows in one step will not sign
    behind it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: MetricKey
    value: Decimal
    source_rows: tuple[SourceRef, ...] = Field(
        min_length=1,
        description=(
            "Every source range that contributed - PDF reservation rows, or days of the room "
            "inventory reference. Empty would mean a value from nowhere."
        ),
    )
    policy_version: str = Field(
        min_length=1,
        description="The ruleset that produced this number. A value without its ruleset is not "
        "defensible (D-EV-04).",
    )

    @property
    def primary_ref(self) -> SourceRef:
        """The citation shown first in a finding, when one row range has to stand for many."""
        return self.source_rows[0]


class NotVerifiable(BaseModel):
    """A metric that could not be verified, and why — stated rather than approximated.

    The case this exists for is occupancy without an inventory reference (D-RNA-04). The
    honest output is "not verifiable, here is what is missing"; the dishonest one is an
    estimate that looks like a measurement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: MetricKey
    reason: str = Field(min_length=1, examples=["missing_inventory_reference"])
    detail: str = Field(
        min_length=1,
        description="What a reviewer needs in order to make it verifiable next time.",
    )


def index_by_key(claims: Sequence[Claim]) -> dict[str, Claim]:
    """Index claims by rendered key, refusing duplicates.

    Two claims on the same key means the workbook asserts the same figure twice. If they
    agree it is redundant; if they disagree the workbook contradicts itself. Either way the
    caller must see it, so this raises rather than picking one — silently keeping the last
    would make a self-contradicting workbook look consistent.
    """
    indexed: dict[str, Claim] = {}
    for claim in claims:
        rendered = claim.key.rendered
        if rendered in indexed:
            raise ValueError(
                f"duplicate claim for {rendered}: "
                f"{indexed[rendered].excel_ref.citation} and {claim.excel_ref.citation}"
            )
        indexed[rendered] = claim
    return indexed
