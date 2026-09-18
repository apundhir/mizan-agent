"""The three artefacts: what each one must carry, and the two things neither may do.

**A file must not be able to misreport its own contents.** `verdict.json` carries a summary block,
because the reader who most needs the counts will not derive them — and `VerdictDocument` recomputes
that block on load and refuses a document whose summary disagrees with its own arrays. A hand-edited
verdict claiming three material findings over a list of five does not load.

**The submitted workbook is never modified.** It is the hotel's evidence, and the comparison has to
stay re-runnable against it. `annotate` digests the original before and after and raises if the two
differ, so the promise is a postcondition rather than an intention. The test below proves the check
works by comparing digests itself.

Beyond that, each test corresponds to one line of PRD-92: the colour of each cell class, the
comment contents each colour must carry, the order of the memo's blocks, and the separation of
definitional items from findings — which is D-MAT-06 again, in a document this time. A policy
disagreement printed in a table headed "Findings" is a correct finding that reads as an accusation.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from docx import Document as DocxDocument
from openpyxl import load_workbook

from tda.contracts import (
    Claim,
    EscalationTarget,
    ExcelRef,
    ExtractionSummary,
    Finding,
    Metric,
    MetricKey,
    NotVerifiable,
    PdfRef,
    ReviewerDecision,
    ReviewRecord,
    Severity,
    VarianceClass,
    Verdict,
    VerdictStatus,
)
from tda.excel.selfcheck import no_pdf
from tda.obs.ledger import file_digest
from tda.outputs import write_outputs
from tda.outputs.memo import MEMO_FILE, write_memo
from tda.outputs.ooxml import CORE_PROPERTIES, EPOCH
from tda.outputs.verdict import VERDICT_FILE, VerdictDocument, read_verdict, write_verdict
from tda.outputs.workbook import (
    BLOCKING,
    COMMENT_LIMIT,
    DEFINITIONAL,
    LEGEND_SHEET,
    MATERIAL,
    OTHER,
    VERIFIED,
    OriginalModifiedError,
    _check_untouched,
    annotate,
)

if TYPE_CHECKING:
    from openpyxl.cell.cell import Cell

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "demo"
SUBMITTED = CORPUS / "submission" / "claims_2026-Q1.xlsx"
HOTEL = "MZN-DXB-001"


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    """A copy of the corpus workbook, because a test must not annotate the repository's fixture.

    Found the hard way while mutation-testing the digest check: a mutant that wrote through to the
    original left `corpus/demo/` dirty, and `make corpus` verifies that exact file against its
    generator. A regression here would corrupt a tracked fixture as a side effect of `make test`.
    """
    destination = tmp_path / "submission" / SUBMITTED.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SUBMITTED, destination)
    return destination


# ── fixtures: one finding of each shape the artefacts must draw ──────────────


def material_finding() -> Finding:
    """A clerical error with a correction to propose. Red, and the only class that may propose."""
    return Finding(
        finding_id="F-0001",
        key=MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period="2026-01"),
        variance_class=VarianceClass.TRANSCRIPTION,
        severity=Severity.MATERIAL,
        escalates_to=EscalationTarget.HOTEL,
        claimed=Decimal("1295"),
        computed=Decimal("1285"),
        difference=Decimal("10"),
        proposed_correction=Decimal("1285"),
        source_ref=PdfRef(file="pms_2026-01.pdf", page=4, row_start=12, row_end=18),
        excel_ref=ExcelRef(sheet="Occupancy", cell="B5"),
        clause="D-RNS-01",
    )


def definitional_finding() -> Finding:
    """A policy difference. Amber, and it must name what reproduces the claim."""
    return Finding(
        finding_id="F-0002",
        key=MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-02"),
        variance_class=VarianceClass.DEFINITIONAL,
        severity=Severity.MATERIAL,
        escalates_to=EscalationTarget.POLICY_OWNER,
        claimed=Decimal("74.80"),
        computed=Decimal("71.20"),
        difference=Decimal("3.60"),
        explaining_permutation="P-OOO-INCLUDED",
        also_explained_by=("P-MONTH-ARRIVAL",),
        source_ref=PdfRef(file="pms_2026-02.pdf", page=2, row_start=1, row_end=40),
        excel_ref=ExcelRef(sheet="Occupancy", cell="D6"),
        clause="D-CLS-07",
    )


def blocking_finding() -> Finding:
    """Something could not be checked. Blue, and it has a cell here — the unplaced case is below."""
    return Finding(
        finding_id="F-0003",
        key=MetricKey(metric=Metric.ROOM_NIGHTS_AVAILABLE, period="2026-03"),
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        source_ref=PdfRef(file="pms_2026-03.pdf", page=1, row_start=1, row_end=1),
        excel_ref=ExcelRef(sheet="Occupancy", cell="C7"),
        clause="D-XLS-06",
    )


def unplaced_finding() -> Finding:
    """A refusal with no cell at all. It cannot be drawn, which is exactly why it must be said."""
    return Finding(
        finding_id="F-0004",
        key=MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period="2026-03"),
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        source_ref=PdfRef(file="pms_2026-03.pdf", page=7, row_start=3, row_end=3),
        excel_ref=no_pdf("extraction halted before claim parsing"),
        clause="D-EV-02",
    )


def rounding_finding() -> Finding:
    """Neither definitional nor material. Grey — the fifth colour, and why it exists."""
    return Finding(
        finding_id="F-0005",
        key=MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-03"),
        variance_class=VarianceClass.ROUNDING,
        severity=Severity.INFORMATIONAL,
        escalates_to=EscalationTarget.NONE,
        claimed=Decimal("69.14"),
        computed=Decimal("69.09"),
        difference=Decimal("0.05"),
        source_ref=PdfRef(file="pms_2026-03.pdf", page=1, row_start=1, row_end=30),
        excel_ref=ExcelRef(sheet="Occupancy", cell="D8"),
        clause="D-MAT-02",
    )


def verdict_with(*findings: Finding, **overrides: object) -> Verdict:
    """An ESCALATED verdict carrying whatever findings a test needs."""
    definitional = tuple(f for f in findings if f.variance_class is VarianceClass.DEFINITIONAL)
    ordinary = tuple(f for f in findings if f.variance_class is not VarianceClass.DEFINITIONAL)
    fields: dict[str, object] = {
        "run_id": "run-0123456789ab",
        "status": VerdictStatus.ESCALATED if findings else VerdictStatus.PASS,
        "hotel_id": HOTEL,
        "period": "2026-Q1",
        "policy_version": "1.3.0",
        "metric_library_version": "1.0.0",
        "model_id": "claude-sonnet-5",
        "provider_mode": "stub",
        "prompt_versions": {"mapping": "v1"},
        "extraction": ExtractionSummary(
            files=("pms_2026-01.pdf",),
            records_extracted=1200,
            pages_read=30,
            printed_total_matched=True,
            duplicate_ids=0,
        ),
        "claims_checked": 94,
        "findings": ordinary,
        "definitional_items": definitional,
    }
    fields.update(overrides)
    return Verdict(**fields)  # type: ignore[arg-type]


def claim_at(sheet: str, cell: str, metric: Metric = Metric.ROOM_NIGHTS_SOLD) -> Claim:
    return Claim(
        key=MetricKey(metric=metric, period="2026-01"),
        value=Decimal("1285"),
        excel_ref=ExcelRef(sheet=sheet, cell=cell),
    )


def fill_of(cell: Cell) -> str:
    """The cell's fill colour as six hex digits, or `""` for no fill."""
    rgb = getattr(cell.fill.fgColor, "rgb", None)
    return "" if not isinstance(rgb, str) or rgb == "00000000" else rgb[-6:]


# ── verdict.json ────────────────────────────────────────────────────────────


def test_the_verdict_file_carries_everything_prd_92_names(tmp_path: Path) -> None:
    """Each of these is a line of the acceptance criteria, asserted against the file rather than
    against the model — the file is what a downstream system actually receives."""
    verdict = verdict_with(material_finding(), definitional_finding())
    path, _ = write_verdict(tmp_path, verdict)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["policy_version"] == "1.3.0"
    assert payload["metric_library_version"] == "1.0.0"
    assert payload["extraction"]["records_extracted"] == 1200
    assert payload["claims_checked"] == 94
    assert payload["status"] in {s.value for s in VerdictStatus}
    assert payload["summary"]["by_severity"] == {"blocking": 0, "informational": 0, "material": 2}

    # Full evidence on every finding, both sides.
    finding = payload["findings"][0]
    assert finding["source_ref"]["page"] == 4
    assert finding["excel_ref"] == {"sheet": "Occupancy", "cell": "B5"}
    assert finding["clause"] == "D-RNS-01"


def test_definitional_items_are_a_separate_array_in_the_file(tmp_path: Path) -> None:
    """D-MAT-06, in the artefact. A consumer that iterates `findings` and counts hotel errors gets
    the right answer by default rather than by remembering to filter."""
    path, _ = write_verdict(tmp_path, verdict_with(material_finding(), definitional_finding()))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert [f["finding_id"] for f in payload["findings"]] == ["F-0001"]
    assert [f["finding_id"] for f in payload["definitional_items"]] == ["F-0002"]
    assert payload["summary"]["hotel_errors"] == 1
    assert payload["summary"]["definitional_items"] == 1


def test_a_verdict_file_that_misreports_its_own_contents_does_not_load(tmp_path: Path) -> None:
    """The failure this prevents is quiet and expensive: somebody edits the findings out, the
    summary keeps saying five, and every reader believes the summary because reading it is cheaper
    than counting."""
    path, _ = write_verdict(tmp_path, verdict_with(material_finding()))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["summary"]["hotel_errors"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match the verdict it summarises"):
        read_verdict(path)


def test_a_written_verdict_reads_back_as_what_was_written(tmp_path: Path) -> None:
    """The round trip `computed_field` would have broken: a dump carrying computed fields fails to
    re-validate under `extra="forbid"`, and a write-only artefact is a strange thing for the file
    that exists to be read."""
    verdict = verdict_with(material_finding(), definitional_finding(), blocking_finding())
    path, _ = write_verdict(tmp_path, verdict)

    back = read_verdict(path)

    assert back.status is VerdictStatus.ESCALATED
    assert [f.finding_id for f in back.findings] == ["F-0001", "F-0003"]
    assert [f.finding_id for f in back.definitional_items] == ["F-0002"]


def test_two_writes_of_one_verdict_are_byte_identical(tmp_path: Path) -> None:
    """What PRD-94's repro diff compares. Sorted keys, no timestamp of its own."""
    verdict = verdict_with(material_finding())
    first, _ = write_verdict(tmp_path / "a", verdict)
    second, _ = write_verdict(tmp_path / "b", verdict)

    assert first.read_bytes() == second.read_bytes()


def test_personal_data_in_an_unmapped_label_is_redacted_from_the_verdict(tmp_path: Path) -> None:
    """`ExtractionSummary.unmapped_labels` is label text copied from the submitted workbook, so it
    carries whatever the property typed into a header cell."""
    leaked = "Prepared by " + "Ms" + ". Jane Doe"
    verdict = verdict_with(
        extraction=ExtractionSummary(
            files=("pms_2026-01.pdf",),
            records_extracted=1,
            pages_read=1,
            printed_total_matched=True,
            duplicate_ids=0,
            unmapped_labels=(leaked,),
        )
    )

    path, redaction = write_verdict(tmp_path, verdict)

    assert "Jane" not in path.read_text(encoding="utf-8")
    assert "Doe" not in path.read_text(encoding="utf-8")
    assert dict(redaction.counts) == {"titled_name": 1}


# ── the annotated workbook ──────────────────────────────────────────────────


def test_the_submitted_workbook_is_not_modified(tmp_path: Path, workbook: Path) -> None:
    """The guarantee PRD-92 asks for, checked the way the code checks it. It is the hotel's
    evidence: the comparison must stay re-runnable against it, and nobody should have to ask
    whether the tool changed what it was judging."""
    before = file_digest(workbook)

    annotate(workbook, verdict_with(material_finding()), [claim_at("Occupancy", "B6")], tmp_path)

    assert file_digest(workbook) == before


def test_the_annotated_copy_is_a_copy_and_carries_the_legend_first(
    tmp_path: Path, workbook: Path
) -> None:
    annotated = annotate(workbook, verdict_with(), [], tmp_path)

    assert annotated.path.name == f"annotated_{SUBMITTED.name}"
    assert annotated.path != workbook
    book = load_workbook(annotated.path)
    assert book.sheetnames[0] == LEGEND_SHEET
    # And the hotel's own sheets are all still there, in order.
    assert book.sheetnames[1:] == load_workbook(workbook).sheetnames


def test_every_colour_says_the_thing_prd_92_says_it_says(tmp_path: Path, workbook: Path) -> None:
    """One assertion per colour, and the fifth is grey.

    Green on a cell that produced a finding would tell a reader it verified clean; red on a
    rounding artefact would escalate it into a material variance. A colour that overstates is how a
    correct system produces an incorrect letter.
    """
    verdict = verdict_with(
        material_finding(), definitional_finding(), blocking_finding(), rounding_finding()
    )
    claims = [claim_at("Occupancy", "B4"), claim_at("Occupancy", "B5")]

    annotated = annotate(workbook, verdict, claims, tmp_path)
    sheet = load_workbook(annotated.path)["Occupancy"]

    assert fill_of(sheet["B4"]) == VERIFIED.fgColor.rgb[-6:]
    assert fill_of(sheet["B5"]) == MATERIAL.fgColor.rgb[-6:]  # a finding overrides its claim
    assert fill_of(sheet["D6"]) == DEFINITIONAL.fgColor.rgb[-6:]
    assert fill_of(sheet["C7"]) == BLOCKING.fgColor.rgb[-6:]
    assert fill_of(sheet["D8"]) == OTHER.fgColor.rgb[-6:]
    # A cell nobody claimed stays uncoloured, so "not checked" is visibly different from "checked
    # and correct" - D-SCOPE-02.
    assert fill_of(sheet["A1"]) == ""


def test_a_material_cell_carries_the_computed_value_the_correction_and_the_page(
    tmp_path: Path, workbook: Path
) -> None:
    """What the next person does with a red cell is check it, and the comment is what they check
    it with."""
    annotated = annotate(workbook, verdict_with(material_finding()), [], tmp_path)
    comment = load_workbook(annotated.path)["Occupancy"]["B5"].comment.text

    assert "Computed: 1285" in comment
    assert "Proposed: 1285" in comment
    assert "pms_2026-01.pdf p.4 rows 12-18" in comment
    assert "F-0001" in comment


def test_a_definitional_cell_names_what_reproduces_the_claim(
    tmp_path: Path, workbook: Path
) -> None:
    """Without the rule named, the amber reads as an accusation about a number that is not wrong —
    and naming one cause when several fit is a false precision (D-CLS-09), so the others are there
    too."""
    annotated = annotate(workbook, verdict_with(definitional_finding()), [], tmp_path)
    comment = load_workbook(annotated.path)["Occupancy"]["D6"].comment.text

    assert "Explained by: P-OOO-INCLUDED" in comment
    assert "Also fits: P-MONTH-ARRIVAL" in comment
    assert "not a hotel error" in comment


def test_a_finding_with_no_cell_is_listed_rather_than_dropped(
    tmp_path: Path, workbook: Path
) -> None:
    """A workbook that silently omitted them would be the most misleading artefact in the set: all
    green, with the blocking findings invisible."""
    annotated = annotate(workbook, verdict_with(unplaced_finding()), [], tmp_path)

    assert annotated.unplaced_ids == ("F-0004",)
    legend = load_workbook(annotated.path)[LEGEND_SHEET]
    text = "\n".join(str(c.value) for row in legend.iter_rows() for c in row if c.value)
    assert "F-0004" in text
    assert "Findings not marked on a cell" in text
    assert "no cell exists" in text
    assert "F-0004" in annotated.render()


def test_the_legend_carries_the_verdict_and_the_rules_it_was_produced_under(
    tmp_path: Path, workbook: Path
) -> None:
    """A workbook of coloured cells with no key is a puzzle rather than a report."""
    annotated = annotate(workbook, verdict_with(material_finding()), [], tmp_path)
    legend = load_workbook(annotated.path)[LEGEND_SHEET]
    text = "\n".join(str(c.value) for row in legend.iter_rows() for c in row if c.value)

    assert "ESCALATED" in text
    assert HOTEL in text
    assert "1.3.0" in text
    assert "Silence is not approval" in text


# ── the memo ────────────────────────────────────────────────────────────────


def paragraphs(path: Path) -> list[str]:
    """The memo's paragraphs. **Not** its tables — see `text_of`."""
    return [p.text for p in DocxDocument(str(path)).paragraphs if p.text.strip()]


def text_of(path: Path) -> str:
    """Everything in the memo, tables included.

    `document.paragraphs` excludes table cells, and every finding field the memo renders lives in a
    table. A leak test written against `paragraphs` alone is blind to exactly the content it is
    supposed to be checking, which is how the first version of
    `test_the_memo_is_rendered_from_the_written_verdict_rather_than_the_one_in_memory` passed
    against a memo rendered from the unredacted verdict.
    """
    document = DocxDocument(str(path))
    cells = [c.text for t in document.tables for row in t.rows for c in row.cells]
    return "\n".join([*(p.text for p in document.paragraphs), *cells])


def tables(path: Path) -> list[list[list[str]]]:
    return [[[c.text for c in row.cells] for row in t.rows] for t in DocxDocument(str(path)).tables]


def test_the_verdict_is_in_the_first_block(tmp_path: Path) -> None:
    """A supervisor reads the first block and nothing else. If the verdict is not in it, the memo
    has failed at its job even if every subsequent page is correct."""
    path = write_memo(tmp_path, verdict_with(material_finding()))
    text = paragraphs(path)

    assert text[:3] == [
        "Quarterly verification memo",
        f"{HOTEL} · 2026-Q1 · run run-0123456789ab",
        "ESCALATED",
    ]
    # The counts are the first table, before any finding detail.
    assert tables(path)[0][0] == [
        "Claims checked",
        "Hotel errors",
        "Definitional items",
        "Not verifiable",
    ]
    assert tables(path)[0][1] == ["94", "1", "0", "0"]


def test_the_findings_table_carries_every_column_an_officer_needs(tmp_path: Path) -> None:
    """Metric, period, claimed, computed, difference, cause, Excel cell and PDF page — the row of
    a letter to a hotel, in one place."""
    path = write_memo(tmp_path, verdict_with(material_finding()))
    findings_table = tables(path)[1]

    assert findings_table[0] == [
        "Finding",
        "Metric",
        "Period",
        "Claimed",
        "Computed",
        "Difference",
        "Cause",
        "Excel cell",
        "Source",
    ]
    assert findings_table[1] == [
        "F-0001",
        "room_nights_sold",
        "2026-01",
        "1295",
        "1285",
        "10",
        "V1 · D-RNS-01",
        "Occupancy!B5",
        "pms_2026-01.pdf p.4 rows 12-18",
    ]


def test_definitional_items_are_in_their_own_section_and_never_in_the_findings_table(
    tmp_path: Path,
) -> None:
    """D-MAT-06 in a document. A policy disagreement listed among clerical errors is a correct
    finding that reads as an accusation, and no recount fixes that."""
    path = write_memo(tmp_path, verdict_with(material_finding(), definitional_finding()))
    text = paragraphs(path)
    findings_table, definitional_table = tables(path)[1], tables(path)[2]

    assert "Definitional items — not hotel errors" in text
    assert [row[0] for row in findings_table[1:]] == ["F-0001"]
    assert [row[0] for row in definitional_table[1:]] == ["F-0002"]
    assert definitional_table[0][6] == "Explained by"
    assert definitional_table[1][6] == "P-OOO-INCLUDED (also fits P-MONTH-ARRIVAL)"
    assert any("not a correction for the property" in line for line in text)


def test_the_signature_block_does_not_claim_a_review_that_did_not_happen(tmp_path: Path) -> None:
    """At the moment a run finishes there is no reviewer — the review gate is PRD-91. A memo that
    printed a name anyway would be a forged sign-off on the one page a supervisor reads."""
    path = write_memo(tmp_path, verdict_with(material_finding()))
    text = paragraphs(path)

    assert any("No human review has been recorded" in line for line in text)
    assert any("Policy version applied: 1.3.0" in line for line in text)
    assert any(line.startswith("Reviewed by:") for line in text)


def test_a_reviewed_verdict_names_who_decided_and_what_is_still_open(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    verdict = verdict_with(
        material_finding(),
        definitional_finding(),
        review_records=(
            ReviewRecord(
                finding_id="F-0001",
                decision=ReviewerDecision.ACCEPT,
                reviewer="A. Officer",
                decided_at=datetime(2026, 4, 2, 9, 30, tzinfo=UTC),
            ),
        ),
    )

    text = paragraphs(write_memo(tmp_path, verdict))

    assert any("A. Officer" in line for line in text)
    assert any("Undecided: F-0002" in line for line in text)
    assert not any("No human review" in line for line in text)


def test_what_was_not_verified_is_stated_rather_than_left_out(tmp_path: Path) -> None:
    """A memo listing only what was checked lets silence read as approval, which D-SCOPE-02 forbids
    in the verdict and which is no less wrong on paper."""
    verdict = verdict_with(
        not_verifiable=(
            NotVerifiable(
                key=MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-03"),
                reason="missing_inventory_reference",
                detail="the room inventory reference has no rows for March",
            ),
        ),
    )

    text = paragraphs(write_memo(tmp_path, verdict))

    assert "Not verified" in text
    assert any("missing_inventory_reference" in line for line in text)
    # The remedy, not only the diagnosis.
    assert any("no rows for March" in line for line in text)


# ── the three together ──────────────────────────────────────────────────────


def test_all_three_artefacts_are_written(tmp_path: Path, workbook: Path) -> None:
    written = write_outputs(
        tmp_path / "out",
        verdict_with(material_finding()),
        [claim_at("Occupancy", "B4")],
        workbook,
    )

    assert sorted(p.name for p in written.files) == sorted(
        [VERDICT_FILE, MEMO_FILE, f"annotated_{SUBMITTED.name}"]
    )
    assert all(p.is_file() for p in written.files)


def test_the_memo_is_rendered_from_the_written_verdict_rather_than_the_one_in_memory(
    tmp_path: Path, workbook: Path
) -> None:
    """The PRD-90 defect, in a Word file this time: `mizan run` printed the unredacted ledger while
    the file beside it said `[redacted:email]`. The memo is rendered from what came off the disk, so
    it cannot say more than the JSON it sits next to."""
    address = "jane.doe" + "@" + "hotel.ae"
    # Seeded into a field the memo actually renders. The first version of this test put the address
    # in `Finding.narrative`, which the memo never prints - so the assertion was true for every
    # possible input, and rendering the memo from the unredacted verdict passed it.
    verdict = verdict_with(
        material_finding(),
        not_verifiable=(
            NotVerifiable(
                key=MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-03"),
                reason="missing_inventory_reference",
                detail=f"the property was asked for it at {address}",
            ),
        ),
    )

    written = write_outputs(tmp_path / "out", verdict, [], workbook)

    assert "jane.doe" not in written.verdict.read_text(encoding="utf-8")
    # `text_of`, not `paragraphs`: the tables are where the findings are.
    assert "jane.doe" not in text_of(written.memo)
    assert "[redacted:email]" in text_of(written.memo)
    assert "[redacted:email]" in json.dumps(json.loads(written.verdict.read_text(encoding="utf-8")))
    assert dict(written.redaction.counts) == {"email": 1}


def test_a_submission_with_no_workbook_gets_no_annotated_copy_and_says_so(tmp_path: Path) -> None:
    """An intake rejection for an incomplete file set never had a workbook. A missing annotated
    copy is the correct output there, not a failure — and an empty one would be a lie."""
    written = write_outputs(tmp_path, verdict_with(), [], None)

    assert written.workbook is None
    assert "no annotated workbook" in written.render()
    assert written.memo.is_file()


def test_the_verdict_document_renders_the_same_first_block_the_memo_leads_with() -> None:
    """One wording, so the console, the memo and the review screen cannot describe one run three
    ways."""
    document = VerdictDocument.of(verdict_with(material_finding(), blocking_finding()))

    rendered = document.render()

    assert "ESCALATED" in rendered
    assert "94 claim(s) checked" in rendered
    assert "1 blocking finding(s)" in rendered


# ── the defects a review found, each with the test that would have caught it ─


def test_a_corrupt_workbook_costs_the_annotation_and_nothing_else(tmp_path: Path) -> None:
    """The case intake exists for, met a second time at the end of the run.

    `openpyxl` raises `zipfile.BadZipFile` on a corrupt `.xlsx`, and that is not an `OSError`. An
    earlier version let it out of `write_outputs`, so a submission intake had already refused as
    unreadable printed its verdict, wrote `verdict.json` and `memo.docx`, and then ended in a
    traceback — with an exit code that collides with `FINDINGS`.
    """
    corrupt = tmp_path / "claims_2026-Q1.xlsx"
    corrupt.write_bytes(b"this is not a zip file")

    written = write_outputs(tmp_path / "out", verdict_with(), [], corrupt)

    assert written.workbook is None
    assert written.workbook_error is not None
    assert "BadZipFile" in written.workbook_error
    assert "no annotated workbook: BadZipFile" in written.render()
    # The other two artefacts are unaffected: the verdict was reached and the memo says so.
    assert written.verdict.is_file()
    assert written.memo.is_file()
    # And no file named `annotated_*` is left behind claiming to be one.
    assert not list((tmp_path / "out").glob("annotated_*"))


def test_a_redacted_sheet_name_does_not_turn_a_material_finding_green(
    tmp_path: Path, workbook: Path
) -> None:
    """The inversion the review found, and the reason cells are looked up by the real reference.

    `Dr. Ahmed Occupancy` is a legal Excel sheet name and `titled_name` fires on it. Rendering the
    workbook from the redacted verdict meant the finding's sheet no longer existed, so nothing was
    drawn — while the claim underneath stayed green with "the reservation records agree" on it. A
    material variance, coloured green, by the privacy control.
    """
    renamed = load_workbook(workbook)
    renamed["Occupancy"].title = "Dr. Ahmed Occupancy"
    renamed.save(workbook)

    finding = material_finding().model_copy(
        update={"excel_ref": ExcelRef(sheet="Dr. Ahmed Occupancy", cell="B5")}
    )
    claim = claim_at("Dr. Ahmed Occupancy", "B5")

    written = write_outputs(tmp_path / "out", verdict_with(finding), [claim], workbook)

    assert written.workbook is not None
    sheet = load_workbook(written.workbook.path)["Dr. Ahmed Occupancy"]
    assert fill_of(sheet["B5"]) == MATERIAL.fgColor.rgb[-6:]
    assert "Verified" not in sheet["B5"].comment.text
    # The verdict still redacts it, and that cost is stated in ADR-0007 rather than hidden.
    assert "Ahmed" not in written.verdict.read_text(encoding="utf-8")


def test_a_finding_naming_a_sheet_that_does_not_exist_is_reported(
    tmp_path: Path, workbook: Path
) -> None:
    """It used to vanish, under a legend that then said every finding was marked on a cell. An
    incomplete artefact asserting it is complete is worse than an incomplete one."""
    finding = material_finding().model_copy(
        update={"excel_ref": ExcelRef(sheet="Ocupancy", cell="B5")}
    )

    annotated = annotate(workbook, verdict_with(finding), [], tmp_path / "out")

    assert annotated.cells_coloured == 0
    assert annotated.unplaced_ids == ("F-0001",)
    assert "does not have" in annotated.unplaced[0].reason
    legend = load_workbook(annotated.path)[LEGEND_SHEET]
    text = "\n".join(str(c.value) for row in legend.iter_rows() for c in row if c.value)
    assert "F-0001" in text
    assert "Every finding in this verdict is marked on a cell" not in text


def test_a_material_finding_beats_a_definitional_one_on_the_same_cell(
    tmp_path: Path, workbook: Path
) -> None:
    """Amber used to win by array order alone. A material variance hidden under "a policy
    difference, not a hotel error" understates, and for a fee decision that is the worse direction
    to be wrong in."""
    material = material_finding().model_copy(
        update={"excel_ref": ExcelRef(sheet="Occupancy", cell="D6")}
    )

    annotated = annotate(
        workbook, verdict_with(material, definitional_finding()), [], tmp_path / "out"
    )
    cell = load_workbook(annotated.path)["Occupancy"]["D6"]

    assert fill_of(cell) == MATERIAL.fgColor.rgb[-6:]
    assert cell.comment.text.startswith("F-0001")


def test_two_claims_in_one_cell_are_reported_rather_than_collapsed(
    tmp_path: Path, workbook: Path
) -> None:
    """A workbook asserting two figures in one cell contradicts itself, and `index_by_key` already
    refuses the same thing for duplicate keys: silently keeping the last would make a
    self-contradicting workbook look consistent."""
    annotated = annotate(
        workbook,
        verdict_with(),
        [
            claim_at("Occupancy", "B4"),
            claim_at("Occupancy", "B4", metric=Metric.OCCUPANCY_PCT),
        ],
        tmp_path / "out",
    )

    assert annotated.duplicate_cells == ("Occupancy!B4",)
    assert "carry more than one claim" in annotated.render()
    legend = load_workbook(annotated.path)[LEGEND_SHEET]
    text = "\n".join(str(c.value) for row in legend.iter_rows() for c in row if c.value)
    assert "Cells claimed twice" in text


def test_the_untouched_check_fires_when_the_original_changes(tmp_path: Path) -> None:
    """The postcondition, tested directly rather than through a path that cannot reach it.

    Nothing in `annotate` opens the original for writing, so the check cannot fire today — which is
    exactly why it is worth testing. The failure it guards against is one careless `save(original)`
    away and would be silent, and a test that only asserts the property (the digest is unchanged)
    passes with the whole check deleted.
    """
    original = tmp_path / "claims.xlsx"
    original.write_bytes(b"the bytes we were handed")
    before = file_digest(original)

    _check_untouched(original, before)  # unchanged: no complaint

    original.write_bytes(b"something else entirely")
    with pytest.raises(OriginalModifiedError, match="changed while it was being annotated"):
        _check_untouched(original, before)


def test_the_annotated_copy_never_lands_beside_the_evidence(tmp_path: Path, workbook: Path) -> None:
    """A second `.xlsx` in the submission directory is one the next run's file discovery would
    find, and a submission directory that accumulates this system's own output is no longer
    evidence."""
    with pytest.raises(OriginalModifiedError, match="refusing to write an annotated copy"):
        annotate(workbook, verdict_with(), [], workbook.parent)


def test_a_failure_part_way_through_leaves_no_file_claiming_to_be_annotated(
    tmp_path: Path, workbook: Path
) -> None:
    """The copy happens before the drawing. An officer who opens `annotated_claims.xlsx` and finds
    an unmarked workbook has no way to know it is the copy rather than the answer."""
    workbook.write_bytes(b"corrupt, after the digest was taken")
    destination = tmp_path / "out"

    with pytest.raises(zipfile.BadZipFile):
        annotate(workbook, verdict_with(), [], destination)

    assert not list(destination.glob("annotated_*"))


def test_a_comment_longer_than_excel_allows_is_truncated(tmp_path: Path, workbook: Path) -> None:
    """`Finding.narrative` is model prose and bounded nowhere upstream. Excel refuses to open a
    workbook with a note over its limit and offers to repair it, which is a worse way to find out."""
    finding = material_finding().model_copy(update={"narrative": "x" * 40_000})

    annotated = annotate(workbook, verdict_with(finding), [], tmp_path / "out")
    comment = load_workbook(annotated.path)["Occupancy"]["B5"].comment.text

    assert len(comment) <= COMMENT_LIMIT + 60
    assert "truncated" in comment
    assert "verdict.json" in comment


def test_a_workbook_that_already_has_a_legend_sheet_is_still_counted_honestly(
    tmp_path: Path, workbook: Path
) -> None:
    """The legend used to be replaced *after* the marking loop, so a mark drawn on a pre-existing
    legend sheet was destroyed and still counted. A count that includes a cell nobody can see is a
    count nobody should read."""
    existing = load_workbook(workbook)
    existing.create_sheet(LEGEND_SHEET, 0)["B5"] = "left over from a previous run"
    existing.save(workbook)

    finding = material_finding().model_copy(
        update={"excel_ref": ExcelRef(sheet=LEGEND_SHEET, cell="B5")}
    )
    annotated = annotate(workbook, verdict_with(finding), [], tmp_path / "out")

    assert annotated.cells_coloured == 0
    assert annotated.unplaced_ids == ("F-0001",)


def test_all_three_artefacts_are_byte_identical_across_two_runs(
    tmp_path: Path, workbook: Path
) -> None:
    """What `make repro` (PRD-94) will compare, for every artefact rather than only the JSON.

    Neither a `.docx` nor an `.xlsx` is reproducible by accident: both writers stamp
    `dcterms:modified` with the wall clock inside `save()` — openpyxl discards whatever the caller
    set — and every zip entry carries a DOS timestamp of its own. Two runs a second apart differ,
    and the two-second granularity of that timestamp would make the docx's stability a coin toss
    rather than a property. `tda.outputs.ooxml` repacks both afterwards.

    The sleep is deliberate: without it this test passes whether or not anything is normalised,
    because both runs land in the same second.
    """
    verdict = verdict_with(material_finding())
    claims = [claim_at("Occupancy", "B4")]

    first = write_outputs(tmp_path / "a", verdict, claims, workbook)
    time.sleep(1.1)
    second = write_outputs(tmp_path / "b", verdict, claims, workbook)

    assert first.verdict.read_bytes() == second.verdict.read_bytes()
    assert first.memo.read_bytes() == second.memo.read_bytes()
    assert first.workbook is not None
    assert second.workbook is not None
    assert first.workbook.path.read_bytes() == second.workbook.path.read_bytes()


def test_a_generated_document_carries_a_fixed_timestamp_rather_than_the_clock(
    tmp_path: Path, workbook: Path
) -> None:
    """The mechanism behind the test above, asserted directly.

    A run's real time is recorded in `run.json`, where it is marked as excluded from the repro diff
    (ADR-0006). Putting the wall clock inside a document as well would mean two identical
    verifications produce two different files and nobody can tell that from a real difference.
    """
    annotated = annotate(workbook, verdict_with(), [], tmp_path / "out")

    with zipfile.ZipFile(annotated.path) as archive:
        core = archive.read(CORE_PROPERTIES).decode()
        assert {info.date_time for info in archive.infolist()} == {EPOCH.timetuple()[:6]}

    stamps = re.findall(r"<dcterms:(?:created|modified)[^>]*>([^<]+)<", core)
    assert stamps == ["2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"]
    # The period in the workbook's own title legitimately contains a year, so the assertion is
    # about the timestamps rather than about the file not mentioning this year anywhere.
    assert str(datetime.now(UTC).year) not in "".join(stamps)


def test_the_verdict_file_needs_the_document_type_to_read_it_back(tmp_path: Path) -> None:
    """Stated rather than implied, because the docstring argues the round trip is the point.

    `Verdict` is `extra="forbid"`, so the `summary` key means the base contract cannot load the
    file. A consumer that ignores unknown keys — anything not pydantic-strict — is unaffected, and
    inside this repository the type to read it with is `VerdictDocument`.
    """
    path, _ = write_verdict(tmp_path, verdict_with(material_finding()))

    with pytest.raises(ValueError, match="summary"):
        Verdict.model_validate_json(path.read_text(encoding="utf-8"))

    assert read_verdict(path).summary.hotel_errors == 1
