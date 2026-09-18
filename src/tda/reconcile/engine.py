"""Reconciliation end to end: join, classify, cite, and keep definitional items apart.

The three modules beside this one each answer one question — *which claim goes with which computed
value*, *does an alternative ruleset explain this*, *what class is it* — and this turns the answers
into `Finding`s a reviewer can act on. Its own job is the part none of them can do: **evidence**, and
**keeping the definitional items out of the hotel-error count**.

## Evidence, and the two honest absences

D-EV-01 requires both references on every finding, and `Finding` refuses construction without them.
Most of the time both are obvious — the claim's cell, and the computed value's rows. Two cases are
not, and both are resolved by carrying a typed absence with its reason rather than by inventing a
citation:

- An **orphan claim** nothing explains has no computed side at all, so there are no source rows. The
  finding cites the cell and records why there is no source.
- A **missing claim** has no cell, because the hotel wrote nothing. That *is* the finding.

A definitional finding is never either of those: it asserts that a specific computation reproduced
the claim, so the rows that computation used exist and are cited — and, importantly, they are the
**permutation's** rows, not the baseline's. Citing the baseline would point a reviewer at a
calculation that did not produce the number in the finding.

## Definitional items are not hotel errors (D-MAT-06)

`policy.materiality.definitional_items_separate_array` is `true`, and this is where that becomes
real. Findings are issued into one list so ids stay unique and stable, and the separation is
exposed as views over it: `definitional`, `raised`, `informational`, and a `hotel_errors` count that
runs through `Finding.is_hotel_error` — which excludes V2 and V7 by construction, so a caller cannot
get the count wrong even by trying. A "hotel errors" number that included policy disagreements would
be a wrong number presented as a right one.

## What is deliberately not here

No narrative. Every field a reviewer acts on is fixed before a model is asked for a sentence
(D-CLS-10), and `Finding.narrative` stays `None` until the narrative agent runs. A `detail` string is
written here in plain English so the finding is already readable without one — a system whose output
is incomprehensible until a model has been called is a system that cannot be trusted when the model
is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tda.contracts import (
    Finding,
    FindingIds,
    NotReached,
    Severity,
    VarianceClass,
)
from tda.reconcile.classify import classify
from tda.reconcile.join import Pairing, join
from tda.reconcile.permutations import PermutationIndex

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.contracts import (
        Claim,
        ExcelCitation,
        InventoryDay,
        NotVerifiable,
        Period,
        ReservationRecord,
        SourceCitation,
    )
    from tda.metrics import MetricResults
    from tda.policy import Policy
    from tda.reconcile.classify import Classification
    from tda.reconcile.join import Pair


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Every finding this submission produced, with the views a report needs.

    One list, several views. Ids are issued once in a stable order, so a reviewer's recorded
    decision keeps pointing at the same finding no matter which array a report renders it in.
    """

    findings: tuple[Finding, ...] = ()
    not_verifiable: tuple[NotVerifiable, ...] = ()
    pairs: tuple[Pair, ...] = ()

    @property
    def definitional(self) -> tuple[Finding, ...]:
        """V2, carried separately and escalated to the policy owner (D-MAT-06)."""
        return tuple(f for f in self.findings if f.variance_class is VarianceClass.DEFINITIONAL)

    @property
    def raised(self) -> tuple[Finding, ...]:
        """What a reviewer must decide on: everything except the informational residue (D-TOL-04)."""
        return tuple(f for f in self.findings if f.severity is not Severity.INFORMATIONAL)

    @property
    def informational(self) -> tuple[Finding, ...]:
        """Inside tolerance. Logged, never raised (D-MAT-03)."""
        return tuple(f for f in self.findings if f.severity is Severity.INFORMATIONAL)

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCKING)

    @property
    def halted(self) -> bool:
        """Derived, never stored — the same rule the extraction and claim layers use."""
        return bool(self.blocking)

    @property
    def hotel_errors(self) -> tuple[Finding, ...]:
        """The findings that count against the property.

        Through `Finding.is_hotel_error`, which excludes V2 and V7 by construction. A count assembled
        any other way here could drift from the contract; this one cannot.
        """
        return tuple(f for f in self.findings if f.is_hotel_error)


def _excel_citation(pair: Pair) -> ExcelCitation:
    if pair.claim is not None:
        return pair.claim.excel_ref
    return NotReached(
        reason=(
            "the workbook states no figure for this key - that absence is what the finding reports"
        )
    )


def _source_citation(pair: Pair, classification: Classification) -> SourceCitation:
    """Where the computed side came from, preferring the computation the finding is about.

    For a definitional finding that is the **permutation's** computation, not the baseline's. The
    finding says "this alternative ruleset reproduces your figure"; citing the baseline's rows would
    send a reviewer to check a calculation that produced a different number.
    """
    if classification.explanations:
        return classification.explanations[0].computed.primary_ref
    if pair.computed is not None:
        return pair.computed.primary_ref
    return NotReached(
        reason=(
            "the source data supports no value for this key, so there are no rows to cite - that "
            "absence is what the finding reports"
        )
    )


def reconcile(
    claims: Sequence[Claim],
    results: MetricResults,
    records: Sequence[ReservationRecord],
    inventory: Sequence[InventoryDay] | None,
    periods: Sequence[Period],
    policy: Policy,
    *,
    index: PermutationIndex | None = None,
) -> Reconciliation:
    """Join, classify and cite. No model is consulted anywhere below this line (D-CLS-10).

    `records`, `inventory` and `periods` are taken as well as `results` because the permutation
    runner has to *recompute* the metrics under alternative rulesets — the baseline results alone
    cannot answer "what would this have been under a different definition", and that question is the
    whole of D-CLS-07.

    `index` lets a caller reconciling the same records repeatedly — the permutation report, the
    repro harness, a test suite — build it once instead of once per call. It is a cache of a pure
    function of exactly the four arguments above, so supplying one computed from different records
    would produce confidently wrong causes; the parameter is keyword-only to make that an obvious
    thing to be doing rather than an easy positional mistake.
    """
    index = index or PermutationIndex(records, inventory, periods, policy)
    ids = FindingIds()

    findings: list[Finding] = []
    unverifiable: list[NotVerifiable] = []
    pairs = join(claims, results)

    for pair in pairs:
        if pair.pairing is Pairing.NOT_VERIFIABLE and pair.claim is None:
            # Nothing was claimed and nothing could be computed. There is no variance to report and
            # no one to report it to: a finding here would say "we could not check a figure nobody
            # stated". It is carried to the verdict as a stated non-result instead, which is what
            # `NotVerifiable` is for.
            assert pair.not_verifiable is not None  # implied by the pairing
            unverifiable.append(pair.not_verifiable)
            continue

        classification = classify(pair, index, policy)
        if classification is None:
            continue  # an exact match; there is nothing to say about it

        claimed = pair.claim.value if pair.claim is not None else None
        computed = pair.computed.value if pair.computed is not None else None
        findings.append(
            Finding(
                finding_id=ids.take(),
                key=pair.key,
                variance_class=classification.variance_class,
                severity=classification.severity,
                escalates_to=classification.escalates_to,
                claimed=claimed,
                computed=computed,
                # Required when both sides exist and forbidden otherwise; the contract checks the
                # arithmetic, so a wrong subtraction here fails construction rather than reaching a
                # reviewer.
                difference=(
                    claimed - computed if claimed is not None and computed is not None else None
                ),
                proposed_correction=classification.proposed_correction,
                explaining_permutation=classification.explaining_permutation,
                also_explained_by=classification.also_explained_by,
                source_ref=_source_citation(pair, classification),
                excel_ref=_excel_citation(pair),
                clause=classification.clause,
                # Plain English, written by code. The narrative agent replaces nothing here; it adds
                # a sentence beside it (D-CLS-10).
                narrative=classification.detail,
            )
        )

    return Reconciliation(
        findings=tuple(findings),
        not_verifiable=tuple(unverifiable),
        pairs=tuple(pairs),
    )
