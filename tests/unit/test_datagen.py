"""The corpus generator, checked against the definitions it claims to implement.

Two kinds of test live here, and the second kind is the reason this file matters.

**The ordinary kind** asserts the generator does what it says: deterministic in its seed, quotas
met, inventory complete, documents reproducible.

**The cross-check kind** asserts the generator and the product agree *without sharing code*. The
import guard forbids `tools/datagen/` from importing any of `tda`, so the generator re-derives month
apportionment, the qualifying filter and the canonical key format from `docs/01-definitions.md`. That
independence is only worth having if somebody checks the two readings match — otherwise it is two
chances to be wrong instead of one. These tests are allowed to import both sides, which is exactly
what a test is for, and they are where a divergence between the generator and the product surfaces.

A third group proves the byte-reproducibility machinery rejects things, rather than trusting that it
works. Every fix in `reproducible.py` was written after watching a diff, and each is pinned here so
a library upgrade that reintroduces the problem fails a test instead of quietly producing a corpus
that changes on every build.
"""

from __future__ import annotations

import dataclasses
import json
import re
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from datagen import aggregate, claims, ledger, reproducible, spec
from datagen.__main__ import DEFAULT_OUT, LEDGER_COLUMNS, Manifest, build
from datagen.render_pdf import render_month
from datagen.render_workbook import render_workbook
from tda.contracts import (
    InventoryDay,
    InventoryRef,
    MetricKey,
    PdfRef,
    RateCode,
    ReservationRecord,
    Status,
)
from tda.policy import load_policy

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(scope="module")
def rows() -> list[ledger.LedgerRow]:
    return ledger.generate_ledger()


@pytest.fixture(scope="module")
def inventory() -> list[ledger.InventoryRow]:
    return ledger.generate_inventory()


@pytest.fixture(scope="module")
def policy() -> aggregate.PolicyView:
    return aggregate.load_policy_view()


@pytest.fixture(scope="module")
def truth(
    rows: list[ledger.LedgerRow],
    inventory: list[ledger.InventoryRow],
    policy: aggregate.PolicyView,
) -> dict[str, int | float]:
    return aggregate.compute_truth(rows, inventory, policy)


# ── determinism ──────────────────────────────────────────────────────────────


def test_the_ledger_is_deterministic_in_its_seed() -> None:
    """Everything downstream rests on this. A generator that drifted would make every committed
    digest, every fixture and `make repro` itself meaningless — and it would do so silently."""
    assert ledger.generate_ledger() == ledger.generate_ledger()


def test_a_different_seed_produces_a_different_ledger() -> None:
    """The control. A generator that ignored its seed would pass the test above trivially."""
    assert ledger.generate_ledger(seed=1) != ledger.generate_ledger(seed=2)


def test_the_reservation_count_is_exactly_what_the_spec_says() -> None:
    """The quota top-ups run before the bulk fill precisely so this is exact rather than "about".
    A count that floats with the seed makes every other number in the corpus unquotable."""
    assert len(ledger.generate_ledger()) == spec.TOTAL_RESERVATIONS


# ── the derivations the canonical record also enforces ───────────────────────


def test_every_row_satisfies_the_night_and_room_night_derivations(
    rows: list[ledger.LedgerRow],
) -> None:
    """D-RNS-01 and D-RNS-02, asserted over the whole corpus rather than a sample.

    `LedgerRow.__post_init__` raises on violation, so a failure here would mean the dataclass was
    constructed by something that bypassed it. Cheap enough to check all 1,200.
    """
    for row in rows:
        assert row.nights == (row.departure_date - row.arrival_date).days, row.reservation_id
        assert row.room_nights == row.nights * row.rooms, row.reservation_id


def test_every_ledger_row_is_accepted_by_the_canonical_record(
    rows: list[ledger.LedgerRow],
) -> None:
    """The bridge. The generator emits its own type, and the product accepts a `ReservationRecord`.

    If the ledger produced anything the canonical contract rejects — a status string the enum does
    not carry, a `guest_ref` outside the pattern, dates that contradict a night count — the
    extractor in S5 would fail on the corpus it was built against, and it would fail there rather
    than here, a story later, looking like a parser bug.
    """
    source = PdfRef(file="pms_2026-01.pdf", page=1, row_start=1, row_end=1)
    for row in rows:
        record = ReservationRecord(
            reservation_id=row.reservation_id,
            hotel_id=row.hotel_id,
            guest_ref=row.guest_ref,
            nationality_iso2=row.nationality_iso2,
            adults=row.adults,
            children=row.children,
            rooms=row.rooms,
            nights=row.nights,
            room_nights=row.room_nights,
            arrival_date=row.arrival_date,
            departure_date=row.departure_date,
            status=Status(row.status),
            rate_code=RateCode(row.rate_code),
            source=source,
        )
        assert record.guests == row.guests


def test_the_two_apportionment_implementations_agree(rows: list[ledger.LedgerRow]) -> None:
    """The cross-check that justifies the import guard's widest rule.

    `ledger.LedgerRow.occupied_nights` and `ReservationRecord.occupied_nights` are two independent
    readings of D-RNS-03, written from the clause rather than from each other. Their agreeing is the
    evidence that `truth_metrics.json` is ground truth; one of them calling the other would be the
    tautology `tools/guard/import_guard.py` rule 2 exists to prevent.

    Asserted over every row, including the day-use rows where both must return nothing.
    """
    source = PdfRef(file="pms_2026-01.pdf", page=1, row_start=1, row_end=1)
    for row in rows:
        record = ReservationRecord(
            reservation_id=row.reservation_id,
            hotel_id=row.hotel_id,
            guest_ref=row.guest_ref,
            nationality_iso2=row.nationality_iso2,
            adults=row.adults,
            children=row.children,
            rooms=row.rooms,
            nights=row.nights,
            room_nights=row.room_nights,
            arrival_date=row.arrival_date,
            departure_date=row.departure_date,
            status=Status(row.status),
            rate_code=RateCode(row.rate_code),
            source=source,
        )
        assert list(row.occupied_nights()) == record.occupied_nights(), row.reservation_id


def test_the_documented_worked_example() -> None:
    """The `docs/01-definitions.md` §3.1 example, run through the generator's own apportionment.

    2 rooms, arriving 2026-02-28, departing 2026-03-03: 6 room-nights in total, 2 in February and 4
    in March, with the night of 3 March not occupied. The clause calls this "the single most likely
    source of live definitional variance", so it is worth an assertion of its own rather than
    trusting the corpus-wide check above to have covered a case shaped like it.
    """
    example = ledger.LedgerRow(
        reservation_id="RES-EXAMPLE",
        hotel_id=spec.HOTEL_ID,
        guest_ref="g_0000000000000000",
        nationality_iso2="GB",
        adults=2,
        children=0,
        rooms=2,
        nights=3,
        room_nights=6,
        arrival_date=date(2026, 2, 28),
        departure_date=date(2026, 3, 3),
        status="CHECKED_OUT",
        rate_code="BAR",
    )
    assert example.room_nights == 6
    assert example.room_nights_in("2026-02") == 2
    assert example.room_nights_in("2026-03") == 4
    assert example.months_touched() == ["2026-02", "2026-03"]
    assert date(2026, 3, 3) not in list(example.occupied_nights())


# ── the edges, and the check that they are edges rather than luck ────────────


def test_the_corpus_meets_every_edge_quota(rows: list[ledger.LedgerRow]) -> None:
    ledger.check_quotas(rows)  # raises on shortfall


def test_check_quotas_rejects_a_corpus_that_lost_its_edges(
    rows: list[ledger.LedgerRow],
) -> None:
    """The quota check has to be watched rejecting something.

    This is the guard that stops a seed change from silently removing an edge, and a missing edge
    does not fail downstream — a permutation with nothing to permute returns the baseline, matches,
    and reports success. So a quota check that never fires is worse than useless.
    """
    stripped = [row for row in rows if len(row.months_touched()) == 1]

    with pytest.raises(ledger.GeneratorError, match="month_spanning"):
        ledger.check_quotas(stripped)


def test_day_use_reservations_have_no_room_nights_and_still_have_guests(
    rows: list[ledger.LedgerRow], policy: aggregate.PolicyView
) -> None:
    """D-QUAL-08..10. The one edge where a reservation reaches one metric family and not the other."""
    day_use = [row for row in rows if row.is_day_use]
    assert len(day_use) >= spec.QUOTAS.day_use

    for row in day_use:
        assert row.nights == 0
        assert row.room_nights == 0
        assert list(row.occupied_nights()) == []
        assert row.arrival_date == row.departure_date
        assert row.guests >= 1

    # And they are invisible to occupancy while visible to the nationality table.
    qualifying_day_use = [row for row in day_use if aggregate.qualifies(row, policy)]
    assert qualifying_day_use, "the corpus must contain a day-use row that qualifies"
    for row in qualifying_day_use:
        month = f"{row.arrival_date.year:04d}-{row.arrival_date.month:02d}"
        assert aggregate._apportioned_room_nights(row, month, "occupied_night") == 0


def test_stays_arriving_before_the_period_give_room_nights_but_no_guests(
    rows: list[ledger.LedgerRow], policy: aggregate.PolicyView, truth: dict[str, int | float]
) -> None:
    """D-NAT-08 — the case where the two metric families are *expected* not to reconcile.

    A stay arriving 2025-12-29 and departing 2026-01-04 contributes room-nights to January and no
    guests to January. Hand-written fixtures almost never include this, and a workbook in which the
    two families *do* reconcile is the suspicious one.
    """
    early = [row for row in rows if row.arrival_date < spec.PERIOD_START]
    assert len(early) >= spec.QUOTAS.arriving_before_period

    contributed = sum(
        row.room_nights_in("2026-01") for row in early if aggregate.qualifies(row, policy)
    )
    assert contributed > 0, "the pre-period stays must reach January's room-nights"

    # None of them is counted in January's guests: their arrival month is outside the period.
    counted = aggregate.guests_by_nationality(early, "2026-01", policy)
    assert counted == {}, f"pre-period arrivals leaked into January guests: {counted}"

    assert truth["room_nights_sold:2026-01"] >= contributed


def test_in_house_stays_are_open_at_the_period_end(rows: list[ledger.LedgerRow]) -> None:
    """`IN_HOUSE` has to mean something. A status sprinkled at random over departed stays would
    make the qualifying rules testable and the *data* incoherent, which is worse: a reviewer who
    notices would stop trusting the rest of the corpus."""
    in_house = [row for row in rows if row.status == "IN_HOUSE"]
    assert len(in_house) >= spec.QUOTAS.in_house_at_period_end
    assert all(row.departure_date > spec.PERIOD_END for row in in_house)


def test_one_nationality_appears_in_exactly_one_month(truth: dict[str, int | float]) -> None:
    """S8's completeness path needs a row a workbook can plausibly omit. A country present in all
    three months cannot serve: omitting it from one would look like an obvious mistake."""
    present = {
        month: f"guests_by_nationality:{month}:nationality_iso2={spec.SINGLE_MONTH_NATIONALITY}"
        in truth
        for month in spec.MONTHS
    }
    assert sum(present.values()) == 1, present
    assert present[f"2026-{spec.SINGLE_MONTH_MONTH:02d}"]


# ── inventory ────────────────────────────────────────────────────────────────


def test_inventory_covers_every_day_with_no_gaps(inventory: list[ledger.InventoryRow]) -> None:
    """A missing day is indistinguishable from a closed month (D-RNA-05), and the difference between
    "shut" and "not supplied" is the difference between occupancy 0 and occupancy not verifiable."""
    days = [row.day for row in inventory]
    expected = [
        spec.PERIOD_START + timedelta(days=offset)
        for offset in range((spec.PERIOD_END - spec.PERIOD_START).days + 1)
    ]
    assert days == expected
    assert len(days) == 90  # 31 + 28 + 31; 2026 is not a leap year


def test_the_out_of_order_windows_are_present_and_bounded(
    inventory: list[ledger.InventoryRow],
) -> None:
    """D-RNA-03/04. Without a window somewhere, `rooms_total × days` would be a correct shortcut and
    the inventory reference would be decorative."""
    by_day = {row.day: row for row in inventory}

    for window in spec.OUT_OF_ORDER:
        assert by_day[window.first_day].rooms_out_of_order >= window.rooms
        assert by_day[window.last_day].rooms_out_of_order >= window.rooms
        before = window.first_day - timedelta(days=1)
        if before in by_day:
            assert by_day[before].rooms_out_of_order < window.rooms or any(
                other.covers(before) for other in spec.OUT_OF_ORDER if other is not window
            )

    assert any(row.rooms_out_of_order > 0 for row in inventory)
    assert all(0 <= row.rooms_out_of_order <= row.rooms_total for row in inventory)
    assert all(row.rooms_available == row.rooms_total - row.rooms_out_of_order for row in inventory)


def test_every_inventory_row_is_accepted_by_the_canonical_contract(
    inventory: list[ledger.InventoryRow],
) -> None:
    for index, row in enumerate(inventory, start=1):
        day = InventoryDay(
            hotel_id=row.hotel_id,
            day=row.day,
            rooms_total=row.rooms_total,
            rooms_out_of_order=row.rooms_out_of_order,
            # 1-indexed data rows, matching the CSV the generator writes: the header is not row 1.
            source=InventoryRef(file=spec.LAYOUT.inventory_csv, row_start=index, row_end=index),
        )
        assert day.rooms_available == row.rooms_available


def test_room_nights_available_matches_a_hand_computation(
    inventory: list[ledger.InventoryRow], policy: aggregate.PolicyView
) -> None:
    """The denominator, computed here from the spec constants rather than from the same loop.

    January has no out-of-order window, so it is `31 × 60`. February and March each subtract their
    window. Worked out independently so the assertion is a second opinion, not an echo.
    """
    assert aggregate.room_nights_available(inventory, "2026-01", policy) == 31 * 60
    assert aggregate.room_nights_available(inventory, "2026-02", policy) == 28 * 60 - 15 * 6
    assert aggregate.room_nights_available(inventory, "2026-03", policy) == 31 * 60 - 3 * 2


def test_including_out_of_order_rooms_changes_the_denominator(
    inventory: list[ledger.InventoryRow], policy: aggregate.PolicyView
) -> None:
    """`P-OOO-INCLUDED` has to be able to move a number, or the permutation is a no-op that still
    reports a match."""
    including = dataclasses.replace(policy, occupancy_exclude_out_of_order=False)
    assert aggregate.room_nights_available(inventory, "2026-02", including) == 28 * 60
    assert aggregate.room_nights_available(
        inventory, "2026-02", including
    ) > aggregate.room_nights_available(inventory, "2026-02", policy)


# ── occupancy arithmetic ─────────────────────────────────────────────────────


def test_a_zero_denominator_is_a_closed_month_not_an_error(
    policy: aggregate.PolicyView,
) -> None:
    """D-OCC-03. No exception, no division attempted, and the answer is a number."""
    assert aggregate.occupancy_pct(0, 0, policy) == Decimal("0.00")
    assert aggregate.occupancy_pct(120, 0, policy) == Decimal("0.00")


def test_occupancy_is_not_capped_at_one_hundred(policy: aggregate.PolicyView) -> None:
    """D-OCC-05. Oversold rooms or a wrong inventory reference are findings worth surfacing."""
    assert aggregate.occupancy_pct(120, 100, policy) == Decimal("120.00")


def test_occupancy_rounds_half_up_at_the_midpoint(policy: aggregate.PolicyView) -> None:
    """D-OCC-04, and the reason the computation is in `Decimal`.

    `1/8 = 12.5%`, and at two decimal places `100 × 3 / 8 = 37.5` needs no rounding — so the case
    that matters is one whose third decimal is exactly 5. `100 × 45 / 1600 = 2.8125`: half-up gives
    `2.81`, and banker's rounding — which is what Python's `round()` does — gives the same here,
    while `100 × 55/1600 = 3.4375` separates them: half-up `3.44`, half-even `3.44`. The reliable
    separator is a value whose binary float representation falls below the true midpoint, which is
    why this is asserted against Decimal arithmetic rather than against `round()`.
    """
    assert aggregate.occupancy_pct(1, 8, policy) == Decimal("12.50")
    assert aggregate.occupancy_pct(45, 1600, policy) == Decimal("2.81")
    assert aggregate.occupancy_pct(1, 3, policy) == Decimal("33.33")
    assert aggregate.occupancy_pct(2, 3, policy) == Decimal("66.67")


def test_occupancy_in_the_truth_table_is_sold_over_available(
    truth: dict[str, int | float], policy: aggregate.PolicyView
) -> None:
    for period in (*spec.MONTHS, spec.QUARTER):
        sold = truth[f"room_nights_sold:{period}"]
        available = truth[f"room_nights_available:{period}"]
        assert isinstance(sold, int)
        assert isinstance(available, int)
        assert truth[f"occupancy_pct:{period}"] == float(
            aggregate.occupancy_pct(sold, available, policy)
        )


def test_the_quarter_is_the_sum_of_its_months(truth: dict[str, int | float]) -> None:
    """True for the two count metrics and deliberately *not* asserted for occupancy: a quarter's
    occupancy is its own ratio, not the mean of three monthly ratios, and the distinction is why
    D-KEY-01 makes a quarter a separate key."""
    for metric in ("room_nights_sold", "room_nights_available"):
        monthly = sum(int(truth[f"{metric}:{month}"]) for month in spec.MONTHS)
        assert truth[f"{metric}:{spec.QUARTER}"] == monthly


# ── the canonical key format, stated twice and checked once ──────────────────


def test_every_truth_key_parses_as_a_canonical_metric_key(
    truth: dict[str, int | float],
) -> None:
    """The cross-check for `aggregate._render_key`.

    The generator states the key format independently of `tda.contracts.MetricKey`, for the same
    reason it states the apportionment independently. This is what makes the duplication safe: a
    key the generator emits and the product cannot parse fails here rather than in the
    reconciliation join, where it would look like a missing claim.
    """
    assert truth, "the truth table is not empty"
    for key in truth:
        parsed = MetricKey.parse(key)
        assert parsed.rendered == key, f"{key} does not round-trip through MetricKey"


def test_nationality_keys_carry_a_dimension_and_others_do_not(
    truth: dict[str, int | float],
) -> None:
    for key in truth:
        parsed = MetricKey.parse(key)
        assert (parsed.dimension is not None) == parsed.metric.requires_dimension, key


# ── the claim table states the key format a third time ──────────────────────


def test_every_baseline_claim_carries_a_truth_key_and_the_truth_value(
    truth: dict[str, int | float],
) -> None:
    """The cross-check for `ClaimCell.key`.

    `aggregate._render_key` writes the keys, `tda.contracts.MetricKey` parses them, and
    `ClaimCell.key` now builds one from a claim's coordinates so the derivation can look truth up
    by it. Three readings of one format in `docs/01-definitions.md`, and this asserts the third
    agrees with the other two.

    The value is asserted alongside the key on purpose. A key that matched while pointing at the
    wrong figure would satisfy a format check and still derive an expectation against a number the
    workbook never printed, which is the failure a format-only test cannot see.
    """
    table = claims.baseline_table(spec.MONTHS, dict(truth))
    assert table, "the baseline table is not empty"
    for cell in table:
        assert cell.key in truth, f"{cell.key} at {cell.sheet}!{cell.cell} is not a truth key"
        assert MetricKey.parse(cell.key).rendered == cell.key
        assert cell.value == truth[cell.key], cell.key


def test_the_baseline_table_covers_every_truth_key_exactly_once(
    truth: dict[str, int | float],
) -> None:
    """Every verifiable figure is claimed somewhere, and no figure is claimed twice.

    Both halves matter to the derivation. A truth key with no claim cell would make a deletion
    mutation a silent no-op, because there would be nothing to remove and no V5 to derive. A key
    claimed in two cells would make a transposition derive one finding where the pipeline raises
    two. Neither shows up as a failure until a fixture scores wrongly, which is late.
    """
    table = claims.baseline_table(spec.MONTHS, dict(truth))
    keys = [cell.key for cell in table]
    assert sorted(keys) == sorted(truth), "the claim table and the truth table cover the same keys"
    assert len(keys) == len(set(keys)), "no figure is claimed in two cells"


def test_a_country_absent_from_a_month_gets_no_claim_cell(
    truth: dict[str, int | float],
) -> None:
    """A blank is not a claim of zero, and the table has to keep the distinction.

    The demo corpus has countries that travelled in some months and not others, so the skip path
    is exercised by real data rather than by a constructed case. If the table filled those with
    zero, F4's deletion and an ordinary quiet month would derive the same expectation.
    """
    table = claims.baseline_table(spec.MONTHS, dict(truth))
    nationality = [c for c in table if c.metric == claims.NATIONALITY_METRIC]
    countries = claims.value_keys_in_order(nationality)
    periods = (*spec.MONTHS, spec.QUARTER)
    assert len(nationality) < len(countries) * len(periods), (
        "the demo corpus contains at least one country-month with no guests, "
        "so the skip path is covered here"
    )
    for cell in nationality:
        assert cell.value > 0, f"{cell.key} is present, so it is not a blank"


# ── the policy the generator reads is the policy the product loads ───────────


def test_the_generators_policy_view_matches_the_product_loader(
    policy: aggregate.PolicyView,
) -> None:
    """Drift detection, in both directions.

    The generator reads `policy.yaml` with `yaml.safe_load` and a handful of dotted paths; the
    product reads it through a JSON-Schema gate and a typed Pydantic loader. Sharing the *input* is
    intended — ground truth is only meaningful with respect to a ruleset — but two readers of one
    file can disagree, and if they did, `truth_metrics.json` would describe a policy the pipeline
    never ran under. That failure is invisible from either side alone.
    """
    product = load_policy()

    assert policy.version == str(product.version)
    assert policy.status_included == {status.value for status in product.qualifying.status.included}
    assert policy.rate_code_included == {
        code.value for code in product.qualifying.rate_code.included
    }
    assert policy.day_use_counts_room_nights == product.qualifying.day_use.counts_room_nights
    assert policy.day_use_counts_guests == product.qualifying.day_use.counts_guests
    assert policy.occupancy_month_basis == product.metrics.occupancy_pct.month_basis
    assert policy.occupancy_exclude_out_of_order == (
        product.metrics.occupancy_pct.exclude_out_of_order
    )
    assert policy.occupancy_decimal_places == (
        product.metrics.occupancy_pct.presentation_decimal_places
    )
    assert policy.guests_month_basis == product.metrics.guests_by_nationality.month_basis
    assert policy.guests_include_children == product.metrics.guests_by_nationality.include_children
    assert policy.guests_counts == product.metrics.guests_by_nationality.counts


def test_a_missing_policy_rule_is_an_error_naming_the_path(tmp_path: Path) -> None:
    """A default here would produce a `truth_metrics.json` describing a policy nobody configured."""
    partial = tmp_path / "policy.yaml"
    partial.write_text("version: 9.9.9\n", encoding="utf-8")

    with pytest.raises(aggregate.PolicyReadError, match=r"qualifying\.status\.included"):
        aggregate.load_policy_view(partial)


def test_a_policy_rule_of_the_wrong_type_is_rejected_not_coerced(tmp_path: Path) -> None:
    """The case `_require_type` exists for, and the reason it checks rather than coerces.

    `exclude_out_of_order: "false"` is a plausible hand-edit. `bool("false")` is `True`, so a
    coercing reader would silently include out-of-order rooms in the denominator while the file says
    to exclude them — and the corpus would then describe the opposite of the configured policy.
    """
    source = (aggregate.POLICY_PATH).read_text(encoding="utf-8")
    broken = tmp_path / "policy.yaml"
    broken.write_text(
        source.replace("exclude_out_of_order: true", 'exclude_out_of_order: "false"'),
        encoding="utf-8",
    )

    with pytest.raises(aggregate.PolicyReadError, match="expected bool"):
        aggregate.load_policy_view(broken)


# ── no guest name, anywhere ──────────────────────────────────────────────────


def test_guest_references_are_opaque_and_match_the_canonical_pattern(
    rows: list[ledger.LedgerRow],
) -> None:
    """D-EV-03, enforced by having nothing to leak rather than by withholding something.

    `guest_ref` is a digest under a published salt. There is no name behind it, which is a stronger
    guarantee than a name that is carefully not printed: the careful version is one stray log line
    away from failing.
    """
    pattern = re.compile(r"^g_[0-9a-f]{8,32}$")
    for row in rows:
        assert pattern.match(row.guest_ref), row.guest_ref

    assert len({row.guest_ref for row in rows}) == len(rows), "guest refs collided"


def test_the_ledger_has_no_name_shaped_column() -> None:
    """A structural check on the committed corpus: the ledger CSV has no name column at all.

    Asserting on the *column set* rather than scanning values for names, because a scan can only
    ever find the names it knows to look for. Absent columns cannot leak.
    """
    forbidden = {"guest_name", "name", "first_name", "last_name", "surname", "email", "passport"}
    assert not forbidden & set(LEDGER_COLUMNS)
    assert "guest_ref" in LEDGER_COLUMNS


# ── byte-reproducibility, proven rather than asserted ────────────────────────


def _sample(rows: list[ledger.LedgerRow]) -> list[ledger.LedgerRow]:
    """A small slice, so the reproducibility tests render in well under a second each."""
    return [row for row in rows if "2026-02" in row.months_touched()][:40]


def test_the_pdf_renderer_is_byte_reproducible(
    tmp_path: Path,
    rows: list[ledger.LedgerRow],
    inventory: list[ledger.InventoryRow],
    policy: aggregate.PolicyView,
) -> None:
    """reportlab stamps a creation date and a document ID unless `invariant=1` is passed.

    Without this test the fix is a keyword argument nobody would think to question, and a reportlab
    upgrade that renamed or ignored it would leave `make repro` measuring the generator's entropy.
    """
    sample = _sample(rows)
    first, second = tmp_path / "a.pdf", tmp_path / "b.pdf"
    render_month(first, "2026-02", sample, inventory, policy)
    render_month(second, "2026-02", sample, inventory, policy)

    assert first.read_bytes() == second.read_bytes()


def test_the_workbook_is_byte_reproducible_only_after_finalising(
    tmp_path: Path, truth: dict[str, int | float]
) -> None:
    """Both halves, because the interesting assertion is the negative one.

    A saved-but-not-finalised workbook differs between runs: `Workbook.save` stamps
    `time.localtime()` into every zip entry and rewrites `dcterms:modified` from the wall clock.
    Asserting only that `finalise_xlsx` produces identical bytes would pass even if the whole fix
    were removed — on a fast enough machine two saves can land in the same second. So this pins the
    difference the fix makes, by comparing what is inside the archives rather than the archives
    themselves.
    """
    raw_a, raw_b = tmp_path / "raw_a.xlsx", tmp_path / "raw_b.xlsx"
    render_workbook(raw_a, spec.MONTHS, truth)
    render_workbook(raw_b, spec.MONTHS, truth)

    def modified_stamp(path: Path) -> bytes:
        with zipfile.ZipFile(path) as archive:
            core = archive.read("docProps/core.xml")
        return core.split(b"<dcterms:modified")[1].split(b"</dcterms:modified>")[0]

    # Unfinalised, the modification stamp is the wall clock rather than the pinned constant.
    assert spec.PINNED_OOXML_MODIFIED.encode() not in modified_stamp(raw_a)

    reproducible.finalise_xlsx(raw_a)
    reproducible.finalise_xlsx(raw_b)

    assert spec.PINNED_OOXML_MODIFIED.encode() in modified_stamp(raw_a)
    assert raw_a.read_bytes() == raw_b.read_bytes()


def test_finalising_pins_every_zip_entry_timestamp(
    tmp_path: Path, truth: dict[str, int | float]
) -> None:
    """The first of the three fixes, checked on its own.

    Entry timestamps are invisible in the spreadsheet and decide the file's digest, which is the
    combination that makes this worth a test rather than a comment.
    """
    path = tmp_path / "claims.xlsx"
    render_workbook(path, spec.MONTHS, truth)
    reproducible.finalise_xlsx(path)

    with zipfile.ZipFile(path) as archive:
        stamps = {info.date_time for info in archive.infolist()}
        systems = {info.create_system for info in archive.infolist()}

    assert stamps == {spec.PINNED_ZIP_DATE_TIME}
    assert systems == {3}, "create_system must be pinned, or Windows and Linux builds differ"


def test_the_workbook_still_opens_after_the_archive_is_rewritten(
    tmp_path: Path, truth: dict[str, int | float]
) -> None:
    """Rewriting a zip in place is the kind of fix that works until somebody opens the file.

    The member order is preserved for exactly this reason, and preserving it is only credible if
    something loads the result.
    """
    from openpyxl import load_workbook

    path = tmp_path / "claims.xlsx"
    render_workbook(path, spec.MONTHS, truth)
    reproducible.finalise_xlsx(path)

    workbook = load_workbook(path)
    assert workbook.sheetnames == [
        spec.WORKBOOK.summary_sheet,
        spec.WORKBOOK.occupancy_sheet,
        spec.WORKBOOK.nationality_sheet,
        spec.WORKBOOK.out_of_scope_sheet,
    ]
    occupancy = workbook[spec.WORKBOOK.occupancy_sheet]
    assert occupancy.cell(row=spec.WORKBOOK.header_row, column=1).value == "Month"


def test_the_modified_stamp_patch_fails_loudly_if_it_matches_nothing() -> None:
    """A substitution that silently matched nothing is how this regresses.

    The fix would still be in the code, still be called, and do nothing — and the corpus would go
    back to changing on every build, with `make corpus` failing on every machine for a reason that
    points at the wrong file.
    """
    with pytest.raises(reproducible.ReproducibilityError, match="found 0"):
        reproducible._patch_core_properties("docProps/core.xml", b"<coreProperties/>")

    # And a member that is not core.xml passes through untouched.
    payload = b"<worksheet/>"
    assert reproducible._patch_core_properties("xl/worksheets/sheet1.xml", payload) is payload


# ── the committed corpus matches the generator ───────────────────────────────


def test_the_committed_corpus_is_reproducible_from_this_code(tmp_path: Path) -> None:
    """`make corpus` as a test, so a stale corpus fails the suite and not only the Makefile.

    This catches both halves of what can go wrong: a generator change nobody regenerated for, and a
    corpus file edited by hand. The second is the worse one — `truth_metrics.json` would then no
    longer describe the documents beside it, and every number downstream would be checked against a
    fiction.
    """
    committed_path = DEFAULT_OUT / "manifest.json"
    assert committed_path.exists(), "corpus/demo is not committed; run `make datagen`"

    committed = Manifest.parse(json.loads(committed_path.read_text(encoding="utf-8")))
    rebuilt = build(tmp_path / "corpus")

    assert rebuilt.files == committed.files, (
        "the committed corpus does not match this generator. Run `make datagen` and commit the "
        "result, or find out who edited a corpus file by hand."
    )
    assert rebuilt.seed == committed.seed == spec.SEED
    assert rebuilt.spec_digest == committed.spec_digest


def test_the_committed_truth_metrics_match_a_fresh_aggregation(
    truth: dict[str, int | float],
) -> None:
    committed = json.loads(
        (DEFAULT_OUT / spec.LAYOUT.ground_truth / spec.LAYOUT.truth_metrics).read_text(
            encoding="utf-8"
        )
    )
    assert committed["metrics"] == truth
    assert committed["policy_version"] == str(load_policy().version)
