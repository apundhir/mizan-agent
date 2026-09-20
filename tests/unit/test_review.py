"""The review gate: the evidence a decision rests on, and the record it leaves.

Two properties carry the weight, and neither needs a browser to check.

**A citation and its picture must agree.** `PdfRef.row_start` and the crop the reviewer sees come
from one function — `tda.extract.pdf.row_bands` reuses the very generator that numbers the rows
during extraction. The test below proves the crop is taken from the cited rows by reading the words
inside the band back out and matching them against the row the extractor parsed. A screen that
highlighted the wrong row while captioning it correctly would be the most dangerous artefact in
this repository: it would make a reviewer confident about the wrong thing.

**A decision names who made it.** FR-10 and FR-11 — nothing material passes without a human
decision, and the record says who and when. The gate is exercised here rather than through the
screen, because `tda.review.decisions` is what both the screen and PRD-91's fallback console flow
would call, and a guarantee tested through one surface is a guarantee the other can quietly break.

The Streamlit module gets an import check and nothing more. It is a shell over the two modules
above; asserting on its widgets would test Streamlit.
"""

from __future__ import annotations

import io
import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest
from docx import Document as DocxDocument
from openpyxl import load_workbook
from PIL import Image
from tests.unit.test_outputs import (
    definitional_finding,
    material_finding,
    unplaced_finding,
    verdict_with,
)

import tda.review.app as app
from tda.contracts import (
    ExcelRef,
    ExtractionSummary,
    InventoryRef,
    PdfRef,
    ReviewerDecision,
    Severity,
    VarianceClass,
)
from tda.extract import load_lookups, parse_page, row_bands
from tda.obs.ledger import InputFile
from tda.outputs import write_outputs
from tda.outputs.verdict import VERDICT_FILE, read_verdict
from tda.review.decisions import ReviewError, record_decision, superseded, undecided
from tda.review.evidence import (
    GAP_PX,
    RESOLUTION,
    cell_region,
    evidence_for,
    page_region,
)
from tda.review.present import cause_line, cell_table, chip_colour, decision_summary, headline


def _red_rows(image: Image.Image) -> list[int]:
    """The y coordinates that carry the outline, as pixel rows."""
    flatten = getattr(image, "get_flattened_data", None) or image.getdata
    pixels: list[tuple[int, int, int]] = list(flatten())
    width = image.width
    return [
        y
        for y in range(image.height)
        if any(r > 150 and g < 90 and b < 90 for r, g, b in pixels[y * width : (y + 1) * width])
    ]


def _report_with_rows_at_the_top(tmp_path: Path) -> Path:
    """A one-page report whose first data row sits flush with the top edge, with no heading."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas as pdf_canvas

    path = tmp_path / "continuation.pdf"
    page = landscape(A4)
    drawing = pdf_canvas.Canvas(str(path), pagesize=page)
    drawing.setFont("Helvetica", 8)
    for index in range(6):
        drawing.drawString(
            40, page[1] - 4 - index * 12, f"RES-2026Q1-{index:05d}  g_abcdef012345  IN"
        )
    drawing.save()
    return path


CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "demo"
SUBMISSION = CORPUS / "submission"
REPORT = SUBMISSION / "pms_2026-01.pdf"
WORKBOOK = SUBMISSION / "claims_2026-Q1.xlsx"


@pytest.fixture
def reviewed_run(tmp_path: Path) -> Path:
    """A run directory with a verdict, a memo and an annotated workbook, ready to review."""
    run = tmp_path / "run-0123456789ab"
    write_outputs(run, verdict_with(material_finding(), definitional_finding()), [], WORKBOOK)
    return run


# ── the evidence, and the one way it must not be wrong ───────────────────────


def test_the_band_for_a_row_holds_the_record_the_extractor_cited_at_that_row() -> None:
    """The property the whole screen rests on, checked against the extractor rather than itself.

    An earlier version of this test asserted only that band 12 contained exactly one reservation
    id and sat between bands 11 and 13 — all true of *any* consistent numbering, including a wrong
    one. Making `row_bands` count heading lines as rows left it green while every crop in the app
    pointed four rows off.

    So it asks the parser what it cited at row 12 and looks for that record's own id inside the
    band the screen would draw. The two numberings share a generator; this is what proves it.
    """
    with pdfplumber.open(REPORT) as document:
        page = document.pages[0]
        words = page.extract_words()
        bands = row_bands(words)
        parsed = parse_page(1, page.extract_text(), words, REPORT.name, load_lookups())

    cited = next(r for r in parsed.records if r.source.row_start == 12)
    top, bottom = bands[12]
    inside = {
        str(word["text"]) for word in words if top - 0.5 <= float(str(word["top"])) <= bottom + 0.5
    }

    assert cited.reservation_id in inside
    # And only that row: the rows either side are outside the band the reviewer sees outlined.
    neighbours = {r.reservation_id for r in parsed.records if r.source.row_start in (11, 13)}
    assert not (neighbours & inside)


def test_the_outline_lands_on_the_cited_rows_and_nowhere_else() -> None:
    """Where the red box actually is, in pixels, against where the cited rows actually are.

    The previous version of this test counted red pixels **anywhere** in the image. Five separate
    mutations passed it: drawing the box three rows lower, offsetting it forty points, dropping the
    heading strip, never stacking, closing the gap. A picture nobody can assert about is a picture
    nobody has checked, which is why `page_region` returns its geometry.
    """
    reference = PdfRef(file=REPORT.name, page=1, row_start=12, row_end=14)
    with pdfplumber.open(REPORT) as document:
        bands = row_bands(document.pages[0].extract_words())

    region = page_region(REPORT, reference)

    scale = RESOLUTION / 72
    expected_top = int(min(bands[row][0] for row in (12, 13, 14)) * scale)
    expected_bottom = int(max(bands[row][1] for row in (12, 13, 14)) * scale)
    # The box is where the cited rows are, once the stacking offset is applied.
    assert abs((region.box_bottom_px - region.box_top_px) - (expected_bottom - expected_top)) <= 2

    image = Image.open(io.BytesIO(region.png)).convert("RGB")
    rows_with_red = _red_rows(image)
    assert rows_with_red, "the cited rows are not outlined at all"
    assert min(rows_with_red) == pytest.approx(region.box_top_px, abs=4)
    assert max(rows_with_red) == pytest.approx(region.box_bottom_px, abs=4)
    # And the row above the cited band is outside the box, so the reviewer is not looking at row 11.
    assert int(bands[11][1] * scale) < expected_top


def test_the_crop_carries_the_column_headings_above_a_gap() -> None:
    """A band of numbers with no headings is not evidence that the figure under `RN Total` is the
    one in dispute — and two strips with no gap read as one continuous extract, which would put
    row 3 next to row 14."""
    region = page_region(REPORT, PdfRef(file=REPORT.name, page=1, row_start=12, row_end=14))

    assert region.stacked
    assert region.heading_px > 0
    image = Image.open(io.BytesIO(region.png))
    # Heading, gap, band: the image is taller than the heading and shorter than the whole page.
    assert image.height > region.heading_px + GAP_PX
    with pdfplumber.open(REPORT) as document:
        page_px = int(document.pages[0].height * RESOLUTION / 72)
    assert image.height < page_px


def test_a_citation_near_the_top_of_the_page_is_one_crop_rather_than_two() -> None:
    """A gap drawn between two touching strips would imply rows were omitted."""
    region = page_region(REPORT, PdfRef(file=REPORT.name, page=1, row_start=1, row_end=2))

    assert not region.stacked
    assert region.heading_px == 0


def test_a_page_whose_table_starts_at_the_very_top_still_renders(tmp_path: Path) -> None:
    """A continuation page has no heading block, and the heading height went negative — raising
    `Coordinate 'lower' is less than 'upper'` for *every* finding on that page. The caller then
    reported that as an unreadable submission, which the runbook defines as a defect in the file
    rather than in this code."""
    report = _report_with_rows_at_the_top(tmp_path)

    region = page_region(report, PdfRef(file=report.name, page=1, row_start=1, row_end=1))

    assert region.png.startswith(b"\x89PNG")
    assert not region.stacked


def test_a_row_span_that_overruns_the_page_says_which_rows_are_missing() -> None:
    """Drawing six rows under a caption reading "rows 25-35" tells a reviewer they have seen all
    eleven. The rows that are not there are named instead."""
    with pdfplumber.open(REPORT) as document:
        rows = len(row_bands(document.pages[0].extract_words()))

    region = page_region(
        REPORT, PdfRef(file=REPORT.name, page=1, row_start=rows - 1, row_end=rows + 5)
    )

    assert region.absent_rows == tuple(range(rows + 1, rows + 6))
    finding = material_finding().model_copy(
        update={
            "source_ref": PdfRef(file=REPORT.name, page=1, row_start=rows - 1, row_end=rows + 5)
        }
    )
    evidence = evidence_for(finding, SUBMISSION)
    assert "are not on this page" in evidence.source_caption
    assert f"{rows + 1}-{rows + 5}" in evidence.source_caption


def test_a_page_the_report_does_not_have_is_refused_with_the_count() -> None:
    """A citation that cannot be shown is a defect in the finding, and the message says so rather
    than producing an empty picture."""
    with pytest.raises(ValueError, match="page"):
        page_region(REPORT, PdfRef(file=REPORT.name, page=99, row_start=1, row_end=1))

    with pytest.raises(ValueError, match="no rows 500-500"):
        page_region(REPORT, PdfRef(file=REPORT.name, page=1, row_start=500, row_end=500))


def test_the_cell_region_carries_the_labels_that_make_the_number_mean_something() -> None:
    """`1285` under no heading is a fact about nothing. The neighbourhood is what turns it into
    'room nights sold, January 2026'."""
    view = cell_region(WORKBOOK, ExcelRef(sheet="Occupancy", cell="B5"))

    assert view.target == "B5"
    assert view.value_at("B", 5) == "1285"
    assert view.value_at("A", 5) == "January 2026"
    assert "Room Nights Sold" in view.value_at("B", 4)


def test_the_cell_region_shows_the_value_and_not_the_formula() -> None:
    """Read `data_only=True`, as the claim parser reads. Showing `=SUM(B2:B4)` where the verdict
    says 1,295 sends an officer looking for a discrepancy that is not there."""
    view = cell_region(WORKBOOK, ExcelRef(sheet="Occupancy", cell="D8"))

    assert not view.value_at("D", 8).startswith("=")


def test_both_sides_are_assembled_for_an_ordinary_finding() -> None:
    evidence = evidence_for(material_finding(), SUBMISSION)

    assert evidence.complete
    assert evidence.missing == ()
    assert evidence.cell is not None
    assert evidence.cell.target == "B5"


def test_a_finding_with_no_cell_says_why_rather_than_showing_an_empty_pane() -> None:
    """ "There is no cell because the hotel wrote nothing there" and "the screen failed to load the
    cell" look identical to a reviewer otherwise, and only one of them is a defect."""
    evidence = evidence_for(unplaced_finding(), SUBMISSION)

    assert not evidence.complete
    assert evidence.cell is None
    assert any("no cell to show" in problem for problem in evidence.missing)
    assert any("extraction halted" in problem for problem in evidence.missing)


def test_a_finding_whose_source_is_the_inventory_reference_says_so() -> None:
    """D-RNA-01 makes rooms available a property attribute, so there is no page anywhere in the
    system to crop — and inventing one would put a picture in front of a reviewer that shows
    nothing about the finding."""
    finding = material_finding().model_copy(
        update={"source_ref": InventoryRef(file="inventory_2026-Q1.csv", row_start=3, row_end=3)}
    )

    evidence = evidence_for(finding, SUBMISSION)

    assert evidence.source_png is None
    assert any("room inventory reference" in problem for problem in evidence.missing)


def test_a_missing_report_costs_one_pane_rather_than_the_screen(tmp_path: Path) -> None:
    """A screen listing twelve findings must not go blank because one PDF moved: the reviewer can
    still judge the other eleven, and the one that could not be loaded says why on its own card."""
    evidence = evidence_for(material_finding(), tmp_path)

    assert evidence.source_png is None
    assert evidence.cell is None
    assert len(evidence.missing) == 2
    assert any("could not read" in problem for problem in evidence.missing)


# ── the gate ────────────────────────────────────────────────────────────────


def test_a_decision_is_written_into_the_verdict_with_a_name_and_a_time(
    reviewed_run: Path,
) -> None:
    """FR-11. The reviewer's name and the timestamp are required because the point of the gate is
    that somebody accountable looked; an anonymous decision records that a button was pressed."""
    recorded = record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.ACCEPT,
        reviewer="A. Officer",
        note="checked against the January report",
        at=datetime(2026, 4, 2, 9, 30, tzinfo=UTC),
    )

    on_disk = json.loads((reviewed_run / VERDICT_FILE).read_text(encoding="utf-8"))
    assert on_disk["review_records"] == [
        {
            "finding_id": "F-0001",
            "decision": "accept",
            "reviewer": "A. Officer",
            "decided_at": "2026-04-02T09:30:00Z",
            "note": "checked against the January report",
            "amended_value": None,
        }
    ]
    assert recorded.verdict.summary.undecided_findings == 1


def test_the_memo_is_re_issued_so_the_two_artefacts_cannot_disagree(reviewed_run: Path) -> None:
    """Both are handed to the same supervisor. A verdict recording three decisions beside a memo
    that says "No human review has been recorded" is one of them lying, and it is the one written
    in Word that gets forwarded."""
    from docx import Document

    before = [p.text for p in Document(str(reviewed_run / "memo.docx")).paragraphs]
    assert any("No human review has been recorded" in line for line in before)

    record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.ACCEPT,
        reviewer="A. Officer",
    )

    after = [p.text for p in Document(str(reviewed_run / "memo.docx")).paragraphs]
    assert not any("No human review has been recorded" in line for line in after)
    assert any("A. Officer" in line for line in after)
    assert any("Undecided: F-0002" in line for line in after)


def test_an_anonymous_decision_is_refused(reviewed_run: Path) -> None:
    with pytest.raises(ReviewError, match="must name the person who made it"):
        record_decision(
            reviewed_run,
            finding_id="F-0001",
            decision=ReviewerDecision.ACCEPT,
            reviewer="   ",
        )


def test_an_amendment_without_a_figure_is_refused(reviewed_run: Path) -> None:
    """An AMEND with no value says the number is wrong without saying what it should be, which
    leaves the hotel nothing to act on."""
    with pytest.raises(ReviewError, match="requires the amended value"):
        record_decision(
            reviewed_run,
            finding_id="F-0001",
            decision=ReviewerDecision.AMEND,
            reviewer="A. Officer",
        )


def test_a_decision_on_a_finding_this_verdict_does_not_have_is_refused(
    reviewed_run: Path,
) -> None:
    with pytest.raises(ReviewError, match="not in this verdict"):
        record_decision(
            reviewed_run,
            finding_id="F-9999",
            decision=ReviewerDecision.ACCEPT,
            reviewer="A. Officer",
        )


def test_reviewing_a_run_that_produced_no_verdict_says_which_directory(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match=r"no verdict\.json"):
        record_decision(
            tmp_path,
            finding_id="F-0001",
            decision=ReviewerDecision.ACCEPT,
            reviewer="A. Officer",
        )


def test_a_changed_mind_is_kept_rather_than_overwritten(reviewed_run: Path) -> None:
    """An officer who accepts a finding and later rejects it has done something an auditor needs to
    see. Replacing the first record would destroy exactly the evidence a review gate exists to
    produce."""
    record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.AMEND,
        reviewer="A. Officer",
        amended_value=Decimal("1285"),
    )
    second = record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.REJECT,
        reviewer="B. Supervisor",
        note="the amendment was premature",
    )

    verdict = second.verdict
    assert len(verdict.review_records) == 2
    standing = verdict.standing_decisions["F-0001"]
    assert (standing.decision, standing.reviewer) == (ReviewerDecision.REJECT, "B. Supervisor")
    earlier = superseded(verdict, "F-0001")
    assert [(r.decision.value, r.reviewer) for r in earlier] == [("amend", "A. Officer")]


def test_a_second_reviewer_cannot_silently_drop_the_first_ones_decisions(
    reviewed_run: Path,
) -> None:
    """The recorder re-reads the verdict immediately before every write, so appending to what is
    actually on disk *is* the merge. A cached copy from when a screen loaded would lose whatever
    the other officer decided in the meantime, and neither would ever know."""
    stale = read_verdict(reviewed_run / VERDICT_FILE)
    assert not stale.review_records

    record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.ACCEPT,
        reviewer="A. Officer",
    )
    # A second officer working from the copy loaded before that decision existed.
    second = record_decision(
        reviewed_run,
        finding_id="F-0002",
        decision=ReviewerDecision.ACCEPT,
        reviewer="B. Supervisor",
    )

    assert [r.finding_id for r in second.verdict.review_records] == ["F-0001", "F-0002"]
    assert second.verdict.undecided_findings == ()


def test_what_is_left_to_decide_is_answered_once(reviewed_run: Path) -> None:
    """The screen needs the findings and the verdict exposes the ids. Deriving them twice is how
    two surfaces come to disagree about what is outstanding."""
    verdict = read_verdict(reviewed_run / VERDICT_FILE)
    assert [f.finding_id for f in undecided(verdict)] == ["F-0001", "F-0002"]

    after = record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.ACCEPT,
        reviewer="A. Officer",
    ).verdict
    assert [f.finding_id for f in undecided(after)] == ["F-0002"]


# ── the words on the screen ─────────────────────────────────────────────────


def test_a_definitional_item_is_never_described_as_an_error() -> None:
    """D-MAT-06 keeps a V2 out of the hotel error count in the verdict and out of the findings
    table in the memo. The screen is the one place a human actually reads, so the sentence is said
    out loud rather than implied by a colour."""
    line = cause_line(definitional_finding())

    assert "P-OOO-INCLUDED" in line
    assert "also by P-MONTH-ARRIVAL" in line
    assert "not a hotel error" in line


def test_a_definitional_item_takes_the_workbooks_amber_rather_than_its_severity_colour() -> None:
    """Its severity is `material`, which policy assigned. The colour is what a reviewer reads as
    *who is at fault*, and for a V2 the answer is nobody."""
    assert chip_colour(definitional_finding()) == "#FFE699"
    assert chip_colour(material_finding()) != chip_colour(definitional_finding())


def test_a_blocking_finding_reads_as_could_not_be_checked() -> None:
    line = cause_line(unplaced_finding())

    assert "Could not be checked" in line
    assert unplaced_finding().variance_class is VarianceClass.EXTRACTION_LIMIT
    assert unplaced_finding().severity is Severity.BLOCKING


def test_the_cell_table_makes_exactly_one_cell_unmistakable() -> None:
    """A grid where the reviewer has to find `B5` by reading the axis labels costs the seconds the
    whole screen is budgeted in."""
    view = cell_region(WORKBOOK, ExcelRef(sheet="Occupancy", cell="B5"))

    html = cell_table(view)

    # The thickened border, not its colour: every other cell and header draws 1px, so a count of
    # one is the property being claimed. Asserting the hex pinned the grid to a light background,
    # which is what made it unreadable in dark mode before the theme existed.
    assert html.count("border:2px solid") == 1
    assert "1285" in html
    assert "January 2026" in html


def test_a_workbook_label_is_escaped_rather_than_rendered(tmp_path: Path) -> None:
    """The grid is HTML, and every value in it was typed by somebody outside this system. A label
    reading `<script>` must arrive as text on the screen, not as markup in the page."""
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Sheet1"
    sheet["A1"] = "<script>alert('x')</script>"
    sheet["B2"] = "1285"
    path = tmp_path / "hostile.xlsx"
    book.save(path)

    html = cell_table(cell_region(path, ExcelRef(sheet="Sheet1", cell="B2")))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_decided_finding_says_what_was_decided_and_by_whom() -> None:
    from tda.contracts import ReviewRecord

    record = ReviewRecord(
        finding_id="F-0001",
        decision=ReviewerDecision.AMEND,
        reviewer="A. Officer",
        decided_at=datetime(2026, 4, 2, 9, 30, tzinfo=UTC),
        amended_value="1285",
        note="agreed by phone",
    )

    summary = decision_summary(record, superseded_count=1)

    assert "Amended" in summary
    assert "1285" in summary
    assert "A. Officer" in summary
    assert "agreed by phone" in summary
    assert "superseded 1 earlier decision(s)" in summary


def test_a_finding_headline_names_what_is_in_dispute() -> None:
    assert headline(material_finding()) == "F-0001 · room_nights_sold:2026-01"


# ── where a run's evidence lives ──────────────────────────────────────────────
#
# Not widgets - `env_path` and `submission_for` are plain functions the console reuses, and the
# thing worth proving is the fallback order, not anything Streamlit renders.


def test_an_unset_environment_variable_falls_back_to_the_default(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback"
    assert app.env_path("MIZAN_DOES_NOT_EXIST", fallback) == fallback


def test_an_empty_environment_variable_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make review` used to export the variable with nothing after the `=` when the caller left it
    unspecified, and `Path("")` is `Path(".")` - the whole screen pointed at the repository root."""
    fallback = Path("/fallback")
    monkeypatch.setenv("MIZAN_TEST_PATH", "")
    assert app.env_path("MIZAN_TEST_PATH", fallback) == fallback
    monkeypatch.setenv("MIZAN_TEST_PATH", "   ")
    assert app.env_path("MIZAN_TEST_PATH", fallback) == fallback


def test_a_set_environment_variable_wins_over_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIZAN_TEST_PATH", "/somewhere")
    assert app.env_path("MIZAN_TEST_PATH", Path("/fallback")) == Path("/somewhere")


def test_submission_for_prefers_a_console_session_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run-0123456789ab"
    run.mkdir()
    staged = run / "submission"
    staged.mkdir()
    session_dir = tmp_path / "from-console"
    session_dir.mkdir()

    import streamlit as st

    monkeypatch.setitem(st.session_state, app.SESSION_SUBMISSION, str(session_dir))

    assert app.submission_for(run) == session_dir


def test_submission_for_falls_back_to_the_runs_own_staged_submission(tmp_path: Path) -> None:
    run = tmp_path / "run-0123456789ab"
    staged = run / "submission"
    staged.mkdir(parents=True)

    assert app.submission_for(run) == staged


def test_submission_for_falls_back_to_the_environment_then_the_demo_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run-0123456789ab"  # no submission/ staged under it

    monkeypatch.delenv("MIZAN_SUBMISSION", raising=False)
    assert app.submission_for(run) == app.DEFAULT_SUBMISSION

    monkeypatch.setenv("MIZAN_SUBMISSION", str(SUBMISSION))
    assert app.submission_for(run) == SUBMISSION


def test_importing_the_screen_renders_nothing(tmp_path: Path) -> None:
    """A smoke check, and the guard that makes it one.

    `main()` used to run at module level, so importing the app rendered every finding of whatever
    run was latest — opening PDFs and rasterising pages — and **raised outright** on a verdict that
    no longer validated, taking all of this module's tests with it. Streamlit execs the file with
    `__name__ == "__main__"`, so the screen still runs under `make review`.

    Tested in a subprocess against exactly that: an artifacts directory whose `verdict.json` does
    not validate. Monkeypatching `main` cannot test this, because reloading the module rebinds the
    name before the module-level call would reach it — the first attempt at this test passed with
    the guard deleted.

    The version before that asserted `ARTIFACTS.name == "artifacts"`, which fails for any developer
    with `MIZAN_ARTIFACTS` exported — a test that breaks on an environment variable it does not own.
    """
    import os
    import subprocess
    import sys

    artifacts = tmp_path / "artifacts"
    (artifacts / "run-0123456789ab").mkdir(parents=True)
    (artifacts / "run-0123456789ab" / VERDICT_FILE).write_text('{"not": "a verdict"}')

    finished = subprocess.run(
        [sys.executable, "-c", "import tda.review.app"],
        env={**os.environ, "MIZAN_ARTIFACTS": str(artifacts)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr[-800:]
    assert "ValidationError" not in finished.stderr


# ── the defects a review found ──────────────────────────────────────────────


def test_evidence_from_another_submission_is_refused_rather_than_shown(tmp_path: Path) -> None:
    """The worst thing this screen could do, and it took one environment variable.

    `make run SUBMISSION=/data/hotel-x` then `make review` pointed the crop at the demo corpus and
    captioned it with this run's citations — a different property's rows, outlined in red, under a
    correct citation, with `Evidence.complete` true and nothing said. `run.json` was already
    recording the digest that makes it detectable.
    """
    elsewhere = tmp_path / "another-submission"
    elsewhere.mkdir()
    # A file of the same name, different bytes — exactly the case a name check misses.
    (elsewhere / REPORT.name).write_bytes(REPORT.read_bytes() + b"%not the same file\n")
    inputs = (InputFile.of(REPORT),)

    unchecked = evidence_for(material_finding(), elsewhere)
    checked = evidence_for(material_finding(), elsewhere, inputs)

    assert unchecked.source_png is not None, "without the ledger there is nothing to check against"
    assert checked.source_png is None
    assert not checked.complete
    assert any("not the file this run read" in problem for problem in checked.missing)


def test_the_real_submission_passes_the_check() -> None:
    """The guard must not fire on the ordinary case, or somebody turns it off."""
    evidence = evidence_for(material_finding(), SUBMISSION, (InputFile.of(REPORT),))

    assert evidence.source_png is not None
    assert evidence.missing == ()


def test_the_workbook_shown_is_the_one_the_run_read(tmp_path: Path) -> None:
    """Two implementations of "which workbook is the submission's": the pipeline prefers
    `claims_<period>.xlsx` and the screen used to take whatever sorted first. A submission holding
    `amended_claims_2026-Q1.xlsx` beside it had the pipeline verify one file and the screen show
    cells from the other, under a correct-looking caption."""
    submission = tmp_path / "submission"
    submission.mkdir()
    shutil.copy2(WORKBOOK, submission / WORKBOOK.name)
    decoy = submission / "amended_claims_2026-Q1.xlsx"
    book = load_workbook(WORKBOOK)
    book["Occupancy"]["B5"] = 9999
    book.save(decoy)
    assert sorted(p.name for p in submission.glob("*.xlsx"))[0] == decoy.name

    inputs = (InputFile.of(submission / WORKBOOK.name),)
    evidence = evidence_for(material_finding(), submission, inputs)

    assert evidence.cell is not None
    assert evidence.cell.value_at("B", 5) == "1285"


def test_the_reviewers_name_survives_the_artefact_that_records_it(reviewed_run: Path) -> None:
    """Redaction inverted FR-11.

    `tda.obs.redact` fires `titled_name` on `Dr. Jane Doe` and `email` on an address, so a verdict
    used to record its reviewer as `[redacted:titled_name]` — an accountability record saying a
    button was pressed by nobody, with two titled reviewers indistinguishable. The name is typed
    into this system, by that reviewer, for the express purpose of being recorded; redaction is for
    text this system copied out of somebody else's file.
    """
    recorded = record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.ACCEPT,
        reviewer="Dr. Jane Doe",
    )

    on_disk = json.loads((reviewed_run / VERDICT_FILE).read_text(encoding="utf-8"))
    assert on_disk["review_records"][0]["reviewer"] == "Dr. Jane Doe"
    assert recorded.verdict.review_records[0].reviewer == "Dr. Jane Doe"

    memo = [p.text for p in DocxDocument(str(reviewed_run / "memo.docx")).paragraphs]
    assert any("Dr. Jane Doe" in line for line in memo)
    assert not any("redacted" in line for line in memo)


def test_the_rest_of_the_verdict_is_still_redacted(tmp_path: Path) -> None:
    """The exemption is `review_records` and nothing else."""
    leaked = "Prepared by " + "Ms" + ". Jane Doe"
    run = tmp_path / "run-0123456789ab"
    write_outputs(
        run,
        verdict_with(
            material_finding(),
            extraction=ExtractionSummary(
                files=("pms_2026-01.pdf",),
                records_extracted=1,
                pages_read=1,
                printed_total_matched=True,
                duplicate_ids=0,
                unmapped_labels=(leaked,),
            ),
        ),
        [],
        WORKBOOK,
    )

    record_decision(
        run, finding_id="F-0001", decision=ReviewerDecision.ACCEPT, reviewer="Dr. Jane Doe"
    )

    text = (run / VERDICT_FILE).read_text(encoding="utf-8")
    assert "[redacted:titled_name]" in text
    assert text.count("Jane Doe") == 1, "only the reviewer's own name survives"


def test_a_decision_is_not_recorded_at_all_if_the_memo_cannot_follow(
    reviewed_run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict is written first and the memo second, so a failure in between used to leave the
    exact disagreement this module exists to prevent — a decision in `verdict.json` beside a memo
    saying nobody had looked, with the officer seeing an error and reasonably concluding nothing
    was recorded. A decision is now in both artefacts or in neither."""
    before = (reviewed_run / VERDICT_FILE).read_bytes()

    def refuse(*_args: object, **_kwargs: object) -> Path:
        raise PermissionError("memo.docx is open in Word")

    monkeypatch.setattr("tda.review.decisions.write_memo", refuse)

    with pytest.raises(ReviewError, match="was not recorded"):
        record_decision(
            reviewed_run,
            finding_id="F-0001",
            decision=ReviewerDecision.ACCEPT,
            reviewer="A. Officer",
        )

    assert (reviewed_run / VERDICT_FILE).read_bytes() == before


def test_a_definitional_item_cannot_be_amended_by_the_gate(reviewed_run: Path) -> None:
    """The rule used to be a `disabled=` on a Streamlit button, so it held for the surface that
    implemented it and not for the gate — PRD-91's own console fallback would have recorded a
    corrected figure against a policy difference, telling a hotel to change a number that is not
    wrong."""
    with pytest.raises(ReviewError, match="cannot be amended"):
        record_decision(
            reviewed_run,
            finding_id="F-0002",
            decision=ReviewerDecision.AMEND,
            reviewer="A. Officer",
            amended_value=Decimal("71.20"),
        )


def test_an_amended_value_on_a_decision_that_is_not_an_amendment_is_refused(
    reviewed_run: Path,
) -> None:
    """A `ReviewError`, not a pydantic `ValidationError`: the distinct type exists so a UI can tell
    "you filled in the wrong box" apart from a failure to write the file."""
    with pytest.raises(ReviewError, match="must not carry an amended value"):
        record_decision(
            reviewed_run,
            finding_id="F-0001",
            decision=ReviewerDecision.ACCEPT,
            reviewer="A. Officer",
            amended_value=Decimal("1285"),
        )


@pytest.mark.parametrize("figure", ["NaN", "Infinity", "-Infinity"])
def test_an_amended_value_that_is_not_a_figure_is_refused(reviewed_run: Path, figure: str) -> None:
    """`Decimal("NaN")` does not raise on construction, and `ReviewRecord.amended_value` is a
    string. A memo instructing a property to restate its return to `NaN` is not a correction."""
    with pytest.raises(ReviewError, match="not a figure"):
        record_decision(
            reviewed_run,
            finding_id="F-0001",
            decision=ReviewerDecision.AMEND,
            reviewer="A. Officer",
            amended_value=Decimal(figure),
        )


@pytest.mark.parametrize("name", ["A" * 200, "A. Officer\nB. Supervisor"])
def test_a_reviewer_name_that_is_not_one_is_refused(reviewed_run: Path, name: str) -> None:
    """A signature block and a JSON field both have to hold it. Unbounded, a paste of an entire
    email thread ends up in the memo."""
    with pytest.raises(ReviewError, match="is not a name"):
        record_decision(
            reviewed_run,
            finding_id="F-0001",
            decision=ReviewerDecision.ACCEPT,
            reviewer=name,
        )


def test_the_memo_prints_the_figure_an_amendment_proposes(reviewed_run: Path) -> None:
    """Without it the forwarded memo reads "amend by X" and leaves the hotel nothing to restate the
    return to — which is the condition `record_decision` refuses an amendment for."""
    record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.AMEND,
        reviewer="A. Officer",
        amended_value=Decimal("1285"),
    )

    memo = [p.text for p in DocxDocument(str(reviewed_run / "memo.docx")).paragraphs]
    assert any("amend → 1285" in line for line in memo)


def test_a_superseded_decision_is_noted_in_the_memo(reviewed_run: Path) -> None:
    """The standing decision is what the memo prints, and a changed mind is said rather than
    hidden. This half of `Verdict.standing_decisions` had no coverage."""
    record_decision(
        reviewed_run, finding_id="F-0001", decision=ReviewerDecision.ACCEPT, reviewer="A. Officer"
    )
    record_decision(
        reviewed_run,
        finding_id="F-0001",
        decision=ReviewerDecision.REJECT,
        reviewer="B. Supervisor",
    )

    memo = [p.text for p in DocxDocument(str(reviewed_run / "memo.docx")).paragraphs]
    lines = [line for line in memo if line.startswith("F-0001")]
    assert len(lines) == 1, "the memo prints the standing decision, not every record"
    assert "reject by B. Supervisor" in lines[0]
    assert "superseded 1 earlier decision(s)" in lines[0]
