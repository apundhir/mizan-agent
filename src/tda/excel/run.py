"""The claim parser end to end: map, check the mapping, check the workbook, then read.

The order is the design, and it is the order the story argues for:

1. **Map** — a model says which range holds which metric, from a digest that cannot contain a value.
2. **Account for every sheet** — by comparing the mapping to the workbook's sheet list, not by
   trusting the mapper to have been exhaustive.
3. **Resolve** — every metric name against policy's vocabulary, every range against the sheet.
4. **Read** — `openpyxl` walks the resolved ranges. No model in the call stack.
5. **Check the workbook against itself** — before any of these claims is put beside a figure of ours.

Two things come out of it that are not claims, and both are deliberate:

**Inconsistencies**, which are the hotel's arithmetic disagreeing with itself (D-XLS-04). They are
first in the result and first in any report, because everything else a reviewer is about to read may
be a consequence of one of them.

**Blocking findings for what could not be mapped** (D-XLS-06). These are V7 extraction limits and
they say the same thing the PDF-side refusals say: *the system could not read this, and that is not
the hotel's fault*. `Finding.is_hotel_error` is false for V7 by construction, so an unmapped sheet
can never be counted against a property even by a caller who tries.

The parser raises no findings of its own for a *figure*. Claimed-against-computed is reconciliation's
job and needs both sides; this half only produces one of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import openpyxl

from tda.contracts import (
    EscalationTarget,
    ExcelRef,
    Finding,
    FindingIds,
    Metric,
    MetricKey,
    Severity,
    VarianceClass,
)
from tda.excel.mapping import Disposition, resolve, unaccounted_sheets, uncovered_values
from tda.excel.read import UNMAPPABLE_LABEL, read_block
from tda.excel.selfcheck import (
    check_cover,
    check_dimension_totals,
    check_occupancy_consistency,
    check_period_rollups,
    no_pdf,
)
from tda.extract.normalise import load_lookups

if TYPE_CHECKING:
    from pathlib import Path

    from openpyxl.workbook.workbook import Workbook

    from tda.contracts import Claim, Period
    from tda.excel.mapping import ResolvedBlock, WorkbookMapping
    from tda.excel.read import BlockReading, ReadDefect, StatedTotal
    from tda.excel.selfcheck import IdentityMismatch, Inconsistency
    from tda.extract.normalise import Lookups
    from tda.policy import Policy


@dataclass(frozen=True, slots=True)
class OutOfScopeClaim:
    """A header block naming a measure the policy has declared out of scope (D-SCOPE-02).

    Recorded, not parsed and not reported as a finding. Silence would be mistaken for approval, and
    a finding would be an accusation about a figure nobody asked us to check.
    """

    metric: str
    sheet: str
    cells: str


@dataclass(frozen=True, slots=True)
class ClaimSet:
    """What the workbook asserts, what it gets wrong about itself, and what could not be read."""

    claims: tuple[Claim, ...] = ()
    inconsistencies: tuple[Inconsistency, ...] = ()
    identity_mismatches: tuple[IdentityMismatch, ...] = ()
    findings: tuple[Finding, ...] = ()
    out_of_scope: tuple[OutOfScopeClaim, ...] = ()
    defects: tuple[ReadDefect, ...] = ()
    stated_totals: tuple[StatedTotal, ...] = ()
    resolved: tuple[ResolvedBlock, ...] = field(default=())

    @property
    def halted(self) -> bool:
        """Whether a blocking finding was raised, and therefore whether the run must stop.

        Derived, never stored — the same rule as `ExtractionResult.halted`, for the same reason: a
        stored flag is one code path away from being left unset, and the consequence is a
        verification that proceeds on a workbook it admitted it could not map.
        """
        return any(finding.severity is Severity.BLOCKING for finding in self.findings)

    @property
    def self_consistent(self) -> bool:
        """Whether the workbook's own arithmetic and identity hold.

        A caller that reports findings without checking this will present consequences as causes.
        """
        return not self.inconsistencies and not self.identity_mismatches


def _unmapped_finding(
    ids: FindingIds, sheet: str, cells: str, reason: str, period: Period
) -> Finding:
    """One V7 for something the parser could not place.

    Keyed to `room_nights_sold` for the period. That is an escape hatch and it is worth being
    honest about: an unmapped block has, by definition, no metric, so no key describes it. The
    alternative — omitting the finding — is the one thing D-XLS-06 forbids, and a key that is
    admittedly approximate on a blocking finding that halts the run costs a reviewer nothing,
    because the run stops and the detail names the sheet and range.
    """
    return Finding(
        finding_id=ids.take(),
        key=MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=str(period)),
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        claimed=None,
        computed=None,
        # No PDF was consulted to reach this, and D-EV-05 is explicit that the absence is typed and
        # carries its reason rather than being an invented page number.
        source_ref=no_pdf("the workbook could not be mapped, so no report was compared against it"),
        excel_ref=ExcelRef(sheet=sheet, cell=cells.split(":")[0].upper()),
        clause="D-XLS-06",
        narrative=(
            f"{sheet}!{cells} could not be mapped to a metric and needs a human mapping: {reason}"
        ),
    )


def _unmappable_label_finding(ids: FindingIds, defect: ReadDefect, period: Period) -> Finding:
    """One V7 for a dimension label the committed lookup has no entry for (D-NAT-12).

    Deliberately narrower than `_unmapped_finding`: this fires only for `ReadDefect.kind ==
    UNMAPPABLE_LABEL`, never for an uncached formula or a malformed period label. Those are refused
    claims, one row poorer than the workbook it came from; this is a refused submission, because a
    row this parser cannot place a country against is a row it cannot check at all, and the closest
    match in the lookup is precisely the kind of help that produces a confident wrong answer.

    Keyed to `room_nights_sold` for the same reason `_unmapped_finding` is: the one thing an
    unresolved dimension value cannot supply is a `MetricKey` naming it, and a key that is
    admittedly approximate on a finding that halts the run costs a reviewer nothing, since the run
    stops and `narrative` names the string that failed.
    """
    return Finding(
        finding_id=ids.take(),
        key=MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=str(period)),
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        claimed=None,
        computed=None,
        source_ref=no_pdf("the label could not be resolved, so no report was compared against it"),
        excel_ref=ExcelRef(sheet=defect.sheet, cell=defect.cell),
        clause="D-NAT-12",
        narrative=f"{defect.citation}: {defect.reason}",
    )


def parse_claims(
    workbook: Workbook,
    mapping: WorkbookMapping,
    period: Period,
    policy: Policy,
    *,
    lookups: Lookups | None = None,
    formulas: Workbook | None = None,
    expected_property: str | None = None,
) -> ClaimSet:
    """Turn a mapping and a workbook into claims, inconsistencies and refusals.

    `workbook` is the `data_only=True` view — cached values. `formulas` is the same file opened
    `data_only=False`, and it is what makes D-XLS-03 reachable: in the value view an empty cell and
    an uncached formula are both `None`, and they mean opposite things. `open_submission` returns
    the pair.
    """
    resolved_lookups = lookups or load_lookups()
    ids = FindingIds()

    claims: list[Claim] = []
    totals: list[StatedTotal] = []
    defects: list[ReadDefect] = []
    findings: list[Finding] = []
    out_of_scope: list[OutOfScopeClaim] = []
    resolved_blocks: list[ResolvedBlock] = []

    # ── 2. every sheet accounted for, established by checking rather than by asking ──
    for sheet in unaccounted_sheets(mapping, workbook):
        findings.append(
            _unmapped_finding(
                ids,
                sheet,
                str(workbook[sheet].dimensions),
                "the mapping does not mention this sheet at all",
                period,
            )
        )

    for unmapped in mapping.unmapped:
        findings.append(
            _unmapped_finding(ids, unmapped.sheet, unmapped.cells, unmapped.reason, period)
        )

    # ── 3 and 4. resolve, then read ─────────────────────────────────────────
    for block in mapping.blocks:
        outcome = resolve(block, workbook, policy)
        resolved_blocks.append(outcome)

        if outcome.disposition is Disposition.NEEDS_HUMAN_MAPPING:
            findings.append(
                _unmapped_finding(
                    ids,
                    block.sheet,
                    block.value_range,
                    outcome.problem or "the mapping could not be resolved",
                    period,
                )
            )
            continue

        if outcome.disposition is Disposition.OUT_OF_SCOPE:
            out_of_scope.append(
                OutOfScopeClaim(
                    metric=outcome.out_of_scope_name or block.metric,
                    sheet=block.sheet,
                    cells=block.value_range,
                )
            )
            continue

        reading: BlockReading = read_block(outcome, workbook, resolved_lookups, formulas=formulas)
        claims.extend(reading.claims)
        totals.extend(reading.stated_totals)
        defects.extend(reading.defects)

    # ── 4a. a label the lookup cannot resolve, promoted to a refusal (D-NAT-12) ──
    # Scoped to this one defect kind on purpose. An uncached formula or a malformed period label is
    # a claim the parser declines to make; the workbook is still checkable everywhere else. A label
    # nothing in the lookup matches is different in kind, not degree: the row it sits on cannot be
    # placed against anything, and guessing the nearest country is the one thing D-NAT-12 forbids.
    for defect in defects:
        if defect.kind == UNMAPPABLE_LABEL:
            findings.append(_unmappable_label_finding(ids, defect, period))

    # ── 4b. figures no mapped range covers (D-XLS-06, the block-level half) ──
    # After resolving, not before: an out-of-scope block still covers its cells, and a block sent
    # for human mapping is already reported, so neither should produce a second finding here.
    for sheet, cells in uncovered_values(workbook, resolved_blocks).items():
        shown = ", ".join(cells[:8])
        more = f" (+{len(cells) - 8} more)" if len(cells) > 8 else ""
        findings.append(
            _unmapped_finding(
                ids,
                sheet,
                cells[0],
                f"{len(cells)} cell(s) hold a figure that no mapped range covers: {shown}{more}",
                period,
            )
        )

    # ── 5. the workbook against itself, before anything is compared to it ────
    inconsistencies = [
        *check_dimension_totals(claims, totals),
        *check_period_rollups(claims),
        *check_occupancy_consistency(claims, policy),
    ]

    # The identity check needs something to check against, so it runs only when the caller supplies
    # it. Skipping it silently would be the wrong default — but the alternative, inventing an
    # expectation, would let a workbook for the wrong quarter pass the one check meant to catch it.
    # The caller not passing one is a caller that has not yet read the reports.
    identity: list[IdentityMismatch] = []
    if mapping.cover is not None and expected_property is not None:
        identity = check_cover(
            workbook,
            mapping.cover,
            expected_property=expected_property,
            expected_period=period,
        )

    return ClaimSet(
        claims=tuple(claims),
        inconsistencies=tuple(inconsistencies),
        identity_mismatches=tuple(identity),
        findings=tuple(findings),
        out_of_scope=tuple(out_of_scope),
        defects=tuple(defects),
        stated_totals=tuple(totals),
        resolved=tuple(resolved_blocks),
    )


def open_submission(path: Path) -> tuple[Workbook, Workbook]:
    """Open a submitted workbook twice, and return `(values, formulas)`.

    Two views of one file, because neither alone can answer both questions the parser has to ask.
    `data_only=True` gives a formula cell its cached result and gives an *uncached* one `None` —
    indistinguishable from an empty cell, which means the opposite thing. `data_only=False` gives
    the formula text and gives every computed cell its formula instead of its value, which is no use
    for reading a claim.

    So: values from the first, and the second consulted only where the first says `None`. The cost
    is parsing the file twice, on a workbook of a few hundred cells, once per run.
    """
    return (
        openpyxl.load_workbook(path, data_only=True, read_only=False),
        openpyxl.load_workbook(path, data_only=False, read_only=False),
    )
