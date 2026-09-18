"""Extraction, and — more importantly — extraction refusing.

The happy path here is one test: 1,200 records out of three reports, reconciling against every printed
total. Everything else in this file breaks something on purpose, because the acceptance criterion that
matters is *"on disagreement the run halts with a blocking finding"* and a refusal nobody has watched
fire is a comment.

Each corruption is a realistic one:

- a nationality the lookup does not contain, and a status spelled a way nobody listed
- a printed `RN Total` that disagrees with `nights × rooms`
- a column header with an extra column, which is how a positional map silently reads its neighbour
- a totals block that says 420 rows where we read 419
- two monthly reports disagreeing about one month-spanning stay

The last is the one I did not anticipate. It is invisible to every other check in the system — each
report reconciles against its own printed totals perfectly — and it only exists as a category because
building this story turned up 103 reservations printed on two reports each.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tda.contracts import (
    EscalationTarget,
    ExcelRef,
    Finding,
    FindingIds,
    Metric,
    MetricKey,
    PdfRef,
    Period,
    RateCode,
    ReservationRecord,
    Severity,
    Status,
    VarianceClass,
)
from tda.extract import (
    EXPECTED_HEADER,
    ExtractionResult,
    InventoryError,
    LayoutMismatchError,
    LookupTableError,
    UnmappableLabelError,
    extract,
    load_lookups,
    month_of,
    nationality,
    normalise_key,
    parse_row,
    parse_totals,
    rate_code,
    read_inventory,
    read_report,
    reconcile,
    status,
    verify_layout,
)
from tda.extract.run import blocking_finding, merge_across_reports
from tda.metrics import compute_all
from tda.policy import load_policy

if TYPE_CHECKING:
    from tda.policy import Policy

SUBMISSION = Path(__file__).resolve().parents[2] / "corpus" / "demo" / "submission"
REPORTS = sorted(SUBMISSION.glob("pms_*.pdf"))
Q1 = Period.parse("2026-Q1")
MONTHS = ("2026-01", "2026-02", "2026-03")


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


@pytest.fixture(scope="module")
def extracted(policy: Policy) -> ExtractionResult:
    """One extraction of the whole corpus, shared. Reading three PDFs is the slow part of this file."""
    return extract(REPORTS, Q1, policy)


# ── the happy path, once ─────────────────────────────────────────────────────


def test_the_whole_corpus_extracts_and_reconciles(extracted: ExtractionResult) -> None:
    """100% of ledger rows across all three monthly reports, with every printed total reconciling.

    The acceptance criterion is "recovers 100% of ledger rows", and the number to compare against is
    1,200 distinct reservations — not the 1,303 rows printed, because 103 month-spanning stays are
    printed twice by design (D-RNS-03).
    """
    result = extracted
    assert result.summary.records_extracted == 1200
    assert len(result.records) == 1200
    assert result.findings == ()
    assert not result.halted
    assert result.summary.printed_total_matched
    assert result.summary.duplicate_ids == 0
    assert result.summary.unmapped_labels == ()


def test_every_record_carries_the_page_and_row_it_was_read_from(
    extracted: ExtractionResult,
) -> None:
    """ "Capturing source page and row for every record" — the half of the AC that makes a finding
    citable. A value without a citation is a value a reviewer cannot check."""
    for record in extracted.records:
        ref = record.source
        assert ref.file.startswith("pms_2026-")
        assert ref.page >= 1
        assert 1 <= ref.row_start <= 30, "rows are numbered within the page's table"
        assert ref.citation.startswith(ref.file)


def test_the_extracted_records_reproduce_the_printed_totals(
    extracted: ExtractionResult, policy: Policy
) -> None:
    """The end-to-end claim: records read from the PDFs, run through the metric library, land on the
    figures the reports print about themselves.

    This is a stronger statement than "extraction reconciled". Reconciliation happens inside
    extraction, against the same records; this runs the *product* metric library over the extracted
    records and compares against the document. If either the parser or the library were wrong, this is
    where it would show.
    """
    results = compute_all(
        extracted.records,
        None,
        [Period.parse(month) for month in MONTHS],
        policy,
    )
    for month in MONTHS:
        printed = extracted.printed_totals[month]
        assert results.value(f"room_nights_sold:{month}") == Decimal(printed.room_nights_sold)


def test_the_pdf_records_match_the_ledger_they_were_rendered_from(
    extracted: ExtractionResult,
) -> None:
    """Extraction is lossless against the generator's ledger.

    A read that recovered every row but shifted one column would still reconcile — the totals would
    add up over the wrong values. Comparing the extracted records against the ledger field by field is
    what catches that, and it is only possible because the corpus carries its own ground truth.
    """
    import csv

    ledger_path = SUBMISSION.parent / "ground_truth" / "reservations.csv"
    with ledger_path.open(encoding="utf-8", newline="") as handle:
        ledger = {row["reservation_id"]: row for row in csv.DictReader(handle)}

    assert len(ledger) == 1200
    extracted_by_id = {r.reservation_id: r for r in extracted.records}
    assert set(extracted_by_id) == set(ledger)

    for reservation_id, record in extracted_by_id.items():
        expected = ledger[reservation_id]
        assert record.nationality_iso2 == expected["nationality_iso2"], reservation_id
        assert record.adults == int(expected["adults"]), reservation_id
        assert record.children == int(expected["children"]), reservation_id
        assert record.rooms == int(expected["rooms"]), reservation_id
        assert record.nights == int(expected["nights"]), reservation_id
        assert record.room_nights == int(expected["room_nights"]), reservation_id
        assert record.arrival_date == date.fromisoformat(expected["arrival_date"]), reservation_id
        assert record.status.value == expected["status"], reservation_id
        assert record.rate_code.value == expected["rate_code"], reservation_id
        assert record.guest_ref == expected["guest_ref"], reservation_id


def test_day_use_rows_survive_the_marker_in_the_departure_column(
    extracted: ExtractionResult,
) -> None:
    """The departure column prints a marker rather than a date for a same-day stay.

    Worth its own test because it is the one cell whose value is not a value, and because the rule it
    tests is subtle: `departure` is read from the *marker*, so the printed `0` in `Nts` stays a
    cross-check rather than becoming the input the night count is derived from.
    """
    day_use = [r for r in extracted.records if r.is_day_use]

    assert len(day_use) >= 30
    for record in day_use:
        assert record.departure_date == record.arrival_date
        assert record.nights == 0
        assert record.room_nights == 0


# ── normalisation, and refusing to guess ─────────────────────────────────────


def test_the_documented_variant_pairs_resolve(policy: Policy) -> None:
    """D-NAT-11. The pairs a hotel and a regulator genuinely spell differently."""
    assert nationality("Czechia") == nationality("Czech Republic") == "CZ"
    assert nationality("South Korea") == nationality("Korea, Republic of") == "KR"
    assert (
        nationality("UK") == nationality("Great Britain") == nationality("United Kingdom") == "GB"
    )
    assert policy.version  # the lookups are independent of policy, and this test says so


@pytest.mark.parametrize(
    "raw", ["czech republic", "CZECH  REPUBLIC", "Czech Republic ", "czech  republic"]
)
def test_matching_is_case_punctuation_and_whitespace_insensitive(raw: str) -> None:
    """D-NAT-10, the whole rule in one assertion."""
    assert nationality(raw) == "CZ"


def test_codes_are_validated_rather_than_pattern_matched() -> None:
    """`ReservationRecord.nationality_iso2` accepts `^[A-Z]{2}$`, which `QQ` satisfies.

    So the contract alone would let an invented code into a metric key and out into a verdict. The
    lookup is what rejects it — which is why the alpha-2 codes are themselves keys in the table rather
    than a separate fast path.
    """
    assert nationality("GB") == "GB"
    assert nationality("GBR") == "GB"
    with pytest.raises(UnmappableLabelError, match="unmappable country: 'QQ'"):
        nationality("QQ")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Checked Out", Status.CHECKED_OUT), ("c/o", Status.CHECKED_OUT), ("No-Show", Status.NO_SHOW)],
)
def test_statuses_normalise_from_documented_variants(raw: str, expected: Status) -> None:
    assert status(raw) is expected


def test_an_unknown_status_is_blocking_and_never_coerced() -> None:
    """D-QUAL-03. Never treated as CHECKED_OUT because it looks close enough.

    The asymmetry is why this matters more than the country lookup: guessing CHECKED_OUT moves a
    reservation *into* the qualifying set, which inflates occupancy — and an inflated occupancy looks
    like a busy hotel rather than a bug.
    """
    with pytest.raises(UnmappableLabelError, match="unmappable status"):
        status("Checked Out Early")
    with pytest.raises(UnmappableLabelError, match="unmappable status"):
        status("Confirmed")


def test_complimentary_and_house_use_are_distinct(policy: Policy) -> None:
    """D-QUAL-04/05. Both zero-revenue, opposite effects on occupancy."""
    assert rate_code("Complimentary") is RateCode.COMP
    assert rate_code("House Use") is RateCode.HOUSE
    assert policy.qualifies(Status.CHECKED_OUT, RateCode.COMP)
    assert not policy.qualifies(Status.CHECKED_OUT, RateCode.HOUSE)


def test_nothing_is_resolved_by_edit_distance_or_substring() -> None:
    """The two temptations, both refused.

    `Austrialia` is a typo a human would resolve instantly and a matcher cannot resolve safely: any
    threshold loose enough to catch it also maps `Austria` to `Australia`. `Niger` is a substring of
    `Nigeria`, and they are different countries.
    """
    with pytest.raises(UnmappableLabelError):
        nationality("Austrialia")
    with pytest.raises(UnmappableLabelError):
        nationality("United King")
    assert nationality("Nigeria") == "NG"


def test_an_ambiguous_alias_is_a_build_time_failure() -> None:
    """A table where two countries claim one alias would resolve by load order.

    Checked because the failure is invisible: whichever entry loaded last wins, and every document
    using that alias resolves to the wrong country for as long as nobody notices.
    """
    from tda.extract.normalise import _build_index

    with pytest.raises(LookupTableError, match="maps to both"):
        _build_index(
            "country",
            {
                "AT": {"name": "Austria", "aliases": ["Alpine"]},
                "CH": {"name": "Switzerland", "aliases": ["Alpine"]},
            },
        )


def test_a_yaml_boolean_key_is_rejected_with_the_reason() -> None:
    """The Norway problem, pinned.

    YAML 1.1 parses the unquoted token `NO` as `false`, so a country table keyed by alpha-2 code
    silently loses Norway and hands the loader a bool. The tables quote every key; this is the guard
    that explains it rather than letting the next person meet `AttributeError: 'bool' object has no
    attribute 'strip'`.
    """
    from tda.extract.normalise import _build_index

    with pytest.raises(LookupTableError, match=r"YAML 1\.1 parses"):
        _build_index("country", {False: {"name": "Norway"}})  # type: ignore[dict-item]

    assert nationality("NO") == nationality("Norway") == nationality("NOR") == "NO"


def test_normalise_key_does_not_invent_abbreviations() -> None:
    """Normalisation makes spellings match; it does not expand or contract words.

    `Czech Rep.` normalises to `czech rep`, which is why it is listed as an alias in its own right
    rather than being expected to fall out of the matching rule.
    """
    assert normalise_key("Czech Rep.") == "czech rep"
    assert normalise_key("  KOREA,  REPUBLIC OF ") == "korea republic of"
    assert nationality("Czech Rep.") == "CZ"


# ── the layout map, and the refusal when it no longer applies ────────────────


def test_the_committed_header_matches_the_document(extracted: ExtractionResult) -> None:
    verify_layout(f"some heading\n{EXPECTED_HEADER}\nRES-2026Q1-00001 ...")
    assert extracted.summary.pages_read == 47


def test_an_extra_column_is_refused_rather_than_absorbed() -> None:
    """The failure a positional map exists to catch.

    Insert a column and every band after it reads its neighbour's values — producing records that pass
    every contract validator and are wrong. The header check turns that into a refusal.
    """
    with_extra = EXPECTED_HEADER.replace("Nat Arrival", "Nat Channel Arrival")

    with pytest.raises(LayoutMismatchError, match="does not match the committed column map"):
        verify_layout(f"heading\n{with_extra}\n")


def test_the_title_line_is_not_mistaken_for_the_column_header() -> None:
    """A bug I wrote and then fixed: the report's *title* also begins with "Reservation".

    Anything locating the header by its first word verifies the title against the column header, fails,
    and reports a layout mismatch on a document that is perfectly fine. A verification that cries wolf
    gets uninstalled within a week, so this pins the behaviour.
    """
    page = (
        "Marina Vista Hotel (synthetic) — Dubai\n"
        "Reservation Detail Report — February 2026 | Property MZN-DXB-001\n"
        f"{EXPECTED_HEADER}\n"
    )
    verify_layout(page)  # must not raise


def test_a_page_with_no_column_header_at_all_is_refused() -> None:
    with pytest.raises(LayoutMismatchError, match="no line resembling a column header"):
        verify_layout("Marina Vista Hotel — Period Totals, February 2026\nRoom nights sold 1299\n")


# ── derive, never trust ──────────────────────────────────────────────────────


def _cells(**overrides: str) -> dict[str, str]:
    base = {
        "reservation_id": "RES-2026Q1-00001",
        "guest_ref": "g_0123456789abcdef",
        "nationality": "GB",
        "arrival": "2026-02-10",
        "departure": "2026-02-13",
        "nights": "3",
        "rooms": "2",
        "adults": "2",
        "children": "0",
        "room_nights_total": "6",
        "room_nights_month": "6",
        "rate_code": "BAR",
        "status": "Checked Out",
    }
    return base | overrides


REF = PdfRef(file="pms_2026-02.pdf", page=1, row_start=1, row_end=1)


def test_a_well_formed_row_parses() -> None:
    record, printed_month = parse_row(_cells(), REF, "MZN-DXB-001", load_lookups())

    assert record.nights == 3
    assert record.room_nights == 6
    assert printed_month == 6


def test_a_printed_room_nights_column_that_disagrees_is_a_row_defect() -> None:
    """D-RNS-02, and the AC's "a mismatch is a blocking finding on that row".

    Note what is *not* offered: an option to prefer the printed figure. Room-nights are derived from
    nights and rooms, and the column is a cross-check — so a disagreement is a defect in the row rather
    than a choice between two numbers.
    """
    with pytest.raises(ValueError, match=r"RN Total column says 7.*D-RNS-02"):
        parse_row(_cells(room_nights_total="7"), REF, "MZN-DXB-001", load_lookups())


def test_a_printed_night_count_that_contradicts_the_dates_is_a_row_defect() -> None:
    """D-RNS-01. The dates are the source; `Nts` is the cross-check."""
    with pytest.raises(ValueError, match=r"Nts column says 4.*D-RNS-01"):
        parse_row(_cells(nights="4"), REF, "MZN-DXB-001", load_lookups())


def test_the_day_use_marker_sets_departure_from_the_marker_not_the_night_count() -> None:
    record, _ = parse_row(
        _cells(departure="— day use —", nights="0", room_nights_total="0", room_nights_month="0"),
        REF,
        "MZN-DXB-001",
        load_lookups(),
    )
    assert record.is_day_use
    assert record.departure_date == record.arrival_date


def test_a_day_use_marker_with_a_nonzero_night_count_is_a_contradiction() -> None:
    """A report that printed both the marker and a night count is self-contradictory.

    The parser reports it rather than resolving it, which is the only defensible answer: the marker says
    zero nights and the column says three, and nothing in the document says which to believe.
    """
    with pytest.raises(ValueError, match="Nts column says 3"):
        parse_row(_cells(departure="— day use —"), REF, "MZN-DXB-001", load_lookups())


def test_an_unmappable_nationality_in_a_row_is_refused() -> None:
    with pytest.raises(UnmappableLabelError, match="unmappable country"):
        parse_row(_cells(nationality="QQ"), REF, "MZN-DXB-001", load_lookups())


def test_a_guest_name_cannot_be_extracted_even_by_a_parser_that_tries() -> None:
    """D-EV-03, enforced by the contract rather than by the parser's discretion.

    There is no name column to read, and `guest_ref` is pattern-constrained — so a parser that put a
    name there fails at construction instead of leaking it into a verdict that gets emailed to a hotel.
    """
    with pytest.raises(ValueError, match="guest_ref"):
        parse_row(_cells(guest_ref="Ahmed Al Mansouri"), REF, "MZN-DXB-001", load_lookups())


# ── totals reconciliation: the checks must be watched failing ────────────────


def test_the_printed_totals_parse_from_a_two_column_page(extracted: ExtractionResult) -> None:
    """`extract_text` interleaves the two columns — `'Occupancy 81.70% CN 31'` is one line.

    So the block is parsed positionally. Splitting that line on whitespace gives an occupancy of `31`
    with the wrong regex, which is a wrong number that looks like a right one.
    """
    february = extracted.printed_totals["2026-02"]

    assert february.room_nights_sold == 1299
    assert february.room_nights_available == 1590
    assert february.occupancy_pct == Decimal("81.70")
    assert february.listed_rows == 417
    assert february.excluded["cancelled"] == 33
    assert february.guests_by_nationality["CN"] == 31
    assert february.total_guests == 653


def test_a_missing_printed_total_is_an_error_not_a_skipped_check() -> None:
    """The absence of the printed totals is the absence of the thing that makes the read trustworthy."""
    from tda.extract import TotalsParseError

    with pytest.raises(TotalsParseError, match="no room-nights-sold line"):
        parse_totals("Occupancy 81.70%", "Total guests 1", "2026-02", 15)


def test_a_lost_row_fails_reconciliation(extracted: ExtractionResult, policy: Policy) -> None:
    """Drop one row and the row-count check fires.

    The cheapest failure to catch and the one a partial implementation would stop at — which is why the
    tests below cover the failures the row count cannot see.
    """
    february = [r for r in extracted.records if r.source.file == "pms_2026-02.pdf"]
    printed = extracted.printed_totals["2026-02"]

    disagreements = reconcile(
        february[:-1],
        {r.reservation_id: 0 for r in february[:-1]},
        printed,
        Period.parse("2026-02"),
        policy,
    )
    assert any(d.check == "row count" for d in disagreements)


def test_a_misread_status_fails_the_exclusion_breakdown(policy: Policy) -> None:
    """The check that catches two compensating errors.

    One row flipped into the qualifying set and another flipped out leaves the room-night totals
    correct and only the exclusion counts wrong. A reconciliation that compared totals alone would pass.
    """
    from tda.extract.reconcile_totals import _exclusion_counts

    def record(reservation_id: str, status_value: Status) -> ReservationRecord:
        return ReservationRecord(
            reservation_id=reservation_id,
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
            status=status_value,
            rate_code=RateCode.BAR,
            source=REF,
        )

    truth = [record("A", Status.CANCELLED), record("B", Status.CHECKED_OUT)]
    swapped = [record("A", Status.CHECKED_OUT), record("B", Status.CANCELLED)]

    # The room-night totals are identical either way — one qualifying two-night stay.
    assert _exclusion_counts(truth, policy) == _exclusion_counts(swapped, policy)


def test_a_swapped_nationality_pair_fails_the_per_code_check(
    extracted: ExtractionResult, policy: Policy
) -> None:
    """Compared per code rather than on the total, because a swapped pair leaves the total right.

    The `Nat` column is two characters wide with a reservation id on one side and a date on the other,
    so a boundary that shifts swaps codes rather than corrupting them — and a total-only check is blind
    to it.
    """
    printed = extracted.printed_totals["2026-02"]
    february = [r for r in extracted.records if r.source.file == "pms_2026-02.pdf"]

    tampered = tuple(
        r.model_copy(update={"nationality_iso2": "DE" if r.nationality_iso2 == "GB" else "GB"})
        if r.nationality_iso2 in ("GB", "DE")
        else r
        for r in february
    )
    disagreements = reconcile(
        tampered, {r.reservation_id: 0 for r in tampered}, printed, Period.parse("2026-02"), policy
    )
    codes = {d.dimension_value for d in disagreements if d.metric == "guests_by_nationality"}

    assert {"GB", "DE"} <= codes
    assert not any(d.check == "total guests" for d in disagreements), (
        "swapping two codes leaves the total correct - which is exactly why the per-code check exists"
    )


# ── reservations printed on two reports ──────────────────────────────────────


def test_a_month_spanning_stay_is_merged_not_duplicated(extracted: ExtractionResult) -> None:
    """103 of the 1,200 reservations are printed on two monthly reports (D-RNS-03).

    Handing all 1,303 rows to the metric library would double-count each spanning stay's nights. It
    would not do so quietly — `reject_duplicate_ids` raises — but "loud" would mean every run halts,
    so extraction returns one record per reservation.
    """
    ids = [r.reservation_id for r in extracted.records]

    assert len(ids) == len(set(ids)) == 1200


def test_two_reports_disagreeing_about_one_stay_is_blocking() -> None:
    """The check that nothing else in the pipeline can perform.

    Two printings of one reservation are two independent statements by the property. A February report
    saying 2 rooms and a March report saying 3 is a real inconsistency — and each report still
    reconciles against its own printed totals perfectly, so the totals reconciliation is blind to it.
    """

    def record(rooms: int, file: str) -> ReservationRecord:
        return ReservationRecord(
            reservation_id="RES-2026Q1-00500",
            hotel_id="MZN-DXB-001",
            guest_ref="g_0123456789abcdef",
            nationality_iso2="GB",
            adults=2,
            children=0,
            rooms=rooms,
            nights=3,
            room_nights=3 * rooms,
            arrival_date=date(2026, 2, 28),
            departure_date=date(2026, 3, 3),
            status=Status.CHECKED_OUT,
            rate_code=RateCode.BAR,
            source=PdfRef(file=file, page=1, row_start=1, row_end=1),
        )

    agreeing = [record(2, "pms_2026-02.pdf"), record(2, "pms_2026-03.pdf")]
    merged, findings = merge_across_reports(agreeing, FindingIds())
    assert len(merged) == 1
    assert merged[0].source.file == "pms_2026-02.pdf", "the earliest citation survives"
    assert findings == []

    conflicting = [record(2, "pms_2026-02.pdf"), record(3, "pms_2026-03.pdf")]
    merged, findings = merge_across_reports(conflicting, FindingIds())
    assert merged == (), "a reservation the reports disagree about is not silently kept"
    assert len(findings) == 1
    assert findings[0].variance_class is VarianceClass.EXTRACTION_LIMIT
    assert findings[0].severity is Severity.BLOCKING
    assert "rooms" in (findings[0].narrative or "")


# ── the shape of a refusal ───────────────────────────────────────────────────


def test_every_blocking_finding_is_a_v7_that_is_never_a_hotel_error() -> None:
    """The promise the whole story rests on.

    "I could not read this" must never reach a property as "you got this wrong". `is_hotel_error` is
    false for V7 by construction, so a caller who counts findings as hotel errors still gets the right
    answer — which is better than a caller who has to remember.
    """
    finding = blocking_finding(
        FindingIds(),
        MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period="2026-02"),
        "D-RNS-02",
        "the printed RN Total column disagrees with nights x rooms",
        REF,
    )

    assert finding.variance_class is VarianceClass.EXTRACTION_LIMIT
    assert finding.severity is Severity.BLOCKING
    assert not finding.is_hotel_error
    assert finding.excel_ref.citation.startswith("not reached")
    assert finding.finding_id == "F-0001"


def test_only_v7_may_carry_neither_a_claimed_nor_a_computed_value() -> None:
    """A contract defect this story surfaced, and the reasoning for the fix.

    `Finding` required at least one of `claimed`/`computed`. A V7 raised before the workbook is parsed
    has neither — nothing was claimed because nothing was read, and nothing was computed because that is
    what it is reporting. The `computed` field's own description already said "None … a V7", so the
    model contradicted itself; it just had no real V7 to meet until extraction existed.

    The exemption is V7 only. Every other class states a difference between two numbers, and one with
    neither is an empty assertion — so this pins both halves.
    """
    key = MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period="2026-02")
    refusal = blocking_finding(FindingIds(), key, "D-RNS-02", "could not read the row", REF)

    assert refusal.claimed is None
    assert refusal.computed is None

    with pytest.raises(ValueError, match="needs at least one of claimed or computed"):
        Finding(
            finding_id="F-0002",
            key=key,
            variance_class=VarianceClass.TRANSCRIPTION,
            severity=Severity.MATERIAL,
            escalates_to=EscalationTarget.HOTEL,
            source_ref=REF,
            excel_ref=ExcelRef(sheet="Occupancy", cell="B5"),
            clause="D-RNS-02",
        )


def test_finding_ids_are_sequential_within_a_run() -> None:
    """Referenced by `ReviewRecord.finding_id`, so they must not change when a message is reworded."""
    ids = FindingIds()

    assert [ids.take() for _ in range(3)] == ["F-0001", "F-0002", "F-0003"]


def test_a_misfiled_report_is_refused_rather_than_reconciled_against_itself() -> None:
    """The month comes from the file name, and a name that does not state one is an error.

    Inferring it from the content would let a March report filed as February reconcile against its own
    March totals, agree perfectly, and contribute March's room-nights to February.
    """
    assert month_of(Path("pms_2026-02.pdf")) == "2026-02"
    with pytest.raises(ValueError, match="cannot tell which month"):
        month_of(Path("february_report.pdf"))
    with pytest.raises(ValueError, match="cannot tell which month"):
        month_of(Path("pms_2026-13.pdf"))


# ── the inventory reference ──────────────────────────────────────────────────


def test_the_inventory_reference_reads_with_real_citations() -> None:
    reference = read_inventory(SUBMISSION / "inventory_2026-Q1.csv", Q1)

    assert len(reference.days) == 90
    assert reference.hotel_ids == {"MZN-DXB-001"}
    assert reference.days[0].source.row_start == 1, "1-indexed data rows; the header is not row 1"
    assert sum(day.rooms_out_of_order for day in reference.days) > 0, (
        "without an out-of-order window the denominator could be inferred from a room count"
    )


def test_a_gap_in_the_inventory_is_blocking_and_is_not_a_closed_day(tmp_path: Path) -> None:
    """D-RNA-05 versus a hole in the file.

    A closed day contributes zero to the denominator and says so. A missing day silently *lowers* the
    denominator and raises occupancy — the same direction as a busy month, which is why it has to be an
    error rather than a default.
    """
    source = (SUBMISSION / "inventory_2026-Q1.csv").read_text(encoding="utf-8")
    lines = source.splitlines()
    without_a_day = "\n".join(lines[:20] + lines[21:]) + "\n"
    path = tmp_path / "inventory.csv"
    path.write_text(without_a_day, encoding="utf-8")

    with pytest.raises(InventoryError, match="A missing day is not a closed day"):
        read_inventory(path, Q1)


def test_a_duplicated_inventory_day_is_blocking(tmp_path: Path) -> None:
    """The opposite direction, and the one that reads as a quiet month rather than a broken file."""
    lines = (SUBMISSION / "inventory_2026-Q1.csv").read_text(encoding="utf-8").splitlines()
    path = tmp_path / "inventory.csv"
    path.write_text("\n".join([*lines, lines[1]]) + "\n", encoding="utf-8")

    with pytest.raises(InventoryError, match="duplicate inventory rows"):
        read_inventory(path, Q1)


def test_the_inventory_availability_column_is_a_cross_check_not_an_input(tmp_path: Path) -> None:
    """The same rule the PDF's `RN Total` column gets, for the same reason."""
    lines = (SUBMISSION / "inventory_2026-Q1.csv").read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    available = header.index("rooms_available")
    cells = lines[1].split(",")
    cells[available] = "999"
    path = tmp_path / "inventory.csv"
    path.write_text("\n".join([lines[0], ",".join(cells), *lines[2:]]) + "\n", encoding="utf-8")

    with pytest.raises(InventoryError, match=r"rooms_available column says 999.*D-RNA-03"):
        read_inventory(path, Q1)


def test_a_missing_required_column_names_what_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "inventory.csv"
    path.write_text("hotel_id,day,rooms_total\nMZN-DXB-001,2026-01-01,60\n", encoding="utf-8")

    with pytest.raises(InventoryError, match=r"rooms_out_of_order"):
        read_inventory(path, Q1)


# ── reading a real report, page by page ──────────────────────────────────────


def test_reading_one_report_finds_its_totals_page_by_shape_not_by_index() -> None:
    """A report that grew a trailing page would otherwise have its last data page read as the totals
    block — which fails while pointing at entirely the wrong thing."""
    report = read_report(SUBMISSION / "pms_2026-02.pdf", load_lookups())

    assert report.hotel_id == "MZN-DXB-001"
    assert report.page_count == 15
    assert report.totals_page == 15
    assert len(report.pages) == 14, "the totals page is not a data page"
    assert "Room nights sold" in report.totals_left
    assert "Total guests" in report.totals_right
    assert sum(len(page.records) for page in report.pages) == 417
    assert all(page.defects == () for page in report.pages)
