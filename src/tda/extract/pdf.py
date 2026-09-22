"""The PDF parser. Derive, never trust.

Every value the report prints is either **derived** or treated as a **cross-check**. Nothing printed is
adopted as an input:

- `nights` is `departure − arrival` (D-RNS-01). The printed `Nts` column is compared.
- `room_nights` is `nights × rooms` (D-RNS-02). The printed `RN Total` column is compared, and a
  mismatch is a blocking finding on that row — never a reason to prefer the printed figure.
- `RN Month` is compared against the apportionment the dates imply (D-RNS-03).

The distinction matters because the printed columns are the *hotel's* arithmetic. Adopting them would
make the system agree with the hotel by construction on exactly the values it exists to check, and the
agreement would be invisible: the numbers would reconcile perfectly and mean nothing.

**Where a value cannot be derived, the document must state it explicitly.** A day-use row prints a
marker in place of a departure date, and the parser reads `departure = arrival` from that marker rather
than from the printed night count. That keeps the rule intact: a printed `0` in `Nts` is still only a
cross-check, and a report that printed the marker *and* a non-zero night count is a contradiction the
parser reports rather than resolves.

**Guest names are structurally impossible to extract.** There is no name column, and `guest_ref` is
constrained to `^g_[0-9a-f]{8,32}$` by the contract — so a parser that reached for a name would fail at
construction rather than leak one into a verdict (D-EV-03).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

from tda.contracts import PdfRef, ReservationRecord
from tda.extract import layout
from tda.extract.normalise import UnmappableLabelError, nationality, rate_code, status

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from tda.extract.normalise import Lookups

_RESERVATION_ID: Final = re.compile(layout.RESERVATION_ID_PATTERN)
_DATE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class RowDefect:
    """One row the parser could not turn into a trustworthy record.

    Carries the citation, so the blocking finding built from it points a reviewer at the exact printed
    row. `clause` names the rule that was broken — a defect that cannot cite the rule it violates is an
    opinion, and a reviewer has no way to check an opinion against the document.
    """

    ref: PdfRef
    reservation_id: str
    reason: str
    clause: str
    detail: str

    def __str__(self) -> str:
        return f"{self.ref.citation}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ParsedPage:
    """One page's worth of reading, successes and failures kept together.

    Both, because a page that yielded 28 good records and 2 defects is not a page that yielded 28
    records. Returning only the successes is how a parser comes to under-report totals silently.
    """

    page: int
    records: tuple[ReservationRecord, ...]
    defects: tuple[RowDefect, ...]
    printed_room_nights_month: dict[str, int]


def _cells(words: Sequence[dict[str, object]]) -> dict[str, str]:
    """Assign a row's words to columns by the band their midpoint falls in.

    A word whose midpoint lands in no band raises: the committed map is a claim about the document's
    shape, and a word outside every band means the claim no longer holds. Words within a band are
    joined with a space, so `Checked Out` and `— day use —` survive intact — which is the reason this
    is positional rather than a `str.split()`.
    """
    collected: dict[str, list[tuple[float, str]]] = {}
    for word in words:
        x0 = float(str(word["x0"]))
        x1 = float(str(word["x1"]))
        text = str(word["text"])
        band = layout.band_for((x0 + x1) / 2)
        if band is None:
            raise layout.LayoutMismatchError(
                expected=f"every word inside a committed band ({len(layout.BANDS)} columns)",
                found=f"{text!r} at x={x0:.1f}..{x1:.1f}, which falls in no band",
            )
        collected.setdefault(band.field, []).append((x0, text))

    return {
        field: " ".join(text for _, text in sorted(values)) for field, values in collected.items()
    }


def _rows(words: Sequence[dict[str, object]]) -> Iterator[tuple[int, list[dict[str, object]]]]:
    """Group a page's words into visual lines, yielding only the data rows, numbered from 1.

    Rows are recognised by their first cell matching a reservation id, and numbered by their position
    among the data rows on the page — which is exactly what `PdfRef.row_start` means, "1-indexed row
    within the page's table". Counting every line instead would make a citation point at a row number
    that shifts when the heading block gains a line.
    """
    lines: dict[float, list[dict[str, object]]] = {}
    for word in words:
        lines.setdefault(round(float(str(word["top"])), 1), []).append(word)

    row_number = 0
    for top in sorted(lines):
        line = sorted(lines[top], key=lambda w: float(str(w["x0"])))
        first = str(line[0]["text"])
        if not _RESERVATION_ID.match(first):
            continue
        row_number += 1
        yield row_number, line


def row_bands(words: Sequence[dict[str, object]]) -> dict[int, tuple[float, float]]:
    """Row number -> the vertical band that row occupies on the page, in PDF points.

    Exists for the review screen, which has to show a reviewer **the rows a finding cites**
    rather than the page they are somewhere on. A screen that renders the whole page and leaves the
    officer to find row 14 has failed the ten-second test as surely as one that renders nothing.

    It reuses `_rows`, and that is the point rather than a convenience: the numbering a citation is
    written with and the geometry the crop is taken from now cannot disagree. Two implementations
    would drift the first time a layout changed, and the symptom would be the worst one an evidence
    screen can have — a highlighted row that is not the row the finding is about, shown to somebody
    deciding whether to accuse a hotel of miscounting.
    """
    bands: dict[int, tuple[float, float]] = {}
    for number, line in _rows(words):
        tops = [float(str(word["top"])) for word in line]
        bottoms = [float(str(word["bottom"])) for word in line]
        bands[number] = (min(tops), max(bottoms))
    return bands


def _int(cells: dict[str, str], field: str) -> int:
    raw = cells.get(field, "")
    if not raw.isdigit():
        raise ValueError(f"{field} is {raw!r}, which is not a whole number")
    return int(raw)


def _date(cells: dict[str, str], field: str) -> date:
    raw = cells.get(field, "")
    if not _DATE.match(raw):
        raise ValueError(f"{field} is {raw!r}, which is not an ISO date")
    return date.fromisoformat(raw)


def parse_row(
    cells: dict[str, str], ref: PdfRef, hotel_id: str, lookups: Lookups
) -> tuple[ReservationRecord, int]:
    """One row's cells to a canonical record, plus the printed `RN Month` for reconciliation.

    Raises `ValueError`, `UnmappableLabelError` or `LayoutMismatchError` on anything it cannot read. Every one
    becomes a blocking finding: there is no path here that returns a partial record, because a record
    with one guessed field is indistinguishable downstream from one that was read correctly.
    """
    reservation_id = cells.get("reservation_id", "")
    arrival = _date(cells, "arrival")

    departure_raw = cells.get("departure", "")
    if layout.DAY_USE_MARKER in departure_raw.lower():
        # The report states the fact rather than repeating the arrival date. Reading it from the
        # marker keeps the printed night count a cross-check rather than an input.
        departure = arrival
    else:
        departure = _date(cells, "departure")

    rooms = _int(cells, "rooms")
    nights = (departure - arrival).days

    printed_nights = _int(cells, "nights")
    if printed_nights != nights:
        raise ValueError(
            f"the printed Nts column says {printed_nights}, but {arrival} to {departure} is "
            f"{nights} nights (D-RNS-01). The dates are the source; the column is a cross-check, so "
            "a disagreement is a defect in the row rather than a reason to prefer one of them."
        )

    printed_room_nights = _int(cells, "room_nights_total")
    if printed_room_nights != nights * rooms:
        raise ValueError(
            f"the printed RN Total column says {printed_room_nights}, but {nights} nights x {rooms} "
            f"rooms is {nights * rooms} (D-RNS-02). Room-nights are always derived; the printed "
            "column is never adopted."
        )

    record = ReservationRecord(
        reservation_id=reservation_id,
        hotel_id=hotel_id,
        guest_ref=cells.get("guest_ref", ""),
        nationality_iso2=nationality(cells.get("nationality", ""), lookups),
        adults=_int(cells, "adults"),
        children=_int(cells, "children"),
        rooms=rooms,
        nights=nights,
        room_nights=nights * rooms,
        arrival_date=arrival,
        departure_date=departure,
        status=status(cells.get("status", ""), lookups),
        rate_code=rate_code(cells.get("rate_code", ""), lookups),
        source=ref,
    )
    return record, _int(cells, "room_nights_month")


_PROPERTY: Final = re.compile(r"Property\s+(?P<code>[A-Z0-9][A-Z0-9-]{2,31})")


def hotel_id_of(page_text: str) -> str:
    """The property code the report prints in its heading block.

    Read rather than assumed. A submission whose PDFs disagree with each other, or with the workbook,
    is an intake rejection (`HOTEL_MISMATCH`) and not something extraction should paper over — but it
    can only be checked if each file states its own answer.
    """
    match = _PROPERTY.search(page_text)
    if match is None:
        raise layout.LayoutMismatchError(
            expected="a `Property <code>` heading on every page",
            found="<no property code in the page heading>",
        )
    return match["code"]


def parse_page(
    page_number: int,
    page_text: str,
    words: Sequence[dict[str, object]],
    file_name: str,
    lookups: Lookups,
) -> ParsedPage:
    """Read one page: verify the layout, then every data row on it.

    The layout check runs first and on every page, not once per file. A report whose last page has a
    different column set is exactly the case a once-per-file check misses, and it is not hypothetical —
    the totals page of this very report has a completely different shape.
    """
    layout.verify_layout(page_text)
    # Read from the page rather than accepted as an argument, so a record's hotel_id is never
    # something a caller asserted. `intake` is what checks the four submitted files agree on
    # it; that check is only possible if each file states its own answer.
    hotel_id = hotel_id_of(page_text)

    records: list[ReservationRecord] = []
    defects: list[RowDefect] = []
    printed_month: dict[str, int] = {}

    for row_number, line in _rows(words):
        ref = PdfRef(file=file_name, page=page_number, row_start=row_number, row_end=row_number)
        reservation_id = str(line[0]["text"])
        try:
            cells = _cells(line)
            record, printed_rn_month = parse_row(cells, ref, hotel_id, lookups)
        except UnmappableLabelError as unmappable:
            defects.append(
                RowDefect(
                    ref=ref,
                    reservation_id=reservation_id,
                    reason=f"unmappable_{unmappable.kind.replace(' ', '_')}",
                    clause="D-NAT-12" if unmappable.kind == "country" else "D-QUAL-03",
                    detail=str(unmappable),
                )
            )
        except (ValueError, layout.LayoutMismatchError) as problem:
            defects.append(
                RowDefect(
                    ref=ref,
                    reservation_id=reservation_id,
                    reason="unreadable_row",
                    clause="D-RNS-02",
                    detail=str(problem),
                )
            )
        else:
            records.append(record)
            printed_month[record.reservation_id] = printed_rn_month

    if records and len(records) + len(defects) > layout.ROWS_PER_PAGE:
        raise layout.LayoutMismatchError(
            expected=f"at most {layout.ROWS_PER_PAGE} data rows per page",
            found=f"{len(records) + len(defects)} on page {page_number}",
        )

    return ParsedPage(
        page=page_number,
        records=tuple(records),
        defects=tuple(defects),
        printed_room_nights_month=printed_month,
    )


@dataclass(frozen=True, slots=True)
class ReadReport:
    """One monthly report, read once.

    The totals page is carried here rather than re-opened by the caller. Opening the same PDF twice
    worked and was wrong for a subtler reason than the wasted I/O: two opens can disagree. Whatever
    decides which page is the totals block has to be the same decision both times, and keeping it in
    one place is the only way that stays true.
    """

    pages: tuple[ParsedPage, ...]
    hotel_id: str
    page_count: int
    totals_page: int
    totals_left: str
    totals_right: str


def read_report(path: Path, lookups: Lookups) -> ReadReport:
    """Read every page of one monthly report, data pages and grand-total block together.

    The `pdfplumber` import is function-local. The extract package is installed with the `extract`
    extra, and a module-level import would make `tda.extract.pdf` unimportable — and therefore every
    test in this file uncollectable — in an environment that only needs the metric library.

    The totals page is identified by **not carrying the data column header**, rather than by being
    last. A report that grew a trailing page would otherwise have its final data page parsed as the
    totals block, which fails while pointing at entirely the wrong thing.
    """
    import pdfplumber

    from tda.extract.totals import split_columns

    pages: list[ParsedPage] = []
    hotel_id = ""
    totals: tuple[int, str, str] | None = None

    with pdfplumber.open(path) as document:
        for index, page in enumerate(document.pages, start=1):
            text = page.extract_text() or ""
            if not hotel_id:
                hotel_id = hotel_id_of(text)
            try:
                layout.verify_layout(text)
            except layout.LayoutMismatchError:
                left, right = split_columns(page.extract_words())
                totals = (index, left, right)
                continue
            pages.append(parse_page(index, text, page.extract_words(), path.name, lookups))

        page_count = len(document.pages)

    if totals is None:
        raise layout.LayoutMismatchError(
            expected="a grand-total page with no data column header",
            found=f"every page of {path.name} carries the data column header",
        )

    totals_page, totals_left, totals_right = totals
    return ReadReport(
        pages=tuple(pages),
        hotel_id=hotel_id,
        page_count=page_count,
        totals_page=totals_page,
        totals_left=totals_left,
        totals_right=totals_right,
    )
