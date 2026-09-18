"""The Excel claim parser, and the guarantees it is supposed to make.

Written the way the rest of this repository's tests are written: the interesting cases are the ones
where something is supposed to be **refused**, and a refusal nobody has watched happen is a comment.

So the tests here fall into three groups.

**The tool surface cannot emit a value.** Not "does not in the cases we thought of" — the test
builds a workbook whose every figure is a distinctive nine-digit number and asserts that none of
them appears anywhere in what the agent is given, including the text-stored and formula forms that a
type-based filter would wave through.

**Every check fires on bad input and stays quiet on good input.** Both halves matter equally. A
check that fires on the real corpus is a check somebody switches off within a week, so each one is
asserted against the committed workbook too — which is correct, and must stay reported as correct.

**The parser reproduces the ground truth.** 94 claims, keyed identically to `truth_metrics.json`,
with values that match exactly. That is the end-to-end statement the story is worth making.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from tda.contracts import (
    Claim,
    ExcelRef,
    Metric,
    NotReached,
    Period,
    Severity,
    VarianceClass,
)
from tda.excel import (
    REDACTED_FORMULA,
    REDACTED_VALUE,
    CoverSheet,
    Disposition,
    MetricBlock,
    Orientation,
    RangeError,
    Rect,
    ResolvedBlock,
    UnmappedBlock,
    WorkbookMapping,
    check_cover,
    check_dimension_totals,
    check_occupancy_consistency,
    check_period_rollups,
    digest,
    list_sheets,
    looks_numeric,
    open_submission,
    parse_claims,
    parse_period_label,
    parse_value,
    peek_headers,
    read_block,
    render_cell,
    resolve,
    unaccounted_sheets,
    uncovered_values,
)
from tda.excel.read import CellValueError, StatedTotal
from tda.extract.normalise import Lookups, load_lookups
from tda.policy import Policy, load_policy

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "demo"
SUBMISSION = CORPUS / "submission" / "claims_2026-Q1.xlsx"
TRUTH = CORPUS / "ground_truth" / "truth_metrics.json"


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


@pytest.fixture(scope="module")
def lookups() -> Lookups:
    return load_lookups()


@pytest.fixture
def submission() -> tuple[Workbook, Workbook]:
    values, formulas = open_submission(SUBMISSION)
    return values, formulas


def _sheet(workbook: Workbook, title: str) -> Worksheet:
    """The active sheet, narrowed. `Workbook.active` is optional in openpyxl's stubs."""
    sheet = workbook.active
    assert isinstance(sheet, Worksheet)
    sheet.title = title
    return sheet


def _problem(outcome: ResolvedBlock) -> str:
    """The reason a block needs a human, asserted present.

    A `NEEDS_HUMAN_MAPPING` outcome without a reason would be a bug in its own right - a flag with
    no explanation is exactly what `Abstention.reason` exists to prevent elsewhere.
    """
    assert outcome.problem is not None
    return outcome.problem


def _occupancy_blocks() -> list[MetricBlock]:
    return [
        MetricBlock(
            sheet="Occupancy",
            metric=metric,
            orientation=Orientation.PERIODS_DOWN_ROWS,
            value_range=column,
            period_label_range="A5:A8",
        )
        for metric, column in (
            ("room_nights_sold", "B5:B8"),
            ("room_nights_available", "C5:C8"),
            ("occupancy_pct", "D5:D8"),
        )
    ]


def _nationality_block() -> MetricBlock:
    return MetricBlock(
        sheet="Nationality",
        metric="guests_by_nationality",
        orientation=Orientation.PERIODS_ACROSS_COLUMNS,
        value_range="B5:E25",
        period_label_range="B4:E4",
        dimension="nationality_iso2",
        dimension_label_range="A5:A25",
        dimension_total_range="B26:E26",
    )


def _rate_blocks() -> list[MetricBlock]:
    return [
        MetricBlock(
            sheet="Rate & Revenue",
            metric=metric,
            orientation=Orientation.PERIODS_ACROSS_COLUMNS,
            value_range=row,
            period_label_range="B4:D4",
        )
        for metric, row in (
            ("average_daily_rate", "B5:D5"),
            ("revpar", "B6:D6"),
            ("average_length_of_stay", "B7:D7"),
        )
    ]


def correct_mapping() -> WorkbookMapping:
    """The mapping a competent agent returns for the committed workbook."""
    return WorkbookMapping(
        cover=CoverSheet(sheet="Summary", property_code_cell="B5", period_cell="B6"),
        blocks=(*_occupancy_blocks(), _nationality_block(), *_rate_blocks()),
    )


# ══ the tool surface cannot emit a value ═════════════════════════════════════


def test_no_value_reaches_the_agent_whatever_its_storage_type() -> None:
    """The guarantee, watched.

    Every figure is a distinctive nine-digit number so that a match cannot be a coincidence with a
    row number, a year or a timestamp. The four forms are the four a real workbook uses, and the
    two in the middle are the ones a `isinstance(value, str)` filter would hand straight over.
    """
    workbook = Workbook()
    sheet = _sheet(workbook, "Figures")
    sheet["A1"] = "Month"
    sheet["B1"] = "Room Nights Sold"
    secrets: dict[str, str | int] = {
        "B2": 918273645,  # a number
        "B3": "645918273",  # a number stored as text
        "B4": "817263.45%",  # a percentage stored as text
        "B5": "=273645918",  # a literal wearing a formula's clothes
        "B6": "AED 546372819",  # a number wearing a currency's clothes
    }
    for index, (cell, value) in enumerate(secrets.items(), start=2):
        sheet[f"A{index}"] = f"Label {index}"
        sheet[cell] = value

    rendered = digest(workbook)

    for cell, value in secrets.items():
        bare = str(value).lstrip("=").removesuffix("%").replace("AED ", "")
        assert bare not in rendered, f"{cell} leaked {bare!r} into the agent's view"

    # …and the labels it legitimately needs did survive.
    assert "Room Nights Sold" in rendered
    assert "Label 2" in rendered


def test_a_percentage_label_survives_but_a_percentage_value_does_not() -> None:
    """The distinction a type-based filter gets exactly backwards."""
    assert looks_numeric("81.70%") is True
    assert looks_numeric("Occupancy %") is False
    assert render_cell("Occupancy %") == "Occupancy %"
    assert render_cell("81.70%") == REDACTED_VALUE


@pytest.mark.parametrize(
    ("text", "numeric"),
    [
        ("January 2026", False),
        ("2026-Q1 total", False),
        ("Korea, Republic of", False),
        ("MZN-DXB-001", False),
        ("Average Daily Rate (AED)", False),
        ("Q1,2026", False),
        ("%", False),
        ("2026", True),  # deliberately the safe side - see the module docstring in tools.py
        ("1,285", True),
        ("(44)", True),
        ("AED 612.40", True),
        ("3.44", True),
    ],
)
def test_label_and_value_classification(text: str, numeric: bool) -> None:
    assert looks_numeric(text) is numeric


def test_a_formula_is_redacted_as_a_formula_not_as_a_value() -> None:
    """Distinct tokens, because a reader of a recorded cassette needs to tell them apart."""
    assert render_cell("=SUM(B5:B7)") == REDACTED_FORMULA
    assert render_cell(4140) == REDACTED_VALUE


def test_a_date_cell_is_a_value_not_a_label() -> None:
    """openpyxl returns dates as Python objects; they are ordinal numbers in the file."""
    from datetime import date

    assert render_cell(date(2026, 3, 31)) == REDACTED_VALUE


def test_peeking_does_not_change_the_workbook(
    submission: tuple[Workbook, Workbook],
) -> None:
    """Reading a workbook must not mutate it.

    `iter_rows` beyond the used range creates the cells it walks, which grows `dimensions`. An
    earlier version peeked a fixed 16 columns and made one sheet report two different used ranges in
    the same prompt.
    """
    values, _ = submission
    before = {sheet.name: sheet.used_range for sheet in list_sheets(values)}
    digest(values)
    after = {sheet.name: sheet.used_range for sheet in list_sheets(values)}
    assert before == after
    assert before["Summary"] == "A1:B11"


def test_the_peek_shows_the_whole_label_axis(submission: tuple[Workbook, Workbook]) -> None:
    """A range whose end the agent cannot see is a range it has to guess at."""
    values, _ = submission
    peek = peek_headers(values, "Nationality")
    labels = {cell.text for cell in peek.cells}
    assert "Australia" in labels  # the first country
    assert "United States" in labels  # the last one, far below any fixed row window
    assert "Total" in labels  # and the total row, which the mapping must exclude
    assert peek.labels_omitted == 0


# ══ geometry ═════════════════════════════════════════════════════════════════


def test_a_single_cell_is_a_one_by_one_rectangle() -> None:
    """So no caller has to branch on which form it was given."""
    rect = Rect.parse("D14")
    assert (rect.rows, rect.cols) == (1, 1)
    assert rect.rendered == "D14"


@pytest.mark.parametrize("text", ["$B$5", "Sheet1!B5", "B:E", "5B", "", "B0", "ABCD5"])
def test_a_malformed_range_is_refused(text: str) -> None:
    """Absolute markers are rejected rather than stripped: `$B$5` means something produced a
    formula reference, and quietly normalising it hides whatever that was."""
    with pytest.raises(RangeError):
        Rect.parse(text)


def test_range_round_trips() -> None:
    assert Rect.parse("b5:e25").rendered == "B5:E25"


# ══ the mapping is checked, never trusted ════════════════════════════════════


def test_an_invented_metric_name_never_becomes_a_metric(
    submission: tuple[Workbook, Workbook], policy: Policy
) -> None:
    """D-KEY-03, at the one place a model could influence which metric a number belongs to."""
    values, _ = submission
    block = MetricBlock(
        sheet="Occupancy",
        metric="room_nights_booked",  # plausible, and not a thing
        orientation=Orientation.PERIODS_DOWN_ROWS,
        value_range="B5:B8",
        period_label_range="A5:A8",
    )
    outcome = resolve(block, values, policy)
    assert outcome.disposition is Disposition.NEEDS_HUMAN_MAPPING
    assert "room_nights_booked" in _problem(outcome)
    assert "D-KEY-03" in _problem(outcome)


def test_an_out_of_scope_metric_is_recorded_not_refused(
    submission: tuple[Workbook, Workbook], policy: Policy
) -> None:
    """D-SCOPE-02. Silence would be mistaken for approval; a finding would be an accusation."""
    values, _ = submission
    outcome = resolve(_rate_blocks()[0], values, policy)
    assert outcome.disposition is Disposition.OUT_OF_SCOPE
    assert outcome.out_of_scope_name == "average_daily_rate"
    assert outcome.problem is None


def test_a_range_past_the_used_range_is_refused(
    submission: tuple[Workbook, Workbook], policy: Policy
) -> None:
    """Reading empty cells as figures is how a workbook grows claims it never made."""
    values, _ = submission
    block = MetricBlock(
        sheet="Occupancy",
        metric="room_nights_sold",
        orientation=Orientation.PERIODS_DOWN_ROWS,
        value_range="B5:B40",
        period_label_range="A5:A40",
    )
    outcome = resolve(block, values, policy)
    assert outcome.disposition is Disposition.NEEDS_HUMAN_MAPPING
    assert "past the used range" in _problem(outcome)


def test_a_label_count_mismatch_is_refused(
    submission: tuple[Workbook, Workbook], policy: Policy
) -> None:
    """The silent one: every value after the mismatch lands under the wrong period."""
    values, _ = submission
    block = MetricBlock(
        sheet="Occupancy",
        metric="room_nights_sold",
        orientation=Orientation.PERIODS_DOWN_ROWS,
        value_range="B5:B8",
        period_label_range="A5:A7",  # three labels, four values
    )
    outcome = resolve(block, values, policy)
    assert outcome.disposition is Disposition.NEEDS_HUMAN_MAPPING
    assert "period label" in _problem(outcome)


def test_a_dimensioned_metric_without_a_dimension_is_refused(
    submission: tuple[Workbook, Workbook], policy: Policy
) -> None:
    values, _ = submission
    block = MetricBlock(
        sheet="Nationality",
        metric="guests_by_nationality",
        orientation=Orientation.PERIODS_ACROSS_COLUMNS,
        value_range="B5:E25",
        period_label_range="B4:E4",
    )
    outcome = resolve(block, values, policy)
    assert outcome.disposition is Disposition.NEEDS_HUMAN_MAPPING
    assert "dimension" in _problem(outcome)


def test_a_dimension_on_an_undimensioned_metric_is_refused(
    submission: tuple[Workbook, Workbook], policy: Policy
) -> None:
    values, _ = submission
    block = MetricBlock(
        sheet="Occupancy",
        metric="room_nights_sold",
        orientation=Orientation.PERIODS_DOWN_ROWS,
        value_range="B5:B8",
        period_label_range="A5:A8",
        dimension="nationality_iso2",
        dimension_label_range="A5:A8",
    )
    outcome = resolve(block, values, policy)
    assert outcome.disposition is Disposition.NEEDS_HUMAN_MAPPING
    assert "takes no dimension" in _problem(outcome)


def test_a_sheet_nobody_mentioned_is_found_by_checking(
    submission: tuple[Workbook, Workbook],
) -> None:
    """Exhaustiveness is established by the code, not by the agent's diligence."""
    values, _ = submission
    partial = WorkbookMapping(blocks=tuple(_occupancy_blocks()))
    assert unaccounted_sheets(partial, values) == ("Summary", "Nationality", "Rate & Revenue")


def test_the_correct_mapping_leaves_no_sheet_unaccounted(
    submission: tuple[Workbook, Workbook],
) -> None:
    values, _ = submission
    assert unaccounted_sheets(correct_mapping(), values) == ()


def test_a_header_block_missed_inside_a_mapped_sheet_is_found(
    submission: tuple[Workbook, Workbook], policy: Policy
) -> None:
    """The case that actually happens, and the one sheet-level accounting misses entirely."""
    values, _ = submission
    blocks = (*_occupancy_blocks(), _nationality_block(), _rate_blocks()[0])
    resolved = [resolve(block, values, policy) for block in blocks]
    uncovered = uncovered_values(values, resolved)
    assert set(uncovered) == {"Rate & Revenue"}
    assert uncovered["Rate & Revenue"] == ("B6", "C6", "D6", "B7", "C7", "D7")


def test_the_correct_mapping_covers_every_figure(
    submission: tuple[Workbook, Workbook], policy: Policy
) -> None:
    """The other half: this must stay quiet on a workbook that is fully mapped."""
    values, _ = submission
    resolved = [resolve(block, values, policy) for block in correct_mapping().blocks]
    assert uncovered_values(values, resolved) == {}


# ══ stage two reads values, and refuses what it cannot read ══════════════════


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1285, Decimal("1285")),
        (81.7, Decimal("81.7")),
        ("1,285", Decimal("1285")),
        ("81.70%", Decimal("81.70")),
        ("(44)", Decimal("-44")),
        ("AED 612.40", Decimal("612.40")),
        ("  69.09  ", Decimal("69.09")),
    ],
)
def test_values_parse(raw: object, expected: str) -> None:
    value, _ = parse_value(raw)
    assert value == expected


def test_a_float_never_becomes_a_binary_approximation() -> None:
    """`Decimal(81.7)` is `81.70000000000000284...`, and a variance caused by our own
    representation error would be indefensible."""
    value, _ = parse_value(81.7)
    assert str(value) == "81.7"


def test_a_text_value_keeps_what_the_hotel_actually_typed() -> None:
    """`Claim.raw_text` exists so a reviewer sees the cell, not only our reading of it."""
    value, raw = parse_value("81.70%")
    assert value == Decimal("81.70")
    assert raw == "81.70%"


def test_a_numeric_cell_has_no_raw_text() -> None:
    _, raw = parse_value(1285)
    assert raw is None


def test_a_percentage_stored_as_a_fraction_is_scaled() -> None:
    """Excel's native percentage: `0.6909` under a `0.00%` format displays as `69.09%`.

    Reading it as `0.6909` would produce a claimed occupancy of 0.69% against a computed 69.09% —
    a 68-point false finding on a workbook that is entirely correct. The committed corpus stores
    plain numbers under a `0.00` format, which is exactly why this needs its own test.
    """
    value, _ = parse_value(0.6909, number_format="0.00%")
    assert value == Decimal("69.09")


def test_a_plain_number_is_not_scaled() -> None:
    value, _ = parse_value(69.09, number_format="0.00")
    assert value == Decimal("69.09")


@pytest.mark.parametrize("raw", [True, None, "", "not a number", "Q1"])
def test_a_cell_that_is_not_a_figure_is_refused(raw: object) -> None:
    """`True` is the interesting one: `bool` is a subclass of `int` and would otherwise read as 1."""
    with pytest.raises(CellValueError):
        parse_value(raw)


def test_an_uncached_formula_is_refused_by_name() -> None:
    with pytest.raises(CellValueError, match="Formulas are not evaluated"):
        parse_value("=SUM(B5:B7)")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("January 2026", "2026-01"),
        ("february 2026", "2026-02"),
        ("Mar 2026", "2026-03"),
        ("2026-Q1 total", "2026-Q1"),
        ("2026-Q1", "2026-Q1"),
        ("2026-01", "2026-01"),
        ("2026", "2026"),
        ("Total 2026-Q1", "2026-Q1"),
    ],
)
def test_period_labels_parse(label: str, expected: str) -> None:
    assert parse_period_label(label) == expected


@pytest.mark.parametrize("label", ["Smarch 2026", "Q1", "", "total", "the first quarter"])
def test_an_unrecognised_period_label_is_refused(label: str) -> None:
    """A claim under a guessed period is worse than no claim: it reconciles against a figure it
    has nothing to do with."""
    with pytest.raises(CellValueError):
        parse_period_label(label)


def test_an_uncached_formula_in_a_mapped_range_becomes_a_defect(
    policy: Policy, lookups: Lookups, tmp_path: Path
) -> None:
    """The refusal that would be unreachable with only one view of the workbook.

    In the `data_only=True` view an uncached formula and an empty cell are both `None`. Without the
    second view this figure would silently vanish from the output as though the hotel had left the
    cell blank.
    """
    path = tmp_path / "formula.xlsx"
    workbook = Workbook()
    sheet = _sheet(workbook, "Occupancy")
    sheet["A1"], sheet["B1"] = "Month", "Room Nights Sold"
    sheet["A2"], sheet["B2"] = "January 2026", 1285
    sheet["A3"], sheet["B3"] = "February 2026", "=B2+14"
    workbook.save(path)

    values, formulas = open_submission(path)
    block = MetricBlock(
        sheet="Occupancy",
        metric="room_nights_sold",
        orientation=Orientation.PERIODS_DOWN_ROWS,
        value_range="B2:B3",
        period_label_range="A2:A3",
    )
    reading = read_block(resolve(block, values, policy), values, lookups, formulas=formulas)
    assert len(reading.claims) == 1
    assert len(reading.defects) == 1
    assert "D-XLS-03" in reading.defects[0].reason
    assert reading.defects[0].citation == "Occupancy!B3"


def test_an_empty_cell_produces_no_claim_and_no_defect(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """D-XLS-02, and the real case it was written for.

    Iceland has three March guests and blank January and February cells, and `truth_metrics.json`
    has **no key at all** for those two months. A parser reading blanks as zeros would manufacture
    two claims the workbook never made.
    """
    values, formulas = submission
    reading = read_block(
        resolve(_nationality_block(), values, policy), values, lookups, formulas=formulas
    )
    assert reading.defects == ()
    iceland = {claim.key.rendered for claim in reading.claims if claim.key.value == "IS"}
    assert iceland == {
        "guests_by_nationality:2026-03:nationality_iso2=IS",
        "guests_by_nationality:2026-Q1:nationality_iso2=IS",
    }


def test_every_claim_carries_its_cell(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """There is no such thing as an uncited claim (D-XLS-01)."""
    values, formulas = submission
    reading = read_block(
        resolve(_nationality_block(), values, policy), values, lookups, formulas=formulas
    )
    citations = {claim.excel_ref.citation for claim in reading.claims}
    assert "Nationality!D14" in citations
    assert all(claim.excel_ref.sheet == "Nationality" for claim in reading.claims)


def test_a_country_label_that_cannot_be_resolved_is_never_guessed(
    policy: Policy, lookups: Lookups, tmp_path: Path
) -> None:
    """D-NAT-12. The closest match is exactly the kind of help that produces a confident wrong
    answer, and one wrong accusation costs more trust than a hundred correct findings earn."""
    path = tmp_path / "nationality.xlsx"
    workbook = Workbook()
    sheet = _sheet(workbook, "Nationality")
    sheet["A1"], sheet["B1"] = "Country", "January 2026"
    sheet["A2"], sheet["B2"] = "Germany", 83
    sheet["A3"], sheet["B3"] = "Freedonia", 11
    workbook.save(path)

    values, formulas = open_submission(path)
    block = MetricBlock(
        sheet="Nationality",
        metric="guests_by_nationality",
        orientation=Orientation.PERIODS_ACROSS_COLUMNS,
        value_range="B2:B3",
        period_label_range="B1:B1",
        dimension="nationality_iso2",
        dimension_label_range="A2:A3",
    )
    reading = read_block(resolve(block, values, policy), values, lookups, formulas=formulas)
    assert [claim.key.value for claim in reading.claims] == ["DE"]
    assert len(reading.defects) == 1
    assert reading.defects[0].citation == "Nationality!A3"


# ══ the workbook against itself ══════════════════════════════════════════════


def _claims_and_totals(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> tuple[list[Claim], list[StatedTotal]]:
    values, formulas = submission
    claims: list[Claim] = []
    totals: list[StatedTotal] = []
    for block in (*_occupancy_blocks(), _nationality_block()):
        reading = read_block(resolve(block, values, policy), values, lookups, formulas=formulas)
        claims.extend(reading.claims)
        totals.extend(reading.stated_totals)
    return claims, totals


def test_the_committed_workbook_is_self_consistent(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """The half that stops this being a check nobody trusts.

    Every one of these must stay silent on the real submission. A check that fires on correct input
    is a check somebody switches off, and then it is not there for the input that is wrong.
    """
    claims, totals = _claims_and_totals(submission, policy, lookups)
    assert check_dimension_totals(claims, totals) == []
    assert check_period_rollups(claims) == []
    assert check_occupancy_consistency(claims, policy) == []


def test_a_broken_dimension_total_is_caught(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    claims, totals = _claims_and_totals(submission, policy, lookups)
    january = next(
        total for total in totals if total.period == "2026-01" and total.metric.requires_dimension
    )
    tampered = [
        total
        if total is not january
        else type(january)(
            metric=january.metric,
            period=january.period,
            value=january.value + Decimal(3),
            ref=january.ref,
        )
        for total in totals
    ]
    found = check_dimension_totals(claims, tampered)
    assert len(found) == 1
    assert found[0].difference == Decimal(3)
    # A total *across* a dimension has no key the grammar can express, so it carries none rather
    # than filing an arithmetic error against a country picked at random.
    assert found[0].key is None
    assert found[0].excel_ref.citation == "Nationality!B26"


def test_a_broken_period_rollup_is_caught(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    claims, _ = _claims_and_totals(submission, policy, lookups)
    quarter = next(claim for claim in claims if claim.key.rendered == "room_nights_sold:2026-Q1")
    tampered = [
        claim if claim is not quarter else quarter.model_copy(update={"value": Decimal(4139)})
        for claim in claims
    ]
    found = check_period_rollups(tampered)
    assert len(found) == 1
    assert found[0].stated == Decimal(4139)
    assert found[0].implied == Decimal(4140)
    assert found[0].key is not None
    assert found[0].key.rendered == "room_nights_sold:2026-Q1"


def test_a_blank_month_hiding_an_omission_is_caught_by_the_arithmetic(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """The completeness case, detected without demanding a value in every cell.

    Iceland's quarter is raised to 10 while only its March figure (3) is claimed. Nothing about the
    blank cells changes; the arithmetic simply stops closing, and the blanks are where a reviewer
    then looks.
    """
    claims, _ = _claims_and_totals(submission, policy, lookups)
    key = "guests_by_nationality:2026-Q1:nationality_iso2=IS"
    tampered = [
        claim if claim.key.rendered != key else claim.model_copy(update={"value": Decimal(10)})
        for claim in claims
    ]
    found = [
        item
        for item in check_period_rollups(tampered)
        if item.key is not None and item.key.value == "IS"
    ]
    assert len(found) == 1
    assert (found[0].stated, found[0].implied) == (Decimal(10), Decimal(3))


def test_percentages_are_never_added_up(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """The quarter's occupancy is 78.05, not 234.72. A check that added them would fire on every
    correct workbook ever submitted."""
    claims, _ = _claims_and_totals(submission, policy, lookups)
    percentages = [claim for claim in claims if claim.key.metric is Metric.OCCUPANCY_PCT]
    assert len(percentages) == 4  # three months and the quarter, all present
    assert check_period_rollups(percentages) == []


def test_an_occupancy_that_disagrees_with_its_own_two_figures_is_caught(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    claims, _ = _claims_and_totals(submission, policy, lookups)
    tampered = [
        claim
        if claim.key.rendered != "occupancy_pct:2026-01"
        else claim.model_copy(update={"value": Decimal("72.50")})
        for claim in claims
    ]
    found = check_occupancy_consistency(tampered, policy)
    assert len(found) == 1
    assert found[0].stated == Decimal("72.50")
    assert found[0].implied == Decimal("69.09")


def test_a_rounding_convention_difference_is_within_tolerance(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """A hotel rounding half-even where we round half-up is not a clerical error, and this check
    is for clerical errors."""
    claims, _ = _claims_and_totals(submission, policy, lookups)
    tampered = [
        claim
        if claim.key.rendered != "occupancy_pct:2026-01"
        else claim.model_copy(update={"value": Decimal("69.08")})
        for claim in claims
    ]
    assert check_occupancy_consistency(tampered, policy) == []


def test_the_cover_sheet_identifies_the_right_submission(
    submission: tuple[Workbook, Workbook],
) -> None:
    values, _ = submission
    cover = CoverSheet(sheet="Summary", property_code_cell="B5", period_cell="B6")
    assert (
        check_cover(
            values,
            cover,
            expected_property="MZN-DXB-001",
            expected_period=Period.parse("2026-Q1"),
        )
        == []
    )


def test_a_workbook_for_the_wrong_quarter_is_caught(
    submission: tuple[Workbook, Workbook],
) -> None:
    """Every figure in it is wrong, and comparing them would produce a hundred findings describing
    one filing mistake."""
    values, _ = submission
    cover = CoverSheet(sheet="Summary", property_code_cell="B5", period_cell="B6")
    found = check_cover(
        values, cover, expected_property="MZN-DXB-001", expected_period=Period.parse("2026-Q2")
    )
    assert len(found) == 1
    assert found[0].field == "reporting period"
    assert found[0].stated == "2026-Q1"


def test_a_workbook_for_the_wrong_property_is_caught(
    submission: tuple[Workbook, Workbook],
) -> None:
    values, _ = submission
    cover = CoverSheet(sheet="Summary", property_code_cell="B5", period_cell="B6")
    found = check_cover(
        values, cover, expected_property="MZN-AUH-002", expected_period=Period.parse("2026-Q1")
    )
    assert len(found) == 1
    assert found[0].field == "property code"


# ══ end to end ═══════════════════════════════════════════════════════════════


def test_the_parser_reproduces_the_ground_truth_exactly(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """94 claims, keyed identically to `truth_metrics.json`, with values matching exactly.

    Including the two keys that are correctly *absent*: Iceland claims nothing in January or
    February, and neither does the truth.
    """
    values, formulas = submission
    result = parse_claims(
        values,
        correct_mapping(),
        Period.parse("2026-Q1"),
        policy,
        lookups=lookups,
        formulas=formulas,
        expected_property="MZN-DXB-001",
    )

    truth = json.loads(TRUTH.read_text())["metrics"]
    claimed = {claim.key.rendered: claim.value for claim in result.claims}

    assert set(claimed) == set(truth)
    assert all(claimed[key] == Decimal(str(value)) for key, value in truth.items())
    assert result.findings == ()
    assert result.defects == ()
    assert result.halted is False
    assert result.self_consistent is True


def test_out_of_scope_blocks_are_recorded_and_produce_no_claims(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    values, formulas = submission
    result = parse_claims(
        values,
        correct_mapping(),
        Period.parse("2026-Q1"),
        policy,
        lookups=lookups,
        formulas=formulas,
    )
    recorded = {item.metric for item in result.out_of_scope}
    assert recorded == {"average_daily_rate", "revpar", "average_length_of_stay"}
    assert not any(claim.key.metric.value in recorded for claim in result.claims)


def test_an_unmapped_sheet_halts_the_run_and_is_never_the_hotels_fault(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """D-XLS-06, and the thing that makes a refusal safe to report: a V7 can never be counted as a
    hotel error, even by a caller who tries."""
    values, formulas = submission
    partial = WorkbookMapping(blocks=tuple(_occupancy_blocks()))
    result = parse_claims(
        values, partial, Period.parse("2026-Q1"), policy, lookups=lookups, formulas=formulas
    )

    assert result.halted is True
    assert len(result.findings) >= 3
    for finding in result.findings:
        assert finding.variance_class is VarianceClass.EXTRACTION_LIMIT
        assert finding.severity is Severity.BLOCKING
        assert finding.is_hotel_error is False
        assert finding.clause == "D-XLS-06"


def test_a_workbook_only_finding_cites_the_cell_and_explains_the_missing_page(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """D-EV-05. A typed absence carrying its reason, never an invented page number."""
    values, formulas = submission
    partial = WorkbookMapping(blocks=tuple(_occupancy_blocks()))
    result = parse_claims(
        values, partial, Period.parse("2026-Q1"), policy, lookups=lookups, formulas=formulas
    )
    finding = result.findings[0]
    assert isinstance(finding.source_ref, NotReached)
    assert "could not be mapped" in finding.source_ref.reason
    assert isinstance(finding.excel_ref, ExcelRef)
    assert finding.excel_ref.sheet in {"Summary", "Nationality", "Rate & Revenue"}


def test_an_agent_declared_unmapped_block_is_reported(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """Said out loud by the agent, and still reported — the reason goes to the human."""
    values, formulas = submission
    mapping = correct_mapping().model_copy(
        update={
            "unmapped": (
                UnmappedBlock(
                    sheet="Rate & Revenue",
                    cells="A9:D12",
                    reason="a block of figures with no header I could interpret",
                ),
            )
        }
    )
    result = parse_claims(
        values, mapping, Period.parse("2026-Q1"), policy, lookups=lookups, formulas=formulas
    )
    assert result.halted is True
    assert any(
        f.narrative is not None and "no header I could interpret" in f.narrative
        for f in result.findings
    )


def test_a_bad_mapping_does_not_discard_the_good_blocks(
    submission: tuple[Workbook, Workbook], policy: Policy, lookups: Lookups
) -> None:
    """One unmappable block must not take down the parse of the other three."""
    values, formulas = submission
    broken = MetricBlock(
        sheet="Nationality",
        metric="guests_by_nationality",
        orientation=Orientation.PERIODS_ACROSS_COLUMNS,
        value_range="B5:E25",
        period_label_range="B4:D4",  # three labels against four columns
        dimension="nationality_iso2",
        dimension_label_range="A5:A25",
    )
    mapping = WorkbookMapping(
        cover=CoverSheet(sheet="Summary", property_code_cell="B5", period_cell="B6"),
        blocks=(*_occupancy_blocks(), broken, *_rate_blocks()),
    )
    result = parse_claims(
        values, mapping, Period.parse("2026-Q1"), policy, lookups=lookups, formulas=formulas
    )
    assert len(result.claims) == 12  # the three occupancy blocks still read
    assert result.halted is True


def _one_nationality_block(
    tmp_path: Path, *, label: str
) -> tuple[WorkbookMapping, Workbook, Workbook]:
    """A workbook with one country row, and a mapping that covers only it.

    `label` is what column A says. Reused for both the resolvable and the unmappable case, so the
    two tests differ only in the one thing this story is about.
    """
    workbook = Workbook()
    sheet = _sheet(workbook, "Nationality")
    sheet["A1"], sheet["B1"] = "Country", "January 2026"
    sheet["A2"], sheet["B2"] = label, 7
    path = tmp_path / f"{label}.xlsx"
    workbook.save(path)
    values, formulas = open_submission(path)
    mapping = WorkbookMapping(
        blocks=(
            MetricBlock(
                sheet="Nationality",
                metric="guests_by_nationality",
                orientation=Orientation.PERIODS_ACROSS_COLUMNS,
                value_range="B2:B2",
                period_label_range="B1:B1",
                dimension="nationality_iso2",
                dimension_label_range="A2:A2",
            ),
        )
    )
    return mapping, values, formulas


def test_a_label_the_lookup_cannot_resolve_halts_the_run(
    tmp_path: Path, policy: Policy, lookups: Lookups
) -> None:
    """D-NAT-12. A row this parser cannot place a country against is a row it cannot check, and the
    submission refuses rather than the parser guessing the nearest country."""
    mapping, values, formulas = _one_nationality_block(tmp_path, label="Freedonia")
    result = parse_claims(
        values, mapping, Period.parse("2026-01"), policy, lookups=lookups, formulas=formulas
    )

    assert result.halted is True
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.clause == "D-NAT-12"
    assert finding.variance_class is VarianceClass.EXTRACTION_LIMIT
    assert finding.severity is Severity.BLOCKING
    assert finding.is_hotel_error is False
    assert isinstance(finding.excel_ref, ExcelRef)
    assert finding.excel_ref.citation == "Nationality!A2"
    assert finding.narrative is not None and "Freedonia" in finding.narrative
    assert result.claims == ()


def test_a_resolvable_label_produces_no_finding_at_all(
    tmp_path: Path, policy: Policy, lookups: Lookups
) -> None:
    """The control for the test above. A label the lookup resolves must not halt anything, or the
    D-NAT-12 path would be firing on every submission rather than on the one it exists for."""
    mapping, values, formulas = _one_nationality_block(tmp_path, label="Germany")
    result = parse_claims(
        values, mapping, Period.parse("2026-01"), policy, lookups=lookups, formulas=formulas
    )

    assert result.halted is False
    assert result.findings == ()
    assert len(result.claims) == 1


def test_only_an_unmappable_label_is_promoted_to_a_refusal(
    tmp_path: Path, policy: Policy, lookups: Lookups
) -> None:
    """The scoping this story turns on, exercised against a defect that actually exists.

    A clean submission has no defects at all, so asserting "no D-NAT-12" against one proves
    nothing: there is nothing here for the scoping to have wrongly promoted. This plants a real
    D-XLS-03 defect instead, the same uncached-formula shape
    `test_an_uncached_formula_is_refused_not_guessed` exercises at the `read_block` layer, and
    checks it stays a refused claim rather than becoming a refused submission.
    """
    path = tmp_path / "formula.xlsx"
    workbook = Workbook()
    sheet = _sheet(workbook, "Occupancy")
    sheet["A1"], sheet["B1"] = "Month", "Room Nights Sold"
    sheet["A2"], sheet["B2"] = "January 2026", 1285
    sheet["A3"], sheet["B3"] = "February 2026", "=B2+14"
    workbook.save(path)

    values, formulas = open_submission(path)
    mapping = WorkbookMapping(
        blocks=(
            MetricBlock(
                sheet="Occupancy",
                metric="room_nights_sold",
                orientation=Orientation.PERIODS_DOWN_ROWS,
                value_range="B2:B3",
                period_label_range="A2:A3",
            ),
        )
    )
    result = parse_claims(
        values, mapping, Period.parse("2026-01"), policy, lookups=lookups, formulas=formulas
    )

    assert len(result.claims) == 1
    assert result.halted is False
    assert not any(finding.clause == "D-NAT-12" for finding in result.findings)
