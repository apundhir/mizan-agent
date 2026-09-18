"""Month apportionment — the clause this POC exists to arbitrate.

D-RNS-03 says a reservation's room-nights are apportioned to the month of **each occupied night**,
where the occupied nights run from arrival through departure − 1. The night of the departure date is
not occupied. The definitions call this "the single most likely source of live definitional
variance", and the worked example is the one on the front of every discussion:

    2 rooms, 2026-02-28 → 2026-03-03
    occupied nights: 28 Feb, 1 Mar, 2 Mar          → nights = 3, room_nights = 6
    February: 1 night  × 2 rooms = 2
    March:    2 nights × 2 rooms = 4

A hotel that books the whole stay to the arrival month reports February 4 too high and March 4 too
low, from data that is entirely correct. That is a **definitional** disagreement, not a clerical
error, and the difference matters enormously to the hotel being told about it.

**All three bases are implemented here, side by side.** `OCCUPIED_NIGHT` is the baseline;
`ARRIVAL_MONTH` and `DEPARTURE_MONTH` exist because they are what a hotel actually does when it gets
this wrong, and the permutation engine (PRD-87) needs to be able to *reproduce* the wrong answer in
order to name the cause. Keeping them in one function, dispatched on policy, means a reader can see
all three readings of the clause at once — which is the only way to tell they are three readings of
one clause rather than three unrelated code paths.

Nothing here rounds, caps, or filters. Apportionment is arithmetic over dates; the qualifying rules
live in `qualifying.py` and the presentation rules in `occupancy.py`. Keeping them apart is what lets
a monthly PDF honestly print a cancelled row's room-nights while the total leaves it out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tda.contracts import Period
from tda.policy import MonthBasis

if TYPE_CHECKING:
    from tda.contracts import ReservationRecord


def room_nights_in(record: ReservationRecord, period: Period, basis: MonthBasis) -> int:
    """Room-nights this reservation contributes to `period` under `basis`.

    Arithmetic only — this takes no view on whether the reservation qualifies. `room_nights_sold`
    applies the qualifying filter; separating the two is what makes the "printed honestly, excluded
    from the total" behaviour of a real PMS report expressible.

    A day-use reservation contributes **zero** under every basis (D-QUAL-09): it has no occupied
    nights, so there is nothing to apportion. That is not a special case bolted on — it falls out of
    `occupied_nights()` being empty — but it is asserted explicitly below, because the whole-stay
    bases would otherwise attribute its zero `room_nights` to a month and the intent would be
    unclear to a reader.
    """
    if record.is_day_use:
        return 0

    match basis:
        case MonthBasis.OCCUPIED_NIGHT:
            # D-RNS-03/04. The only basis that can split one reservation across two periods.
            occupied = sum(1 for night in record.occupied_nights() if period.contains(night))
            return record.rooms * occupied
        case MonthBasis.ARRIVAL_MONTH:
            # The whole stay booked to the month it began in. Reproduces P-MONTH-ARRIVAL.
            return record.room_nights if period.contains(record.arrival_date) else 0
        case MonthBasis.DEPARTURE_MONTH:
            # The whole stay booked to the month it ended in. Reproduces P-MONTH-DEPARTURE.
            #
            # Note this is the departure DATE, which is not an occupied night. That is deliberate:
            # the basis describes what a hotel did, and a hotel keying off the checkout date keys
            # off the checkout date. Correcting it here would make the permutation unable to
            # reproduce the claim it exists to explain.
            return record.room_nights if period.contains(record.departure_date) else 0


def guest_periods(record: ReservationRecord, basis: MonthBasis) -> list[Period]:
    """The periods this reservation's guests are counted in, under `basis`.

    A list rather than a single period because `OCCUPIED_NIGHT` — which a hotel does use for the
    nationality table, and which `P-NAT-MONTH-OCCUPIED` reproduces — can put one reservation in two
    months. The baseline `ARRIVAL_MONTH` always returns exactly one (D-NAT-06): a guest arrives
    once and is counted once.

    This is the function that makes D-NAT-08 visible. A stay arriving in December and departing in
    January contributes room-nights to January and **no guests to January**, because its arrival
    month is outside the period. The two metric families are not expected to reconcile with each
    other, and a workbook in which they do is the suspicious case.
    """
    match basis:
        case MonthBasis.ARRIVAL_MONTH:
            return [Period.of_month(record.arrival_date)]
        case MonthBasis.DEPARTURE_MONTH:
            return [Period.of_month(record.departure_date)]
        case MonthBasis.OCCUPIED_NIGHT:
            if record.is_day_use:
                # Zero occupied nights, but a real guest of the destination (D-QUAL-10). Counted in
                # the month they arrived, or a day-use visitor would be counted nowhere under this
                # basis and the nationality total would silently drop them.
                return [Period.of_month(record.arrival_date)]
            seen: list[Period] = []
            for night in record.occupied_nights():
                month = Period.of_month(night)
                if month not in seen:
                    seen.append(month)
            return seen
