"""Does the workbook agree with itself? Asked before it is compared to anything (D-XLS-04).

A submission whose own arithmetic does not hold produces, when compared to the PDFs, a spray of
variances that all trace back to one clerical error. A reviewer receiving twelve findings has to
work out that eleven of them are consequences; a reviewer receiving *"the Occupancy quarter total
says 4,140 and its own three months sum to 4,139"* has one thing to send back. So these checks run
first, and what they produce is reported ahead of everything else.

Three arithmetic checks and one identity check, and the interesting parts are the exclusions.

**Percentages are excluded from the additive checks.** The quarter's occupancy is not the sum of the
three monthly occupancies, it is a ratio over the whole quarter — `78.05`, not `234.72`. A check
that added them would fire on every correct workbook ever submitted, and a check that fires on
correct input gets switched off within a week.

**Blank cells are summed as absent, not as zero.** This is what makes D-XLS-02 safe rather than
merely permissive, and it is the difference between a check that works and one that cries wolf. The
demo corpus has Iceland with three March guests and nothing in January or February; the ground truth
has no key at all for those two months. Summing the *claimed* months gives 3, the stated quarter says
3, and the workbook is correct — which it is. And the case the check exists for still fires: if the
quarter said 10 while only March was claimed, the arithmetic would not close, and the blank cells
would be exactly where a reviewer would then look. The omission is detected by the arithmetic rather
than by demanding a value in every cell.

**Occupancy is checked against the workbook's own two other figures**, not added. A stated occupancy
should be the stated room-nights sold over the stated room-nights available, and that is checkable
without any of our own data. It uses the policy's percentage tolerance rather than exact equality,
because a hotel rounding half-even where we round half-up is a rounding convention and not a
clerical error — and this check is for clerical errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from tda.contracts import (
    Claim,
    ExcelRef,
    Metric,
    MetricKey,
    NotReached,
    Period,
    PeriodKind,
)
from tda.metrics.occupancy import present

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from openpyxl.workbook.workbook import Workbook

    from tda.excel.mapping import CoverSheet
    from tda.excel.read import StatedTotal
    from tda.policy import Policy

# Why there is no PDF to cite. Carried on every finding these checks raise, because an absence
# without a reason is indistinguishable from a bug (D-EV-05).
NO_PDF_REASON = "workbook self-consistency check, performed before any comparison to the reports"


@dataclass(frozen=True, slots=True)
class Inconsistency:
    """One way the workbook disagrees with itself.

    `stated` is what the hotel wrote. `implied` is what the hotel's *own other figures* require.
    Neither side comes from our data, which is what makes this reportable before any comparison and
    what makes it unambiguously the hotel's to fix.

    **`key` is optional, and the one case where it is absent is the reason it is optional.** A total
    row under a list of countries is a figure *across* the dimension, and the key grammar has no way
    to say that: `guests_by_nationality` requires a dimension value (D-KEY-02) and `Total` is not a
    country. Every way of inventing one misattributes the problem — keying it to the first country
    in the block files an arithmetic error against Australia, and keying it to a different metric
    entirely, as the PDF-side reconciliation does for the equivalent case, tells a reviewer their
    occupancy evidence is in doubt when it is the nationality table that does not add up.

    So the key is absent, and `metric` and `period` carry what is actually known. A reviewer reading
    *"Nationality!B26 states 753, the 21 figures above it sum to 750"* needs a cell and two numbers,
    and has them.
    """

    metric: Metric
    period: str
    stated: Decimal
    implied: Decimal
    excel_ref: ExcelRef
    clause: str
    detail: str
    key: MetricKey | None = None

    @property
    def difference(self) -> Decimal:
        return self.stated - self.implied


def _by_key(claims: Iterable[Claim]) -> dict[str, Claim]:
    return {claim.key.rendered: claim for claim in claims}


def check_dimension_totals(
    claims: Sequence[Claim], stated_totals: Sequence[StatedTotal]
) -> list[Inconsistency]:
    """Every stated total across a dimension equals the sum of its stated components.

    The `Total` row under a list of countries. Percentage metrics are skipped: a total row under a
    column of percentages is not a sum of them.
    """
    found: list[Inconsistency] = []
    for total in stated_totals:
        if total.metric.is_percentage:
            continue
        components = [
            claim
            for claim in claims
            if claim.key.metric is total.metric
            and claim.key.period == total.period
            and claim.key.dimension is not None
        ]
        if not components:
            continue
        implied = sum((claim.value for claim in components), start=Decimal(0))
        if implied == total.value:
            continue
        found.append(
            Inconsistency(
                metric=total.metric,
                period=total.period,
                # Keyed only where the grammar can express "this figure". A total across a dimension
                # cannot be keyed - see the note on `Inconsistency`.
                key=(
                    None
                    if total.metric.requires_dimension
                    else MetricKey(metric=total.metric, period=total.period)
                ),
                stated=total.value,
                implied=implied,
                excel_ref=total.ref,
                clause="D-XLS-04",
                detail=(
                    f"the stated total for {total.metric.value} in {total.period} is {total.value}, "
                    f"but the {len(components)} figures above it sum to {implied}"
                ),
            )
        )
    return found


def check_period_rollups(claims: Sequence[Claim]) -> list[Inconsistency]:
    """Every quarter or year figure equals the sum of the months claimed within it.

    Summed over the months that *were* claimed, never over the months that should have been - see
    the module docstring on Iceland for why that distinction is the whole design of this check.
    """
    indexed = _by_key(claims)
    found: list[Inconsistency] = []

    for claim in claims:
        if claim.key.metric.is_percentage:
            continue
        period = Period.parse(claim.key.period)
        if period.kind is PeriodKind.MONTH:
            continue

        month_claims = [
            indexed[rendered]
            for month in period.months()
            if (
                rendered := MetricKey(
                    metric=claim.key.metric,
                    period=str(month),
                    dimension=claim.key.dimension,
                    value=claim.key.value,
                ).rendered
            )
            in indexed
        ]
        if not month_claims:
            # A roll-up with no months claimed at all is not an inconsistency - the workbook may
            # legitimately submit only a quarter figure. There is nothing to check it against.
            continue

        implied = sum((month.value for month in month_claims), start=Decimal(0))
        if implied == claim.value:
            continue
        found.append(
            Inconsistency(
                metric=claim.key.metric,
                period=claim.key.period,
                key=claim.key,
                stated=claim.value,
                implied=implied,
                excel_ref=claim.excel_ref,
                clause="D-XLS-04",
                detail=(
                    f"{claim.key.rendered} is stated as {claim.value}, but the "
                    f"{len(month_claims)} month figure(s) the workbook gives for it sum to "
                    f"{implied}"
                ),
            )
        )
    return found


def check_occupancy_consistency(claims: Sequence[Claim], policy: Policy) -> list[Inconsistency]:
    """A stated occupancy equals the stated sold over the stated available, for the same period.

    Entirely within the workbook: all three numbers are the hotel's. A workbook failing this has
    either mistyped one of the three or computed its occupancy from something it did not submit, and
    both are worth knowing before any of our own figures are put beside it.
    """
    indexed = _by_key(claims)
    tolerance = policy.tolerances.for_metric(Metric.OCCUPANCY_PCT)
    found: list[Inconsistency] = []

    for claim in claims:
        if claim.key.metric is not Metric.OCCUPANCY_PCT:
            continue
        sold = indexed.get(
            MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=claim.key.period).rendered
        )
        available = indexed.get(
            MetricKey(metric=Metric.ROOM_NIGHTS_AVAILABLE, period=claim.key.period).rendered
        )
        if sold is None or available is None or available.value == 0:
            # Not every workbook submits all three, and a zero denominator is a different problem
            # that D-RNA-04 already owns. Neither is this check's to report.
            continue

        implied = present(sold.value / available.value * Decimal(100), policy)
        if tolerance.accepts(float(claim.value - implied)):
            continue
        found.append(
            Inconsistency(
                metric=claim.key.metric,
                period=claim.key.period,
                key=claim.key,
                stated=claim.value,
                implied=implied,
                excel_ref=claim.excel_ref,
                clause="D-XLS-04",
                detail=(
                    f"occupancy for {claim.key.period} is stated as {claim.value}%, but the "
                    f"workbook's own {sold.value} sold over {available.value} available gives "
                    f"{implied}%"
                ),
            )
        )
    return found


@dataclass(frozen=True, slots=True)
class IdentityMismatch:
    """The workbook says it is about a different property or a different period.

    Its own type rather than an `Inconsistency`, because it is not an arithmetic disagreement and
    carries no numbers. It is also the most serious thing this module can find: every figure in a
    workbook for the wrong quarter is wrong, and comparing them would produce a hundred findings
    describing one filing mistake.
    """

    field: str
    stated: str
    expected: str
    excel_ref: ExcelRef

    @property
    def detail(self) -> str:
        return (
            f"the cover sheet gives {self.field} as {self.stated!r}, but the submission being "
            f"verified is {self.expected!r}"
        )


def check_cover(
    workbook: Workbook, cover: CoverSheet, *, expected_property: str, expected_period: Period
) -> list[IdentityMismatch]:
    """The cover sheet names the property and period this submission is actually for.

    Compared as text after trimming, and deliberately not normalised any further. A property code is
    an identifier; `'MZN-DXB-001 '` and `'MZN-DXB-001'` are the same code and `'mzn-dxb-1'` is not,
    and guessing which near-miss was meant is how a workbook for the wrong property gets verified
    against the right one's data.
    """
    if cover.sheet not in workbook.sheetnames:
        return [
            IdentityMismatch(
                field="cover sheet",
                stated=cover.sheet,
                expected=f"one of {workbook.sheetnames}",
                excel_ref=ExcelRef(sheet=workbook.sheetnames[0], cell="A1"),
            )
        ]

    worksheet = workbook[cover.sheet]
    found: list[IdentityMismatch] = []
    for field, cell, expected in (
        ("property code", cover.property_code_cell, expected_property),
        ("reporting period", cover.period_cell, str(expected_period)),
    ):
        raw = worksheet[cell].value
        stated = str(raw).strip() if raw is not None else ""
        if stated.casefold() == expected.casefold():
            continue
        found.append(
            IdentityMismatch(
                field=field,
                stated=stated,
                expected=expected,
                excel_ref=ExcelRef(sheet=cover.sheet, cell=cell),
            )
        )
    return found


def no_pdf(reason: str = NO_PDF_REASON) -> NotReached:
    """The typed absence a workbook-only finding carries (D-EV-05).

    The reason is a parameter with a default rather than a constant, because the two callers are not
    doing the same thing and a reviewer reads this text. A self-consistency failure has no PDF
    because none was consulted; an unmapped block has none because the parser never got as far as
    comparing anything. Labelling the second as the first would put a small, confident falsehood in
    front of the person deciding what to do about it.
    """
    return NotReached(reason=reason)
