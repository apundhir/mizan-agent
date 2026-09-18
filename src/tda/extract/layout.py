"""The committed column map for the monthly PMS report.

Extraction against a known layout is a **map plus a verification that the map still applies** — not a
best-effort read. This module is the map, and `verify_layout` is the verification.

**Why x-ranges rather than splitting text on whitespace.** The obvious approach is
`line.split()`, and it works until a cell contains a space. Two do: the status column prints
`Checked Out`, and a day-use row prints a marker in place of a departure date. Splitting on whitespace
turns a 13-column row into 14 or 15 and every field after the break shifts by one — which does not
raise, it produces a reservation with a plausible-looking wrong value. So each column is a horizontal
band, words are assigned to the band their midpoint falls in, and a word that lands in no band is a
layout change rather than something to absorb.

**Why the header is verified on every page.** A committed map is a claim that the document has the
shape it had when the map was written. If the report gains a column, every band after the insertion
point silently reads its neighbour's values. Checking the header text against `EXPECTED_HEADER` turns
that from a wrong number into a refusal, which is the whole argument of this story.

**The bands are stated here, not derived from the generator.** `tools/datagen/render_pdf.py` knows the
exact x positions it drew at, and importing them would make extraction a round-trip through the
generator's own constants — the same tautology the import guard exists to prevent, one layer up. The
bands below were measured from the rendered document, which is what a parser written against a real
property's export would have to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Band:
    """One column, as a horizontal range in PDF points.

    `left` inclusive, `right` exclusive. A word belongs to the band containing the midpoint of its
    bounding box — midpoint rather than left edge, because a right-aligned numeric column and its
    left-aligned neighbour can have bounding boxes that start on the wrong side of the boundary.
    """

    field: str
    left: float
    right: float
    heading: str

    def contains(self, midpoint: float) -> bool:
        return self.left <= midpoint < self.right


# Measured from the rendered report at landscape A4, from the observed word midpoints rather than
# computed from column widths. My first attempt did compute them, and two boundaries landed within a
# point of a real midpoint: a right-aligned single digit in the `Nts` column has its midpoint at
# 415.91, and a boundary arithmetic put at 416.0 would have dropped it into the next band. Boundaries
# now sit in the middle of the observed gaps, where a substituted font or a wider value cannot reach
# them.
#
# Observed midpoint ranges across a full page, for reference when these need revisiting:
#   reservation_id 63.98 · guest_ref 160.8-163.5 · nat 243.3-245.4 · arrival 289.2 · departure 351.2
#   nts 413.8-415.9 · rms 445.9 · ad 471.9 · ch 497.9 · rn_total 545.8-547.9 · rn_month 597.8-599.9
#   rate 613.7-619.8 · status 664.8 and 687.7
BANDS: Final[tuple[Band, ...]] = (
    Band("reservation_id", 20.0, 118.0, "Reservation"),
    Band("guest_ref", 118.0, 232.0, "Guest Ref"),
    Band("nationality", 232.0, 262.0, "Nat"),
    Band("arrival", 262.0, 324.0, "Arrival"),
    Band("departure", 324.0, 386.0, "Departure"),
    Band("nights", 386.0, 430.0, "Nts"),
    Band("rooms", 430.0, 458.0, "Rms"),
    Band("adults", 458.0, 484.0, "Ad"),
    Band("children", 484.0, 510.0, "Ch"),
    Band("room_nights_total", 510.0, 572.0, "RN Total"),
    Band("room_nights_month", 572.0, 604.0, "RN Month"),
    Band("rate_code", 604.0, 646.0, "Rate"),
    Band("status", 646.0, 760.0, "Status"),
)

# The header row as `extract_text` returns it, whitespace-collapsed. Checked per page.
EXPECTED_HEADER: Final = (
    "Reservation Guest Ref Nat Arrival Departure Nts Rms Ad Ch RN Total RN Month Rate Status"
)

# What a day-use row prints where a departure date would go. The report states the fact rather than
# repeating the arrival date, which is what a PMS does — and it means the parser derives
# `departure = arrival` from an explicit marker instead of inferring it from a printed night count.
DAY_USE_MARKER: Final = "day use"

# A reservation id, as the report prints it. Used to recognise a data row: a page also carries a
# heading block, a column header and a subtotal line, and none of them start with one of these.
RESERVATION_ID_PATTERN: Final = r"^RES-[0-9]{4}Q[1-4]-[0-9]{5}$"

# Rows per page, as the report lays them out. Not used to *find* rows — they are found by matching a
# reservation id — but asserted against the count actually read, because a page that yields 29 rows
# where the layout puts 30 has lost one, and a lost row is a silently wrong total.
ROWS_PER_PAGE: Final = 30

FIELDS: Final[tuple[str, ...]] = tuple(band.field for band in BANDS)


def band_for(midpoint: float) -> Band | None:
    """The band a word's midpoint falls in, or `None` if it falls in no band.

    `None` is a real answer and the caller must treat it as a layout failure. Returning a nearest
    match would defeat the purpose: the whole point of committed bands is that a word outside them all
    means the document is not the document the map was written for.
    """
    for band in BANDS:
        if band.contains(midpoint):
            return band
    return None


def verify_layout(page_text: str) -> None:
    """Raise unless the page carries the exact column header the bands were measured against.

    A cheap check that catches the expensive failure. If a column is inserted, renamed or reordered,
    every band after it reads its neighbour's values and produces reservations that pass every contract
    validator while being wrong.

    Searches the page's lines rather than taking a line index. The report's *title* line also begins
    with the word "Reservation" — "Reservation Detail Report — February 2026 | Property …" — so
    anything that located the header by its first word would verify the title against the column
    header, fail, and report a layout mismatch on a document that is perfectly fine. Found while
    testing, and worth the sentence: a verification that cries wolf is uninstalled within a week.
    """
    for line in page_text.splitlines():
        if " ".join(line.split()) == EXPECTED_HEADER:
            return
    candidates = [
        " ".join(line.split())
        for line in page_text.splitlines()
        if "Nts" in line or "RN Total" in line
    ]
    raise LayoutMismatchError(
        expected=EXPECTED_HEADER,
        found=candidates[0] if candidates else "<no line resembling a column header>",
    )


@dataclass(frozen=True, slots=True)
class LayoutMismatchError(Exception):
    """The document does not have the shape the committed column map was written for."""

    expected: str
    found: str

    def __str__(self) -> str:
        return (
            "the report's column header does not match the committed column map.\n"
            f"  expected: {self.expected}\n"
            f"  found:    {self.found}\n"
            "The map assigns values by horizontal position, so a changed column layout makes every "
            "band after the change read its neighbour's values - producing records that satisfy every "
            "validator and are wrong. A new layout needs a new map, not a looser parser."
        )
