"""Extraction end to end: read the reports, reconcile, and refuse when it does not add up.

This is where the story's title earns itself. Everything above produces records or complains; this
turns a complaint into a **blocking V7 finding** and a halted run, and the shape of that refusal is the
deliverable:

- **`halted` is a property, not a flag somebody sets.** It is true exactly when a blocking finding
  exists. A flag would eventually be left unset on one code path, and the failure mode of forgetting it
  is a run that continues on data it admitted it could not read.
- **Records are returned even from a halted extraction.** Not so the caller can use them — the caller
  must check `halted` — but so a reviewer can see how far the read got. "2 of 419 rows on page 3
  failed" is diagnosable; "extraction failed" is not.
- **No finding here is ever a hotel error.** `Finding.is_hotel_error` is false for V7 by construction,
  so a V7 cannot be counted as one even by a caller who tries. That is the whole point of the refusal:
  the system saying "I could not read this" must never reach a property as "you got this wrong".

Every finding's `excel_ref` is `NotReached` (D-EV-02) — the one case the contract permits it, because
extraction halted before the workbook was parsed and there is no cell to cite. A typed absence carrying
a reason, never an empty string: an empty string is indistinguishable from a bug.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tda.contracts import (
    Dimension,
    EscalationTarget,
    ExtractionSummary,
    Finding,
    FindingIds,
    Metric,
    MetricKey,
    NotReached,
    PdfRef,
    Period,
    Severity,
    VarianceClass,
)
from tda.extract.normalise import load_lookups
from tda.extract.pdf import read_report
from tda.extract.reconcile_totals import reconcile
from tda.extract.totals import parse_totals

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from tda.contracts import ReservationRecord
    from tda.extract.pdf import RowDefect
    from tda.extract.reconcile_totals import Disagreement
    from tda.extract.totals import PrintedTotals
    from tda.policy import Policy


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """What extraction established, what it could not, and whether the run may continue."""

    # One record per reservation, not one per printed row. A month-spanning stay appears on two
    # monthly reports (D-RNS-03) and is merged here, with the two printings checked against each
    # other — see `merge_across_reports` for why that check is worth having.
    records: tuple[ReservationRecord, ...]
    findings: tuple[Finding, ...]
    summary: ExtractionSummary
    printed_totals: dict[str, PrintedTotals] = field(default_factory=dict)

    @property
    def halted(self) -> bool:
        """Whether a blocking finding was raised, and therefore whether the run must stop.

        Derived from the findings rather than stored. A stored flag is one code path away from being
        left unset, and the consequence is a verification that proceeds on data it could not read.
        """
        return any(finding.severity is Severity.BLOCKING for finding in self.findings)


def blocking_finding(
    ids: FindingIds, key: MetricKey, clause: str, detail: str, source_ref: PdfRef
) -> Finding:
    """One V7 blocking finding. Nothing is defaulted: every field a reader acts on is passed in."""
    return Finding(
        finding_id=ids.take(),
        key=key,
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        claimed=None,
        computed=None,
        source_ref=source_ref,
        excel_ref=NotReached(reason="extraction halted before claim parsing"),
        clause=clause,
        narrative=detail,
    )


def extract(reports: Sequence[Path], period: Period, policy: Policy) -> ExtractionResult:
    """Read every monthly report for a period and reconcile each against its own printed totals.

    The month comes from the **file name**, not from the report's heading. That is the stricter choice
    and the deliberate one: a March report filed as `pms_2026-02.pdf` would otherwise be reconciled
    against its own March totals, agree with itself perfectly, and contribute March's room-nights to
    February. Taking the month from the name makes the two statements comparable, and PRD-89's intake
    is where the mismatch between them becomes a rejection.
    """
    lookups = load_lookups()
    ids = FindingIds()

    records: list[ReservationRecord] = []
    findings: list[Finding] = []
    printed_by_month: dict[str, PrintedTotals] = {}
    files: list[str] = []
    pages_read = 0
    unmapped: list[str] = []
    reconciled = True

    for path in sorted(reports):
        month = month_of(path)
        files.append(path.name)

        report = read_report(path, lookups)
        pages_read += report.page_count

        month_records: list[ReservationRecord] = []
        printed_room_nights: dict[str, int] = {}
        for page in report.pages:
            month_records.extend(page.records)
            printed_room_nights.update(page.printed_room_nights_month)
            for defect in page.defects:
                findings.append(_from_defect(ids, defect, month))
                if defect.reason.startswith("unmappable"):
                    unmapped.append(defect.detail)

        printed = parse_totals(report.totals_left, report.totals_right, month, report.totals_page)
        printed_by_month[month] = printed

        disagreements = reconcile(
            month_records, printed_room_nights, printed, Period.parse(month), policy
        )
        if disagreements:
            reconciled = False
        for disagreement in disagreements:
            findings.append(_from_disagreement(ids, disagreement, printed))

        records.extend(month_records)

    # One record per reservation. A month-spanning stay is printed on two reports by design, and the
    # metric library is entitled to assume it is given each reservation once — see merge_across_reports.
    distinct, merge_findings = merge_across_reports(records, ids)
    findings.extend(merge_findings)

    duplicates = duplicate_ids(records)
    for reservation_id, refs in duplicates.items():
        findings.append(
            blocking_finding(
                ids,
                MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=period.rendered),
                "D-QUAL-07",
                (
                    f"reservation id {reservation_id} appears {len(refs)} times in one report, at "
                    f"{', '.join(ref.citation for ref in refs[:3])}. Never de-duplicated: two rows "
                    "with one id may be a double export or two bookings with a clerical collision, "
                    "and nothing in the data distinguishes them (D-QUAL-07)."
                ),
                refs[0],
            )
        )

    summary = ExtractionSummary(
        files=tuple(files),
        # Distinct reservations, not rows read. 1,303 rows across three reports are 1,200
        # reservations; reporting the row count here would make a reader think the corpus is larger
        # than it is, and the row counts are already reconciled against each report's printed total.
        records_extracted=len(distinct),
        pages_read=pages_read,
        printed_total_matched=reconciled,
        duplicate_ids=len(duplicates),
        unmapped_labels=tuple(unmapped),
    )

    return ExtractionResult(
        records=distinct,
        findings=tuple(findings),
        summary=summary,
        printed_totals=printed_by_month,
    )


def merge_across_reports(
    records: Sequence[ReservationRecord], ids: FindingIds
) -> tuple[tuple[ReservationRecord, ...], list[Finding]]:
    """Collapse a reservation printed on two monthly reports into one record, checking they agree.

    **Why this is necessary.** A month-spanning stay appears on *both* months' reports — that is what
    makes apportionment visible in the documents (D-RNS-03), and the demo corpus has 103 of them across
    1,200 reservations. Extraction therefore reads 1,303 rows for 1,200 reservations, and handing all
    1,303 to the metric library would count each spanning stay's nights twice.

    It would not do so quietly, which is worth saying: `reject_duplicate_ids` raises on the repeated ids
    (D-QUAL-07), so the failure is loud. But "loud" would mean every run halts, and the right answer is
    not to relax that guard — it is to give the metric library the record set it is entitled to assume:
    one record per reservation.

    **Why the agreement check is worth having.** Two printings of one reservation are two independent
    statements by the property about the same booking, and nothing else in the pipeline compares them. A
    February report saying 2 rooms and a March report saying 3 for the same stay is a real internal
    inconsistency — and it is invisible to the totals reconciliation, because each report reconciles
    against *its own* printed totals perfectly well. So a disagreement is a blocking V7 naming the field
    and both citations.

    The surviving record keeps the **earliest** citation, so a reviewer following it lands on the first
    report the stay appears in — which is the one that shows the arrival.
    """
    grouped: dict[str, list[ReservationRecord]] = {}
    for record in records:
        grouped.setdefault(record.reservation_id, []).append(record)

    merged: list[ReservationRecord] = []
    findings: list[Finding] = []

    for reservation_id in sorted(grouped):
        group = sorted(
            grouped[reservation_id],
            key=lambda r: (r.source.file, r.source.page, r.source.row_start),
        )
        first = group[0]
        if len(group) == 1:
            merged.append(first)
            continue

        baseline = first.model_dump(exclude={"source"})
        conflicts: list[str] = []
        for other in group[1:]:
            for field_name, value in other.model_dump(exclude={"source"}).items():
                if baseline[field_name] != value:
                    conflicts.append(
                        f"{field_name}: {baseline[field_name]!r} on {first.source.citation}, "
                        f"{value!r} on {other.source.citation}"
                    )

        if conflicts:
            findings.append(
                blocking_finding(
                    ids,
                    MetricKey(
                        metric=Metric.ROOM_NIGHTS_SOLD,
                        period=Period.of_month(first.arrival_date).rendered,
                    ),
                    "D-RNS-03",
                    (
                        f"{reservation_id} is printed on {len(group)} monthly reports and they "
                        f"disagree about it - {'; '.join(conflicts[:3])}. A stay crossing a month end "
                        "appears on both reports by design, so the two printings are two statements "
                        "about one booking. Each report reconciles against its own totals perfectly, "
                        "which is why nothing else in the pipeline can see this."
                    ),
                    first.source,
                )
            )
            continue

        merged.append(first)

    return tuple(merged), findings


def month_of(path: Path) -> str:
    """`pms_2026-02.pdf` → `2026-02`. Raises on anything else.

    Strict rather than lenient. A file whose name does not state its month cannot be reconciled against
    the right printed block, and inferring the month from the content would let a misfiled report
    reconcile against itself and pass.
    """
    _, _, candidate = path.stem.rpartition("_")
    if not Period.is_valid(candidate):
        raise ValueError(
            f"cannot tell which month {path.name} covers. Monthly reports are named "
            "`pms_YYYY-MM.pdf`; the month is taken from the name so a misfiled report is caught "
            "rather than reconciled against its own totals."
        )
    return candidate


def duplicate_ids(records: Sequence[ReservationRecord]) -> dict[str, list[PdfRef]]:
    """Reservation ids appearing more than once **within one report**.

    Scoped per file, and that scope is the whole subtlety. A month-spanning stay legitimately appears on
    two monthly reports — that is what makes apportionment visible in the documents (D-RNS-03) — so a
    naive check across all files would make every one of the 25+ spanning stays a blocking finding and
    halt every run. Grouping by `(id, file)` first is what distinguishes "printed on two reports, as
    intended" from "printed twice on one report, which nothing in the data explains".
    """
    seen: dict[tuple[str, str], list[PdfRef]] = {}
    for record in records:
        seen.setdefault((record.reservation_id, record.source.file), []).append(record.source)
    return {reservation_id: refs for (reservation_id, _file), refs in seen.items() if len(refs) > 1}


def _from_defect(ids: FindingIds, defect: RowDefect, month: str) -> Finding:
    """A row-level defect, keyed to the metric it makes untrustworthy.

    A single bad row is not a metric-wide failure in principle, but it is in practice: the metric
    library sums over the records it is given, so one row that could not be read means `room_nights_sold`
    for that month is computed from an incomplete set. Keying it to the affected metric is what lets the
    verdict say which figure to distrust rather than just that something went wrong.
    """
    return blocking_finding(
        ids,
        MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=month),
        defect.clause,
        f"{defect.reservation_id}: {defect.detail}",
        defect.ref,
    )


def _from_disagreement(
    ids: FindingIds, disagreement: Disagreement, printed: PrintedTotals
) -> Finding:
    """A reconciliation failure, keyed to the metric it makes untrustworthy.

    Keyed rather than generic, so the verdict says *which* figure cannot be relied on. A nationality
    disagreement does not invalidate occupancy and vice versa, and collapsing both onto one key would
    make a reviewer discard more evidence than the failure requires.

    The metric and the clause come from fields on the `Disagreement` rather than from parsing its
    display text. Recovering data from a human-readable label works until somebody rewords the label,
    at which point every finding silently gets the wrong key.
    """
    if disagreement.metric == "guests_by_nationality" and disagreement.dimension_value:
        key = MetricKey(
            metric=Metric.GUESTS_BY_NATIONALITY,
            period=disagreement.month,
            dimension=Dimension.NATIONALITY_ISO2,
            value=disagreement.dimension_value,
        )
    else:
        # A total-guests disagreement has no single nationality to name, so it keys to the metric whose
        # underlying record set is in doubt. `guests_by_nationality` cannot carry a key without a
        # dimension (D-KEY-02), and inventing one would put a finding against a country at random.
        key = MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=disagreement.month)

    return blocking_finding(
        ids,
        key,
        disagreement.clause,
        str(disagreement),
        PdfRef(
            file=f"pms_{disagreement.month}.pdf",
            page=printed.page,
            row_start=1,
            row_end=1,
        ),
    )
