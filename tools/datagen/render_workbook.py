"""The claim workbook — what the hotel asserts, rendered from the same ledger.

The demo workbook claims the **correct** figures. That is the clean-pass scene, and it is
deliberately not evidence of anything: a system verifying data it was rendered from will always
agree with itself. The scored fixtures carry planted errors and arrive in S11; this one exists so
the happy path is demonstrable and so the claim parser has a realistic document to be built against.

"Correct" is not the same as "convenient", and three wrinkles are here on purpose:

- **Out-of-scope claims exist.** `Rate & Revenue` carries ADR, RevPAR and average length of stay.
  None is verified. A claim the system neither checks nor mentions would read as approval, so
  out-of-scope claims have to be present in order for `out_of_scope` to be a thing the verdict can
  record (D-SCOPE-02).
- **Countries are labels, not codes.** `Czech Republic`, `Korea, Republic of`, `Russian Federation`.
  The normalisation path (D-NAT-09..11) is untested against a workbook that has already done the
  work, and `Czech Republic` → `CZ` is the canonical variant pair.
- **Values do not start at `A1`.** A title block sits above a two-row header, so the mapping agent
  has to locate the header rather than assume a corner. That is the difference between a mapping
  agent and a hard-coded cell range.

Byte-reproducibility needs **three** fixes, and this module only performs the first: pinning
`properties.created`. The other two happen after the save, because they can only happen after the
save — every zip entry timestamp, and `dcterms:modified`, which openpyxl assigns from the wall clock
on the way out regardless of what was set here. Call `reproducible.finalise_xlsx` on the result. All
three were found by generating twice and diffing; the docstring there records the order.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from datagen.aggregate import month_label
from datagen.claims import (
    NATIONALITY_DIMENSION,
    NATIONALITY_METRIC,
    OCCUPANCY_METRICS,
    ClaimCell,
    baseline_table,
    by_key,
    value_keys_in_order,
)
from datagen.spec import (
    HOTEL_ID,
    HOTEL_NAME,
    OUT_OF_SCOPE_CLAIMS,
    PINNED_DOC_AUTHOR,
    PINNED_TIMESTAMP,
    QUARTER,
    WORKBOOK,
)

if TYPE_CHECKING:
    from pathlib import Path

    from openpyxl.worksheet.worksheet import Worksheet

# openpyxl writes `docProps/core.xml` from these. Naive on purpose: the OOXML field is a naive
# dateTime and openpyxl will not serialise an aware one.
PINNED_PROPERTIES_DT: Final = datetime(2026, 3, 31, 19, 59, 59)  # noqa: DTZ001 — see above

TITLE_FONT: Final = Font(bold=True, size=13)
HEADER_FONT: Final = Font(bold=True)
NOTE_FONT: Final = Font(italic=True, size=9)


def _title_block(sheet: Worksheet, subtitle: str, note: str) -> None:
    """The two-line title above the header row, plus a note explaining the sheet.

    The note is not decoration: a workbook whose sheets are unlabelled forces the mapping agent to
    infer intent from numbers, and inferring intent from numbers is exactly the thing this
    architecture forbids it from doing.
    """
    sheet["A1"] = f"{HOTEL_NAME} — {HOTEL_ID}"
    sheet["A1"].font = TITLE_FONT
    sheet["A2"] = subtitle
    sheet["A3"] = note
    sheet["A3"].font = NOTE_FONT


def _write_header(sheet: Worksheet, row: int, headings: list[str]) -> None:
    for column, heading in enumerate(headings, start=1):
        cell = sheet.cell(row=row, column=column, value=heading)
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")


def _fit_columns(sheet: Worksheet, widths: dict[int, int]) -> None:
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = width


def _summary_sheet(sheet: Worksheet, months: tuple[str, ...]) -> None:
    _title_block(
        sheet,
        f"Quarterly statistical submission — {QUARTER}",
        "Cover sheet. Figures are on the Occupancy and Nationality sheets.",
    )
    rows = [
        ("Property", HOTEL_NAME),
        ("Property code", HOTEL_ID),
        ("Reporting period", QUARTER),
        ("Months covered", ", ".join(month_label(month) for month in months)),
        ("Submitted by", WORKBOOK.submitted_by),
        ("Prepared", PINNED_TIMESTAMP),
        ("Source", "Property management system reservation detail reports (attached)"),
        ("Data classification", "Synthetic — contains no real property or guest data"),
    ]
    for offset, (label, value) in enumerate(rows):
        row = WORKBOOK.header_row + offset
        sheet.cell(row=row, column=1, value=label).font = HEADER_FONT
        sheet.cell(row=row, column=2, value=value)
    _fit_columns(sheet, {1: 22, 2: 62})


def _occupancy_sheet(
    sheet: Worksheet, months: tuple[str, ...], indexed: dict[str, ClaimCell]
) -> None:
    """Every figure and every coordinate comes from the claim table.

    The sheet still owns its own fonts, widths and the number format on the occupancy column, which
    are properties of the document rather than of the claim. What it no longer owns is *where a
    claim lives*, because that is the thing a mutation has to be able to move.
    """
    _title_block(
        sheet,
        f"Occupancy — {QUARTER}",
        "Room nights sold and available by month, with occupancy. Quarter row is the period total.",
    )
    header = WORKBOOK.header_row
    _write_header(
        sheet, header, ["Month", "Room Nights Sold", "Room Nights Available", "Occupancy %"]
    )

    for period in (*months, QUARTER):
        claims = [indexed[f"{metric}:{period}"] for metric in OCCUPANCY_METRICS]
        sheet[claims[0].label_cell] = claims[0].label
        for metric, claim in zip(OCCUPANCY_METRICS, claims, strict=True):
            written = sheet[claim.cell]
            written.value = claim.value
            if metric == "occupancy_pct":
                written.number_format = "0.00"

    _fit_columns(sheet, {1: 18, 2: 19, 3: 22, 4: 13})


def _nationality_sheet(
    sheet: Worksheet, months: tuple[str, ...], table: tuple[ClaimCell, ...]
) -> None:
    """Countries down, months across — the cross-tab a hotel actually submits.

    A long-format table with one row per (country, month) would be easier to parse and is not what
    arrives. The mapping agent's job is to say "this header block is `guests_by_nationality`, the
    column axis is month, the row axis is nationality label"; it can only have that job if the
    document has two axes.

    Row order, labels and coordinates all come from the claim table, so a relabelling mutation
    changes what column A prints and a deletion moves the `Total` row up, without either being
    special-cased here.
    """
    _title_block(
        sheet,
        f"Guests by nationality — {QUARTER}",
        "Guests counted in the month of arrival. Country as recorded by the property.",
    )
    header = WORKBOOK.header_row
    _write_header(
        sheet,
        header,
        ["Country", *(month_label(month) for month in months), f"{QUARTER} total"],
    )

    nationality = [cell for cell in table if cell.metric == NATIONALITY_METRIC]
    indexed = by_key(nationality)
    present = value_keys_in_order(nationality)

    # Rows assigned fresh from position in `present`, column letters fresh from period index -
    # never from `claim.label_cell` / `claim.cell`. Those are fixed once, at `baseline_table`'s
    # construction, and a deletion filters `present` without ever touching them: reading them back
    # here would leave a country's stored row exactly where a country before it, alphabetically,
    # used to sit. `total_row` below is computed the same way, from `len(present)`, and the two
    # having independent sources is exactly how they drifted apart before this was written this way
    # - one was recomputed for the shorter table and the other was not.
    for offset, iso2 in enumerate(present):
        row = header + 1 + offset
        label_written = False
        for index, period in enumerate((*months, QUARTER), start=2):
            # A country absent from a month is left blank rather than zeroed. A blank is what a
            # hotel submits, and treating blank as zero is the claim parser's decision to make
            # explicitly rather than a decision this generator makes for it.
            claim = indexed.get(f"{NATIONALITY_METRIC}:{period}:{NATIONALITY_DIMENSION}={iso2}")
            if claim is None:
                continue
            if not label_written:
                sheet.cell(row=row, column=1, value=claim.label)
                label_written = True
            sheet.cell(row=row, column=index, value=claim.value)

    # The stated total is the sum of what the sheet shows, not of what truth holds. A hotel that
    # omitted a row totals the rows it printed, and `tda.excel.read` reads this as a `StatedTotal`
    # rather than comparing it against anything.
    total_row = WORKBOOK.header_row + 1 + len(present)
    sheet.cell(row=total_row, column=1, value="Total").font = HEADER_FONT
    for index, period in enumerate((*months, QUARTER), start=2):
        total = sum(cell.value for cell in nationality if cell.period == period)
        sheet.cell(row=total_row, column=index, value=total).font = HEADER_FONT

    _fit_columns(sheet, {1: 26, 2: 15, 3: 15, 4: 15, 5: 15})


def _out_of_scope_sheet(sheet: Worksheet, months: tuple[str, ...]) -> None:
    """Metrics the system does not verify, present so that it can say so (D-SCOPE-02).

    The values are invented outright rather than derived. Deriving an average daily rate would mean
    the generator had invented a rate for every reservation, and a number the system never checks is
    a number nobody should be able to mistake for ground truth.
    """
    _title_block(
        sheet,
        f"Rate and revenue — {QUARTER}",
        "Submitted for completeness. Outside the scope of statistical verification.",
    )
    header = WORKBOOK.header_row
    _write_header(sheet, header, ["Measure", *(month_label(month) for month in months)])

    for offset, (measure, by_month) in enumerate(OUT_OF_SCOPE_CLAIMS.items()):
        row = header + 1 + offset
        sheet.cell(row=row, column=1, value=measure)
        for index, month in enumerate(months, start=2):
            cell = sheet.cell(row=row, column=index, value=by_month[month])
            cell.number_format = "0.00"

    _fit_columns(sheet, {1: 32, 2: 15, 3: 15, 4: 15})


def render_workbook(
    path: Path,
    months: tuple[str, ...],
    truth: dict[str, int | float],
    claims: tuple[ClaimCell, ...] | None = None,
) -> None:
    """Write the claim workbook. Call `reproducible.finalise_xlsx` on the result afterwards.

    `claims` defaults to the correct figures, which is the demo workbook. The scored fixtures pass a
    mutated table instead, and that is the only difference between the clean submission and one
    carrying a planted error: the same renderer, a different set of claims.

    The normalisation is deliberately *not* done here: it is a property of the file format, not of
    this workbook, and keeping it in `reproducible.py` next to the PDF fix puts every hazard and
    every explanation in one place.
    """
    table = baseline_table(months, dict(truth)) if claims is None else claims

    workbook = Workbook()
    default = workbook.active
    assert default is not None
    workbook.remove(default)

    _summary_sheet(workbook.create_sheet(WORKBOOK.summary_sheet), months)
    _occupancy_sheet(workbook.create_sheet(WORKBOOK.occupancy_sheet), months, by_key(table))
    _nationality_sheet(workbook.create_sheet(WORKBOOK.nationality_sheet), months, table)
    _out_of_scope_sheet(workbook.create_sheet(WORKBOOK.out_of_scope_sheet), months)

    # One third of the reproducibility fix; `modified` set here does not survive the save. See the
    # module docstring and `reproducible.finalise_xlsx`.
    workbook.properties.creator = PINNED_DOC_AUTHOR
    workbook.properties.lastModifiedBy = PINNED_DOC_AUTHOR
    workbook.properties.created = PINNED_PROPERTIES_DT
    workbook.properties.modified = PINNED_PROPERTIES_DT
    workbook.properties.title = f"{HOTEL_NAME} — Quarterly submission {QUARTER}"

    workbook.save(path)
