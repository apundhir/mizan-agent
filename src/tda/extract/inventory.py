"""The room inventory reference — a required input, read strictly.

Occupancy cannot be verified without this file (D-RNA-04), and it is the one input with no printed
totals to reconcile against. So the checks here are structural instead: a gap in the dates, a value out
of range, or a `rooms_available` column that disagrees with `rooms_total − rooms_out_of_order`.

**The `rooms_available` column is a cross-check, never an input** — the same rule as the PDF's
`RN Total`. The generator writes it because a real reference would, and adopting it would mean the
occupancy denominator came from a column somebody typed rather than from the two values it is derived
from.

**A missing day is not zero.** A reference with no row for 14 February is not a property that was shut
that day — it is a reference with a hole in it, and the difference matters: a closed day legitimately
contributes zero to the denominator (D-RNA-05), while a missing day silently lowers it and raises
occupancy. So the days must be contiguous across the period, and a gap is blocking.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

from tda.contracts import InventoryDay, InventoryRef

if TYPE_CHECKING:
    from pathlib import Path

    from tda.contracts import Period

REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"hotel_id", "day", "rooms_total", "rooms_out_of_order"}
)


class InventoryError(Exception):
    """The inventory reference cannot be used as the occupancy denominator.

    Raised rather than returning a partial reference. A denominator assembled from most of a file is an
    approximation, and D-RNA-04 is explicit that this value is never approximated — the honest outcome
    is occupancy reported `not_verifiable` with the reason.
    """


@dataclass(frozen=True, slots=True)
class InventoryReference:
    """The reference, with the citation each day came from."""

    days: tuple[InventoryDay, ...]
    file: str
    hotel_ids: frozenset[str]


def read_inventory(path: Path, period: Period) -> InventoryReference:
    """Read and validate the inventory reference for a period.

    Row numbers in the citations are 1-indexed **data** rows — the header is not row 1 — because that
    is how a spreadsheet application numbers the file a reviewer will open to check a citation.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise InventoryError(f"{path.name} is empty")
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if missing:
            raise InventoryError(
                f"{path.name} is missing required column(s): {sorted(missing)}. Rooms available is a "
                "property attribute and cannot be inferred from anything else in the submission "
                "(D-RNA-04)."
            )
        rows = list(reader)

    days: list[InventoryDay] = []
    hotel_ids: set[str] = set()

    for index, row in enumerate(rows, start=1):
        ref = InventoryRef(file=path.name, row_start=index, row_end=index)
        try:
            day = date.fromisoformat(row["day"])
            rooms_total = int(row["rooms_total"])
            out_of_order = int(row["rooms_out_of_order"])
        except (KeyError, TypeError, ValueError) as problem:
            raise InventoryError(f"{ref.citation}: {problem}") from problem

        # The printed availability column is compared, never adopted — the same rule the PDF's
        # RN Total column gets, for the same reason.
        printed = row.get("rooms_available")
        if printed is not None and printed.strip():
            if not printed.strip().isdigit():
                raise InventoryError(f"{ref.citation}: rooms_available is {printed!r}")
            if int(printed) != rooms_total - out_of_order:
                raise InventoryError(
                    f"{ref.citation}: the rooms_available column says {printed}, but "
                    f"{rooms_total} total - {out_of_order} out of order is "
                    f"{rooms_total - out_of_order} (D-RNA-03). Availability is always derived; the "
                    "column is a cross-check."
                )

        hotel_ids.add(row["hotel_id"])
        days.append(
            InventoryDay(
                hotel_id=row["hotel_id"],
                day=day,
                rooms_total=rooms_total,
                rooms_out_of_order=out_of_order,
                source=ref,
            )
        )

    _check_contiguous(days, period, path.name)
    return InventoryReference(days=tuple(days), file=path.name, hotel_ids=frozenset(hotel_ids))


def _check_contiguous(days: list[InventoryDay], period: Period, file_name: str) -> None:
    """Every date in the period present exactly once, and nothing outside it unexplained.

    A duplicate is as dangerous as a gap and in the opposite direction: it doubles that day's
    contribution to the denominator and *lowers* occupancy, which looks like a quiet month rather than
    a broken file.
    """
    seen: dict[date, int] = {}
    for day in days:
        seen[day.day] = seen.get(day.day, 0) + 1

    duplicates = sorted(d for d, count in seen.items() if count > 1)
    if duplicates:
        raise InventoryError(
            f"{file_name}: duplicate inventory rows for {[str(d) for d in duplicates[:5]]}. A "
            "duplicated day doubles its contribution to the occupancy denominator and lowers "
            "occupancy, which reads as a quiet month rather than a broken file."
        )

    expected = period.first_day
    missing: list[date] = []
    while expected <= period.last_day:
        if expected not in seen:
            missing.append(expected)
        expected += timedelta(days=1)

    if missing:
        raise InventoryError(
            f"{file_name}: no inventory row for {[str(d) for d in missing[:5]]}"
            f"{f' and {len(missing) - 5} more' if len(missing) > 5 else ''} in {period}. A missing "
            "day is not a closed day: a closed day contributes zero to the denominator and states so "
            "(D-RNA-05), while a hole in the file silently lowers the denominator and raises "
            "occupancy. Supply every date in the period."
        )
