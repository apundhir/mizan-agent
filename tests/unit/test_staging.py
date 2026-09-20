"""Uploads become a submission `discover()` recognises, or a named refusal - never a guess.

**Nothing is inferred from content.** A report's month comes from its own file name, matching
`tda.extract.run.month_of`'s own rule - the two claims below (a month-less name refused, two
reports for one month refused) are the same discipline `tda.graph.intake.check_period` already
enforces once a file is staged, applied one step earlier, before staging even happens.

**A refusal names exactly which file and why.** An officer who uploaded the wrong thing for a role
needs the file name in the message, not a stack trace.
"""

from __future__ import annotations

import io
import zipfile
from typing import TYPE_CHECKING

import openpyxl
import pytest
from reportlab.pdfgen import canvas as pdf_canvas

from tda.contracts import Period
from tda.review.staging import (
    MANIFEST_FILE,
    MAX_PDF_PAGES,
    MAX_REPORTS,
    MAX_UPLOAD_BYTES,
    MAX_XLSX_DECOMPRESSED_BYTES,
    SUBMISSION_DIR,
    Upload,
    UploadError,
    month_in_name,
    role_names,
    stage_upload,
    write_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path

PERIOD = Period.parse("2026-Q1")


def workbook(name: str = "hotel export.xlsx") -> Upload:
    """A minimal, genuinely readable `.xlsx` - staging opens the archive to bound its
    decompressed size (H-1 of the G5 security review), so a magic-bytes-only fake no longer
    reaches `role_names`/`stage_upload` at all."""
    buffer = io.BytesIO()
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet["A1"] = "placeholder"
    book.save(buffer)
    return Upload(name=name, data=buffer.getvalue())


def report(name: str, *, pages: int = 1) -> Upload:
    """A minimal, genuinely readable `.pdf` of `pages` pages - staging counts them (M-1 of the
    same review)."""
    buffer = io.BytesIO()
    drawing = pdf_canvas.Canvas(buffer)
    for _ in range(pages):
        drawing.drawString(40, 40, "placeholder")
        drawing.showPage()
    drawing.save()
    return Upload(name=name, data=buffer.getvalue())


def inventory(name: str = "rooms.csv") -> Upload:
    return Upload(name=name, data=b"room,status\n101,IN\n")


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def _minimal_xlsx(sheet_xml: str) -> bytes:
    """A hand-built, structurally complete `.xlsx` around one worksheet body. `role_names` opens
    the archive with `openpyxl` itself as well as with `zipfile`, and `openpyxl` needs the parts a
    real workbook carries - content types, relationships, a workbook part - to open at all."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


# ── month_in_name ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "month"),
    [
        ("pms_2026-01.pdf", "2026-01"),
        ("Hotel January 2026-01 export.pdf", "2026-01"),
        ("2026-01_report_final.pdf", "2026-01"),
        ("report (2026-02).pdf", "2026-02"),
    ],
)
def test_month_in_name_finds_a_real_month_wherever_it_sits(name: str, month: str) -> None:
    assert month_in_name(name) == month


@pytest.mark.parametrize(
    "name",
    [
        "monthly report.pdf",
        "report_2026.pdf",
        "report_2026-Q1.pdf",
        "report_2026-13.pdf",
        "report_9999-99.pdf",
    ],
)
def test_month_in_name_is_none_when_nothing_reads_as_a_real_month(name: str) -> None:
    assert month_in_name(name) is None


# ── role_names ───────────────────────────────────────────────────────────────


def test_role_names_targets_every_upload_by_the_convention_discover_expects() -> None:
    targets = role_names(
        PERIOD,
        workbook(),
        [report("jan (2026-01).pdf"), report("feb (2026-02).pdf")],
        inventory(),
    )

    assert set(targets) == {
        "claims_2026-Q1.xlsx",
        "pms_2026-01.pdf",
        "pms_2026-02.pdf",
        "inventory_2026-Q1.csv",
    }


def test_inventory_is_optional() -> None:
    targets = role_names(PERIOD, workbook(), [report("2026-01.pdf")], None)
    assert "inventory_2026-Q1.csv" not in targets
    assert set(targets) == {"claims_2026-Q1.xlsx", "pms_2026-01.pdf"}


def test_no_reports_is_refused() -> None:
    with pytest.raises(UploadError, match="no monthly report"):
        role_names(PERIOD, workbook(), [], None)


def test_a_report_whose_name_carries_no_month_is_refused_not_guessed() -> None:
    with pytest.raises(UploadError, match=r"scan\.pdf.*does not carry a month"):
        role_names(PERIOD, workbook(), [report("scan.pdf")], None)


def test_two_reports_for_one_month_are_refused() -> None:
    with pytest.raises(UploadError, match="both carry '2026-01'"):
        role_names(
            PERIOD,
            workbook(),
            [report("jan-copy-1 (2026-01).pdf"), report("jan-copy-2 (2026-01).pdf")],
            None,
        )


def test_a_wrong_extension_is_refused() -> None:
    with pytest.raises(UploadError, match=r"must be a \.xlsx file"):
        role_names(PERIOD, workbook("claims.csv"), [report("2026-01.pdf")], None)
    with pytest.raises(UploadError, match=r"must be a \.pdf file"):
        role_names(PERIOD, workbook(), [report("2026-01.docx")], None)
    with pytest.raises(UploadError, match=r"must be a \.csv file"):
        role_names(PERIOD, workbook(), [report("2026-01.pdf")], inventory("rooms.xlsx"))


def test_the_extension_check_is_case_insensitive() -> None:
    targets = role_names(PERIOD, workbook("EXPORT.XLSX"), [report("REPORT (2026-01).PDF")], None)
    assert set(targets) == {"claims_2026-Q1.xlsx", "pms_2026-01.pdf"}


def test_an_oversize_upload_is_refused() -> None:
    huge = Upload(name="claims.xlsx", data=b"0" * (MAX_UPLOAD_BYTES + 1))
    with pytest.raises(UploadError, match="over the"):
        role_names(PERIOD, huge, [report("2026-01.pdf")], None)


def test_a_decompression_bomb_disguised_as_a_workbook_is_refused() -> None:
    """`MAX_UPLOAD_BYTES` bounds the compressed upload; nothing about a small file proves its
    content is small once unzipped, and `tda.excel.run.open_submission` loads a workbook twice
    with `read_only=False`. One repeated byte compresses to almost nothing, so this archive clears
    the upload cap by a wide margin while its declared content does not."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"0" * (MAX_XLSX_DECOMPRESSED_BYTES + 1))
    bomb = Upload(name="claims.xlsx", data=buffer.getvalue())
    assert len(bomb.data) < MAX_UPLOAD_BYTES, "the compressed size must still clear the byte cap"
    with pytest.raises(UploadError, match="decompresses to"):
        role_names(PERIOD, bomb, [report("2026-01.pdf")], None)


def test_a_cell_dense_or_range_expanding_workbook_under_the_byte_cap_is_accepted_here() -> None:
    """What staging no longer tries to catch, on purpose. Four rounds of a G5 security review
    tried to bound what `openpyxl` builds *from* a workbook's text here - a real cell count, a
    namespace-prefixed tag, a merge needing no `<c>` element, a dimension hint rescanned rather
    than trusted - and a fifth round measured that the guessing itself, run unsandboxed in this
    process to make the guess, cost several seconds of CPU and hundreds of megabytes on a file
    sized to slip under the byte cap anyway. None of the three constructs below - a namespace
    prefix, a merge covering far more cells than are declared, a `<dimension>` that understates
    real content - are refused here any more; `tests/unit/test_sandbox.py` proves what actually
    contains them now: the resource-limited subprocess `open_submission` itself runs inside,
    regardless of which OOXML construct a file uses to reach it. See ADR-0010 §7."""
    prefixed_cells = "".join(f'<x:c r="A{n}"><x:v>1</x:v></x:c>' for n in range(1, 200))
    prefixed = _minimal_xlsx(
        '<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<x:sheetData><x:row>{prefixed_cells}</x:row></x:sheetData></x:worksheet>"
    )
    merged = _minimal_xlsx(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row><c r="A1"><v>1</v></c></row></sheetData>'
        '<mergeCells count="1"><mergeCell ref="A1:XFD200"/></mergeCells></worksheet>'
    )
    lying_dimension = _minimal_xlsx(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<dimension ref="A1:A1"/>'
        '<sheetData><row><c r="A1"><v>1</v></c></row></sheetData></worksheet>'
    )
    for payload in (prefixed, merged, lying_dimension):
        upload = Upload(name="claims.xlsx", data=payload)
        assert len(payload) < MAX_UPLOAD_BYTES
        targets = role_names(PERIOD, upload, [report("2026-01.pdf")], None)
        assert "claims_2026-Q1.xlsx" in targets


def test_a_workbook_that_is_not_really_a_zip_archive_is_refused() -> None:
    """The magic bytes are the first four bytes of the zip format, and a corrupt or truncated
    file can start with them without being an archive `zipfile` can open at all."""
    corrupt = Upload(name="claims.xlsx", data=b"PK\x03\x04" + b"not actually a zip archive")
    with pytest.raises(UploadError, match=r"not a readable \.xlsx archive"):
        role_names(PERIOD, corrupt, [report("2026-01.pdf")], None)


def test_a_report_over_the_page_cap_is_refused() -> None:
    """`tda.extract.pdf` walks every page with no cap of its own, built to trust `corpus/demo/` -
    an upload with no such history needs the cap staging owns instead."""
    too_long = report("2026-01.pdf", pages=MAX_PDF_PAGES + 1)
    with pytest.raises(UploadError, match="page limit"):
        role_names(PERIOD, workbook(), [too_long], None)


def test_a_report_that_is_not_really_a_pdf_is_refused() -> None:
    corrupt = Upload(name="2026-01.pdf", data=b"%PDF-" + b"not actually a pdf" * 5)
    with pytest.raises(UploadError, match=r"not a readable \.pdf file"):
        role_names(PERIOD, workbook(), [corrupt], None)


def test_content_that_does_not_match_the_name_is_refused() -> None:
    """The extension is what a viewer typed; the magic bytes are what the file actually is. A
    `.pdf` that opens with neither signature is refused on content, not on the name it was given."""
    not_really_a_pdf = Upload(name="2026-01.pdf", data=b"just some bytes, not a PDF at all")
    with pytest.raises(UploadError, match="does not start like a"):
        role_names(PERIOD, workbook(), [not_really_a_pdf], None)

    not_really_a_workbook = Upload(name="claims.xlsx", data=b"just some bytes")
    with pytest.raises(UploadError, match="does not start like a"):
        role_names(PERIOD, not_really_a_workbook, [report("2026-01.pdf")], None)


def test_a_csv_is_not_content_checked_since_nothing_structural_distinguishes_one() -> None:
    """Plain text has no magic bytes - the inventory role is still name- and size-checked, just
    not content-checked, and that limit is real rather than an oversight."""
    plain_text = Upload(name="rooms.csv", data=b"anything at all")
    targets = role_names(PERIOD, workbook(), [report("2026-01.pdf")], plain_text)
    assert "inventory_2026-Q1.csv" in targets


def test_more_reports_than_a_period_could_cover_is_refused() -> None:
    many = [report(f"r{i} (2026-0{(i % 9) + 1}).pdf") for i in range(MAX_REPORTS + 1)]
    with pytest.raises(UploadError, match="at most"):
        role_names(PERIOD, workbook(), many, None)


# ── stage_upload ─────────────────────────────────────────────────────────────


def test_stage_upload_writes_every_target_under_submission(tmp_path: Path) -> None:
    destination = stage_upload(
        tmp_path, PERIOD, workbook(), [report("2026-01.pdf"), report("2026-02.pdf")], inventory()
    )

    assert destination == tmp_path / SUBMISSION_DIR
    assert sorted(p.name for p in destination.iterdir()) == [
        "claims_2026-Q1.xlsx",
        "inventory_2026-Q1.csv",
        "pms_2026-01.pdf",
        "pms_2026-02.pdf",
    ]
    assert (destination / "claims_2026-Q1.xlsx").read_bytes() == workbook().data


def test_stage_upload_refuses_before_writing_anything(tmp_path: Path) -> None:
    """A rejected upload leaves no partial submission directory behind for the pipeline to find
    half-populated on a retry."""
    with pytest.raises(UploadError):
        stage_upload(tmp_path, PERIOD, workbook("claims.csv"), [report("2026-01.pdf")], None)

    assert not (tmp_path / SUBMISSION_DIR).exists()


# ── write_manifest ───────────────────────────────────────────────────────────


def test_the_manifest_sits_beside_the_submission_and_the_cli_reads_it_back(tmp_path: Path) -> None:
    from tda.cli import declaration_from_manifest

    stage_upload(tmp_path, PERIOD, workbook(), [report("2026-01.pdf")], None)
    write_manifest(tmp_path, "MZN-DXB-001", str(PERIOD))

    assert (tmp_path / MANIFEST_FILE).exists()
    declared = declaration_from_manifest(tmp_path / SUBMISSION_DIR)
    assert declared == ("MZN-DXB-001", "2026-Q1")
