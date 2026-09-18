"""The monthly PMS reports — text-layer PDFs with per-page subtotals and a grand-total block.

One file per month, rendered from the ledger. Three choices here are load-bearing.

**The table is drawn onto a canvas, not flowed by platypus.** `PdfRef.row_start` is documented as
"1-indexed row within the page's table", so a citation names a row that the renderer decided. If a
layout engine chose where the page broke, the row a finding cites would be a function of font
metrics, and a change to the header text would silently renumber every citation in every fixture.
Thirty rows per page, header repeated, and the page/row of every reservation is returned to the
caller.

**A month-spanning stay appears on both months' reports.** That is what a PMS in-house report does,
and it is what makes apportionment visible in the *documents* rather than only in the ledger. Each
row therefore prints two room-night columns: `RN Total` for the whole stay (`nights × rooms`, the
D-RNS-02 cross-check) and `RN Month` for the part falling in this month. A reader can check the
apportionment by hand, which is the point of a teaching corpus.

**The grand total is the qualifying total, and the report says what it left out.** Cancellations,
no-shows and house-use rows are printed with their real dates and their arithmetic room-nights,
because a PMS export prints them too — and the total excludes them. The exclusion block names each
count, so S5's totals reconciliation is a check an extractor can actually perform: filter by the
stated rules, sum, and match. A report whose total was simply the sum of every printed row would
make that reconciliation trivial and would teach the wrong lesson.

No guest name is rendered, because none exists (see `ledger._guest_ref`). The reports carry the
opaque reference instead, which is what the canonical record carries too.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen.canvas import Canvas

from datagen.aggregate import (
    PolicyView,
    guests_by_nationality,
    month_label,
    occupancy_pct,
    qualifies,
    room_nights_available,
    room_nights_sold,
)
from datagen.spec import (
    HOTEL_CITY,
    HOTEL_ID,
    HOTEL_NAME,
    PINNED_DOC_AUTHOR,
    PINNED_TIMESTAMP,
    ROWS_PER_PAGE,
)

if TYPE_CHECKING:
    from pathlib import Path

    from datagen.ledger import InventoryRow, LedgerRow

PAGE_SIZE: Final = landscape(A4)
# Coerced to float because reportlab is untyped: without this every y coordinate derived from the
# page height is `Any`, and mypy --strict stops checking the arithmetic that decides the layout.
PAGE_WIDTH: Final[float] = float(PAGE_SIZE[0])
PAGE_HEIGHT: Final[float] = float(PAGE_SIZE[1])

MARGIN: Final = 30.0
ROW_HEIGHT: Final = 13.5
BODY_FONT: Final = "Helvetica"
BOLD_FONT: Final = "Helvetica-Bold"
BODY_SIZE: Final = 7.5
HEADING_SIZE: Final = 13.0


@dataclass(frozen=True, slots=True)
class Column:
    """One column of the report table. `x` is the left edge; `right` right-aligns numerics."""

    heading: str
    x: float
    width: float
    right: bool = False


# Widths are hand-fitted to landscape A4 so nothing is clipped at 7.5pt. Deliberately explicit
# rather than computed: a column that silently narrows when a heading is reworded is a column that
# will one day truncate a date.
def _columns() -> tuple[Column, ...]:
    headings: tuple[tuple[str, float, bool], ...] = (
        ("Reservation", 92, False),
        ("Guest Ref", 110, False),
        ("Nat", 26, False),
        ("Arrival", 58, False),
        ("Departure", 58, False),
        ("Nts", 24, True),
        ("Rms", 26, True),
        ("Ad", 22, True),
        ("Ch", 22, True),
        ("RN Total", 46, True),
        ("RN Month", 48, True),
        ("Rate", 40, False),
        ("Status", 68, False),
    )
    columns: list[Column] = []
    x = MARGIN
    for heading, width, right in headings:
        columns.append(Column(heading=heading, x=x, width=width, right=right))
        x += width + 4
    return tuple(columns)


COLUMNS: Final = _columns()


@dataclass(frozen=True, slots=True)
class RowLocation:
    """Where a reservation was printed, in the terms a `PdfRef` uses.

    Returned so the callers that need citations — the eval fixtures in S11, and any test asserting
    an extractor cited the right place — can be built from the generator's own record rather than
    from a second guess about how the page was laid out.
    """

    file: str
    page: int
    row: int  # 1-indexed within this page's table


# The totals page is two columns: notes on the left, the nationality table on the right. A width
# rather than a guess, because "it looked fine in February" is how a note ends up printed through a
# country name in March.
LEFT_COLUMN_WIDTH: Final = 360.0
NOTE_LEADING: Final = 11.0


def _draw_wrapped(canvas: Canvas, x: float, y: float, width: float, text: str) -> float:
    """Draw `text` wrapped to `width`, returning the y below the last line.

    Measured with `stringWidth` against the current font rather than by character count: at 8.5pt
    Helvetica an `l` and a `W` differ by a factor of four, so a character budget is either far too
    conservative or silently wrong.
    """
    words = text.split()
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if canvas.stringWidth(candidate) <= width or not line:
            line = candidate
            continue
        canvas.drawString(x, y, line)
        y -= NOTE_LEADING
        line = word
    if line:
        canvas.drawString(x, y, line)
        y -= NOTE_LEADING
    return y


def _draw_text(canvas: Canvas, column: Column, y: float, value: str) -> None:
    if column.right:
        canvas.drawRightString(column.x + column.width, y, value)
    else:
        canvas.drawString(column.x, y, value)


def _draw_page_furniture(canvas: Canvas, month: str, page: int) -> float:
    """Heading, sub-heading and column headers. Returns the y of the first data row.

    The heading block carries the hotel id, the period and a pinned generation timestamp. The
    timestamp is pinned rather than omitted because a PMS report has one and an extractor should not
    be built against a document that is unrealistically clean — but it is a constant, or
    `make datagen` would rewrite three files every time anybody ran it.
    """
    canvas.setFont(BOLD_FONT, HEADING_SIZE)
    canvas.drawString(MARGIN, PAGE_HEIGHT - MARGIN - 4, f"{HOTEL_NAME} — {HOTEL_CITY}")

    canvas.setFont(BODY_FONT, 8.5)
    canvas.drawString(
        MARGIN,
        PAGE_HEIGHT - MARGIN - 19,
        f"Reservation Detail Report — {month_label(month)}   |   "
        f"Property {HOTEL_ID}   |   Generated {PINNED_TIMESTAMP}",
    )
    canvas.drawRightString(PAGE_WIDTH - MARGIN, PAGE_HEIGHT - MARGIN - 19, f"Page {page}")
    canvas.drawString(
        MARGIN,
        PAGE_HEIGHT - MARGIN - 31,
        "Rows list every reservation with an occupied night in this period, plus same-day "
        "(day-use) arrivals. A stay crossing a month end appears on both months' reports.",
    )

    header_y = PAGE_HEIGHT - MARGIN - 50
    canvas.setFont(BOLD_FONT, BODY_SIZE)
    for column in COLUMNS:
        _draw_text(canvas, column, header_y, column.heading)

    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, header_y - 4, PAGE_WIDTH - MARGIN, header_y - 4)
    return header_y - 4 - ROW_HEIGHT


def _draw_row(canvas: Canvas, row: LedgerRow, month: str, y: float) -> None:
    values = (
        row.reservation_id,
        row.guest_ref,
        row.nationality_iso2,
        row.arrival_date.isoformat(),
        row.departure_date.isoformat() if not row.is_day_use else "— day use —",
        str(row.nights),
        str(row.rooms),
        str(row.adults),
        str(row.children),
        str(row.room_nights),
        str(row.room_nights_in(month)),
        row.rate_code,
        row.status.replace("_", " ").title(),
    )
    canvas.setFont(BODY_FONT, BODY_SIZE)
    for column, value in zip(COLUMNS, values, strict=True):
        _draw_text(canvas, column, y, value)


def _draw_page_subtotal(
    canvas: Canvas, page_rows: list[LedgerRow], month: str, y: float, policy: PolicyView
) -> None:
    """A per-page subtotal, over the qualifying rows on this page only.

    Per-page subtotals are what a real report prints and what makes a page-by-page extraction
    checkable: an extractor that lost a row can detect it without having read the whole document.
    Qualifying-only, consistent with the grand total — a subtotal on one basis and a grand total on
    another would be a defect in the report, not a puzzle worth setting.
    """
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, y + 9, PAGE_WIDTH - MARGIN, y + 9)
    canvas.setFont(BOLD_FONT, BODY_SIZE)
    canvas.drawString(MARGIN, y, f"Page subtotal ({len(page_rows)} rows listed, qualifying only)")

    counted = [row for row in page_rows if qualifies(row, policy)]
    rn_total = COLUMNS[9]
    rn_month = COLUMNS[10]
    canvas.drawRightString(
        rn_total.x + rn_total.width, y, str(sum(row.room_nights for row in counted))
    )
    canvas.drawRightString(
        rn_month.x + rn_month.width, y, str(sum(row.room_nights_in(month) for row in counted))
    )


def _draw_totals_block(
    canvas: Canvas,
    rows: list[LedgerRow],
    inventory: list[InventoryRow],
    month: str,
    policy: PolicyView,
) -> None:
    """The grand-total block. S5's reconciliation target, so it has to be complete and honest.

    Prints the three occupancy figures, the exclusions that produced them, and the nationality
    breakdown. The exclusion counts are what make the total reproducible by a reader: "sum the RN
    Month column, then subtract these" is a check somebody can do with a calculator, which is the
    only kind of check a reviewer will actually perform.
    """
    sold = room_nights_sold(rows, month, policy)
    available = room_nights_available(inventory, month, policy)
    occupancy = occupancy_pct(sold, available, policy)

    listed = sum(r.room_nights_in(month) for r in rows)
    excluded = Counter(
        "cancelled"
        if r.status == "CANCELLED"
        else "no-show"
        if r.status == "NO_SHOW"
        else "house use"
        if r.rate_code == "HOUSE"
        else "day use (zero room-nights)"
        if r.is_day_use
        else "qualifying"
        for r in rows
    )

    y = PAGE_HEIGHT - MARGIN - 4
    canvas.setFont(BOLD_FONT, HEADING_SIZE)
    canvas.drawString(MARGIN, y, f"{HOTEL_NAME} — Period Totals, {month_label(month)}")

    y -= 20
    canvas.setFont(BODY_FONT, 8.5)
    canvas.drawString(
        MARGIN, y, f"Property {HOTEL_ID}   |   Generated {PINNED_TIMESTAMP}   |   Grand total block"
    )

    y -= 26
    canvas.setFont(BOLD_FONT, 9.5)
    canvas.drawString(MARGIN, y, "Occupancy")
    canvas.setFont(BODY_FONT, 9)
    for label, value in (
        ("Room nights sold", str(sold)),
        ("Room nights available", str(available)),
        ("Occupancy", f"{occupancy}%"),
    ):
        y -= 14
        canvas.drawString(MARGIN + 12, y, label)
        canvas.drawRightString(MARGIN + 260, y, value)

    y -= 24
    canvas.setFont(BOLD_FONT, 9.5)
    canvas.drawString(MARGIN, y, "Reconciliation of the room-nights listed above")
    canvas.setFont(BODY_FONT, 8.5)
    y -= 14
    for note in (
        f"Sum of the RN Month column over all {len(rows)} listed rows: {listed}",
        "Rows listed but excluded from the total: "
        + ", ".join(
            f"{count} {name}" for name, count in sorted(excluded.items()) if name != "qualifying"
        ),
        "Complimentary (COMP) rooms are included: the room was occupied by a guest. "
        "House use (HOUSE) is the property's own and is excluded.",
        "Room nights available is net of rooms out of order; see the inventory reference "
        "supplied with this submission.",
    ):
        y = _draw_wrapped(canvas, MARGIN + 12, y, LEFT_COLUMN_WIDTH, note)

    # The nationality block, in a second column. `LEFT_COLUMN_WIDTH` is what keeps the two apart:
    # the first version of this page let the notes run at full width and one of them printed
    # straight through the country names — legible to nobody and, worse, extractable as neither.
    guests_x = MARGIN + LEFT_COLUMN_WIDTH + 40
    guest_y = PAGE_HEIGHT - MARGIN - 56
    canvas.setFont(BOLD_FONT, 9.5)
    canvas.drawString(guests_x, guest_y, "Guests by nationality (arrivals this period)")
    canvas.setFont(BODY_FONT, 8.5)

    guests = guests_by_nationality(rows, month, policy)
    guest_y -= 15
    for iso2, count in sorted(guests.items()):
        canvas.drawString(guests_x + 12, guest_y, iso2)
        canvas.drawRightString(guests_x + 90, guest_y, str(count))
        guest_y -= 11.5
    canvas.setFont(BOLD_FONT, 8.5)
    canvas.drawString(guests_x + 12, guest_y - 4, "Total guests")
    canvas.drawRightString(guests_x + 90, guest_y - 4, str(sum(guests.values())))


def render_month(
    path: Path,
    month: str,
    rows: list[LedgerRow],
    inventory: list[InventoryRow],
    policy: PolicyView,
) -> list[RowLocation]:
    """Render one month's report and return where every reservation was printed.

    `invariant=1` is the whole of the byte-reproducibility fix for PDFs: without it reportlab
    stamps a creation date and a document ID, and two identical renders a second apart differ.
    `pageCompression=1` keeps the committed files small; it is deterministic.
    """
    canvas = Canvas(
        str(path),
        pagesize=PAGE_SIZE,
        invariant=1,
        pageCompression=1,
        lang="en",
    )
    canvas.setTitle(f"{HOTEL_NAME} Reservation Detail — {month_label(month)}")
    canvas.setAuthor(PINNED_DOC_AUTHOR)
    canvas.setSubject(f"Synthetic PMS export for {month}. No real property or guest data.")
    canvas.setCreator(PINNED_DOC_AUTHOR)

    locations: list[RowLocation] = []
    ordered = sorted(rows, key=lambda row: (row.arrival_date, row.reservation_id))

    page_number = 1
    for start in range(0, len(ordered), ROWS_PER_PAGE):
        page_rows = ordered[start : start + ROWS_PER_PAGE]
        y = _draw_page_furniture(canvas, month, page_number)

        for offset, row in enumerate(page_rows, start=1):
            _draw_row(canvas, row, month, y)
            locations.append(RowLocation(file=path.name, page=page_number, row=offset))
            y -= ROW_HEIGHT

        _draw_page_subtotal(canvas, page_rows, month, y - 4, policy)
        canvas.showPage()
        page_number += 1

    _draw_totals_block(canvas, ordered, inventory, month, policy)
    canvas.showPage()
    canvas.save()
    return locations
