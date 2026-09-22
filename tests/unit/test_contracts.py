"""The contracts must make non-compliant data unconstructable.

Each test names the definitions clause it enforces, so a reader can go from a failing test to
the rule it protects without guessing (the convention S4's metric tests follow too).

The emphasis is on the *negative* cases. A contract that accepts valid data is table stakes;
the value here is that invalid data cannot exist, so downstream code never has to check.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tda.contracts import (
    Claim,
    ComputedValue,
    Dimension,
    EscalationTarget,
    ExcelRef,
    ExtractionSummary,
    Finding,
    InventoryDay,
    InventoryRef,
    Metric,
    MetricKey,
    NotReached,
    PdfRef,
    RateCode,
    ReservationRecord,
    ReviewerDecision,
    ReviewRecord,
    Severity,
    Status,
    VarianceClass,
    Verdict,
    VerdictStatus,
    index_by_key,
)

PDF = PdfRef(file="2026-01.pdf", page=3, row_start=12, row_end=12)
XL = ExcelRef(sheet="Nationality", cell="D14")


def reservation(**overrides: object) -> ReservationRecord:
    """A valid record. Tests override one field to make exactly one thing wrong."""
    base: dict[str, object] = {
        "reservation_id": "R-0001",
        "hotel_id": "RAK-001",
        "guest_ref": "g_4f2a91c07b3e",
        "nationality_iso2": "DE",
        "adults": 2,
        "children": 1,
        "rooms": 1,
        "nights": 3,
        "room_nights": 3,
        "arrival_date": date(2026, 1, 10),
        "departure_date": date(2026, 1, 13),
        "status": Status.CHECKED_OUT,
        "rate_code": RateCode.BAR,
        "source": PDF,
    }
    return ReservationRecord(**(base | overrides))  # type: ignore[arg-type]


# ── D-RNS-02: room_nights is always derived ──────────────────────────────────


def test_room_nights_must_equal_nights_times_rooms() -> None:
    """D-RNS-02. Derive, never trust.

    This is what makes "a printed room_nights column is a cross-check, never an input"
    structural: a parser that adopts the printed value cannot build the record.
    """
    with pytest.raises(ValidationError, match="is not nights x rooms"):
        reservation(rooms=2, nights=3, room_nights=3)  # printed value adopted; truth is 6


def test_room_nights_derived_correctly_for_multiple_rooms() -> None:
    assert reservation(rooms=2, nights=3, room_nights=6).room_nights == 6


def test_nights_must_match_the_dates() -> None:
    """D-RNS-01. A record cannot carry a night count that contradicts its own dates."""
    with pytest.raises(ValidationError, match="contradicts the dates"):
        reservation(nights=5, room_nights=5)  # dates say 3


def test_departure_before_arrival_is_rejected() -> None:
    with pytest.raises(ValidationError, match="precedes arrival"):
        reservation(
            arrival_date=date(2026, 1, 13),
            departure_date=date(2026, 1, 10),
            nights=3,
            room_nights=3,
        )


# ── D-RNS-03: occupied nights, and the month-spanning worked example ─────────


def test_occupied_nights_excludes_the_departure_date() -> None:
    """D-RNS-03. The night of the departure date is not occupied."""
    nights = reservation().occupied_nights()
    assert nights == [date(2026, 1, 10), date(2026, 1, 11), date(2026, 1, 12)]
    assert date(2026, 1, 13) not in nights


def test_month_spanning_stay_matches_the_worked_example() -> None:
    """D-RNS-03 / A-04, the exact example in the definitions: 2 rooms, 28 Feb to 3 Mar.

    The single most likely source of live definitional variance, so it is pinned here with the
    arithmetic spelled out rather than left to the metric library's own tests.
    """
    record = reservation(
        rooms=2,
        nights=3,
        room_nights=6,
        arrival_date=date(2026, 2, 28),
        departure_date=date(2026, 3, 3),
    )
    nights = record.occupied_nights()

    assert nights == [date(2026, 2, 28), date(2026, 3, 1), date(2026, 3, 2)]
    february = [n for n in nights if n.month == 2]
    march = [n for n in nights if n.month == 3]
    assert len(february) * record.rooms == 2, "February gets the night of the 28th"
    assert len(march) * record.rooms == 4, "March gets the nights of the 1st and 2nd"
    assert record.room_nights == 6


# ── D-QUAL-08..10: day-use, and the deliberate disagreement ──────────────────


def test_day_use_has_zero_room_nights_but_still_has_guests() -> None:
    """D-QUAL-09 and D-QUAL-10 disagree on purpose, asserted on one record.

    Occupancy measures rooms sold overnight; the nationality table counts people who came. A
    day-use visitor is one and not the other, and this test is where that is visible.
    """
    day_use = reservation(
        nights=0, room_nights=0, arrival_date=date(2026, 1, 10), departure_date=date(2026, 1, 10)
    )
    assert day_use.is_day_use
    assert day_use.room_nights == 0
    assert day_use.occupied_nights() == [], "invisible to occupancy"
    assert day_use.guests == 3, "still three guests of the destination"


# ── D-EV-03: no guest name enters state ──────────────────────────────────────


@pytest.mark.parametrize("leaked", ["Jane Doe", "jane.doe@example.com", "DOE/JANE MRS", ""])
def test_guest_ref_rejects_anything_name_shaped(leaked: str) -> None:
    """D-EV-03, enforced by the field pattern rather than by a scan after the fact.

    A parser that reaches for the name column fails here, loudly, instead of leaking a name
    into a verdict that gets emailed to a hotel.
    """
    with pytest.raises(ValidationError):
        reservation(guest_ref=leaked)


# ── D-QUAL-03: unknown enum values are blocking, never coerced ───────────────


@pytest.mark.parametrize("vendor_string", ["Checked Out", "CO", "checked_out", "DEPARTED"])
def test_unknown_status_strings_are_rejected_not_coerced(vendor_string: str) -> None:
    """D-QUAL-03. Normalisation is the parser's committed-lookup problem, not this enum's
    guessing problem. `Checked Out` is *obviously* CHECKED_OUT to a human, which is exactly
    why permitting it here would be the start of a slide."""
    with pytest.raises(ValidationError):
        reservation(status=vendor_string)


def test_nationality_must_already_be_iso2() -> None:
    """D-NAT-12 / D-NAT-15. Only canonical codes reach a record."""
    for bad in ["Germany", "DEU", "de", "D"]:
        with pytest.raises(ValidationError):
            reservation(nationality_iso2=bad)


# ── references ───────────────────────────────────────────────────────────────


def test_source_ref_rejects_a_path() -> None:
    """An absolute path in a citation leaks the machine that ran the verification into an
    artefact that gets forwarded to a hotel."""
    with pytest.raises(ValidationError, match="bare filename"):
        PdfRef(file="/home/ci/corpus/2026-01.pdf", page=1, row_start=1, row_end=1)


def test_source_ref_rejects_reversed_rows() -> None:
    with pytest.raises(ValidationError, match="precedes row_start"):
        PdfRef(file="x.pdf", page=1, row_start=9, row_end=4)


def test_pdf_citation_reads_as_a_human_would_write_it() -> None:
    assert PdfRef(file="a.pdf", page=4, row_start=12, row_end=18).citation == "a.pdf p.4 rows 12-18"
    assert PdfRef(file="a.pdf", page=4, row_start=12, row_end=12).citation == "a.pdf p.4 row 12"


@pytest.mark.parametrize("cell", ["$A$1", "A", "1", "a1", "AAAA1", "A0"])
def test_excel_ref_rejects_non_a1_cells(cell: str) -> None:
    """`$A$1` is rejected rather than stripped: an absolute marker means the parser read a
    formula, not a value, and quietly normalising it hides that."""
    with pytest.raises(ValidationError):
        ExcelRef(sheet="S", cell=cell)


def test_excel_citation_is_canonical_a1() -> None:
    assert XL.citation == "Nationality!D14"


# ── metric keys ──────────────────────────────────────────────────────────────


def test_metric_key_round_trips() -> None:
    for key in [
        MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-01"),
        MetricKey(
            metric=Metric.GUESTS_BY_NATIONALITY,
            period="2026-02",
            dimension=Dimension.NATIONALITY_ISO2,
            value="DE",
        ),
        MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period="2026-Q1"),
    ]:
        assert MetricKey.parse(key.rendered) == key


def test_nationality_key_requires_a_dimension() -> None:
    """A nationality figure without a country is not a claim about anything."""
    with pytest.raises(ValidationError, match="requires a dimension"):
        MetricKey(metric=Metric.GUESTS_BY_NATIONALITY, period="2026-01")


def test_occupancy_key_rejects_a_dimension() -> None:
    with pytest.raises(ValidationError, match="takes no dimension"):
        MetricKey(
            metric=Metric.OCCUPANCY_PCT,
            period="2026-01",
            dimension=Dimension.NATIONALITY_ISO2,
            value="DE",
        )


@pytest.mark.parametrize("period", ["2026-13", "2026-00", "26-01", "2026-1", "Q1-2026", ""])
def test_malformed_periods_are_rejected(period: str) -> None:
    with pytest.raises(ValidationError):
        MetricKey(metric=Metric.OCCUPANCY_PCT, period=period)


def test_dimension_value_must_be_canonical() -> None:
    """D-KEY-02. `CZ`, never `Czechia` — whatever the document said."""
    with pytest.raises(ValidationError, match="alpha-2"):
        MetricKey(
            metric=Metric.GUESTS_BY_NATIONALITY,
            period="2026-01",
            dimension=Dimension.NATIONALITY_ISO2,
            value="Czechia",
        )


# ── claims and computed values ───────────────────────────────────────────────


def test_duplicate_claims_are_refused_not_deduplicated() -> None:
    """Two claims on one key means the workbook asserts the same figure twice. Silently
    keeping the last would make a self-contradicting workbook look consistent."""
    key = MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-01")
    claims = [
        Claim(key=key, value=Decimal("71.40"), excel_ref=ExcelRef(sheet="Occ", cell="B2")),
        Claim(key=key, value=Decimal("71.90"), excel_ref=ExcelRef(sheet="Occ", cell="B3")),
    ]
    with pytest.raises(ValueError, match="duplicate claim"):
        index_by_key(claims)

    assert len(index_by_key(claims[:1])) == 1


def test_computed_value_needs_at_least_one_source_row() -> None:
    """A value with no source rows is a value from nowhere."""
    with pytest.raises(ValidationError):
        ComputedValue(
            key=MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-01"),
            value=Decimal("71.4"),
            source_rows=(),
            policy_version="1.1.0",
        )


# ── Finding: the four promises ───────────────────────────────────────────────


def finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "finding_id": "F-0001",
        "key": MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-01"),
        "variance_class": VarianceClass.TRANSCRIPTION,
        "severity": Severity.MATERIAL,
        "escalates_to": EscalationTarget.HOTEL,
        "claimed": Decimal("71.40"),
        "computed": Decimal("68.20"),
        "difference": Decimal("3.20"),
        "source_ref": PDF,
        "excel_ref": XL,
        "clause": "D-OCC-01",
    }
    return Finding(**(base | overrides))  # type: ignore[arg-type]


def test_a_valid_finding_carries_both_citations() -> None:
    assert finding().citation == "2026-01.pdf p.3 row 12 | Nationality!D14"


def test_a_missing_excel_cell_is_legal_only_where_no_cell_exists() -> None:
    """D-EV-02 and D-MAT-04. There are exactly two such cases, and the rule names both.

    This started as "only V7" and was widened by reconciliation and classification, which is the same correction validator 6
    needed in PDF extraction: the rule was written when V7 was the only absence anyone had met. A **missing
    claim** — the source supports a figure and the workbook is silent about it — has no cell because
    the hotel wrote nothing, and that silence *is* the finding.

    The widening is narrow on purpose, and the third case below is what keeps it honest: a V5
    **orphan** claim has a cell, and is still refused.
    """
    absent = NotReached(reason="extraction halted before claim parsing")

    with pytest.raises(ValidationError, match="Only a V7, or a V5 with no claimed value"):
        finding(excel_ref=absent)

    blocking = finding(
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        excel_ref=absent,
        computed=None,
        difference=None,
    )
    assert blocking.excel_ref.citation.startswith("not reached")

    missing_claim = finding(
        variance_class=VarianceClass.COMPLETENESS,
        excel_ref=NotReached(reason="the workbook states no figure for this key"),
        claimed=None,
        difference=None,
        proposed_correction=None,
    )
    assert missing_claim.excel_ref.citation.startswith("not reached")

    # …and the direction where a cell does exist is still refused. A V5 orphan claim was read from
    # somewhere, so it has a citation and must carry it.
    with pytest.raises(ValidationError, match="Only a V7, or a V5 with no claimed value"):
        finding(
            variance_class=VarianceClass.COMPLETENESS,
            excel_ref=absent,
            claimed=Decimal("71.40"),
            computed=None,
            difference=None,
            proposed_correction=None,
        )


def test_definitional_finding_must_name_its_permutation() -> None:
    """D-CLS-07. "This is a policy difference, trust us" is not expressible."""
    with pytest.raises(ValidationError, match="requires explaining_permutation"):
        finding(
            variance_class=VarianceClass.DEFINITIONAL,
            escalates_to=EscalationTarget.POLICY_OWNER,
        )

    named = finding(
        variance_class=VarianceClass.DEFINITIONAL,
        escalates_to=EscalationTarget.POLICY_OWNER,
        explaining_permutation="P-OCC-DENOM-ROOMS",
    )
    assert named.explaining_permutation == "P-OCC-DENOM-ROOMS"


def test_non_definitional_finding_must_not_name_a_permutation() -> None:
    with pytest.raises(ValidationError, match="must not carry explaining_permutation"):
        finding(explaining_permutation="P-OCC-DENOM-ROOMS")


def test_only_transcription_proposes_a_correction() -> None:
    """Proposing a corrected value for a definitional variance tells a hotel to change a
    number that is not wrong."""
    assert finding(proposed_correction=Decimal("68.20")).proposed_correction == Decimal("68.20")

    with pytest.raises(ValidationError, match="must not propose a correction"):
        finding(
            variance_class=VarianceClass.DEFINITIONAL,
            escalates_to=EscalationTarget.POLICY_OWNER,
            explaining_permutation="P-MONTH-ARRIVAL",
            proposed_correction=Decimal("68.20"),
        )


def test_v7_is_always_blocking() -> None:
    """D-MAT-01. "I could not read this" is never informational."""
    with pytest.raises(ValidationError, match="V7 is always blocking"):
        finding(
            variance_class=VarianceClass.EXTRACTION_LIMIT,
            severity=Severity.INFORMATIONAL,
            escalates_to=EscalationTarget.HUMAN_REVIEW,
        )


def test_definitional_finding_cannot_be_escalated_to_the_hotel() -> None:
    """D-MAT-06. Reporting a rule disagreement as a hotel error is how correct findings
    discredit the system."""
    with pytest.raises(ValidationError, match="escalates to the policy owner"):
        finding(
            variance_class=VarianceClass.DEFINITIONAL,
            escalates_to=EscalationTarget.HOTEL,
            explaining_permutation="P-MONTH-ARRIVAL",
        )


def test_difference_must_actually_be_the_difference() -> None:
    """Cheap to check, and it catches a sign error that would otherwise be argued about in a
    meeting with the hotel."""
    with pytest.raises(ValidationError, match="is not claimed - computed"):
        finding(difference=Decimal("-3.20"))


def test_a_finding_needs_at_least_one_side() -> None:
    with pytest.raises(ValidationError, match="at least one of claimed or computed"):
        finding(claimed=None, computed=None, difference=None)


def test_is_hotel_error_excludes_definitional_and_blocking() -> None:
    """The property the memo's "hotel errors" count is built from."""
    assert finding(variance_class=VarianceClass.TRANSCRIPTION).is_hotel_error
    assert finding(
        variance_class=VarianceClass.COMPLETENESS, escalates_to=EscalationTarget.HOTEL
    ).is_hotel_error
    assert not finding(
        variance_class=VarianceClass.DEFINITIONAL,
        escalates_to=EscalationTarget.POLICY_OWNER,
        explaining_permutation="P-MONTH-ARRIVAL",
    ).is_hotel_error
    assert not finding(
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
    ).is_hotel_error


# ── inventory ────────────────────────────────────────────────────────────────

INVENTORY_SOURCE = InventoryRef(file="inventory_2026-Q1.csv", row_start=5, row_end=5)


def test_out_of_order_rooms_reduce_availability() -> None:
    """D-RNA-03 / A-08."""
    day = InventoryDay(
        hotel_id="RAK-001",
        day=date(2026, 1, 5),
        rooms_total=120,
        rooms_out_of_order=8,
        source=INVENTORY_SOURCE,
    )
    assert day.rooms_available == 112


def test_out_of_order_cannot_exceed_total() -> None:
    with pytest.raises(ValidationError, match="exceeds"):
        InventoryDay(
            hotel_id="RAK-001",
            day=date(2026, 1, 5),
            rooms_total=120,
            rooms_out_of_order=121,
            source=INVENTORY_SOURCE,
        )


def test_a_closed_day_is_representable() -> None:
    """D-RNA-05. A closed month is a fact the reference can state, distinct from a missing
    reference (D-RNA-04) — and the two must not be confused, because one is verifiable and
    the other is not."""
    closed = InventoryDay(
        hotel_id="RAK-001",
        day=date(2026, 1, 5),
        rooms_total=0,
        rooms_out_of_order=0,
        source=INVENTORY_SOURCE,
    )
    assert closed.rooms_available == 0


def test_an_inventory_day_cannot_be_built_without_a_citation() -> None:
    """`source` is required, symmetrically with `ReservationRecord.source`.

    `room_nights_available` is in scope, so a workbook can claim it and a variance on it needs
    something to cite. Because rooms available appears in no reservation export (D-RNA-01), that
    citation is an inventory row rather than a PDF page — which is why `InventoryRef` exists at all.
    """
    with pytest.raises(ValidationError, match="source"):
        InventoryDay(  # type: ignore[call-arg]
            hotel_id="RAK-001", day=date(2026, 1, 5), rooms_total=120, rooms_out_of_order=8
        )


def test_an_inventory_citation_reads_as_a_row_range() -> None:
    ref = InventoryRef(file="inventory_2026-Q1.csv", row_start=32, row_end=59)
    assert ref.citation == "inventory_2026-Q1.csv rows 32-59"
    assert InventoryRef(file="i.csv", row_start=7, row_end=7).citation == "i.csv row 7"


def test_an_inventory_citation_rejects_a_path_and_reversed_rows() -> None:
    """The same two rules `PdfRef` enforces. An absolute path in a citation leaks the machine that
    ran the verification into an artefact forwarded to a hotel."""
    with pytest.raises(ValidationError, match="bare filename"):
        InventoryRef(file="/tmp/inventory.csv", row_start=1, row_end=1)
    with pytest.raises(ValidationError, match="precedes"):
        InventoryRef(file="inventory.csv", row_start=9, row_end=4)


# ── Verdict ──────────────────────────────────────────────────────────────────


def verdict(**overrides: object) -> Verdict:
    base: dict[str, object] = {
        "run_id": "run-0001",
        "status": VerdictStatus.FAIL,
        "hotel_id": "RAK-001",
        "period": "2026-Q1",
        "policy_version": "1.1.0",
        "metric_library_version": "0.1.0",
        "model_id": "claude-opus-5",
        "provider_mode": "replay",
        "claims_checked": 42,
        "extraction": ExtractionSummary(
            files=("2026-01.pdf",),
            records_extracted=1200,
            pages_read=40,
            printed_total_matched=True,
            duplicate_ids=0,
        ),
    }
    return Verdict(**(base | overrides))  # type: ignore[arg-type]


DEFINITIONAL = finding(
    finding_id="F-0002",
    variance_class=VarianceClass.DEFINITIONAL,
    escalates_to=EscalationTarget.POLICY_OWNER,
    explaining_permutation="P-OCC-DENOM-ROOMS",
)


def test_definitional_items_may_not_sit_in_findings() -> None:
    """D-MAT-06, enforced structurally rather than by a filter at render time.

    A caller that iterates `findings` and counts hotel errors gets the right answer by
    default, not by remembering to exclude V2.
    """
    with pytest.raises(ValidationError, match="definitional findings in `findings`"):
        verdict(findings=(DEFINITIONAL,))


def test_non_definitional_items_may_not_sit_in_definitional_items() -> None:
    with pytest.raises(ValidationError, match="non-V2 findings in `definitional_items`"):
        verdict(definitional_items=(finding(),))


def test_hotel_error_count_excludes_definitional_items() -> None:
    result = verdict(findings=(finding(),), definitional_items=(DEFINITIONAL,))
    assert result.hotel_error_count == 1, "the V2 must not be counted as a hotel error"
    assert result.severity_counts["material"] == 2, "but it is still a material item overall"


def test_pass_means_zero_findings() -> None:
    """Not "zero findings I considered important". Two of six fixtures must produce exactly
    this, and precision is what a reviewer actually feels."""
    assert verdict(status=VerdictStatus.PASS).status is VerdictStatus.PASS

    with pytest.raises(ValidationError, match="PASS means zero of both"):
        verdict(status=VerdictStatus.PASS, findings=(finding(),))

    with pytest.raises(ValidationError, match="PASS means zero of both"):
        verdict(status=VerdictStatus.PASS, definitional_items=(DEFINITIONAL,))


def test_a_blocking_finding_forces_halted_or_escalated() -> None:
    blocking = finding(
        finding_id="F-0003",
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        excel_ref=NotReached(reason="extraction halted"),
        computed=None,
        difference=None,
    )
    with pytest.raises(ValidationError, match="HALTED or ESCALATED"):
        verdict(status=VerdictStatus.FAIL, findings=(blocking,))

    assert verdict(status=VerdictStatus.HALTED, findings=(blocking,)).status is VerdictStatus.HALTED


def test_rejected_verdict_must_state_a_reason_code() -> None:
    with pytest.raises(ValidationError, match="stated reason code"):
        verdict(status=VerdictStatus.REJECTED)


def test_duplicate_finding_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate finding ids"):
        verdict(findings=(finding(), finding()))


def test_review_records_must_reference_real_findings() -> None:
    stray = ReviewRecord(
        finding_id="F-9999",
        decision=ReviewerDecision.ACCEPT,
        reviewer="A. Officer",
        decided_at=datetime(2026, 4, 1, 9, 30, tzinfo=UTC),
    )
    with pytest.raises(ValidationError, match="unknown findings"):
        verdict(findings=(finding(),), review_records=(stray,))


def test_undecided_findings_drives_the_review_gate() -> None:
    """FR-10. This property is how the review screen knows it is not finished."""
    result = verdict(findings=(finding(),), definitional_items=(DEFINITIONAL,))
    assert result.undecided_findings == ("F-0001", "F-0002")

    decided = verdict(
        findings=(finding(),),
        definitional_items=(DEFINITIONAL,),
        review_records=(
            ReviewRecord(
                finding_id="F-0001",
                decision=ReviewerDecision.ACCEPT,
                reviewer="A. Officer",
                decided_at=datetime(2026, 4, 1, 9, 30, tzinfo=UTC),
            ),
        ),
    )
    assert decided.undecided_findings == ("F-0002",)


def test_amend_must_carry_the_amended_value() -> None:
    with pytest.raises(ValidationError, match="must carry the amended value"):
        ReviewRecord(
            finding_id="F-0001",
            decision=ReviewerDecision.AMEND,
            reviewer="A. Officer",
            decided_at=datetime(2026, 4, 1, 9, 30, tzinfo=UTC),
        )


def test_review_timestamp_must_be_timezone_aware() -> None:
    """A naive timestamp on an audit record is ambiguous by exactly the hours that matter
    when somebody asks who approved what, and when."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        ReviewRecord(
            finding_id="F-0001",
            decision=ReviewerDecision.ACCEPT,
            reviewer="A. Officer",
            decided_at=datetime(2026, 4, 1, 9, 30),  # noqa: DTZ001
        )


def test_contracts_are_frozen() -> None:
    """An extracted record whose citation might no longer describe it is worse than no
    citation, because it is a citation that lies."""
    record = reservation()
    with pytest.raises(ValidationError):
        record.rooms = 9  # type: ignore[misc]
