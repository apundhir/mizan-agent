"""The scorer must fail a wrong verdict, and this is the part provable on every push.

No model, no cassette, no fixture tree. One expectation, one verdict that satisfies it, and then
that verdict mutated one field at a time. Every mutant must fail, and the baseline must pass.

## Why this file is the important one

A scorer that passes everything reports a number that looks like evidence and is not. That failure
is invisible in a green CI job: the eval runs, the report says six of six, and nothing is being
measured. It is also the one property of an eval harness that can be proven without any of the
machinery the harness exists to exercise, so it is proven here, deterministically, on every push.

The mutations are chosen to be **individually constructible under the contracts**. A mutant that
`Finding` or `Verdict` refuses tells us nothing about the scorer, because the pipeline could not
have produced it either. Where a mutation from the brief is refused by a contract, the guarantee is
asserted against the contract instead and the scorer is given the nearest thing a real run could
actually emit: see `test_the_contract_refuses_a_definitional_finding_in_findings` below, and
`misfiles_a_definitional_item`, which is the arrangement a mis-classifying pipeline would publish.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from tda.contracts import (
    EscalationTarget,
    ExcelRef,
    ExtractionSummary,
    Finding,
    MetricKey,
    NotReached,
    PdfRef,
    Severity,
    VarianceClass,
    Verdict,
    VerdictStatus,
)
from tda.eval.expectation import (
    EXPECTED_FILE,
    Expectation,
    ExpectationError,
    load_expectation,
)
from tda.eval.score import score
from tda.eval.scoring import Check, Outcome, verdict_for

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Positions in the baseline verdict's `findings`, named so a mutation says what it mutates.
TRANSCRIPTION = 0
COMPLETENESS = 1
ROUNDING = 2

TRANSPOSED = "occupancy_pct:2026-01"
MISSING = "guests_by_nationality:2026-02:nationality_iso2=DE"
ROUNDED = "occupancy_pct:2026-02"
DEFINITIONAL = "room_nights_sold:2026-03"


def payload() -> dict[str, Any]:
    """A schema-valid `expected.json`, written out and read back through the real loader.

    Built as a dict and validated against `tools/fixtures/expected.schema.json` rather than
    constructed as an `Expectation` directly, so this suite fails if the schema moves under it.
    That is the point of the seam: the producer is another package, and the only way this side
    learns the shape changed is by reading the shape.
    """
    return {
        "fixture_set_version": "1.0.0",
        "fixture_id": "F2",
        "why": "A transposed occupancy figure must be caught as a transcription error and not "
        "explained away as a definitional difference.",
        "mutation": {
            "kind": "transpose_digits",
            "metric": "occupancy_pct",
            "period": "2026-01",
            "value_key": "claimed",
        },
        "status": "FAIL",
        "exhaustive": True,
        "findings": [
            {
                "key": TRANSPOSED,
                "variance_class": "V1",
                "severity": "material",
                "escalates_to": "hotel",
                "claimed": "81.40",
                "computed": "84.10",
                "difference": "-2.70",
                "proposed_correction": "84.10",
                "evidence": {"excel": {"sheet": "Occupancy", "cell": "D5"}, "source": "pdf_page"},
            },
            {
                "key": MISSING,
                "variance_class": "V5",
                "severity": "material",
                "escalates_to": "hotel",
                "claimed": None,
                "computed": "12",
                "evidence": {"excel": "not_reached", "source": "pdf_page"},
            },
            {
                "key": ROUNDED,
                "variance_class": "V6",
                "severity": "informational",
                "escalates_to": "none",
                "claimed": "80.10",
                "computed": "80.09",
                "difference": "0.01",
                "evidence": {"excel": {"sheet": "Occupancy", "cell": "D6"}, "source": "pdf_page"},
            },
        ],
        "definitional_items": [
            {
                "key": DEFINITIONAL,
                "variance_class": "V2",
                "severity": "informational",
                "escalates_to": "policy_owner",
                "claimed": "5000",
                "computed": "5100",
                "difference": "-100",
                "explaining_permutation": "P-COMP-EXCLUDED",
                "evidence": {"excel": {"sheet": "RoomNights", "cell": "B12"}, "source": "pdf_page"},
            }
        ],
    }


def write_expectation(directory: Path, payload_: dict[str, Any]) -> Expectation:
    path = directory / EXPECTED_FILE
    path.write_text(json.dumps(payload_), encoding="utf-8")
    return load_expectation(path)


def rows(page: int) -> PdfRef:
    return PdfRef(file="pms_2026-01.pdf", page=page, row_start=4, row_end=9)


def baseline() -> Verdict:
    """A verdict that satisfies the expectation above in every particular."""
    return Verdict(
        run_id="run-0123456789ab",
        status=VerdictStatus.FAIL,
        hotel_id="MZN-DXB-001",
        period="2026-Q1",
        policy_version="1.3.0",
        metric_library_version="1.0.0",
        model_id="claude-test",
        provider_mode="replay",
        extraction=ExtractionSummary(
            files=("pms_2026-01.pdf",),
            records_extracted=1200,
            pages_read=47,
            printed_total_matched=True,
            duplicate_ids=0,
        ),
        claims_checked=12,
        findings=(
            Finding(
                finding_id="F-0001",
                key=MetricKey.parse(TRANSPOSED),
                variance_class=VarianceClass.TRANSCRIPTION,
                severity=Severity.MATERIAL,
                escalates_to=EscalationTarget.HOTEL,
                claimed=Decimal("81.40"),
                computed=Decimal("84.10"),
                difference=Decimal("-2.70"),
                proposed_correction=Decimal("84.10"),
                source_ref=rows(4),
                excel_ref=ExcelRef(sheet="Occupancy", cell="D5"),
                clause="D-MAT-02",
            ),
            Finding(
                finding_id="F-0002",
                key=MetricKey.parse(MISSING),
                variance_class=VarianceClass.COMPLETENESS,
                severity=Severity.MATERIAL,
                escalates_to=EscalationTarget.HOTEL,
                computed=Decimal("12"),
                source_ref=rows(11),
                excel_ref=NotReached(reason="the workbook states no figure for DE"),
                clause="D-MAT-04",
            ),
            Finding(
                finding_id="F-0004",
                key=MetricKey.parse(ROUNDED),
                variance_class=VarianceClass.ROUNDING,
                severity=Severity.INFORMATIONAL,
                escalates_to=EscalationTarget.NONE,
                claimed=Decimal("80.10"),
                computed=Decimal("80.09"),
                difference=Decimal("0.01"),
                source_ref=rows(6),
                excel_ref=ExcelRef(sheet="Occupancy", cell="D6"),
                clause="D-TOL-02",
            ),
        ),
        definitional_items=(
            Finding(
                finding_id="F-0003",
                key=MetricKey.parse(DEFINITIONAL),
                variance_class=VarianceClass.DEFINITIONAL,
                severity=Severity.INFORMATIONAL,
                escalates_to=EscalationTarget.POLICY_OWNER,
                claimed=Decimal("5000"),
                computed=Decimal("5100"),
                difference=Decimal("-100"),
                explaining_permutation="P-COMP-EXCLUDED",
                source_ref=rows(14),
                excel_ref=ExcelRef(sheet="RoomNights", cell="B12"),
                clause="D-CLS-07",
            ),
        ),
    )


def rebuilt(verdict: Verdict, **changes: Any) -> Verdict:
    """A mutant that went through the contract's validators.

    `model_copy` would be shorter and skips validation, which would let this suite mutate a verdict
    into a shape no run could publish. A mutant the pipeline cannot emit proves nothing about the
    scorer.
    """
    fields = {name: getattr(verdict, name) for name in Verdict.model_fields}
    return Verdict(**{**fields, **changes})


def altered(finding: Finding, **changes: Any) -> Finding:
    fields = {name: getattr(finding, name) for name in Finding.model_fields}
    return Finding(**{**fields, **changes})


def with_finding(verdict: Verdict, index: int, **changes: Any) -> Verdict:
    findings = list(verdict.findings)
    findings[index] = altered(findings[index], **changes)
    return rebuilt(verdict, findings=tuple(findings))


# ── the mutations ────────────────────────────────────────────────────────────


def flips_a_variance_class(verdict: Verdict) -> Verdict:
    """A rounding difference reported as a completeness gap. Both are hotel-side and neither
    carries a correction, so the contracts permit the swap and only the scorer can catch it."""
    return with_finding(verdict, ROUNDING, variance_class=VarianceClass.COMPLETENESS)


def changes_a_severity(verdict: Verdict) -> Verdict:
    return with_finding(verdict, ROUNDING, severity=Severity.MATERIAL)


def changes_an_escalation_target(verdict: Verdict) -> Verdict:
    return with_finding(verdict, ROUNDING, escalates_to=EscalationTarget.HOTEL)


def drops_a_finding(verdict: Verdict) -> Verdict:
    return rebuilt(verdict, findings=verdict.findings[:ROUNDING])


def adds_an_unexpected_finding(verdict: Verdict) -> Verdict:
    """The control-fixture failure, planted on a fixture that expects findings. Containment would
    let this through, which is why the key check is set equality."""
    extra = altered(
        verdict.findings[ROUNDING],
        finding_id="F-0005",
        key=MetricKey.parse("occupancy_pct:2026-03"),
    )
    return rebuilt(verdict, findings=(*verdict.findings, extra))


def duplicates_a_key(verdict: Verdict) -> Verdict:
    """Two findings about one figure. The join is by key, so this makes the score ambiguous."""
    twin = altered(verdict.findings[ROUNDING], finding_id="F-0006")
    return rebuilt(verdict, findings=(*verdict.findings, twin))


def changes_an_excel_cell(verdict: Verdict) -> Verdict:
    return with_finding(verdict, TRANSCRIPTION, excel_ref=ExcelRef(sheet="Occupancy", cell="D7"))


def substitutes_a_cell_for_an_absence(verdict: Verdict) -> Verdict:
    """The expectation says this V5 has no cell, because the workbook states no figure at all
    (D-MAT-04). A cell here would be a citation to something nobody wrote.

    The opposite direction, a cell replaced by an absence, is deliberately absent from this suite:
    `Finding` permits a `NotReached` excel reference only on a V7 or a V5 with no claimed value, so
    a V1 or a V6 losing its cell is refused by the contract before any scorer runs.
    """
    return with_finding(verdict, COMPLETENESS, excel_ref=ExcelRef(sheet="Nationality", cell="D14"))


def drops_the_source_page(verdict: Verdict) -> Verdict:
    """One typed absence is legal on the source side (D-EV-05). The expectation says `pdf_page`,
    and a finding with no page is one a reviewer cannot open on the records side."""
    return with_finding(verdict, ROUNDING, source_ref=NotReached(reason="no source consulted"))


def blanks_a_proposed_correction(verdict: Verdict) -> Verdict:
    """A V1 may omit the correction, so the contract permits this. It is the field that tells the
    hotel what to write instead, and losing it silently is the whole point of checking it."""
    return with_finding(verdict, TRANSCRIPTION, proposed_correction=None)


def changes_a_computed_figure(verdict: Verdict) -> Verdict:
    """`difference` moves with it, because `Finding` requires claimed - computed to hold. The
    mutation is therefore two fields by necessity rather than by choice."""
    return with_finding(
        verdict, TRANSCRIPTION, computed=Decimal("84.20"), difference=Decimal("-2.80")
    )


def changes_an_explaining_permutation(verdict: Verdict) -> Verdict:
    """The named cause of a definitional variance. Naming the wrong one sends a correct finding to
    the policy owner with the wrong rule attached."""
    item = altered(verdict.definitional_items[0], explaining_permutation="P-COMP-INCLUDED")
    return rebuilt(verdict, definitional_items=(item,))


def changes_the_status(verdict: Verdict) -> Verdict:
    return rebuilt(verdict, status=VerdictStatus.ESCALATED)


def misfiles_a_definitional_item(verdict: Verdict) -> Verdict:
    """A finding expected in `findings`, published as a V2 in `definitional_items`.

    This is the constructible form of "move a V2 between the arrays". `Verdict` refuses a V2 in
    `findings` outright, so the arrangement a mis-classifying pipeline can actually publish is the
    mirror: a hotel-side variance reclassified as a policy disagreement and filed where hotel
    errors are never counted. That is the failure D-MAT-06 exists to prevent, seen from the side a
    contract cannot catch.
    """
    moved = altered(
        verdict.findings[ROUNDING],
        variance_class=VarianceClass.DEFINITIONAL,
        severity=Severity.INFORMATIONAL,
        escalates_to=EscalationTarget.POLICY_OWNER,
        explaining_permutation="P-COMP-EXCLUDED",
    )
    return rebuilt(
        verdict,
        findings=verdict.findings[:ROUNDING],
        definitional_items=(*verdict.definitional_items, moved),
    )


def empties_the_claims_counter(verdict: Verdict) -> Verdict:
    """The crashed-run shape: findings that match, over a run that parsed nothing."""
    return rebuilt(verdict, claims_checked=0)


def drops_the_extraction_summary(verdict: Verdict) -> Verdict:
    return rebuilt(verdict, extraction=None)


def unreconciles_the_printed_totals(verdict: Verdict) -> Verdict:
    """Extraction whose totals never matched the totals printed on the PDFs. Nothing below it is
    trustworthy, so a FAIL reached on it is a conclusion without a foundation."""
    assert verdict.extraction is not None
    fields = {name: getattr(verdict.extraction, name) for name in ExtractionSummary.model_fields}
    return rebuilt(
        verdict, extraction=ExtractionSummary(**{**fields, "printed_total_matched": False})
    )


MUTATIONS: tuple[Callable[[Verdict], Verdict], ...] = (
    flips_a_variance_class,
    changes_a_severity,
    changes_an_escalation_target,
    drops_a_finding,
    adds_an_unexpected_finding,
    duplicates_a_key,
    changes_an_excel_cell,
    substitutes_a_cell_for_an_absence,
    drops_the_source_page,
    blanks_a_proposed_correction,
    changes_a_computed_figure,
    changes_an_explaining_permutation,
    changes_the_status,
    misfiles_a_definitional_item,
    empties_the_claims_counter,
    drops_the_extraction_summary,
    unreconciles_the_printed_totals,
)


# ── the tests ────────────────────────────────────────────────────────────────


def test_the_baseline_verdict_passes(tmp_path: Path) -> None:
    """Every check passes on a verdict that matches, and there is at least one check.

    Both halves matter. A scorer that failed the right answer would be caught by the first; one
    that asserted nothing would pass every mutation below and is caught by the second.
    """
    expectation = write_expectation(tmp_path, payload())
    checks = score(baseline(), expectation)
    failed = [check for check in checks if not check.passed]
    assert not failed, [check.render() for check in failed]
    assert len(checks) > 1
    assert verdict_for(checks) is Outcome.PASSED


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda fn: fn.__name__)
def test_each_mutation_fails(mutate: Callable[[Verdict], Verdict], tmp_path: Path) -> None:
    """One field wrong, one fixture failed. No partial credit anywhere in the path."""
    expectation = write_expectation(tmp_path, payload())
    checks = score(mutate(baseline()), expectation)
    assert verdict_for(checks) is Outcome.FAILED, f"{mutate.__name__} scored as a pass"


def test_every_mutation_is_reported_by_name(tmp_path: Path) -> None:
    """A failing check names the property that moved.

    Without this, the suite above would be satisfied by a scorer that fails everything for one
    unrelated reason, and the report would send a reader looking in the wrong place.
    """
    expectation = write_expectation(tmp_path, payload())
    for mutate in MUTATIONS:
        failed = [c.name for c in score(mutate(baseline()), expectation) if not c.passed]
        assert failed, mutate.__name__


def test_an_empty_check_set_is_a_failure() -> None:
    """Nothing asserted is not a pass. This is the property the mutations above rest on."""
    assert verdict_for(()) is Outcome.FAILED
    assert verdict_for((Check("anything", True),)) is Outcome.PASSED


def test_a_non_exhaustive_expectation_cannot_be_scored(tmp_path: Path) -> None:
    """Set equality needs a licence, and the licence is `exhaustive`.

    `const: true` in the schema means no real file can carry `false` today. The guard is here
    because the day one could, every key check would silently become containment and an unexpected
    finding would stop being a failure.
    """
    exhaustive = write_expectation(tmp_path, payload())
    partial = Expectation(
        fixture_set_version=exhaustive.fixture_set_version,
        fixture_id=exhaustive.fixture_id,
        why=exhaustive.why,
        mutation=exhaustive.mutation,
        status=exhaustive.status,
        exhaustive=False,
        findings=exhaustive.findings,
        definitional_items=exhaustive.definitional_items,
    )
    checks = score(baseline(), partial)
    assert verdict_for(checks) is Outcome.FAILED
    assert [c.name for c in checks if not c.passed] == [
        "findings.keys",
        "definitional_items.keys",
    ]


def test_the_contract_refuses_a_definitional_finding_in_findings() -> None:
    """Where the "V2 in `findings`" guarantee actually lives.

    The brief for the scorer asked for that mutation, and it cannot be built: `Verdict`'s validator
    refuses it before any scorer sees it. Asserted here so the guarantee is demonstrated rather
    than assumed, and `misfiles_a_definitional_item` covers the arrangement a real run could
    publish.
    """
    verdict = baseline()
    definitional = verdict.definitional_items[0]
    with pytest.raises(ValidationError, match="definitional findings in `findings`"):
        rebuilt(
            verdict,
            findings=(*verdict.findings, definitional),
            definitional_items=(),
        )


def test_a_foreign_fixture_set_version_is_refused(tmp_path: Path) -> None:
    """A fixture derived by another builder describes outcomes this pipeline may no longer
    produce, and scoring against it would report correct behaviour as a defect."""
    stale = {**payload(), "fixture_set_version": "0.9.0"}
    with pytest.raises(ExpectationError, match=re.escape("derived by fixture set version 0.9.0")):
        write_expectation(tmp_path, stale)


def test_a_drifted_expectation_is_refused(tmp_path: Path) -> None:
    """Schema validation on read, not only on write. The producer is another package, and a file
    is the only place either side can notice that the shape moved."""
    drifted = {**payload(), "status": "PARTIAL"}
    with pytest.raises(ExpectationError, match=re.escape("does not satisfy expected.schema.json")):
        write_expectation(tmp_path, drifted)


def test_a_definitional_expectation_filed_as_a_finding_is_refused(tmp_path: Path) -> None:
    """The loader refuses an expectation that describes a verdict no run can publish.

    A V2 in `findings` is refused by `Verdict`, so an expectation asserting it would fail every
    run forever with no fix available in the pipeline.
    """
    broken = payload()
    broken["findings"] = [*broken["findings"], *broken["definitional_items"]]
    broken["definitional_items"] = []
    with pytest.raises(ExpectationError, match="V2 keys in `findings`"):
        write_expectation(tmp_path, broken)
