"""The metric library, against hand-computed values and the clause each one comes from.

Every expected number here was worked out by hand from `docs/01-definitions.md`, and every test names
the clause it tests. Both halves matter. A test asserting `room_nights_sold(...) == 1285` with no
citation records that the code does what the code does; a test that says *2 rooms × 1 February night
= 2, D-RNS-03* records what the system has agreed to mean, and it is the second kind that survives a
definitional argument with a hotel.

The cases are the ones the PRD asks for — month-spanning, day-use, complimentary, house-use,
cancelled, no-show, closed month, zero denominator — plus the ones that turned out to matter while
building: the two month bases disagreeing, a nationality absent rather than zero, and occupancy above
100 not being hidden.

The wrong readings are tested too. `P-OCC-DENOM-ROOMS` and `P-MONTH-ARRIVAL` have to **reproduce** a
hotel's incorrect number in order for the reconciliation engine to name the cause instead of
reporting a clerical error, so a permutation that failed to reproduce it would be a silent downgrade
of a definitional disagreement. Those tests assert the wrong answer on purpose.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tda.contracts import (
    InventoryDay,
    InventoryRef,
    Metric,
    PdfRef,
    Period,
    RateCode,
    ReservationRecord,
    Status,
)
from tda.metrics import (
    MetricError,
    compute_all,
    guests_by_nationality,
    occupancy_pct,
    occupancy_ratio,
    present,
    qualifies,
    reject_duplicate_ids,
    room_nights_available,
    room_nights_in,
    room_nights_sold,
    rooms_available,
)
from tda.policy import MonthBasis, NationalityCounts, Policy, apply_permutation, load_policy

FEB = Period.parse("2026-02")
MAR = Period.parse("2026-03")
JAN = Period.parse("2026-01")
Q1 = Period.parse("2026-Q1")


@pytest.fixture(scope="module")
def policy() -> Policy:
    """The committed baseline. Deliberately the real `policy.yaml` rather than a fixture: a metric
    library tested only against a hand-built policy is a library nobody has run under the ruleset
    that ships."""
    return load_policy()


def permutation(policy: Policy, permutation_id: str) -> Policy:
    """The policy under one named permutation, looked up by id.

    By id rather than by index, so reordering `permutations.ordered` in `policy.yaml` — which is a
    legitimate change, the order is a heuristic — cannot silently point a test at a different rule.
    """
    for candidate in policy.permutations.ordered:
        if candidate.id == permutation_id:
            return apply_permutation(policy, candidate)
    raise AssertionError(f"no permutation {permutation_id} in policy.yaml")


def reservation(
    *,
    arrival: date,
    nights: int,
    rooms: int = 1,
    adults: int = 2,
    children: int = 0,
    status: Status = Status.CHECKED_OUT,
    rate_code: RateCode = RateCode.BAR,
    nationality: str = "GB",
    reservation_id: str = "RES-0001",
) -> ReservationRecord:
    """A reservation with the boring fields filled in, so a test body shows only what it is about."""
    return ReservationRecord(
        reservation_id=reservation_id,
        hotel_id="MZN-DXB-001",
        guest_ref="g_0123456789abcdef",
        nationality_iso2=nationality,
        adults=adults,
        children=children,
        rooms=rooms,
        nights=nights,
        room_nights=nights * rooms,
        arrival_date=arrival,
        departure_date=arrival + timedelta(days=nights),
        status=status,
        rate_code=rate_code,
        source=PdfRef(file="pms_2026-02.pdf", page=1, row_start=1, row_end=1),
    )


def inventory(
    period: Period, *, rooms_total: int = 60, out_of_order: int = 0
) -> list[InventoryDay]:
    """A flat inventory for the period. Row numbers are 1-indexed data rows, as a CSV reader sees."""
    return [
        InventoryDay(
            hotel_id="MZN-DXB-001",
            day=day,
            rooms_total=rooms_total,
            rooms_out_of_order=out_of_order,
            source=InventoryRef(file="inventory_2026-Q1.csv", row_start=index, row_end=index),
        )
        for index, day in enumerate(period.days(), start=1)
    ]


# ── D-RNS-03/04 · the worked example, and the clause the POC exists for ──────


def test_a_month_spanning_stay_is_apportioned_by_occupied_night(policy: Policy) -> None:
    """D-RNS-03, D-RNS-04 — `docs/01-definitions.md` §3.1, worked by hand.

        2 rooms, 2026-02-28 → 2026-03-03
        occupied nights: 28 Feb | 1 Mar, 2 Mar        (3 March is NOT occupied)
        room_nights total         = 3 × 2 = 6
        room_nights_sold(2026-02) = 1 × 2 = 2
        room_nights_sold(2026-03) = 2 × 2 = 4

    The single most likely source of live definitional variance, and the number the whole
    apportionment argument turns on.
    """
    stay = reservation(arrival=date(2026, 2, 28), nights=3, rooms=2)

    assert stay.room_nights == 6
    assert room_nights_sold([stay], FEB, policy) == 2
    assert room_nights_sold([stay], MAR, policy) == 4
    assert room_nights_sold([stay], Q1, policy) == 6

    assert date(2026, 3, 3) not in stay.occupied_nights()


def test_the_departure_night_is_not_occupied(policy: Policy) -> None:
    """D-RNS-03. A one-night stay occupies one night, not two.

    Worth its own test because the off-by-one is the easiest mistake in the domain and it inflates
    every occupancy figure by roughly one night per stay — a systematic error that looks like a busy
    hotel rather than a bug.
    """
    one_night = reservation(arrival=date(2026, 2, 10), nights=1)

    assert one_night.occupied_nights() == [date(2026, 2, 10)]
    assert room_nights_sold([one_night], FEB, policy) == 1


def test_the_two_month_bases_disagree_and_the_disagreement_is_visible(policy: Policy) -> None:
    """The acceptance criterion asks for this disagreement to be *demonstrated, not hidden*.

    Same reservation, two readings of one clause:

        occupied_night (baseline, D-RNS-03):  February 2, March 4
        arrival_month  (P-MONTH-ARRIVAL):     February 6, March 0

    A hotel on the arrival-month basis reports February 4 too high and March 4 too low from data
    that is entirely correct. That is definitional, escalated to the policy owner, and never a hotel
    error — and the reconciliation engine can only say so because the library can reproduce both.
    """
    stay = reservation(arrival=date(2026, 2, 28), nights=3, rooms=2)
    arrival_basis = permutation(policy, "P-MONTH-ARRIVAL")

    assert (room_nights_sold([stay], FEB, policy), room_nights_sold([stay], MAR, policy)) == (2, 4)
    assert (
        room_nights_sold([stay], FEB, arrival_basis),
        room_nights_sold([stay], MAR, arrival_basis),
    ) == (6, 0)

    # Both bases agree on the quarter: apportionment moves room-nights between months, it does not
    # create or destroy them. A permutation that changed the total would be a bug, not a reading.
    assert room_nights_sold([stay], Q1, policy) == room_nights_sold([stay], Q1, arrival_basis) == 6


def test_the_departure_month_basis_keys_off_a_night_nobody_occupied(policy: Policy) -> None:
    """P-MONTH-DEPARTURE, and why it is not "corrected" to departure − 1.

    A stay arriving 28 February and departing 3 March books entirely to **March** under this basis,
    because a hotel keying off the checkout date keys off the checkout date. Correcting it here would
    make the permutation unable to reproduce the claim it exists to explain, and an unreproduced claim
    is reported as a clerical error.
    """
    stay = reservation(arrival=date(2026, 2, 28), nights=3, rooms=2)
    departure_basis = permutation(policy, "P-MONTH-DEPARTURE")

    assert room_nights_in(stay, MAR, MonthBasis.DEPARTURE_MONTH) == 6
    assert room_nights_sold([stay], FEB, departure_basis) == 0
    assert room_nights_sold([stay], MAR, departure_basis) == 6


# ── D-QUAL · the qualifying set ──────────────────────────────────────────────


def test_a_complimentary_room_counts(policy: Policy) -> None:
    """D-QUAL-04, assumption A-02. COMP is a genuine guest paying nothing — the room was occupied.

    The distinction from HOUSE is the whole rule: both are zero-revenue, and only one of them had a
    visitor in it.
    """
    comp = reservation(arrival=date(2026, 2, 10), nights=3, rate_code=RateCode.COMP)

    assert qualifies(comp, policy)
    assert room_nights_sold([comp], FEB, policy) == 3
    assert guests_by_nationality([comp], FEB, policy) == {"GB": 2}


def test_a_house_use_room_does_not_count(policy: Policy) -> None:
    """D-QUAL-05. HOUSE is the property's own use — staff, maintenance, operations. Not a guest."""
    house = reservation(arrival=date(2026, 2, 10), nights=3, rate_code=RateCode.HOUSE)

    assert not qualifies(house, policy)
    assert room_nights_sold([house], FEB, policy) == 0
    assert guests_by_nationality([house], FEB, policy) == {}


@pytest.mark.parametrize("status", [Status.CANCELLED, Status.NO_SHOW])
def test_cancellations_and_no_shows_do_not_count(policy: Policy, status: Status) -> None:
    """D-QUAL-01, assumption A-01. Neither occupied a room.

    Their dates are *not* zeroed in the record — a cancelled booking still has the dates it was
    booked for, and a real PMS export prints them. They are excluded by rule rather than by looking
    empty, which is why the PDF can list them honestly and the total can leave them out.
    """
    excluded = reservation(arrival=date(2026, 2, 10), nights=3, status=status)

    assert not qualifies(excluded, policy)
    assert excluded.room_nights == 3  # the arithmetic is still true of the dates
    assert room_nights_sold([excluded], FEB, policy) == 0
    assert guests_by_nationality([excluded], FEB, policy) == {}


def test_including_no_shows_is_a_permutation_that_moves_the_number(policy: Policy) -> None:
    """P-STATUS-INCLUDE-NOSHOW. A permutation that could not change a number would explain nothing,
    match nothing, and be named in no finding."""
    no_show = reservation(arrival=date(2026, 2, 10), nights=3, status=Status.NO_SHOW)

    assert room_nights_sold([no_show], FEB, policy) == 0
    assert room_nights_sold([no_show], FEB, permutation(policy, "P-STATUS-INCLUDE-NOSHOW")) == 3


def test_a_day_use_reservation_is_invisible_to_occupancy_and_visible_to_guests(
    policy: Policy,
) -> None:
    """D-QUAL-08/09/10. Zero nights, zero room-nights, and still a guest of the destination.

    The one edge where a reservation reaches one metric family and not the other, which is why
    day-use is a property of the record rather than an exclusion from the qualifying set — excluding
    it would remove the guests too.
    """
    day_use = reservation(arrival=date(2026, 2, 14), nights=0, adults=2, children=1)

    assert day_use.is_day_use
    assert day_use.room_nights == 0
    assert day_use.occupied_nights() == []

    assert qualifies(day_use, policy)
    assert room_nights_sold([day_use], FEB, policy) == 0
    assert guests_by_nationality([day_use], FEB, policy) == {"GB": 3}


def test_counting_day_use_as_a_room_night_is_a_permutation(policy: Policy) -> None:
    """P-DAYUSE-COUNTS-RN. A hotel that counted each same-day booking as one room-night per room."""
    day_use = reservation(arrival=date(2026, 2, 14), nights=0, rooms=2)

    assert room_nights_sold([day_use], FEB, policy) == 0
    assert room_nights_sold([day_use], FEB, permutation(policy, "P-DAYUSE-COUNTS-RN")) == 2


def test_duplicate_reservation_ids_are_refused_never_de_duplicated(policy: Policy) -> None:
    """D-QUAL-07. The library sums what it is given, so a duplicate is counted twice.

    Never de-duplicated: two rows with one id might be a double export or two genuine bookings with a
    clerical collision, and nothing in the data distinguishes them. Silently picking one would make a
    total wrong by a plausible amount with nothing anywhere saying so.
    """
    twice = [
        reservation(arrival=date(2026, 2, 10), nights=3, reservation_id="RES-0001"),
        reservation(arrival=date(2026, 2, 12), nights=2, reservation_id="RES-0001"),
    ]

    with pytest.raises(MetricError, match="duplicate reservation ids: RES-0001"):
        reject_duplicate_ids(twice)
    with pytest.raises(MetricError, match="D-QUAL-07"):
        compute_all(twice, inventory(FEB), [FEB], policy)


# ── D-RNA · the denominator ──────────────────────────────────────────────────


def test_room_nights_available_is_days_times_net_rooms(policy: Policy) -> None:
    """D-RNA-02. February 2026 has 28 days; 28 × 60 = 1,680, worked by hand."""
    assert room_nights_available(inventory(FEB), FEB, policy) == 1680
    assert room_nights_available(inventory(JAN), JAN, policy) == 31 * 60


def test_out_of_order_rooms_are_excluded_from_the_denominator(policy: Policy) -> None:
    """D-RNA-03, assumption A-08. A room that cannot be sold was not available.

    28 days × (60 − 6) = 1,512. Counting the unsellable rooms would depress occupancy for a reason
    that has nothing to do with demand.
    """
    with_ooo = inventory(FEB, out_of_order=6)

    assert room_nights_available(with_ooo, FEB, policy) == 28 * 54 == 1512
    assert room_nights_available(with_ooo, FEB, permutation(policy, "P-OOO-INCLUDED")) == 28 * 60


def test_a_month_with_no_inventory_rows_is_a_closed_month(policy: Policy) -> None:
    """D-RNA-05. The reference exists and says the property was shut: available is 0.

    Distinct from D-RNA-04 below. This is evidence of absence; that is absence of evidence.
    """
    february_only = inventory(FEB)

    assert room_nights_available(february_only, MAR, policy) == 0
    assert occupancy_pct([], february_only, MAR, policy) == Decimal("0.00")


def test_a_missing_inventory_reference_is_not_verifiable_not_zero(policy: Policy) -> None:
    """D-RNA-04. Rooms available is a property attribute and is never inferred from the reservations.

    The honest output is "not verifiable, here is what is missing". A zero would be indistinguishable
    from a closed month, and an estimate would look like a measurement — which is the failure mode
    that costs a verification system its credibility.
    """
    stay = reservation(arrival=date(2026, 2, 10), nights=3)

    with pytest.raises(MetricError, match="never inferred from the reservations"):
        room_nights_available(None, FEB, policy)

    results = compute_all([stay], None, [FEB], policy)

    assert "occupancy_pct:2026-02" not in results.computed
    not_verifiable = results.not_verifiable["occupancy_pct:2026-02"]
    assert not_verifiable.reason == "missing_inventory_reference"
    assert "D-RNA-04" in not_verifiable.detail

    # Room-nights *sold* is unaffected: it needs no denominator.
    assert results.value("room_nights_sold:2026-02") == 3


# ── D-OCC · the percentage ───────────────────────────────────────────────────


def test_occupancy_is_sold_over_available(policy: Policy) -> None:
    """D-OCC-01. 840 sold over 1,680 available is 50.00%, worked by hand."""
    stays = [reservation(arrival=date(2026, 2, 1), nights=28, rooms=30)]

    assert room_nights_sold(stays, FEB, policy) == 28 * 30 == 840
    assert occupancy_pct(stays, inventory(FEB), FEB, policy) == Decimal("50.00")


def test_a_zero_denominator_returns_zero_and_raises_nothing(policy: Policy) -> None:
    """D-OCC-03. No exception, and no division attempted.

    `rooms_total=0` is a property with no sellable rooms at all — a closed month by a different
    route from D-RNA-05, and the same answer.
    """
    shut = inventory(FEB, rooms_total=0)

    assert room_nights_available(shut, FEB, policy) == 0
    assert occupancy_ratio([], shut, FEB, policy) == Decimal(0)
    assert occupancy_pct([], shut, FEB, policy) == Decimal("0.00")


def test_occupancy_is_not_capped_at_one_hundred(policy: Policy) -> None:
    """D-OCC-05. Above 100 means oversold rooms or a wrong inventory reference.

    Both are findings worth surfacing rather than numbers worth hiding — and a cap would turn a
    detectable data problem into a plausible-looking 100%.
    """
    oversold = reservation(arrival=date(2026, 2, 1), nights=28, rooms=70)

    assert occupancy_pct([oversold], inventory(FEB), FEB, policy) > Decimal(100)
    assert occupancy_pct([oversold], inventory(FEB), FEB, policy) == Decimal("116.67")


def test_rounding_happens_once_at_presentation_and_is_half_up(policy: Policy) -> None:
    """D-OCC-04. Carried at full precision, rounded once, half-up.

    `2/3` is the case that separates the two halves of the rule: the ratio is 66.666…, the presented
    value is 66.67, and anything that rounded mid-computation would have lost the digits that decide
    it. Half-up rather than Python's default half-even, so the answer does not depend on the binary
    representation of the input.
    """
    two_thirds = occupancy_ratio(
        [reservation(arrival=date(2026, 2, 1), nights=28, rooms=40)], inventory(FEB), FEB, policy
    )

    assert two_thirds == Decimal(100) * Decimal(1120) / Decimal(1680)
    assert two_thirds != Decimal("66.67")  # the unrounded value really is unrounded
    assert present(two_thirds, policy) == Decimal("66.67")

    assert present(Decimal("2.815"), policy) == Decimal("2.82")  # half-up, not half-even
    assert present(Decimal("2.825"), policy) == Decimal("2.83")


def test_the_full_precision_ratio_is_kept_alongside_the_rounded_one(policy: Policy) -> None:
    """The memo shows the ratio; the comparison uses the rounded value. Both have to survive."""
    stays = [reservation(arrival=date(2026, 2, 1), nights=28, rooms=40)]
    results = compute_all(stays, inventory(FEB), [FEB], policy)

    assert results.value("occupancy_pct:2026-02") == Decimal("66.67")
    assert results.occupancy_full_precision["occupancy_pct:2026-02"] > Decimal("66.666")
    assert results.occupancy_full_precision["occupancy_pct:2026-02"] < Decimal("66.667")


def test_dividing_by_rooms_rather_than_room_nights_is_the_canonical_error(policy: Policy) -> None:
    """D-OCC-02 and P-OCC-DENOM-ROOMS — the definitional error this POC is built to catch (F3).

    Same numerator, two denominators:

        room-nights available: 840 / 1680 =  50.00%
        rooms available:       840 /   60 = 1400.00%

    Inflated by roughly the number of days in the month, which is what makes it recognisable. The
    permutation has to reproduce the absurd figure, because reproducing it is how the engine names
    the cause rather than reporting the hotel 1,350 points wrong.
    """
    stays = [reservation(arrival=date(2026, 2, 1), nights=28, rooms=30)]
    by_rooms = permutation(policy, "P-OCC-DENOM-ROOMS")

    assert occupancy_pct(stays, inventory(FEB), FEB, policy) == Decimal("50.00")
    assert rooms_available(inventory(FEB), FEB, by_rooms) == 60
    assert occupancy_pct(stays, inventory(FEB), FEB, by_rooms) == Decimal("1400.00")


# ── D-NAT · guests by nationality ────────────────────────────────────────────


def test_guests_are_adults_plus_children_on_the_arrival_month(policy: Policy) -> None:
    """D-NAT-02, D-NAT-03 (A-05), D-NAT-06 (A-04). Two adults and one child arriving in February."""
    family = reservation(arrival=date(2026, 2, 20), nights=4, adults=2, children=1)

    assert guests_by_nationality([family], FEB, policy) == {"GB": 3}
    assert guests_by_nationality([family], MAR, policy) == {}


def test_excluding_children_is_a_permutation(policy: Policy) -> None:
    """P-CHILDREN-EXCLUDED. "Guests" meaning "adult guests" is a reading some properties use.

    The record holds adults and children separately (D-NAT-04) precisely so this is testable without
    regenerating the corpus.
    """
    family = reservation(arrival=date(2026, 2, 20), nights=4, adults=2, children=3)

    assert guests_by_nationality([family], FEB, policy) == {"GB": 5}
    assert guests_by_nationality([family], FEB, permutation(policy, "P-CHILDREN-EXCLUDED")) == {
        "GB": 2
    }


def test_a_month_spanning_stay_gives_room_nights_to_two_months_and_guests_to_one(
    policy: Policy,
) -> None:
    """D-NAT-08 — the case the two metric families are *expected* not to reconcile on.

    Arriving 2026-02-28, departing 2026-03-03: room-nights in both months, guests in February only.
    A workbook where occupancy and the nationality table *do* reconcile across a month boundary is
    the suspicious one, and this is the clause that says so.
    """
    stay = reservation(arrival=date(2026, 2, 28), nights=3, rooms=2, adults=2, children=2)

    assert (room_nights_sold([stay], FEB, policy), room_nights_sold([stay], MAR, policy)) == (2, 4)
    assert guests_by_nationality([stay], FEB, policy) == {"GB": 4}
    assert guests_by_nationality([stay], MAR, policy) == {}


def test_a_stay_arriving_before_the_period_gives_january_nights_and_no_january_guests(
    policy: Policy,
) -> None:
    """D-NAT-08 again, from the other side — the harder direction to remember.

    Arriving 2025-12-29 for 6 nights: 29, 30, 31 December then 1, 2, 3 January. Three January nights,
    and zero January guests, because the arrival month is outside the period entirely.
    """
    early = reservation(arrival=date(2025, 12, 29), nights=6)

    assert room_nights_sold([early], JAN, policy) == 3
    assert guests_by_nationality([early], JAN, policy) == {}


@pytest.mark.parametrize(
    ("permutation_id", "counts", "expected"),
    [
        ("P-NAT-COUNTS-ARRIVALS", NationalityCounts.ARRIVALS, 1),
        ("P-NAT-COUNTS-ROOMNIGHTS", NationalityCounts.ROOM_NIGHTS, 12),
    ],
)
def test_the_three_readings_of_one_nationality_cell(
    policy: Policy, permutation_id: str, counts: NationalityCounts, expected: int
) -> None:
    """D-NAT-01 (A-06). One cell, three defensible answers.

    A party of 3 in **3 rooms** for 4 nights is 3 guests, 1 arrival, or 12 room-nights. The three
    numbers are unrelated to each other — room-nights is not a restatement of the guest count, and
    with a different room count all three would move independently. Every one is a number a hotel
    might have put in the cell, which is why the reading is policy rather than a constant, and why
    two permutations exist to reproduce the ones we did not pick.
    """
    party = reservation(arrival=date(2026, 2, 20), nights=4, rooms=3, adults=3, children=0)
    permuted = permutation(policy, permutation_id)

    assert guests_by_nationality([party], FEB, policy) == {"GB": 3}
    assert permuted.metrics.guests_by_nationality.counts is counts
    assert guests_by_nationality([party], FEB, permuted) == {"GB": expected}


def test_a_nationality_absent_from_a_period_is_absent_not_zero(policy: Policy) -> None:
    """D-NAT-07, and the distinction the reconciliation layer depends on.

    A code missing from the result means the corpus has no qualifying arrival of that nationality in
    that period. That is **not** zero: it is the difference between a workbook row the hotel omitted
    (a V5 completeness finding) and a row the corpus never had (nothing at all). Filling in zeroes
    would collapse the two.
    """
    german = reservation(arrival=date(2026, 2, 20), nights=4, adults=2, nationality="DE")

    february = guests_by_nationality([german], FEB, policy)
    assert february == {"DE": 2}
    assert "GB" not in february, "a nationality with no arrivals is absent, not present as 0"
    assert guests_by_nationality([german], MAR, policy) == {}


# ── the orchestrator: keys, citations, provenance ────────────────────────────


def test_every_computed_value_carries_its_policy_version(policy: Policy) -> None:
    """D-EV-04. A number without its ruleset is not defensible."""
    results = compute_all(
        [reservation(arrival=date(2026, 2, 10), nights=3)], inventory(FEB), [FEB], policy
    )

    assert results.computed
    assert all(value.policy_version == policy.version for value in results.computed.values())


def test_the_denominator_is_cited_to_the_inventory_reference_not_a_pdf(policy: Policy) -> None:
    """The gap that made `InventoryRef` necessary.

    `room_nights_available` is in scope, so a workbook can claim it and a variance on it needs a
    citation — but D-RNA-01 is explicit that rooms available appears in no reservation export. So the
    citation is an inventory row, and 28 contiguous February days collapse into one range rather than
    28 references.
    """
    results = compute_all([], inventory(FEB), [FEB], policy)
    available = results.computed["room_nights_available:2026-02"]

    assert len(available.source_rows) == 1
    assert isinstance(available.primary_ref, InventoryRef)
    assert available.primary_ref.citation == "inventory_2026-Q1.csv rows 1-28"


def test_occupancy_cites_both_sides_of_the_division(policy: Policy) -> None:
    """A reviewer told "occupancy is 4pp out" has to be able to check the numerator *and* the
    denominator, and the denominator is not in the PDF."""
    stays = [reservation(arrival=date(2026, 2, 10), nights=3)]
    occupancy = compute_all(stays, inventory(FEB), [FEB], policy).computed["occupancy_pct:2026-02"]

    kinds = {type(ref) for ref in occupancy.source_rows}
    assert kinds == {PdfRef, InventoryRef}


def test_contiguous_citations_collapse_and_gaps_do_not(policy: Policy) -> None:
    """Ranges are merged only when genuinely adjacent.

    Bridging a gap would claim coverage of rows that did not contribute, and a reviewer who follows
    such a citation finds a number that does not add up with no way to tell whose mistake it is. Four
    hundred single-row refs are unreadable; a citation pointing at an unrelated reservation is worse.
    """
    rows = [1, 2, 3, 9, 10]
    stays = [
        ReservationRecord(
            reservation_id=f"RES-{row:04d}",
            hotel_id="MZN-DXB-001",
            guest_ref="g_0123456789abcdef",
            nationality_iso2="GB",
            adults=1,
            children=0,
            rooms=1,
            nights=2,
            room_nights=2,
            arrival_date=date(2026, 2, 10),
            departure_date=date(2026, 2, 12),
            status=Status.CHECKED_OUT,
            rate_code=RateCode.BAR,
            source=PdfRef(file="pms_2026-02.pdf", page=4, row_start=row, row_end=row),
        )
        for row in rows
    ]

    sold = compute_all(stays, inventory(FEB), [FEB], policy).computed["room_nights_sold:2026-02"]
    citations = [ref.citation for ref in sold.source_rows]

    assert citations == ["pms_2026-02.pdf p.4 rows 1-3", "pms_2026-02.pdf p.4 rows 9-10"]


def test_a_period_with_no_contributing_rows_is_not_verifiable_rather_than_zero(
    policy: Policy,
) -> None:
    """`ComputedValue.source_rows` has `min_length=1` — the contract saying a value from nowhere is
    not a value. The honest answer is a `NotVerifiable`, never a zero with an invented citation."""
    results = compute_all([], inventory(FEB), [FEB], policy)

    assert "room_nights_sold:2026-02" not in results.computed
    assert results.not_verifiable["room_nights_sold:2026-02"].reason == "no_source_rows"


def test_keys_are_produced_for_every_in_scope_metric(policy: Policy) -> None:
    stays = [reservation(arrival=date(2026, 2, 10), nights=3)]
    results = compute_all(stays, inventory(FEB), [FEB], policy)

    produced = {
        Metric(key.split(":")[0]) for key in list(results.computed) + list(results.not_verifiable)
    }
    assert produced == set(Metric)
