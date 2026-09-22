"""The two things a reviewer must see to judge a finding, without leaving the screen.

the review screen's acceptance criterion is a stopwatch: *if judging one finding requires opening the PDF in
another window, the screen has failed, regardless of how correct the finding is.* An officer who
cannot check the agent in ten seconds will not sign behind it, and then the correctness of the
finding is beside the point.

So a finding's evidence is two pictures of the same disagreement:

| Side | What it shows |
|---|---|
| source | the **rows** the computed value came from, cropped out of the PDF page and outlined |
| claim | the **cell** the hotel's figure was read from, with enough of its neighbourhood to be legible |

## The crop is taken with the extractor's own row numbering

`tda.extract.pdf.row_bands` reuses the very function that assigns `PdfRef.row_start` in the first
place. Two implementations of "which row is row 12" would drift the first time the report's layout
changed, and the symptom is the worst one an evidence screen can have: a highlighted row that is
not the row the finding is about, shown to somebody deciding whether to tell a hotel it miscounted.

## The files must be the files the run read

`run.json` records a SHA-256 per submitted file, and this module checks it before cropping. That is
not belt and braces: the screen is pointed at a submission directory by configuration, and pointing
it at the wrong one is one environment variable away. A file whose digest does not match is refused
with both digests named, rather than shown.

## Nothing here decides anything

This module reads files and returns pictures and grids. It does not know what a finding means, does
not rank, and does not recompute. The reviewer decides; `tda.review.decisions` records what they
decided. A module that both presented the evidence and had an opinion about it is one where the
presentation eventually starts flattering the opinion.

## Both sides can be legitimately absent

A finding may cite a `NotReached` on one side — a V7 that halted before the workbook was parsed, or
a V5 the hotel never wrote a figure for. `Evidence.missing` says so **in the words of the citation
itself**, because "there is no cell because the hotel wrote nothing there" and "the screen failed to
load the cell" look identical to a reviewer otherwise, and only one of them is a defect.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pdfplumber
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from PIL import Image, ImageDraw

from tda.contracts import ExcelRef, InventoryRef, NotReached, PdfRef
from tda.extract import row_bands
from tda.obs.ledger import file_digest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from tda.contracts import Finding
    from tda.obs.ledger import InputFile

# How much of the page to keep around the cited rows. Enough to carry the column headings and a row
# either side for context; not so much that the reviewer is reading a page again.
MARGIN_PT: Final = 18.0

# The page is rendered at this DPI. 110 is legible on a laptop at the width a two-column layout
# gives it, and keeps a cropped band under a couple of hundred kilobytes.
RESOLUTION: Final = 110

# How many cells around the cited one to show. Two rows up and two down, three columns either side:
# enough for the row label and the column heading, which is what makes a bare number mean something.
# The gap between the heading strip and the band, in pixels. Shared with the geometry reported
# back, so a test can locate the outline in the stacked image.
GAP_PX: Final = 14

CELL_RADIUS_ROWS: Final = 3
CELL_RADIUS_COLUMNS: Final = 3


@dataclass(frozen=True, slots=True)
class CellView:
    """A small grid around one cell, with the target named so a renderer can highlight it."""

    sheet: str
    target: str
    columns: tuple[str, ...]
    rows: tuple[int, ...]
    values: dict[str, str]

    def value_at(self, column: str, row: int) -> str:
        return self.values.get(f"{column}{row}", "")


@dataclass(frozen=True, slots=True)
class Evidence:
    """What the screen shows for one finding. Either side may be absent, with a stated reason."""

    finding_id: str
    source_png: bytes | None = None
    source_caption: str = ""
    cell: CellView | None = None
    cell_caption: str = ""
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Both sides present. The ten-second test is only meaningful when this is true."""
        return self.source_png is not None and self.cell is not None


def evidence_for(finding: Finding, submission: Path, inputs: Sequence[InputFile] = ()) -> Evidence:
    """Assemble both sides for one finding from the files it cites.

    `inputs` is the run ledger's record of what it read — name and SHA-256 per file. Pass it and
    every file is checked before it is cropped. Pass nothing and the check is skipped, which is
    correct for a test holding a file directly and wrong for a screen; `tda.review.app` always
    passes it.

    Failures are captured into `missing` rather than raised. A screen listing twelve findings must
    not go blank because one PDF moved: the reviewer can still judge the other eleven, and the one
    that could not be loaded says why on its own card.
    """
    expected = {item.name: item.sha256 for item in inputs}
    source_png, source_caption, source_missing = _source_side(finding, submission, expected)
    cell, cell_caption, cell_missing = _claim_side(finding, submission, expected)
    return Evidence(
        finding_id=finding.finding_id,
        source_png=source_png,
        source_caption=source_caption,
        cell=cell,
        cell_caption=cell_caption,
        missing=tuple(m for m in (source_missing, cell_missing) if m),
    )


def _source_side(
    finding: Finding, submission: Path, expected: dict[str, str]
) -> tuple[bytes | None, str, str]:
    reference = finding.source_ref
    if isinstance(reference, NotReached):
        return None, "", f"no source to show: {reference.reason}"
    if isinstance(reference, InventoryRef):
        # The one metric whose source is not a PDF. D-RNA-01 makes rooms available a *property*
        # attribute, so there is no page anywhere in the system to crop - and inventing one would
        # put a picture in front of a reviewer that shows nothing about the finding.
        return (
            None,
            "",
            f"the source is the room inventory reference, not a report page: {reference.citation}",
        )
    report = submission / reference.file
    if wrong := _not_the_file_the_run_read(report, expected):
        return None, "", wrong
    try:
        region = page_region(report, reference)
    # Broad on purpose: one unreadable file must not blank a screen listing twelve findings.
    except Exception as exc:
        return None, "", f"could not read {reference.file}: {type(exc).__name__}: {exc}"
    caption = reference.citation
    if region.absent_rows:
        # Said out loud rather than trimmed: a reviewer looking at six rows under a caption reading
        # "rows 25-35" believes they have seen all eleven.
        caption += f"  (rows {_ranges(region.absent_rows)} are not on this page)"
    return region.png, caption, ""


def _claim_side(
    finding: Finding, submission: Path, expected: dict[str, str]
) -> tuple[CellView | None, str, str]:
    reference = finding.excel_ref
    if isinstance(reference, NotReached):
        return None, "", f"no cell to show: {reference.reason}"
    workbook = _workbook_in(submission, expected)
    if workbook is None:
        return None, "", "the submission has no workbook to show a cell from"
    if wrong := _not_the_file_the_run_read(workbook, expected):
        return None, "", wrong
    try:
        view = cell_region(workbook, reference)
    # Broad, as above.
    except Exception as exc:
        return None, "", f"could not read {workbook.name}: {type(exc).__name__}: {exc}"
    return view, reference.citation, ""


def _not_the_file_the_run_read(path: Path, expected: dict[str, str]) -> str:
    """Empty when the file is the one the run hashed, or when there is nothing to check against.

    The check exists because the screen is pointed at a submission by configuration, and pointing
    it at the wrong one is one environment variable away: `make run SUBMISSION=/data/hotel-x`
    followed by `make review` used to crop the demo corpus and caption it with this run's
    citations, with `Evidence.complete` true and nothing said. A wrong picture under a correct
    caption is the one failure this module cannot be allowed to have, and `run.json` was already
    recording the digests that make it detectable.
    """
    wanted = expected.get(path.name)
    if wanted is None:
        return ""
    if not path.is_file():
        return f"{path.name} is not in {path.parent}"
    actual = file_digest(path)
    if actual != wanted:
        return (
            f"{path.name} in {path.parent} is not the file this run read "
            f"({actual[:19]}… rather than {wanted[:19]}…). Showing it would put another "
            "submission's rows under this finding's citation."
        )
    return ""


def _ranges(rows: Sequence[int]) -> str:
    """`31-40`, or `3, 7-9`. Contiguous runs collapsed, because a list of forty numbers is not a
    sentence anybody reads."""
    if not rows:
        return ""
    spans: list[tuple[int, int]] = [(rows[0], rows[0])]
    for row in rows[1:]:
        first, last = spans[-1]
        if row == last + 1:
            spans[-1] = (first, row)
        else:
            spans.append((row, row))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


@dataclass(frozen=True, slots=True)
class PageRegion:
    """A crop of the cited rows, and the geometry it was drawn with.

    The geometry is returned rather than kept private so a **test** can check where the outline
    landed. An earlier version returned only the PNG, and five separate mutations — drawing the box
    three rows down, offsetting it forty points, dropping the heading strip, never stacking at all,
    closing the gap — every one of them passed a suite whose own docstring leads with *"a citation
    and its picture must agree"*. A picture nobody can assert about is a picture nobody has checked.
    """

    png: bytes
    box_top_px: int
    box_bottom_px: int
    heading_px: int
    absent_rows: tuple[int, ...] = ()

    @property
    def stacked(self) -> bool:
        """Whether the heading was cropped separately from the band."""
        return self.heading_px > 0


def page_region(pdf: Path, reference: PdfRef) -> PageRegion:
    """The cited rows, outlined, with the report's column headings above them.

    **Two strips, not one crop.** The obvious implementation takes everything from the top of the
    page down past the cited rows, and it fails the ten-second test in a way that only shows up on
    a real screen: a landscape page scaled into half a browser column is a grey smudge, and the
    reviewer is back to opening the PDF.

    So the heading block and the cited rows are cropped separately and stacked with a gap. The
    result is a few hundred pixels tall instead of a thousand, which means it renders at a size
    somebody can actually read — and it keeps the column headings, without which a band of numbers
    is not evidence of anything: the reviewer has to see that the figure under `RN Total` is the
    one in dispute.

    When the cited rows are near the top of the page the two strips overlap — or the page has no
    heading block at all, which a continuation page may not — one crop is taken instead. A gap
    drawn between two touching strips would imply rows were omitted.

    **A citation that overruns the page is reported, not trimmed.** `rows 25-35` on a thirty-row
    page used to draw six rows and caption them as eleven, which tells a reviewer they are looking
    at the whole of what was cited when they are not. The rows that are not there come back in
    `absent_rows` for the caller to say out loud.
    """
    with pdfplumber.open(pdf) as document:
        if reference.page > len(document.pages):
            raise ValueError(
                f"{pdf.name} has {len(document.pages)} page(s) and the finding cites page "
                f"{reference.page}"
            )
        page = document.pages[reference.page - 1]
        bands = row_bands(page.extract_words())
        wanted = range(reference.row_start, reference.row_end + 1)
        cited = [bands[row] for row in wanted if row in bands]
        if not cited:
            raise ValueError(
                f"page {reference.page} of {pdf.name} has no rows {reference.row_start}-"
                f"{reference.row_end}; it has {len(bands)} data row(s)"
            )

        top = min(band[0] for band in cited)
        bottom = max(band[1] for band in cited)
        image = page.to_image(resolution=RESOLUTION)
        # A transparent fill, not `None`: an opaque rectangle over the cited rows would hide the
        # one thing the reviewer is here to read.
        image.draw_rect(
            (0, top, page.width, bottom), stroke="red", stroke_width=2, fill=(0, 0, 0, 0)
        )
        # `annotated`, not `original`. `draw_rect` paints onto a separate canvas, so cropping the
        # original yields the right rows with no outline on them - a picture of a page where the
        # reviewer has to count rows to find the one being disputed, which is the failure this
        # whole module exists to avoid. It looks correct in every way except the one that matters.
        canvas = image.annotated
        scale = RESOLUTION / 72

        # The heading block: everything above the first data row. Clamped at zero, because a
        # continuation page whose table starts at the very top has no heading block - and a
        # negative height raised `Coordinate 'lower' is less than 'upper'` for *every* finding on
        # that page, which the caller then reported as an unreadable submission rather than as the
        # geometry bug it was.
        heading_ends = max(0.0, min(bands.values(), key=lambda band: band[0])[0] - 2)
        band_starts = max(0.0, top - MARGIN_PT)
        band_ends = min(bottom + MARGIN_PT, float(page.height))

        width = int(page.width * scale)
        absent = tuple(row for row in wanted if row not in bands)
        if heading_ends <= 0 or band_starts <= heading_ends:
            strip = canvas.crop((0, 0, width, int(band_ends * scale)))
            return PageRegion(
                png=_png(strip),
                box_top_px=int(top * scale),
                box_bottom_px=int(bottom * scale),
                heading_px=0,
                absent_rows=absent,
            )
        heading = canvas.crop((0, 0, width, int(heading_ends * scale)))
        band = canvas.crop((0, int(band_starts * scale), width, int(band_ends * scale)))
        offset = heading.height + GAP_PX - int(band_starts * scale)
        return PageRegion(
            png=_png(_stack(heading, band)),
            box_top_px=int(top * scale) + offset,
            box_bottom_px=int(bottom * scale) + offset,
            heading_px=heading.height,
            absent_rows=absent,
        )


def _stack(heading: Image.Image, band: Image.Image) -> Image.Image:
    """The heading above the cited rows, with a visible gap and a rule between them.

    The rule is not decoration: without it the two strips read as one continuous extract, and a
    reviewer would take rows 1-3 of the page to be adjacent to row 14. The gap says rows were
    skipped, which is true.
    """
    combined = Image.new(
        "RGB", (max(heading.width, band.width), heading.height + GAP_PX + band.height), "white"
    )
    combined.paste(heading, (0, 0))
    combined.paste(band, (0, heading.height + GAP_PX))
    ImageDraw.Draw(combined).line(
        (0, heading.height + GAP_PX // 2, combined.width, heading.height + GAP_PX // 2),
        fill="#b0b0b0",
        width=1,
    )
    return combined


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def cell_region(workbook: Path, reference: ExcelRef) -> CellView:
    """The cited cell and the neighbours that make it legible.

    Read with `data_only=True`, which is what the claim parser reads with: the reviewer
    must see the **value** the system compared, not the formula behind it. Showing `=SUM(B2:B4)`
    where the verdict says 1,295 would send an officer looking for a discrepancy that is not there.
    """
    book = load_workbook(workbook, data_only=True, read_only=True)
    try:
        if reference.sheet not in book.sheetnames:
            raise ValueError(
                f"{workbook.name} has no sheet named {reference.sheet!r}; it has {book.sheetnames}"
            )
        sheet = book[reference.sheet]
        column_letter = "".join(c for c in reference.cell if c.isalpha())
        row_number = int("".join(c for c in reference.cell if c.isdigit()))

        first_column = max(1, column_index_from_string(column_letter) - CELL_RADIUS_COLUMNS)
        last_column = column_index_from_string(column_letter) + CELL_RADIUS_COLUMNS
        first_row = max(1, row_number - CELL_RADIUS_ROWS)
        last_row = row_number + CELL_RADIUS_ROWS

        columns = tuple(get_column_letter(c) for c in range(first_column, last_column + 1))
        rows = tuple(range(first_row, last_row + 1))
        values: dict[str, str] = {}
        for row in sheet.iter_rows(
            min_row=first_row, max_row=last_row, min_col=first_column, max_col=last_column
        ):
            for cell in row:
                if cell.value is not None:
                    values[f"{get_column_letter(cell.column)}{cell.row}"] = str(cell.value)
        return CellView(
            sheet=reference.sheet,
            target=reference.cell,
            columns=columns,
            rows=rows,
            values=values,
        )
    finally:
        # `read_only=True` keeps a file handle open until it is closed explicitly, and a review
        # session opens one of these per finding.
        book.close()


def _workbook_in(submission: Path, expected: dict[str, str]) -> Path | None:
    """The workbook **this run read**, by name, falling back to the only one present.

    The fallback is what this used to do on its own, and it was a second implementation of a
    question `tda.graph.run.discover` already answers differently — it prefers
    `claims_<period>.xlsx` and this preferred whatever sorted first. A submission holding
    `amended_claims_2026-Q1.xlsx` beside `claims_2026-Q1.xlsx` therefore had the pipeline verify
    one file and the screen show cells from the other, with no warning and a correct-looking
    caption. Asking the ledger removes the guess: there is exactly one workbook the run hashed.
    """
    if not submission.is_dir():
        return None
    named = sorted(submission / name for name in expected if name.endswith(".xlsx"))
    if named:
        return named[0]
    return next(iter(sorted(submission.glob("*.xlsx"))), None)
