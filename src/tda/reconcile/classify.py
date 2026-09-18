"""The classification ladder. The order is a correctness requirement, not an optimisation.

Five rungs, tested in the order `policy.classification.order` gives — `[V7, V2, V5, V1, V6]` — first
match wins. Get the order wrong and every finding is still *technically* correct while the report
becomes unusable, which is the failure mode this whole module is shaped around:

- **V7 first** (D-CLS-01), so the system never accuses a hotel of an error it could not actually see.
  A figure we could not establish is a figure we have no standing to dispute.
- **V2 before V1** (D-CLS-02), so a policy disagreement is never reported as a clerical mistake.
  Sending a hotel a correction for a number that is right under its own reading of the rules is how a
  correct finding costs more trust than it earns.
- **V5 before V1** (D-CLS-03), because a structural problem outranks a value difference.
- **V6 last** (D-CLS-05), as the residual: inside tolerance, logged and never raised (D-TOL-04).
- **Nothing falls off the end** (D-CLS-06). A variance that reaches the bottom unclassified is
  *blocking*, because there is no "other" bucket — an unexplained difference in a verification
  report is exactly the thing a reviewer cannot act on.

**The order is read from policy, not written here.** A ladder hardcoded in Python and an order
declared in `policy.yaml` would be two statements of the same rule, and they would agree right up
until somebody changed one. A test asserts the policy order is the documented one, and this walks it.

## The precondition on the V2 rung

The definitional test is not "does some permutation land near the claim". It is "does some
permutation reproduce the claim **when our own ruleset does not**". Without that precondition, every
half-point rounding difference would be labelled definitional the moment any of thirteen alternative
rulesets happened to land within tolerance — and definitional findings are routed to the policy owner
rather than the hotel, so the effect would be to quietly stop reporting small clerical errors at all.

## Why classification consults no model

`classification.consults_model` is pinned `false` in policy and the import guard fails the build if
this package imports an agent or a model SDK. The code finds the cause; the model only writes the
sentence (D-CLS-10). A narrative is written from a classification that is already fixed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tda.contracts import Severity, VarianceClass
from tda.reconcile.join import Pairing

if TYPE_CHECKING:
    from decimal import Decimal

    from tda.contracts import EscalationTarget
    from tda.policy import Policy
    from tda.reconcile.join import Pair
    from tda.reconcile.permutations import Explanation, PermutationIndex

# The clause each rung rests on. Cited to the reviewer on the finding, so it is the clause that
# justifies *this decision* rather than the clause that defines the metric.
CLAUSE_BY_CLASS: dict[VarianceClass, str] = {
    VarianceClass.EXTRACTION_LIMIT: "D-CLS-01",
    VarianceClass.DEFINITIONAL: "D-CLS-07",
    VarianceClass.COMPLETENESS: "D-MAT-04",
    VarianceClass.TRANSCRIPTION: "D-CLS-04",
    VarianceClass.ROUNDING: "D-TOL-04",
}


class ClassificationError(Exception):
    """The ladder could not classify a variance, or policy asked for a rung that does not exist."""


@dataclass(frozen=True, slots=True)
class Classification:
    """The decision, complete, before a single word of narrative is written."""

    variance_class: VarianceClass
    severity: Severity
    escalates_to: EscalationTarget
    clause: str
    detail: str
    explanations: tuple[Explanation, ...] = ()
    proposed_correction: Decimal | None = None

    @property
    def explaining_permutation(self) -> str | None:
        """The first permutation that fits. The others are recorded, not discarded (D-CLS-09)."""
        return self.explanations[0].permutation_id if self.explanations else None

    @property
    def also_explained_by(self) -> tuple[str, ...]:
        return tuple(e.permutation_id for e in self.explanations[1:])


def _within_tolerance(pair: Pair, policy: Policy) -> bool:
    """Whether the baseline already accounts for the claim.

    False whenever either side is absent: there is no difference to be inside a tolerance band, and
    answering `True` would let a structural problem pass as a rounding artefact.
    """
    if pair.claim is None or pair.computed is None:
        return False
    tolerance = policy.tolerances.for_metric(pair.key.metric)
    return tolerance.accepts(float(pair.claim.value - pair.computed.value))


def _v7(pair: Pair, _index: PermutationIndex, _policy: Policy) -> Classification | None:
    if pair.pairing is not Pairing.NOT_VERIFIABLE or pair.not_verifiable is None:
        return None
    unverifiable = pair.not_verifiable
    # The specific clause where the data says which one, rather than the generic ladder clause. A
    # reviewer told "D-RNA-04" knows to go and find the inventory reference; one told "D-CLS-01"
    # only knows that something could not be read.
    clause = "D-RNA-04" if unverifiable.reason == "missing_inventory_reference" else "D-CLS-01"
    return Classification(
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=_escalation(VarianceClass.EXTRACTION_LIMIT, _policy),
        clause=clause,
        detail=(
            f"{pair.rendered} could not be established from the source data "
            f"({unverifiable.reason}). {unverifiable.detail}"
        ),
    )


def _v2(pair: Pair, index: PermutationIndex, policy: Policy) -> Classification | None:
    """Definitional: an alternative ruleset reproduces the claim that ours does not."""
    if pair.claim is None:
        # No claimed value to reproduce. A missing claim is structural, and the V5 rung has it.
        return None
    if _within_tolerance(pair, policy):
        # Our own ruleset already accounts for this. See the module docstring: without this the
        # permutation set would absorb every small clerical difference.
        return None

    explanations = index.explanations(pair.key, pair.claim.value)
    if not explanations:
        return None

    return Classification(
        variance_class=VarianceClass.DEFINITIONAL,
        severity=_severity(VarianceClass.DEFINITIONAL, policy),
        escalates_to=_escalation(VarianceClass.DEFINITIONAL, policy),
        clause=CLAUSE_BY_CLASS[VarianceClass.DEFINITIONAL],
        detail=(
            f"the claimed {pair.claim.value} is reproduced exactly by {explanations[0].explains}"
            f" ({explanations[0].permutation_id})"
        ),
        explanations=tuple(explanations),
    )


def _v5(pair: Pair, _index: PermutationIndex, policy: Policy) -> Classification | None:
    """Completeness: one side of the pair does not exist."""
    if pair.pairing is Pairing.MISSING_CLAIM:
        assert pair.computed is not None  # guaranteed by the pairing
        return Classification(
            variance_class=VarianceClass.COMPLETENESS,
            severity=_severity(VarianceClass.COMPLETENESS, policy),
            escalates_to=_escalation(VarianceClass.COMPLETENESS, policy),
            clause="D-MAT-04",
            detail=(
                f"the source data supports {pair.computed.value} for {pair.rendered} and the "
                "workbook states no figure for it - the submission is incomplete"
            ),
        )
    if pair.pairing is Pairing.ORPHAN_CLAIM:
        assert pair.claim is not None  # guaranteed by the pairing
        return Classification(
            variance_class=VarianceClass.COMPLETENESS,
            severity=_severity(VarianceClass.COMPLETENESS, policy),
            escalates_to=_escalation(VarianceClass.COMPLETENESS, policy),
            # The more serious direction, and it gets its own clause so the two are never conflated
            # in a report: the workbook asserts a figure its own export does not produce.
            clause="D-MAT-05",
            detail=(
                f"the workbook states {pair.claim.value} for {pair.rendered}, and the source data "
                "supports no figure for it at all"
            ),
        )
    return None


def _v1(pair: Pair, _index: PermutationIndex, policy: Policy) -> Classification | None:
    """Transcription: a readable, non-definitional, structurally complete difference."""
    if pair.claim is None or pair.computed is None:
        return None
    if _within_tolerance(pair, policy):
        return None
    return Classification(
        variance_class=VarianceClass.TRANSCRIPTION,
        severity=_severity(VarianceClass.TRANSCRIPTION, policy),
        escalates_to=_escalation(VarianceClass.TRANSCRIPTION, policy),
        clause=CLAUSE_BY_CLASS[VarianceClass.TRANSCRIPTION],
        detail=(
            f"the workbook states {pair.claim.value} and the source data supports "
            f"{pair.computed.value}, a difference of "
            f"{pair.claim.value - pair.computed.value} that no committed permutation explains"
        ),
        # Only V1 proposes one, and the contract enforces that. Proposing a corrected value for a
        # definitional variance would be telling a hotel to change a number that is not wrong.
        proposed_correction=pair.computed.value,
    )


def _v6(pair: Pair, _index: PermutationIndex, policy: Policy) -> Classification | None:
    """Rounding: inside tolerance. Logged, never raised (D-TOL-04)."""
    if not _within_tolerance(pair, policy):
        return None
    assert pair.claim is not None and pair.computed is not None  # implied by _within_tolerance
    return Classification(
        variance_class=VarianceClass.ROUNDING,
        severity=_severity(VarianceClass.ROUNDING, policy),
        escalates_to=_escalation(VarianceClass.ROUNDING, policy),
        clause=CLAUSE_BY_CLASS[VarianceClass.ROUNDING],
        detail=(
            f"the workbook states {pair.claim.value} against {pair.computed.value}, inside the "
            f"tolerance for {pair.key.metric.value} - recorded, not raised"
        ),
    )


# The rungs, by the code policy names them with. The ladder walks `policy.classification.order`, so
# this is a lookup rather than a sequence: the sequence is policy's to declare.
RUNGS = {
    VarianceClass.EXTRACTION_LIMIT: _v7,
    VarianceClass.DEFINITIONAL: _v2,
    VarianceClass.COMPLETENESS: _v5,
    VarianceClass.TRANSCRIPTION: _v1,
    VarianceClass.ROUNDING: _v6,
}


def _severity(variance_class: VarianceClass, policy: Policy) -> Severity:
    return policy.severity_for(variance_class)


def _escalation(variance_class: VarianceClass, policy: Policy) -> EscalationTarget:
    return policy.escalation_for(variance_class)


def classify(pair: Pair, index: PermutationIndex, policy: Policy) -> Classification | None:
    """Walk the ladder in policy's order. `None` means there is nothing to report.

    `None` is returned for an exact match only. Every other pair produces a classification, and a
    pair that produces none raises: D-CLS-06 is explicit that an unclassified variance is blocking,
    and returning `None` for one would make it *invisible* instead, which is strictly worse than
    blocking and exactly the silent-drop this system is built to avoid.
    """
    if pair.is_exact_match:
        return None

    for code in policy.classification.order:
        rung = RUNGS.get(code)
        if rung is None:
            raise ClassificationError(
                f"policy.classification.order names {code}, which has no rung. The order and the "
                "ladder must describe the same set of classes."
            )
        result = rung(pair, index, policy)
        if result is not None:
            return result

    raise ClassificationError(
        f"{pair.rendered} reached the end of the classification ladder unclassified "
        f"(pairing={pair.pairing.value}). D-CLS-06: there is no 'other' bucket - an unexplained "
        "difference in a verification report is exactly what a reviewer cannot act on."
    )
