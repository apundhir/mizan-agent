"""The reservation ledger — generated first, so ground truth exists rather than being reconstructed.

This module owns the one decision that makes the rest of the POC possible: the reservations are
**generated before any document exists**, and both the PDFs and the workbook are rendered from
them. Nothing is authored twice.

Two things are worth reading closely.

**Guest references are opaque by construction.** There is no name to redact because no name is
ever produced. `guest_ref` is a truncated digest of the reservation id under a fixed salt, which
matches the `^g_[0-9a-f]{8,32}$` shape the canonical record demands and satisfies D-EV-03 the only
way that actually holds: by having nothing to leak. A generator that invented plausible names and
then withheld them from the output would be one careless `print` away from putting a name in a log.

**The edges are quotas, not luck.** `check_quotas` runs after generation and fails the build if the
corpus is short of month-spanning stays, complimentary rooms, day-use reservations or any of the
other edges S5–S8 depend on. A corpus that lost its edges to a seed change would not fail loudly;
it would make every downstream permutation quietly return the baseline and every test still pass.
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

from datagen.spec import (
    ADULTS_WEIGHTS,
    CHILDREN_WEIGHTS,
    HOTEL_ID,
    NATIONALITY_WEIGHTS,
    NIGHTS_WEIGHTS,
    OPEN_AT_PERIOD_END,
    OUT_OF_ORDER,
    PERIOD_END,
    PERIOD_START,
    PRE_PERIOD_ARRIVALS,
    QUOTAS,
    RATE_CODE_WEIGHTS,
    ROOMS_TOTAL,
    ROOMS_WEIGHTS,
    SEED,
    SINGLE_MONTH_COUNT,
    SINGLE_MONTH_MONTH,
    SINGLE_MONTH_NATIONALITY,
    SPANNING_ARRIVALS,
    STATUS_WEIGHTS,
    TOTAL_RESERVATIONS,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# Fixed, published, and not a secret: the references are opaque because there is nothing behind
# them, not because the salt is hidden. A salt that had to be protected would be a claim about
# confidentiality this repo has no way to keep.
GUEST_REF_SALT: Final = "mizan-synthetic-corpus-v1"

# Non-qualifying statuses need dates that do not contradict themselves: a cancelled booking still
# has the dates it was booked for, and a no-show still has an arrival it did not turn up for.
# Neither is forced to zero, because a PMS export does not zero them either — and the pipeline has
# to exclude them by *rule* rather than by noticing they look empty.
NON_QUALIFYING_STATUSES: Final = frozenset({"CANCELLED", "NO_SHOW"})


class GeneratorError(RuntimeError):
    """The corpus does not contain what the downstream stories need."""


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One reservation, as the generator knows it.

    Deliberately *not* `tda.contracts.ReservationRecord`. Two reasons, and the second is the one
    that matters:

    1. A canonical record requires a `PdfRef`, which does not exist until a page has been laid out.
       The ledger precedes the documents, so it cannot carry a citation into them.
    2. `ReservationRecord.occupied_nights()` is the month-apportionment primitive of D-RNS-03.
       Reusing it here would make `truth_metrics.json` co-derived with the code it checks. The
       derivations below are therefore re-implemented from the clause text, and a unit test asserts
       the two implementations agree — which is a real check, where one implementation agreeing
       with itself is not.
    """

    reservation_id: str
    hotel_id: str
    guest_ref: str
    nationality_iso2: str
    adults: int
    children: int
    rooms: int
    nights: int
    room_nights: int
    arrival_date: date
    departure_date: date
    status: str
    rate_code: str

    def __post_init__(self) -> None:
        # The same three derivations the canonical record enforces (D-RNS-01, D-RNS-02), checked
        # here so a generator bug cannot reach a document. Asserted independently and on purpose.
        if self.departure_date < self.arrival_date:
            raise GeneratorError(f"{self.reservation_id}: departure precedes arrival")
        if self.nights != (self.departure_date - self.arrival_date).days:
            raise GeneratorError(f"{self.reservation_id}: nights contradicts the dates (D-RNS-01)")
        if self.room_nights != self.nights * self.rooms:
            raise GeneratorError(f"{self.reservation_id}: room_nights is not nights x rooms")

    @property
    def is_day_use(self) -> bool:
        """D-QUAL-08. Arrives and departs the same day: zero nights, zero room-nights, one guest."""
        return self.nights == 0

    @property
    def guests(self) -> int:
        """D-NAT-02. What the record contains; whether children *count* is policy."""
        return self.adults + self.children

    def occupied_nights(self) -> Iterator[date]:
        """The nights this stay occupied: arrival through departure − 1 inclusive (D-RNS-03).

        The night of the departure date is not occupied. Re-derived here from the clause rather
        than imported — see the class docstring.
        """
        day = self.arrival_date
        while day < self.departure_date:
            yield day
            day += timedelta(days=1)

    def room_nights_in(self, month: str) -> int:
        """D-RNS-04. `rooms × (occupied nights of this stay falling in `month`)`.

        Arithmetic only: this counts what the dates say and takes no view on whether the
        reservation qualifies. Applying the qualifying rules is `aggregate.py`'s job, and keeping
        the two separate is what lets a PDF print a cancelled row's room-nights honestly while
        leaving it out of the total.
        """
        return self.rooms * sum(1 for day in self.occupied_nights() if _month_key(day) == month)

    def months_touched(self) -> list[str]:
        """The months whose PDF this reservation appears on, in order.

        A month-spanning stay appears on **two** monthly reports, which is what a PMS in-house
        report does and what makes apportionment visible in the documents rather than only in the
        ledger. A day-use stay has no occupied nights at all, so it appears on its arrival month's
        report — otherwise it would be rendered nowhere and D-QUAL-10 would be untestable.
        """
        if self.is_day_use:
            return [_month_key(self.arrival_date)]
        seen: list[str] = []
        for day in self.occupied_nights():
            key = _month_key(day)
            if key not in seen:
                seen.append(key)
        return seen


@dataclass(frozen=True, slots=True)
class InventoryRow:
    """One day of the property's room inventory.

    Rooms available is a property attribute, not a reservation attribute (D-RNA-01), so it is a
    separate file and a separate concept. `rooms_available` is net of out-of-order rooms: a room
    that could not be sold was not available (D-RNA-03).
    """

    hotel_id: str
    day: date
    rooms_total: int
    rooms_out_of_order: int
    note: str

    @property
    def rooms_available(self) -> int:
        return self.rooms_total - self.rooms_out_of_order


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _guest_ref(reservation_id: str) -> str:
    """An opaque reference, 16 hex characters, inside the canonical `^g_[0-9a-f]{8,32}$` shape.

    Derived from the reservation id rather than drawn at random so it is stable across runs
    without needing to be stored anywhere.
    """
    digest = hashlib.sha256(f"{GUEST_REF_SALT}:{reservation_id}".encode()).hexdigest()
    return f"g_{digest[:16]}"


def _weighted[K](rng: random.Random, weights: dict[K, int]) -> K:
    """One draw from a weighted mapping, returning a key of the mapping's own type.

    `random.choices` would do the same job, but it consumes a different number of values from the
    generator depending on the argument shape, which makes the stream sensitive to refactoring.
    Drawing a single integer keeps the stream stable: adding a field later shifts nothing that came
    before it, so a change at the end of `_make_row` cannot renumber the whole corpus.
    """
    items = list(weights.items())
    target = rng.randrange(sum(weight for _, weight in items))
    cumulative = 0
    for key, weight in items:
        cumulative += weight
        if target < cumulative:
            return key
    return items[-1][0]  # pragma: no cover - unreachable while every weight is positive


def _make_row(
    rng: random.Random,
    *,
    index: int,
    arrival: date,
    nights: int,
    status: str | None = None,
    rate_code: str | None = None,
    nationality: str | None = None,
) -> LedgerRow:
    """Build one reservation. Every optional argument is an edge being placed deliberately.

    The draw order is fixed — rooms, adults, children, then the fields the caller did not pin —
    so that pinning a field costs the stream nothing and the realised mix of everything else is
    unchanged by which edges are being seeded.
    """
    rooms = _weighted(rng, ROOMS_WEIGHTS)
    adults = _weighted(rng, ADULTS_WEIGHTS)
    children = _weighted(rng, CHILDREN_WEIGHTS)
    drawn_status = _weighted(rng, STATUS_WEIGHTS)
    drawn_rate = _weighted(rng, RATE_CODE_WEIGHTS)
    drawn_nationality = _weighted(rng, NATIONALITY_WEIGHTS)

    reservation_id = f"RES-2026Q1-{index:05d}"
    return LedgerRow(
        reservation_id=reservation_id,
        hotel_id=HOTEL_ID,
        guest_ref=_guest_ref(reservation_id),
        nationality_iso2=nationality or drawn_nationality,
        adults=adults,
        children=children,
        rooms=rooms,
        nights=nights,
        room_nights=nights * rooms,
        arrival_date=arrival,
        departure_date=arrival + timedelta(days=nights),
        status=status or drawn_status,
        rate_code=rate_code or drawn_rate,
    )


def _period_days() -> list[date]:
    return [
        PERIOD_START + timedelta(days=offset)
        for offset in range((PERIOD_END - PERIOD_START).days + 1)
    ]


def generate_ledger(seed: int = SEED) -> list[LedgerRow]:
    """The reservation ledger for the quarter, deterministic in `seed`.

    Built in a fixed order — seeded edges first, then the bulk, then the quota top-ups — because
    the order is what makes the output reproducible. Sorted at the end by arrival date and id, so
    the ledger reads like a report and the row order does not depend on how it was assembled.
    """
    rng = random.Random(seed)
    rows: list[LedgerRow] = []
    index = 1

    # 1. Month-spanning stays. Seeded rather than left to the draw, so the quota cannot fail
    #    silently when the seed changes (spec.SPANNING_ARRIVALS explains why).
    for arrival, nights in SPANNING_ARRIVALS:
        rows.append(
            _make_row(rng, index=index, arrival=arrival, nights=nights, status="CHECKED_OUT")
        )
        index += 1

    # 2. Stays that began before the reporting period: room-nights in January, no guests in
    #    January (D-NAT-08). The case where the two metric families are expected not to reconcile.
    for arrival, nights in PRE_PERIOD_ARRIVALS:
        rows.append(
            _make_row(rng, index=index, arrival=arrival, nights=nights, status="CHECKED_OUT")
        )
        index += 1

    # 3. Stays still open at the period end, carried as IN_HOUSE so the status means what it says.
    for arrival, nights in OPEN_AT_PERIOD_END:
        rows.append(_make_row(rng, index=index, arrival=arrival, nights=nights, status="IN_HOUSE"))
        index += 1

    # 4. Day-use: arrival == departure, zero room-nights, guests counted (D-QUAL-08..10).
    days = _period_days()
    for _ in range(QUOTAS.day_use):
        arrival = days[rng.randrange(len(days))]
        rows.append(_make_row(rng, index=index, arrival=arrival, nights=0, status="CHECKED_OUT"))
        index += 1

    # 5. A nationality present in exactly one month, so S8's completeness path has a row a
    #    workbook can omit (spec.SINGLE_MONTH_NATIONALITY explains the choice).
    march = [day for day in days if day.month == SINGLE_MONTH_MONTH][:-1]
    for offset in range(SINGLE_MONTH_COUNT):
        rows.append(
            _make_row(
                rng,
                index=index,
                arrival=march[(offset * 7) % len(march)],
                nights=_weighted(rng, NIGHTS_WEIGHTS),
                status="CHECKED_OUT",
                nationality=SINGLE_MONTH_NATIONALITY,
            )
        )
        index += 1

    # 6. The rate-code quotas, pinned before the bulk rather than topped up after it, so the
    #    total stays exactly TOTAL_RESERVATIONS. COMP and HOUSE are 3% each, so a 1,200-row draw
    #    lands *near* the quota — and "near" is not a guarantee, while these two are what make
    #    P-COMP-EXCLUDED and P-HOUSE-INCLUDED move a number at all.
    for code, quota in (("COMP", QUOTAS.comp), ("HOUSE", QUOTAS.house)):
        shortfall = quota - sum(1 for row in rows if row.rate_code == code)
        for _ in range(max(0, shortfall)):
            rows.append(
                _make_row(
                    rng,
                    index=index,
                    arrival=days[rng.randrange(len(days))],
                    nights=_weighted(rng, NIGHTS_WEIGHTS),
                    status="CHECKED_OUT",
                    rate_code=code,
                )
            )
            index += 1

    # 7. The bulk. Arrival uniform across the quarter; everything else drawn from the weights.
    while len(rows) < TOTAL_RESERVATIONS:
        rows.append(
            _make_row(
                rng,
                index=index,
                arrival=days[rng.randrange(len(days))],
                nights=_weighted(rng, NIGHTS_WEIGHTS),
            )
        )
        index += 1

    rows.sort(key=lambda row: (row.arrival_date, row.reservation_id))
    check_quotas(rows)
    return rows


def generate_inventory() -> list[InventoryRow]:
    """One row per calendar day of the quarter, including the out-of-order windows.

    Every day of the period is present. A missing day would be indistinguishable from a closed
    month (D-RNA-05), and the difference between "shut" and "not supplied" is the difference
    between an occupancy of zero and an occupancy that cannot be verified (D-RNA-04).
    """
    rows: list[InventoryRow] = []
    for day in _period_days():
        out_of_order = 0
        note = ""
        for window in OUT_OF_ORDER:
            if window.covers(day):
                out_of_order += window.rooms
                note = window.reason
        rows.append(
            InventoryRow(
                hotel_id=HOTEL_ID,
                day=day,
                rooms_total=ROOMS_TOTAL,
                rooms_out_of_order=out_of_order,
                note=note,
            )
        )
    return rows


def check_quotas(rows: Sequence[LedgerRow]) -> None:
    """Fail the build if the corpus is missing an edge a downstream story needs.

    This is the difference between a corpus that contains its edges and one that happened to.
    Every shortfall here would otherwise surface as a *passing* test downstream: a permutation
    with nothing to permute returns the baseline, matches, and reports success.
    """
    spanning = sum(1 for row in rows if len(row.months_touched()) > 1)
    realised = {
        "month_spanning": spanning,
        "comp": sum(1 for row in rows if row.rate_code == "COMP"),
        "house": sum(1 for row in rows if row.rate_code == "HOUSE"),
        "day_use": sum(1 for row in rows if row.is_day_use),
        "arriving_before_period": sum(1 for row in rows if row.arrival_date < PERIOD_START),
        "in_house_at_period_end": sum(
            1 for row in rows if row.status == "IN_HOUSE" and row.departure_date > PERIOD_END
        ),
        "multi_room": sum(1 for row in rows if row.rooms > 1),
        "nationalities": len({row.nationality_iso2 for row in rows}),
    }

    shortfalls = [
        f"  {name}: have {realised[name]}, need {required}"
        for name, required in dataclasses.asdict(QUOTAS).items()
        if realised[name] < required
    ]
    if shortfalls:
        raise GeneratorError(
            "the generated corpus is short of the edges the downstream stories need:\n"
            + "\n".join(shortfalls)
            + "\n\nThese are not style preferences. A permutation with nothing to permute returns "
            "the baseline and its test passes, so a missing edge shows up as success."
        )
