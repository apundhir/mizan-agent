"""Guests by nationality — the metric where three defensible answers exist for one cell.

A party of three in one room for four nights is **3 guests**, **1 arrival**, or **12 room-nights**.
All three are things a hotel might have put in the cell, and D-NAT-01 picks guests — which is
assumption A-06, not a ratified rule. So `counts` is policy, all three readings are implemented, and
`P-NAT-COUNTS-ARRIVALS` / `P-NAT-COUNTS-ROOMNIGHTS` can reproduce the other two in order to name them
as the cause rather than reporting them as a hotel error.

Two further rules are worth reading closely because they are the ones that surprise people:

**Guests are counted on the arrival month, not the occupied night** (D-NAT-06). This is a *different*
basis from occupancy's, deliberately: a guest arrives once and is counted once. The consequence is
D-NAT-08 — a stay arriving in December and departing in January contributes room-nights to January
and no guests to January. The two metric families are not expected to reconcile with each other, and
a workbook in which they do is the suspicious case. The bases are implemented separately in
`apportion.py` rather than sharing a parameter, so that difference cannot be collapsed by a
refactor.

**Every guest on a reservation gets that reservation's single nationality** (D-NAT-05, assumption
A-07). A PMS reservation export carries one nationality per booking, not one per occupant.
Distributing a party of four across four nationalities would be inventing data, so the whole party is
attributed to one code — and the honest consequence is that a mixed-nationality family is recorded as
whichever nationality the booking carries.

A day-use visitor **is** a guest of the destination (D-QUAL-10) and is counted here even though they
contribute nothing to occupancy. That asymmetry is the point of holding day-use as a property of the
record rather than as an exclusion from the qualifying set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tda.metrics.apportion import guest_periods
from tda.metrics.qualifying import qualifies
from tda.policy import NationalityCounts

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.contracts import Period, ReservationRecord
    from tda.policy import Policy


def guests(record: ReservationRecord, policy: Policy) -> int:
    """D-NAT-02/03. Adults plus children, unless the policy excludes children.

    `include_children` is assumption A-05, and `P-CHILDREN-EXCLUDED` exists because "guests" meaning
    "adult guests" is a reading some properties genuinely use. The record carries adults and children
    separately (D-NAT-04) precisely so this can be tested both ways without regenerating the corpus.
    """
    return record.guests if policy.metrics.guests_by_nationality.include_children else record.adults


def contribution(record: ReservationRecord, policy: Policy) -> int:
    """What this reservation adds to its nationality's cell, under the policy's `counts` rule.

    The three readings of one cell. `ARRIVALS` returning 1 is not a placeholder: a table counting
    arrivals counts bookings, and a party of six is one booking.
    """
    match policy.metrics.guests_by_nationality.counts:
        case NationalityCounts.GUESTS:
            return guests(record, policy)
        case NationalityCounts.ARRIVALS:
            return 1
        case NationalityCounts.ROOM_NIGHTS:
            return record.room_nights


def guests_by_nationality(
    records: Sequence[ReservationRecord], period: Period, policy: Policy
) -> dict[str, int]:
    """D-NAT-07. Guests per ISO 3166-1 alpha-2 code for the period, over the qualifying set.

    Returns only codes that are actually present. A code absent from the result means the corpus has
    no qualifying arrival of that nationality in that period — which is **not** the same as zero, and
    the difference is what lets the reconciliation layer tell an omitted workbook row (a V5
    completeness finding) from a row the corpus never had (nothing at all). Filling the result with
    zeroes for every country in the world would destroy that distinction, and filling it with zeroes
    for every country seen *elsewhere* in the submission would make the answer depend on the period
    being asked about.

    Codes are already canonical: an unmappable label is blocking and never reaches a record
    (D-NAT-12), and `nationality_iso2` is the only nationality representation that reaches a metric
    function (D-NAT-15).
    """
    basis = policy.metrics.guests_by_nationality.month_basis
    counts_day_use = policy.qualifying.day_use.counts_guests

    totals: dict[str, int] = {}
    for record in records:
        if not qualifies(record, policy):
            continue
        if record.is_day_use and not counts_day_use:
            continue
        if not any(period.contains(month.first_day) for month in guest_periods(record, basis)):
            continue
        code = record.nationality_iso2
        totals[code] = totals.get(code, 0) + contribution(record, policy)

    return dict(sorted(totals.items()))
