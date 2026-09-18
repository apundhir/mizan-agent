"""The orchestrator: every metric this submission establishes, keyed canonically and cited.

The functions in the sibling modules return numbers. This turns them into `ComputedValue`s — a number
with the key it answers to, the rows it came from, and the policy version it was computed under. That
last part is D-EV-04 and it is not bookkeeping: a number without its ruleset is not defensible, and
the whole architecture rests on a reviewer being able to see which rules produced a figure.

Two behaviours here are decisions rather than plumbing.

**Missing inventory produces a `NotVerifiable`, not an exception and not a zero.** D-RNA-04 says
occupancy without an inventory reference is not verifiable and the verdict says so. The metric
functions raise — they are pure and have nowhere to put a diagnosis — and this layer catches, because
"we could not verify occupancy, here is what is missing" is a *result*, not an error. Returning zero
would report an occupancy of zero for a month nobody supplied data for.

**Citations are collapsed into ranges, not enumerated.** A month's occupancy draws on several hundred
reservation rows across fifteen pages. Listing every one is accurate and unreadable; listing only the
first is readable and a lie about coverage. So contiguous rows on the same page are merged into
`row_start..row_end` ranges, which is what those fields are for, and a reviewer gets a citation set
they can actually follow back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from tda.contracts import (
    ComputedValue,
    Dimension,
    InventoryRef,
    Metric,
    MetricKey,
    NotVerifiable,
    PdfRef,
)
from tda.metrics.nationality import guests_by_nationality
from tda.metrics.occupancy import (
    occupancy_pct,
    occupancy_ratio,
    room_nights_available,
    room_nights_sold,
)
from tda.metrics.qualifying import MetricError, qualifies, reject_duplicate_ids

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tda.contracts import InventoryDay, Period, ReservationRecord, SourceRef
    from tda.policy import Policy


@dataclass(frozen=True, slots=True)
class MetricResults:
    """What a submission's records support, and what they could not establish.

    Two dictionaries rather than one with a union value, because callers treat them completely
    differently: computed values go to the reconciliation join, and `not_verifiable` entries go
    straight to the verdict with their reason. A union would make every call site branch on a type.
    """

    computed: dict[str, ComputedValue] = field(default_factory=dict)
    not_verifiable: dict[str, NotVerifiable] = field(default_factory=dict)

    # Unrounded occupancy, by rendered key. The memo shows this; nothing compares against it
    # (D-OCC-04 — comparison uses the presentation-rounded value in `computed`).
    occupancy_full_precision: dict[str, Decimal] = field(default_factory=dict)

    def value(self, key: str) -> Decimal:
        """The computed value for a key, or `KeyError`. A convenience for tests and the memo."""
        return self.computed[key].value


def _collapse(refs: Iterable[PdfRef]) -> tuple[PdfRef, ...]:
    """Merge citations into the fewest ranges that cover the same rows.

    Grouped by file and page, sorted, then runs of consecutive rows become one range. Four hundred
    single-row refs become a dozen page ranges that say the same thing and can be read.

    Ranges are merged only when they are genuinely adjacent or overlapping. Bridging a gap would
    claim coverage of rows that did not contribute — a citation that points at an unrelated
    reservation is worse than a long list, because a reviewer following it up finds a number that
    does not add up and has no way to tell whose mistake it is.
    """
    by_page: dict[tuple[str, int], list[tuple[int, int]]] = {}
    for ref in refs:
        by_page.setdefault((ref.file, ref.page), []).append((ref.row_start, ref.row_end))

    collapsed: list[PdfRef] = []
    for (file, page), spans in sorted(by_page.items()):
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        collapsed.extend(
            PdfRef(file=file, page=page, row_start=start, row_end=end) for start, end in merged
        )
    return tuple(collapsed)


def _collapse_inventory(days: Iterable[InventoryDay]) -> tuple[InventoryRef, ...]:
    """The same merge for inventory rows. A month is one contiguous run, so this is usually one ref."""
    by_file: dict[str, list[tuple[int, int]]] = {}
    for day in days:
        by_file.setdefault(day.source.file, []).append((day.source.row_start, day.source.row_end))

    collapsed: list[InventoryRef] = []
    for file, spans in sorted(by_file.items()):
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        collapsed.extend(
            InventoryRef(file=file, row_start=start, row_end=end) for start, end in merged
        )
    return tuple(collapsed)


def _contributing(
    records: Sequence[ReservationRecord], period: Period, policy: Policy
) -> list[ReservationRecord]:
    """The qualifying records that touch the period at all.

    "Touch" is deliberately generous — arrival, departure or any occupied night inside the period.
    A citation set is evidence a reviewer follows to check a number, and a record whose stay overlaps
    the period is part of why the number is what it is even when it contributed zero room-nights to
    it (a day-use arrival, say). Being narrow here would hide the rows most worth looking at.
    """
    touching: list[ReservationRecord] = []
    for record in records:
        if not qualifies(record, policy):
            continue
        if period.contains(record.arrival_date) or period.contains(record.departure_date):
            touching.append(record)
            continue
        if any(period.contains(night) for night in record.occupied_nights()):
            touching.append(record)
    return touching


def _reservation_sources(
    records: Sequence[ReservationRecord], period: Period, policy: Policy
) -> tuple[SourceRef, ...]:
    """Citations for a reservation-derived metric, with a floor of one.

    `ComputedValue.source_rows` has `min_length=1`, which is the contract saying a value from nowhere
    is not a value. A period with no contributing reservations therefore cannot produce a
    `ComputedValue` at all — see `_zero_with_sources` for how that case is handled honestly rather
    than by fabricating a reference.
    """
    return _collapse(record.source for record in _contributing(records, period, policy))


def compute_all(
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay] | None,
    periods: Sequence[Period],
    policy: Policy,
) -> MetricResults:
    """Every in-scope metric, for every period given, from one pass of the records.

    Raises `MetricError` on duplicate reservation ids (D-QUAL-07) before computing anything. That is
    the one condition where producing numbers at all would be wrong: the library sums what it is
    given, so a duplicated row is counted twice and every figure in the submission would be off by a
    plausible amount. The blocking finding is constructed upstream, where there is a citation.
    """
    reject_duplicate_ids(records)

    results = MetricResults()
    for period in periods:
        _room_nights_sold(results, records, period, policy)
        _room_nights_available(results, inventory, period, policy)
        _occupancy(results, records, inventory, period, policy)
        _nationality(results, records, period, policy)
    return results


# ── one metric family per function, so each rule has one home ────────────────


def _record(
    results: MetricResults,
    key: MetricKey,
    value: Decimal,
    sources: tuple[SourceRef, ...],
    policy: Policy,
) -> None:
    """Store a computed value, or a `NotVerifiable` if nothing cites it.

    The `min_length=1` on `source_rows` is not an obstacle to route around. A metric with no
    contributing source is a metric this submission says nothing about, and the honest record of that
    is a `NotVerifiable` — not a zero with an invented citation, which would look like a measurement.
    """
    if not sources:
        results.not_verifiable[key.rendered] = NotVerifiable(
            key=key,
            reason="no_source_rows",
            detail=(
                f"no extracted rows fall in {key.period}. A value with no source is not a value; "
                "if the period should have data, the extraction is incomplete."
            ),
        )
        return
    results.computed[key.rendered] = ComputedValue(
        key=key, value=value, source_rows=sources, policy_version=policy.version
    )


def _room_nights_sold(
    results: MetricResults,
    records: Sequence[ReservationRecord],
    period: Period,
    policy: Policy,
) -> None:
    key = MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=period.rendered)
    sold = room_nights_sold(records, period, policy)
    _record(results, key, Decimal(sold), _reservation_sources(records, period, policy), policy)


def _room_nights_available(
    results: MetricResults,
    inventory: Sequence[InventoryDay] | None,
    period: Period,
    policy: Policy,
) -> None:
    """The denominator, cited to the inventory reference rather than to a PDF (D-RNA-01).

    A closed month — inventory supplied, no rows in this period — is `0` and is *not* verifiable as a
    claim, because there is nothing to cite. That is the honest outcome: the reference says the
    property was shut, and a reviewer asked to check a zero needs to be pointed at something.
    """
    key = MetricKey(metric=Metric.ROOM_NIGHTS_AVAILABLE, period=period.rendered)

    if inventory is None:
        results.not_verifiable[key.rendered] = _missing_inventory(key)
        return

    in_period = [day for day in inventory if period.contains(day.day)]
    available = room_nights_available(inventory, period, policy)
    _record(results, key, Decimal(available), _collapse_inventory(in_period), policy)


def _occupancy(
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay] | None,
    period: Period,
    policy: Policy,
) -> None:
    """Occupancy, carrying both the reservation rows and the inventory rows.

    Both, because occupancy is the one metric whose variance could come from either side: a reviewer
    told "occupancy is 4pp out" needs to be able to check the numerator *and* the denominator, and
    the denominator is not in the PDF.
    """
    key = MetricKey(metric=Metric.OCCUPANCY_PCT, period=period.rendered)

    try:
        ratio = occupancy_ratio(records, inventory, period, policy)
        presented = occupancy_pct(records, inventory, period, policy)
    except MetricError:
        # D-RNA-04. The honest output is "not verifiable, here is what is missing" — never an
        # estimate, and never a zero, which would be indistinguishable from a closed month.
        results.not_verifiable[key.rendered] = _missing_inventory(key)
        return

    sources: tuple[SourceRef, ...] = _reservation_sources(records, period, policy)
    if inventory is not None:
        sources = sources + _collapse_inventory(
            day for day in inventory if period.contains(day.day)
        )

    results.occupancy_full_precision[key.rendered] = ratio
    _record(results, key, presented, sources, policy)


def _nationality(
    results: MetricResults,
    records: Sequence[ReservationRecord],
    period: Period,
    policy: Policy,
) -> None:
    """One key per nationality actually present (D-NAT-07).

    Citations are narrowed to the records of that nationality, not the whole period. A finding about
    German guests that cited every row in the month would be technically accurate and useless.
    """
    totals = guests_by_nationality(records, period, policy)
    for code, count in totals.items():
        key = MetricKey(
            metric=Metric.GUESTS_BY_NATIONALITY,
            period=period.rendered,
            dimension=Dimension.NATIONALITY_ISO2,
            value=code,
        )
        of_code = [record for record in records if record.nationality_iso2 == code]
        sources = _collapse(record.source for record in _contributing(of_code, period, policy))
        _record(results, key, Decimal(count), sources, policy)


def _missing_inventory(key: MetricKey) -> NotVerifiable:
    return NotVerifiable(
        key=key,
        reason="missing_inventory_reference",
        detail=(
            "Rooms available is a property attribute and never inferred from the reservations "
            "(D-RNA-04). Supply the per-day inventory reference - hotel_id, day, rooms_total, "
            "rooms_out_of_order - for the reporting period and re-run."
        ),
    )
