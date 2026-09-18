"""Occupancy — sold, available, and the percentage. Pure functions, policy as a parameter.

Three rules here are the ones a hotel argues about, so each is implemented so that the argument can
only be settled by changing `policy.yaml`:

- **The denominator is room-NIGHTS available, not rooms available** (D-OCC-02). Dividing by a room
  count inflates occupancy by roughly the number of days in the month, and it is the canonical
  definitional error this POC is built to catch. `Denominator.ROOMS_AVAILABLE` is implemented anyway,
  because `P-OCC-DENOM-ROOMS` has to be able to *reproduce* the wrong number in order to name it as
  the cause. The schema pins it as permutation-only and never a legal baseline.
- **A zero denominator returns zero and is a closed month, not an error** (D-OCC-03). No exception,
  no division attempted. A month the property was shut is not a failure of the verification.
- **Occupancy is carried at full precision and rounded only at the presentation boundary**
  (D-OCC-04), and it is **not capped at 100** (D-OCC-05). A value above 100 means rooms were
  oversold or the inventory reference is wrong; both are worth surfacing rather than hiding.

Everything is `Decimal`. The definitions say "full float precision", and `Decimal` is the stricter
reading of the same intent: half-up rounding of a binary float is a coin toss at the midpoint, and a
verification system that reports a variance caused by its own representation error has no business
reporting variances at all.

The inventory reference is a required input and is never approximated (D-RNA-04). `None` means it was
not supplied, which is a different statement from an empty month — see `room_nights_available`.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from tda.metrics.apportion import room_nights_in
from tda.metrics.qualifying import MetricError, qualifies
from tda.policy import Denominator

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.contracts import InventoryDay, Period, ReservationRecord
    from tda.policy import Policy


def room_nights_sold(records: Sequence[ReservationRecord], period: Period, policy: Policy) -> int:
    """D-RNS-04. Room-nights sold in the period, over the qualifying set only.

    Day-use reservations contribute nothing under the baseline: zero nights means zero room-nights,
    so they are invisible to occupancy while remaining guests of the destination (D-QUAL-09,
    D-QUAL-10). When `day_use.counts_room_nights` is set — the `P-DAYUSE-COUNTS-RN` permutation, i.e.
    a hotel that counted each day-use booking as one room-night — each contributes `rooms`, in the
    month it arrived.
    """
    total = 0
    basis = policy.metrics.occupancy_pct.month_basis
    counts_day_use = policy.qualifying.day_use.counts_room_nights

    for record in records:
        if not qualifies(record, policy):
            continue
        if record.is_day_use:
            if counts_day_use and period.contains(record.arrival_date):
                total += record.rooms
            continue
        total += room_nights_in(record, period, basis)
    return total


def room_nights_available(
    inventory: Sequence[InventoryDay] | None, period: Period, policy: Policy
) -> int:
    """D-RNA-02. Σ over the dates in the period of `rooms_total − rooms_out_of_order`.

    **`None` and an empty month are different statements, and the distinction is load-bearing.**
    `None` means the inventory reference was not supplied: occupancy is then *not verifiable*
    (D-RNA-04) and this raises, because returning a number would be an approximation dressed as a
    measurement. Inventory that exists but has no rows in this period means the reference *says the
    property was shut*: a closed month, `0` (D-RNA-05). One is an absence of evidence and the other
    is evidence of absence; a function that returned `0` for both would collapse them, and the
    verdict would report an occupancy of zero for a month nobody supplied data for.

    `exclude_out_of_order` is honoured so `P-OOO-INCLUDED` can reproduce a hotel that left
    unsellable rooms in its denominator (D-RNA-03).
    """
    if inventory is None:
        raise MetricError(
            f"no inventory reference for {period}. Rooms available is a property attribute and is "
            "never inferred from the reservations - not from a room count, not from the maximum "
            "observed room number (D-RNA-04). Occupancy is reported not_verifiable with reason "
            "missing_inventory_reference; an estimate here would look like a measurement."
        )

    exclude = policy.metrics.occupancy_pct.exclude_out_of_order
    return sum(
        day.rooms_available if exclude else day.rooms_total
        for day in inventory
        if period.contains(day.day)
    )


def rooms_available(inventory: Sequence[InventoryDay], period: Period, policy: Policy) -> int:
    """The property's room *count* for the period — the denominator of the canonical error.

    Only `P-OCC-DENOM-ROOMS` uses this. A hotel making that mistake quotes its room count, so this
    is the **maximum** rooms available across the period: the inventory at its fullest, which is what
    somebody means by "we have 60 rooms". The mean would be a number nobody would recognise, and the
    first day's value would be an accident of which day the period starts on.

    The choice only affects whether the permutation *matches* a claim. A permutation that does not
    reproduce the claimed value is simply not named, so being wrong here costs an unexplained
    variance rather than a wrong explanation — but "unexplained" means a definitional disagreement
    gets reported as a clerical error, which is why it is worth getting right rather than close.
    """
    exclude = policy.metrics.occupancy_pct.exclude_out_of_order
    in_period = [
        day.rooms_available if exclude else day.rooms_total
        for day in inventory
        if period.contains(day.day)
    ]
    return max(in_period) if in_period else 0


def occupancy_ratio(
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay] | None,
    period: Period,
    policy: Policy,
) -> Decimal:
    """D-OCC-01, at full precision and **unrounded**. Callers wanting a comparable number want
    `occupancy_pct`.

    Returned separately so the rule that rounding happens once, at the presentation boundary
    (D-OCC-04), is visible in the type of the thing being returned rather than asserted in a comment.
    The memo shows this value; the comparison uses the rounded one.
    """
    sold = room_nights_sold(records, period, policy)

    if policy.metrics.occupancy_pct.denominator is Denominator.ROOMS_AVAILABLE:
        if inventory is None:
            raise MetricError(f"no inventory reference for {period} (D-RNA-04)")
        denominator = rooms_available(inventory, period, policy)
    else:
        denominator = room_nights_available(inventory, period, policy)

    if denominator == 0:
        # D-OCC-03. A closed month, not an error. No division is attempted.
        return Decimal(0)

    return Decimal(100) * Decimal(sold) / Decimal(denominator)


def occupancy_pct(
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay] | None,
    period: Period,
    policy: Policy,
) -> Decimal:
    """Occupancy as it is reported and compared: rounded half-up to the policy's decimal places.

    This is the value a claim is compared against (D-TOL-02 applies the ±0.10pp band to the
    presentation-rounded value), which is why rounding belongs here and nowhere upstream. Rounding
    mid-computation is never done — see `occupancy_ratio`.

    Not capped at 100 (D-OCC-05).
    """
    return present(occupancy_ratio(records, inventory, period, policy), policy)


def present(ratio: Decimal, policy: Policy) -> Decimal:
    """Round a percentage to its presentation form: half-up, at the policy's decimal places.

    Half-up rather than Python's default half-even. `round()` would give `2.815 → 2.82` sometimes and
    `2.81` other times depending on the binary representation of the input, and a verification that
    disagrees with a hotel by one hundredth of a point because of that is indefensible.
    """
    places = policy.metrics.occupancy_pct.presentation_decimal_places
    return ratio.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
