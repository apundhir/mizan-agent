"""Scoring one verdict against one derived expectation.

## The join is by metric key, and it has to be

`Finding.finding_id` is minted per run (`F-0001`, `F-0002`, in the order findings were raised), so
it cannot be predicted by anything derived from a mutation spec, and it moves when an unrelated
finding is added earlier in the run. `MetricKey.rendered` is the only handle both sides can name
independently: the producer derives it from the mutation, the pipeline builds it from the metric
library, and neither had to agree with the other about ordering.

## Set equality, not containment

The expectation is exhaustive, so the check on keys is set equality in both directions. Containment
would make an unexpected finding invisible, and an unexpected finding on a control fixture is the
single most important thing this eval measures: a system that finds errors everywhere is not a
verification system, and precision is the half a reviewer feels.

## What the evidence checks do and do not pin

`evidence.source == "pdf_page"` asserts that the finding carries a `PdfRef` with a page. It does
**not** assert which page. The page a figure derives from is the extractor's to determine, and an
expectation that named it would be a second implementation of extraction: it would fail on a
relayout that changed nothing about the answer, and it would agree with the extractor about a wrong
page whenever both were derived from the same assumption.

## The sanity gates, and the one the schema cannot express

A crashed run must not be able to score as "zero findings", so the count checks are backed by gates
on the run's own foundation: claims were parsed, extraction was summarised, and the printed totals
reconciled. The brief for this module asked for a gate on the *expected record count*, and
`expected.schema.json` carries no field for one: it is `additionalProperties: false` over eight
named keys, and a record count is not among them. So the gate here is `records_extracted > 0`
plus `printed_total_matched`, which is the strongest statement available from the contract's own
rule that an unreconciled extraction makes everything below it untrustworthy. Recorded rather than
worked around quietly, because the missing field is a change to a contract this module does not
own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tda.contracts import ExcelRef, NotReached, PdfRef, VerdictStatus
from tda.eval.expectation import (
    ExpectedAbsence,
    ExpectedCell,
    ExpectedFinding,
    ExpectedSource,
)
from tda.eval.scoring import Check, Tally

if TYPE_CHECKING:
    from decimal import Decimal

    from tda.contracts import Finding, Verdict
    from tda.eval.expectation import Expectation

# Where the run had not yet reached the workbook, so a claim count of zero is the truth rather
# than a symptom. Everything else must have parsed claims to have compared anything.
_STOPS_BEFORE_CLAIMS = (VerdictStatus.REJECTED, VerdictStatus.HALTED)

FINDINGS = "findings"
DEFINITIONAL = "definitional_items"


def observed(verdict: Verdict) -> str:
    """What came out, in one cell of the report table. Shares its shape with `Expectation.rendered`
    so a reader compares two strings rather than two formats."""
    return (
        f"{verdict.status.value}, {len(verdict.findings)} finding(s), "
        f"{len(verdict.definitional_items)} definitional"
    )


def score(verdict: Verdict, expectation: Expectation) -> tuple[Check, ...]:
    """Every property the run had to have, as checks. No partial credit is applied here: this
    returns the checks and `verdict_for` decides, which keeps the aggregation in one place."""
    findings = _index(verdict.findings)
    definitional = _index(verdict.definitional_items)

    checks: list[Check] = [
        Check(
            "status",
            verdict.status is expectation.status,
            f"expected {expectation.status.value}, got {verdict.status.value}",
        ),
        Check(
            "findings.count",
            len(verdict.findings) == len(expectation.findings),
            f"expected exactly {len(expectation.findings)}, got {len(verdict.findings)}",
        ),
        Check(
            "definitional_items.count",
            len(verdict.definitional_items) == len(expectation.definitional_items),
            f"expected exactly {len(expectation.definitional_items)}, "
            f"got {len(verdict.definitional_items)}",
        ),
        _unique_keys(FINDINGS, verdict.findings),
        _unique_keys(DEFINITIONAL, verdict.definitional_items),
        _key_set(FINDINGS, findings, expectation.findings, expectation.exhaustive),
        _key_set(
            DEFINITIONAL, definitional, expectation.definitional_items, expectation.exhaustive
        ),
    ]

    for label, index, expected_list in (
        (FINDINGS, findings, expectation.findings),
        (DEFINITIONAL, definitional, expectation.definitional_items),
    ):
        for expected in expected_list:
            found = index.get(expected.key)
            if found is None:
                checks.append(Check(f"{label}[{expected.key}]", False, "absent from the verdict"))
                continue
            checks.extend(_compare(f"{label}[{expected.key}]", expected, found))

    checks.extend(_sanity(verdict, expectation))
    return tuple(checks)


def tally_for(verdict: Verdict, expectation: Expectation) -> Tally:
    """The aggregate contribution of one fixture.

    Counted separately from the checks because these are measurements rather than assertions.
    Recall over planted material errors and false positives on the controls are the two numbers a
    reader of the scorecard wants, and neither is expressible as a per-fixture pass or fail.
    """
    found = {**_index(verdict.findings), **_index(verdict.definitional_items)}
    expected_all = {f.key: f for f in (*expectation.findings, *expectation.definitional_items)}
    matched_class = {
        key
        for key, expected in expected_all.items()
        if key in found and found[key].variance_class is expected.variance_class
    }
    unexpected = len([key for key in found if key not in expected_all])

    absences = [
        ref
        for finding in found.values()
        for ref in (finding.source_ref, finding.excel_ref)
        if isinstance(ref, NotReached)
    ]
    return Tally(
        material_expected=len(expectation.material_keys),
        material_matched=len(expectation.material_keys & matched_class),
        class_expected=len(expected_all),
        class_matched=len(matched_class),
        controls=1 if expectation.mutation.is_control else 0,
        control_false_positives=unexpected if expectation.mutation.is_control else 0,
        false_positives=unexpected,
        findings_seen=len(found),
        # A finding citing nothing on either side is refused by `Finding` itself, so this counts a
        # contract holding rather than asserting a property that cannot break. The target is not
        # "a page and a cell on every finding": that is unsatisfiable by design, because one typed
        # absence is legitimate for a V5 with no claimed value and for a V7.
        evidence_openable=sum(
            1
            for finding in found.values()
            if not (
                isinstance(finding.source_ref, NotReached)
                and isinstance(finding.excel_ref, NotReached)
            )
        ),
        absences_with_reason=sum(1 for ref in absences if ref.reason.strip()),
        absences_without_reason=sum(1 for ref in absences if not ref.reason.strip()),
    )


def _index(findings: tuple[Finding, ...]) -> dict[str, Finding]:
    """Findings by rendered metric key, first occurrence winning.

    A repeated key is reported by `_unique_keys` rather than resolved here. Silently keeping the
    last one would let a run raise two findings about one figure and be scored on whichever
    happened to be second.
    """
    index: dict[str, Finding] = {}
    for finding in findings:
        index.setdefault(finding.key.rendered, finding)
    return index


def _unique_keys(label: str, findings: tuple[Finding, ...]) -> Check:
    """One key, one finding. The join is by key, so a duplicate makes the score ambiguous."""
    keys = [f.key.rendered for f in findings]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    return Check(
        f"{label}.keys_unique",
        not duplicates,
        f"repeated keys: {duplicates}" if duplicates else f"{len(keys)} key(s)",
    )


def _key_set(
    label: str,
    index: dict[str, Finding],
    expected_list: tuple[ExpectedFinding, ...],
    exhaustive: bool,
) -> Check:
    """Set equality on keys, which is what makes a false positive a failure.

    `exhaustive` licenses the direction that matters. It is `const: true` in the schema, so the
    guard cannot fire today, and it is here rather than assumed: if the contract ever admitted a
    partial expectation, this check would have to become containment, and a silent switch would
    turn every unexpected finding into an unnoticed extra.
    """
    if not exhaustive:
        return Check(
            f"{label}.keys",
            False,
            "the expectation is not exhaustive, so set equality cannot be asserted and an "
            "unexpected finding would go unnoticed",
        )
    wanted = {f.key for f in expected_list}
    got = set(index)
    missing = sorted(wanted - got)
    unexpected = sorted(got - wanted)
    detail = f"{len(got)} key(s) as expected"
    if missing or unexpected:
        detail = f"missing: {missing}; unexpected: {unexpected}"
    return Check(f"{label}.keys", not missing and not unexpected, detail)


def _compare(prefix: str, expected: ExpectedFinding, found: Finding) -> tuple[Check, ...]:
    """Every field of one finding the expectation pins."""
    return (
        Check(
            f"{prefix}.variance_class",
            found.variance_class is expected.variance_class,
            f"expected {expected.variance_class.value}, got {found.variance_class.value}",
        ),
        Check(
            f"{prefix}.severity",
            found.severity is expected.severity,
            f"expected {expected.severity.value}, got {found.severity.value}",
        ),
        Check(
            f"{prefix}.escalates_to",
            found.escalates_to is expected.escalates_to,
            f"expected {expected.escalates_to.value}, got {found.escalates_to.value}",
        ),
        _figure(f"{prefix}.claimed", expected.claimed, found.claimed),
        _figure(f"{prefix}.computed", expected.computed, found.computed),
        _figure(f"{prefix}.difference", expected.difference, found.difference),
        _figure(
            f"{prefix}.proposed_correction",
            expected.proposed_correction,
            found.proposed_correction,
        ),
        # V2 only, and absent otherwise. Both directions come from one comparison because
        # `load_expectation` refuses an expectation that breaks the rule, so the expected value is
        # non-null exactly on a V2.
        Check(
            f"{prefix}.explaining_permutation",
            found.explaining_permutation == expected.explaining_permutation,
            f"expected {expected.explaining_permutation!r}, got {found.explaining_permutation!r}",
        ),
        _excel(f"{prefix}.evidence.excel", expected, found),
        _source(f"{prefix}.evidence.source", expected, found),
    )


def _figure(name: str, expected: Decimal | None, found: Decimal | None) -> Check:
    """Decimal comparison, which is numeric rather than textual: `Decimal("30.00") == Decimal("30")`.
    The written form of a figure in the expectation therefore carries no meaning the scorer can
    accidentally depend on."""
    if expected is None or found is None:
        return Check(name, expected is None and found is None, f"expected {expected}, got {found}")
    return Check(name, expected == found, f"expected {expected}, got {found}")


def _excel(name: str, expected: ExpectedFinding, found: Finding) -> Check:
    """The cell matches, or the typed absence matches. Never one standing in for the other."""
    ref = found.excel_ref
    if isinstance(expected.evidence.excel, ExpectedCell):
        wanted = expected.evidence.excel
        if not isinstance(ref, ExcelRef):
            return Check(name, False, f"expected {wanted.render()}, got {ref.citation}")
        return Check(
            name,
            ref.sheet == wanted.sheet and ref.cell == wanted.cell,
            f"expected {wanted.render()}, got {ref.citation}",
        )
    return Check(
        name,
        isinstance(ref, NotReached),
        f"expected a typed absence ({ExpectedAbsence.NOT_REACHED.value}), got {ref.citation}",
    )


def _source(name: str, expected: ExpectedFinding, found: Finding) -> Check:
    """A page exists, or the absence is typed. Which page is not pinned, deliberately.

    An `InventoryRef` fails here, and the failure is the expectation vocabulary's rather than the
    run's: `expected.schema.json` offers `pdf_page` or `not_reached` and nothing for the one metric
    whose source is not a PDF. `room_nights_available` is a property attribute rather than a
    reservation one (D-RNA-01), so a finding about it legitimately cites the inventory CSV, and the
    detail says so rather than reporting a correct citation as a wrong page.
    """
    ref = found.source_ref
    if expected.evidence.source is ExpectedSource.PDF_PAGE:
        if isinstance(ref, PdfRef):
            return Check(name, True, f"page {ref.page} (not pinned by the expectation)")
        if isinstance(ref, NotReached):
            return Check(name, False, f"expected a PDF page, got {ref.citation}")
        return Check(
            name,
            False,
            f"got an inventory citation ({ref.citation}), which `expected.schema.json` has no "
            "value for: its `source` enum is pdf_page or not_reached only",
        )
    return Check(
        name,
        isinstance(ref, NotReached),
        f"expected a typed absence, got {ref.citation}",
    )


def _sanity(verdict: Verdict, expectation: Expectation) -> tuple[Check, ...]:
    """The gates that stop a crashed run scoring as a clean one.

    Zero findings is the correct answer for two of the six fixtures, and it is also what a run that
    died before reading anything produces. These are what separate the two, and they are asserted
    against the run's own foundation rather than against anything the expectation could restate.
    """
    checks: list[Check] = []
    if expectation.status not in _STOPS_BEFORE_CLAIMS:
        checks.append(
            Check(
                "sanity.claims_checked",
                verdict.claims_checked > 0,
                f"{verdict.claims_checked} claim(s) parsed; a comparison needs at least one",
            )
        )

    extraction = verdict.extraction
    if expectation.status is VerdictStatus.REJECTED:
        # The contract's own rule: `extraction` is None only when intake rejected before
        # extraction ran. A REJECTED verdict carrying a summary was rejected somewhere else.
        checks.append(
            Check(
                "sanity.rejected_before_extraction",
                extraction is None,
                "extraction summary absent" if extraction is None else "extraction ran anyway",
            )
        )
        return tuple(checks)

    if extraction is None:
        checks.append(
            Check("sanity.extraction_recorded", False, "no extraction summary on the verdict")
        )
        return tuple(checks)

    checks.append(
        Check(
            "sanity.extraction_records",
            extraction.records_extracted > 0,
            f"{extraction.records_extracted} record(s) extracted from "
            f"{extraction.pages_read} page(s)",
        )
    )
    if expectation.status is not VerdictStatus.HALTED:
        checks.append(
            Check(
                "sanity.printed_total_matched",
                extraction.printed_total_matched,
                "extracted totals reconciled against the totals printed on the PDFs"
                if extraction.printed_total_matched
                else "the printed totals did not reconcile, so nothing below extraction is "
                "trustworthy and the run should have halted",
            )
        )
    return tuple(checks)
