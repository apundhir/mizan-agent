"""Reconciliation: the join, the permutation runner, and the ladder.

The end-to-end statement first, because it is the one worth making: **the whole pipeline over the
committed corpus produces zero findings.** PDFs to records to metrics, workbook to claims, joined on
the canonical key — 94 against 94, every one an exact match. A verification system that cannot stay
silent about a correct submission is worse than useless, because every real finding it later produces
arrives in a stream of noise.

Everything else here is the opposite half: each rung of the ladder driven until it fires, on figures
taken from the real corpus rather than invented, so the numbers in the assertions are numbers a
reviewer could check.

**The order is tested as a behaviour, not as a list.** `test_definitional_is_tested_before_transcription`
and `test_extraction_limit_outranks_everything` construct inputs where the wrong order gives a
different, plausible, wrong answer — a hotel accused of a typo for a figure that is right under its
own reading of the rules, and a hotel accused of an error the system could not actually see. Those
are the two failures D-CLS-01 and D-CLS-02 exist to prevent, and asserting that `policy.order` equals
`[V7, V2, V5, V1, V6]` would not catch either.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.fixtures.workbook import demo_mapping

from tda.contracts import (
    Claim,
    Dimension,
    EscalationTarget,
    ExcelRef,
    Finding,
    Metric,
    MetricKey,
    NotReached,
    PdfRef,
    Period,
    Severity,
    VarianceClass,
)
from tda.excel import (
    open_submission,
    parse_claims,
)
from tda.extract.inventory import read_inventory
from tda.extract.run import extract
from tda.metrics import compute_all
from tda.policy import Policy, apply_permutation, load_policy
from tda.reconcile import Pairing, PermutationIndex, Reconciliation, join, reconcile

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.contracts import InventoryDay, ReservationRecord
    from tda.metrics import MetricResults

SUBMISSION = Path(__file__).resolve().parents[2] / "corpus" / "demo" / "submission"

# Figures taken from the corpus, so every expectation below is checkable by hand.
FEB_SOLD = "room_nights_sold:2026-02"
FEB_AVAILABLE = "room_nights_available:2026-02"
FEB_OCCUPANCY = "occupancy_pct:2026-02"
FEB_FRANCE = "guests_by_nationality:2026-01:nationality_iso2=FR"


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


@pytest.fixture(scope="module")
def period() -> Period:
    return Period.parse("2026-Q1")


@pytest.fixture(scope="module")
def periods(period: Period) -> list[Period]:
    return [*period.months(), period]


@pytest.fixture(scope="module")
def records(period: Period, policy: Policy) -> tuple[ReservationRecord, ...]:
    result = extract(sorted(SUBMISSION.glob("pms_2026-*.pdf")), period, policy)
    assert not result.halted, "the committed corpus must extract cleanly"
    return result.records


@pytest.fixture(scope="module")
def inventory(period: Period) -> tuple[InventoryDay, ...]:
    return tuple(read_inventory(SUBMISSION / "inventory_2026-Q1.csv", period).days)


@pytest.fixture(scope="module")
def results(
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
) -> MetricResults:
    return compute_all(records, inventory, periods, policy)


@pytest.fixture(scope="module")
def claims(period: Period, policy: Policy) -> tuple[Claim, ...]:
    values, formulas = open_submission(SUBMISSION / "claims_2026-Q1.xlsx")
    parsed = parse_claims(
        values,
        demo_mapping(),
        period,
        policy,
        formulas=formulas,
        expected_property="MZN-DXB-001",
    )
    assert not parsed.halted, "the committed workbook must parse cleanly"
    return parsed.claims


def _swap(claims: Sequence[Claim], key: str, value: object) -> list[Claim]:
    """The committed claims with one figure replaced — a hotel that typed something else."""
    return [
        claim
        if claim.key.rendered != key
        else claim.model_copy(update={"value": Decimal(str(value))})
        for claim in claims
    ]


@pytest.fixture(scope="module")
def index(
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
) -> PermutationIndex:
    """Built once for the whole module.

    The index is a pure function of the records, the inventory, the periods and the policy, all of
    which are module-scoped here. Rebuilding it per test ran the metric library thirteen times per
    call for no new information.
    """
    return PermutationIndex(records, inventory, periods, policy)


def _run(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay] | None,
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex | None = None,
) -> Reconciliation:
    return reconcile(claims, results, records, inventory, periods, policy, index=index)


def _only(reconciliation: Reconciliation) -> Finding:
    assert len(reconciliation.findings) == 1, [f.key.rendered for f in reconciliation.findings]
    return reconciliation.findings[0]


# ══ the statement worth making ═══════════════════════════════════════════════


def test_a_correct_submission_produces_no_findings_at_all(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """The whole pipeline, end to end, silent.

    94 claims read from the workbook against 94 values computed from the PDFs and the inventory
    reference, joined on the canonical key, every pair an exact match. Nothing is raised, nothing is
    logged as informational, and nothing is blocked.
    """
    result = _run(claims, results, records, inventory, periods, policy, index)

    assert len(claims) == 94
    assert len(results.computed) == 94
    assert result.findings == ()
    assert result.hotel_errors == ()
    assert result.definitional == ()
    assert result.informational == ()
    assert result.halted is False


def test_the_join_pairs_every_key_exactly(claims: Sequence[Claim], results: MetricResults) -> None:
    pairs = join(claims, results)
    assert len(pairs) == 94
    assert {pair.pairing for pair in pairs} == {Pairing.MATCHED}
    assert all(pair.is_exact_match for pair in pairs)


def test_the_join_is_ordered_so_finding_ids_are_stable(
    claims: Sequence[Claim], results: MetricResults
) -> None:
    """A reviewer's recorded decision references a finding id. Two runs over identical inputs that
    numbered findings differently would orphan every decision already made."""
    rendered = [pair.rendered for pair in join(claims, results)]
    assert rendered == sorted(rendered)


# ══ V2 · definitional, established mechanically ══════════════════════════════


def test_a_month_basis_disagreement_is_definitional_and_names_the_permutation(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """The headline case, with the corpus's own numbers.

    Our baseline apportions a stay by occupied night and gets 1,299 room-nights for February. A hotel
    that apportioned the whole stay to the arrival month would get exactly 1,265. That is not a
    clerical error and must never be sent to the property as one.
    """
    result = _run(
        _swap(claims, FEB_SOLD, 1265), results, records, inventory, periods, policy, index
    )
    finding = _only(result)

    assert finding.variance_class is VarianceClass.DEFINITIONAL
    assert finding.explaining_permutation == "P-MONTH-ARRIVAL"
    assert finding.escalates_to is EscalationTarget.POLICY_OWNER
    assert finding.is_hotel_error is False
    assert finding.proposed_correction is None
    assert (finding.claimed, finding.computed) == (Decimal(1265), Decimal(1299))
    assert result.definitional == (finding,)
    assert result.hotel_errors == ()


def test_a_definitional_finding_cites_the_permutations_own_rows(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """The finding says an alternative ruleset reproduced the figure, so the rows it cites must be
    the rows *that* computation used. Citing the baseline's would send a reviewer to check a
    calculation that produced a different number."""
    result = _run(
        _swap(claims, FEB_SOLD, 1265), results, records, inventory, periods, policy, index
    )
    finding = _only(result)
    assert isinstance(finding.source_ref, PdfRef)

    index = PermutationIndex(records, inventory, periods, policy)
    explanation = index.explanations(MetricKey.parse(FEB_SOLD), Decimal(1265))[0]
    assert finding.source_ref == explanation.computed.primary_ref


def test_an_inventory_metric_cites_the_inventory_reference_not_a_page(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-RNA-01, and the reason `source_ref` is not called `pdf_ref`.

    Rooms available is a property attribute and appears in no reservation export, so a finding about
    it has no page anywhere in the system to point at. The committed inventory leaves out-of-order
    rooms out of the denominator; a hotel that left them in gets 1,680 against our 1,590.
    """
    result = _run(
        _swap(claims, FEB_AVAILABLE, 1680), results, records, inventory, periods, policy, index
    )
    finding = _only(result)

    assert finding.variance_class is VarianceClass.DEFINITIONAL
    assert finding.explaining_permutation == "P-OOO-INCLUDED"
    assert finding.source_ref.citation.startswith("inventory_2026-Q1.csv")
    assert finding.excel_ref.citation == "Occupancy!C6"


def test_where_several_permutations_fit_the_first_is_named_and_the_rest_recorded(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-CLS-09. Naming one cause as certain when two fit the evidence is a false precision.

    January's French guests are 58 under the baseline and 62 under *either* of two status
    permutations — counting no-shows, or counting cancellations. Both are consistent with the
    evidence and the system says so.
    """
    result = _run(
        _swap(claims, FEB_FRANCE, 62), results, records, inventory, periods, policy, index
    )
    finding = _only(result)

    assert finding.explaining_permutation == "P-STATUS-INCLUDE-NOSHOW"
    assert finding.also_explained_by == ("P-STATUS-INCLUDE-CANCELLED",)

    committed = [p.id for p in policy.permutations.ordered]
    named, also = finding.explaining_permutation, finding.also_explained_by[0]
    assert committed.index(named) < committed.index(also), "the first in committed order is named"


def test_the_permutation_set_is_committed_not_searched(policy: Policy) -> None:
    """D-CLS-08. Finite, ordered and declared in `policy.yaml` — never generated at run time.

    The ids are asserted against the file because the permutation named in a finding is the whole
    explanation a policy owner receives, and a set that could change between runs would make two
    verdicts on identical input disagree about the cause.
    """
    assert policy.permutations.enabled is True
    ids = [p.id for p in policy.permutations.ordered]
    assert ids[:4] == [
        "P-OCC-DENOM-ROOMS",
        "P-MONTH-ARRIVAL",
        "P-MONTH-DEPARTURE",
        "P-OOO-INCLUDED",
    ]
    assert len(ids) == len(set(ids)), "a repeated id would make the named cause ambiguous"


def test_a_permutation_index_only_offers_permutations_that_apply(
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
) -> None:
    """A nationality permutation cannot explain an occupancy variance, and offering it would invite
    exactly the false explanation the tolerance check is there to prevent."""
    index = PermutationIndex(records, inventory, periods, policy)
    for permutation_id in index.applicable(Metric.OCCUPANCY_PCT):
        assert not permutation_id.startswith("P-NAT-")
    assert "P-CHILDREN-EXCLUDED" in index.applicable(Metric.GUESTS_BY_NATIONALITY)


def test_permutations_can_be_disabled_without_breaking_the_ladder(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """Disabled is a legitimate configuration, not a broken one: every variance simply falls through
    the V2 rung. What must not happen is a crash, or a silent reclassification to something else."""
    off = policy.model_copy(
        update={"permutations": policy.permutations.model_copy(update={"enabled": False})}
    )
    result = _run(_swap(claims, FEB_SOLD, 1265), results, records, inventory, periods, off)
    finding = _only(result)
    assert finding.variance_class is VarianceClass.TRANSCRIPTION
    assert finding.explaining_permutation is None


# ══ V1 · transcription ═══════════════════════════════════════════════════════


def test_an_unexplained_difference_is_transcription_with_a_correction(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """A figure no committed permutation reproduces. This one goes back to the hotel, and it is the
    only class that proposes a corrected value."""
    result = _run(
        _swap(claims, FEB_SOLD, 1300), results, records, inventory, periods, policy, index
    )
    finding = _only(result)

    assert finding.variance_class is VarianceClass.TRANSCRIPTION
    assert finding.escalates_to is EscalationTarget.HOTEL
    assert finding.is_hotel_error is True
    assert finding.proposed_correction == Decimal(1299)
    assert finding.difference == Decimal(1)
    assert result.hotel_errors == (finding,)


# ══ V5 · completeness, in both directions ════════════════════════════════════


def test_a_figure_the_workbook_omits_is_reported_as_incomplete(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-MAT-04. And the finding has no Excel cell, because the hotel wrote nothing — which is the
    finding. The absence is typed and carries its reason."""
    without = [claim for claim in claims if claim.key.rendered != FEB_SOLD]
    finding = _only(_run(without, results, records, inventory, periods, policy))

    assert finding.variance_class is VarianceClass.COMPLETENESS
    assert finding.clause == "D-MAT-04"
    assert finding.claimed is None
    assert finding.computed == Decimal(1299)
    assert isinstance(finding.excel_ref, NotReached)
    assert isinstance(finding.source_ref, PdfRef)
    assert finding.is_hotel_error is True


def test_a_figure_the_source_cannot_support_is_the_more_serious_direction(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-MAT-05. The workbook asserts a figure its own export does not produce. It has a cell and no
    source rows — the mirror of the case above, and a separate clause so a report never conflates
    "you left something out" with "you stated something unsupportable"."""
    orphan = Claim(
        key=MetricKey(
            metric=Metric.GUESTS_BY_NATIONALITY,
            period="2026-02",
            dimension=Dimension.NATIONALITY_ISO2,
            value="ZW",
        ),
        value=Decimal(7),
        excel_ref=ExcelRef(sheet="Nationality", cell="C27"),
    )
    finding = _only(_run([*claims, orphan], results, records, inventory, periods, policy))

    assert finding.variance_class is VarianceClass.COMPLETENESS
    assert finding.clause == "D-MAT-05"
    assert finding.claimed == Decimal(7)
    assert finding.computed is None
    assert finding.excel_ref.citation == "Nationality!C27"
    assert isinstance(finding.source_ref, NotReached)


# ══ V6 · rounding, logged and never raised ═══════════════════════════════════


def test_a_difference_inside_tolerance_is_logged_not_raised(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-TOL-04 and D-MAT-03. 81.75 against 81.70 is inside the ±0.10pp band."""
    result = _run(
        _swap(claims, FEB_OCCUPANCY, "81.75"), results, records, inventory, periods, policy, index
    )
    finding = _only(result)

    assert finding.variance_class is VarianceClass.ROUNDING
    assert finding.severity is Severity.INFORMATIONAL
    assert finding.escalates_to is EscalationTarget.NONE
    assert finding.is_hotel_error is False
    assert result.raised == (), "informational findings are logged, never raised"
    assert result.informational == (finding,)


def test_a_count_has_no_tolerance_at_all(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-TOL-01. A difference of one guest is a finding, not a rounding artefact."""
    result = _run(
        _swap(claims, FEB_FRANCE, 59), results, records, inventory, periods, policy, index
    )
    finding = _only(result)
    assert finding.variance_class is not VarianceClass.ROUNDING
    assert finding.difference == Decimal(1)


# ══ V7 · the refusal that is never a hotel error ═════════════════════════════


def test_without_an_inventory_reference_occupancy_is_blocking_not_wrong(
    claims: Sequence[Claim],
    records: Sequence[ReservationRecord],
    periods: Sequence[Period],
    policy: Policy,
) -> None:
    """D-RNA-04 and D-MAT-01. Rooms available is never inferred from the reservations, so occupancy
    cannot be checked at all — and a figure we could not establish is one we have no standing to
    dispute. Every one of these is blocking and none counts against the property."""
    blind = compute_all(records, None, periods, policy)
    result = reconcile(claims, blind, records, None, periods, policy)

    assert result.halted is True
    assert result.hotel_errors == ()
    assert len(result.blocking) == 8  # occupancy and room-nights-available, four periods each
    for finding in result.blocking:
        assert finding.variance_class is VarianceClass.EXTRACTION_LIMIT
        assert finding.severity is Severity.BLOCKING
        assert finding.escalates_to is EscalationTarget.HUMAN_REVIEW
        assert finding.clause == "D-RNA-04"
        assert finding.is_hotel_error is False


def test_a_figure_nobody_claimed_and_nobody_could_compute_is_not_a_finding(
    claims: Sequence[Claim],
    records: Sequence[ReservationRecord],
    periods: Sequence[Period],
    policy: Policy,
) -> None:
    """Reported as a stated non-result rather than as a variance.

    A finding here would say "we could not check a figure nobody stated", which is not something a
    reviewer can act on and not something to send a hotel. It is still carried — silence would be
    the one unacceptable option.
    """
    without_occupancy = [
        claim
        for claim in claims
        if claim.key.metric not in (Metric.OCCUPANCY_PCT, Metric.ROOM_NIGHTS_AVAILABLE)
    ]
    blind = compute_all(records, None, periods, policy)
    result = reconcile(without_occupancy, blind, records, None, periods, policy)

    assert result.findings == ()
    assert len(result.not_verifiable) == 8
    assert {item.reason for item in result.not_verifiable} == {"missing_inventory_reference"}


# ══ the order is the correctness requirement ═════════════════════════════════


def test_definitional_is_tested_before_transcription(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-CLS-02, as a behaviour rather than as a list.

    The same figure — 1,265 February room-nights — is a *definitional* variance escalated to the
    policy owner. Test V1 before V2 and it becomes a transcription error escalated to the hotel,
    with a proposed correction telling a property to change a number that is right under its own
    reading of the rules. Both answers are plausible; only one is usable.
    """
    finding = _only(
        _run(_swap(claims, FEB_SOLD, 1265), results, records, inventory, periods, policy)
    )
    assert finding.variance_class is VarianceClass.DEFINITIONAL
    assert finding.escalates_to is EscalationTarget.POLICY_OWNER
    assert finding.proposed_correction is None
    assert finding.is_hotel_error is False


def test_extraction_limit_outranks_everything(
    claims: Sequence[Claim],
    records: Sequence[ReservationRecord],
    periods: Sequence[Period],
    policy: Policy,
) -> None:
    """D-CLS-01. The claim below is also wrong by 300 — and it does not matter.

    With no inventory reference the system cannot establish occupancy at all, so it has nothing to
    compare against and no standing to call the figure an error. Test V1 before V7 and the hotel is
    accused of a mistake the system could not actually see.
    """
    tampered = _swap(claims, FEB_OCCUPANCY, "99.99")
    blind = compute_all(records, None, periods, policy)
    result = reconcile(tampered, blind, records, None, periods, policy)

    occupancy = next(f for f in result.findings if f.key.rendered == FEB_OCCUPANCY)
    assert occupancy.variance_class is VarianceClass.EXTRACTION_LIMIT
    assert occupancy.is_hotel_error is False


def test_a_small_difference_is_not_absorbed_by_the_permutation_set(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """The precondition on the V2 rung, on the one case in the corpus where it is load-bearing.

    January has no out-of-order rooms, so `P-OOO-INCLUDED` computes **exactly** the baseline 69.09.
    A claim of 69.14 is therefore inside the ±0.10pp tolerance of both our own figure and that
    permutation's, and the rung's precondition is the only thing deciding which one wins.

    Without it a hotel's rounding artefact would be filed as a definitional disagreement and routed
    to the policy owner. The effect compounds: definitional items are never counted as hotel errors,
    so the system would quietly stop reporting small clerical differences at all.

    A first version of this test used February, where no applicable permutation lands anywhere near
    the baseline. It passed with the precondition deleted — which is the only way to discover that a
    test is decorative.
    """
    jan_occupancy = "occupancy_pct:2026-01"
    assert results.computed[jan_occupancy].value == Decimal("69.09")

    finding = _only(
        _run(
            _swap(claims, jan_occupancy, "69.14"),
            results,
            records,
            inventory,
            periods,
            policy,
            index,
        )
    )
    assert finding.variance_class is VarianceClass.ROUNDING
    assert finding.explaining_permutation is None

    # …and the permutation really would have claimed it. Without this the test would still pass on a
    # corpus where nothing came close, which is exactly how the first version went wrong.
    explanations = index.explanations(MetricKey.parse(jan_occupancy), Decimal("69.14"))
    assert "P-OOO-INCLUDED" in [e.permutation_id for e in explanations]


def test_the_ladder_walks_the_order_policy_declares(policy: Policy) -> None:
    """The order lives in one place. A ladder hardcoded here and an order declared in `policy.yaml`
    would be two statements of the same rule that agree until somebody changes one."""
    from tda.reconcile.classify import RUNGS

    assert [code.value for code in policy.classification.order] == ["V7", "V2", "V5", "V1", "V6"]
    assert set(policy.classification.order) == set(RUNGS), (
        "every class in the declared order must have a rung, and every rung must be declared"
    )


def test_a_reordered_ladder_is_refused_at_load(policy: Policy) -> None:
    """The order cannot be changed at all — the policy loader validates it against the documented
    sequence, so a run with V1 ahead of V2 is not something the system can be configured into.

    That is a stronger guarantee than the ladder checking at run time, and it is why the test below
    has to reach past validation to exercise the ladder's own backstop.
    """
    from pydantic import ValidationError

    from tda.policy.loader import Classification as PolicyClassification

    with pytest.raises(ValidationError, match="classification order is"):
        PolicyClassification.model_validate(
            {
                "order": ["V1", "V2", "V5", "V7", "V6"],
                "classes": policy.classification.model_dump()["classes"],
            }
        )


def test_a_variance_that_falls_off_the_ladder_is_loud_not_silent(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-CLS-06. There is no "other" bucket.

    Unreachable through a validated policy — which is the point of the test above — so this reaches
    past validation with `model_copy`, which does not re-run validators, to build a ladder missing
    the rung this variance needs.
    The backstop has to raise: returning `None` would make the variance *invisible*, which is
    strictly worse than blocking and exactly the silent drop this system is built to avoid.
    """
    from tda.reconcile.classify import ClassificationError, classify
    from tda.reconcile.join import join as join_pairs

    # `model_copy(update=...)` does not re-run validators, which is the only way past the loader's
    # order check. The index below is built from the *real* policy on purpose: one built from the
    # broken policy would fail inside `apply_permutation`, which does re-validate, and the test
    # would then pass for entirely the wrong reason.
    broken = policy.model_copy(
        update={
            "classification": policy.classification.model_copy(
                update={"order": (VarianceClass.ROUNDING,)}
            )
        }
    )
    index = PermutationIndex(records, inventory, periods, policy)
    pairs = join_pairs(_swap(claims, FEB_SOLD, 1300), results)
    variance = next(pair for pair in pairs if pair.rendered == FEB_SOLD)

    with pytest.raises(ClassificationError, match="unclassified"):
        classify(variance, index, broken)


# ══ definitional items never count as hotel errors ═══════════════════════════


def test_definitional_items_are_kept_out_of_the_hotel_error_count(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
    index: PermutationIndex,
) -> None:
    """D-MAT-06. A count of hotel errors that included policy disagreements would be a wrong number
    presented as a right one — and the worst kind, because it is the number that gets quoted."""
    mixed = _swap(_swap(claims, FEB_SOLD, 1265), FEB_AVAILABLE, 1680)
    mixed = _swap(mixed, FEB_FRANCE, 1000)  # explained by nothing: a real clerical error
    result = _run(mixed, results, records, inventory, periods, policy)

    assert len(result.findings) == 3
    assert len(result.definitional) == 2
    assert len(result.hotel_errors) == 1
    assert result.hotel_errors[0].key.rendered == FEB_FRANCE
    assert all(f.escalates_to is EscalationTarget.POLICY_OWNER for f in result.definitional)


def test_severity_by_class_agrees_with_the_materiality_thresholds(policy: Policy) -> None:
    """Two statements of the same rule live in policy, and they must not drift.

    `severity_by_class` says V1 is material; `thresholds.count.material_at_or_above` says a count
    difference of one is material. They agree by construction today because V1 is only reached
    outside tolerance and the count tolerance is exact — this asserts the two halves still line up.
    """
    assert policy.severity_for(VarianceClass.TRANSCRIPTION) is Severity.MATERIAL
    assert policy.severity_for(VarianceClass.ROUNDING) is Severity.INFORMATIONAL
    assert policy.severity_for(VarianceClass.EXTRACTION_LIMIT) is Severity.BLOCKING
    assert policy.materiality.thresholds["count"]["material_at_or_above"] == 1
    assert policy.tolerances.for_metric(Metric.ROOM_NIGHTS_SOLD).accepts(1.0) is False


def test_classification_consults_no_model(policy: Policy) -> None:
    """D-CLS-10, from the configuration side. The import guard says the same thing from the other."""
    assert policy.classification.consults_model is False


# ══ the permutation runner reproduces the metric library exactly ═════════════


def test_the_index_agrees_with_a_direct_recomputation(
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay],
    periods: Sequence[Period],
    policy: Policy,
) -> None:
    """The index is a cache, and a cache that disagreed with the thing it caches would name a wrong
    cause with full confidence. Checked against the metric library run directly."""
    index = PermutationIndex(records, inventory, periods, policy)
    permutation = next(p for p in policy.permutations.ordered if p.id == "P-MONTH-ARRIVAL")
    direct = compute_all(records, inventory, periods, apply_permutation(policy, permutation))

    claimed = direct.computed[FEB_SOLD].value
    explanations = index.explanations(MetricKey.parse(FEB_SOLD), claimed)
    assert "P-MONTH-ARRIVAL" in [e.permutation_id for e in explanations]
    assert next(e for e in explanations if e.permutation_id == "P-MONTH-ARRIVAL").value == claimed
