"""The derivation: what a fixture should produce, computed from the fixture rather than declared.

This module is the acceptance criterion of PRD-94, and it is written so that the criterion is
checkable by reading it. Two properties hold, and both are structural rather than promised:

**Whether there is a finding is decided by arithmetic, never by the spec.** `expectation` takes the
mutated claim table, ground truth and the policy views, and walks a set difference over metric
keys. It reaches the mutation only after a non-match has already been established, and only to ask
what *kind* of non-match it is. So retargeting F2 to another country moves the expected cell
without a line of the expectation changing, and a spec that claims a mutation the renderer did not
apply derives an empty expectation and fails its own test rather than passing quietly.

**Severity and escalation come from `policy.yaml`.** Never from a spec file and never from a
literal here. `materiality.severity_by_class` says a V1 is material; `classification.classes`
says it escalates to the hotel and proposes a correction. A fixture that typed `material` into a
spec would keep asserting it after the policy said otherwise, and the eval would then be measuring
the fixture author's memory of the ruleset.

## The four outcomes of the set difference

- claimed absent, computed present: the workbook omitted a figure the source data supports. V5
  under D-MAT-04, and there is no Excel cell to cite because the cell does not exist.
- claimed present, computed absent: the workbook asserts a figure nothing supports. V5 under
  D-MAT-05, and this time the absence is on the source side.
- both present, inside tolerance: nothing. Logged by the pipeline as a rounding artefact and never
  raised (D-TOL-04), so it is not in the expectation either.
- both present, outside tolerance: a finding, carrying the cell and both values. The class comes
  from the mutation, because the numbers alone cannot distinguish a typo from a definitional
  disagreement, and that distinction is the one the fixture exists to test.

## The blocking short-circuit

A blocking finding halts the run before reconciliation, so a fixture whose mutation raises one
expects exactly that finding and nothing downstream. Implemented here rather than left to the
scorer: an expectation listing a blocking finding *and* the reconciliation findings that would
have followed is an expectation no correct run can satisfy, and it would read as a system defect.
None of F1 to F3 plants one, which is precisely why the rule is written now instead of when F6
arrives and it is load-bearing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import jsonschema

from datagen.claims import by_key
from datagen.spec import QUARTER
from fixtures import FIXTURE_SET_VERSION
from fixtures.mutate import load_policy_document, policy_rule
from fixtures.spec import FixtureError, FixtureSpec, Mutation, MutationKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from datagen.aggregate import PolicyView
    from datagen.claims import ClaimCell

SCHEMA_PATH: Final = Path(__file__).with_name("expected.schema.json")

PERCENTAGE_SUFFIX: Final = "_pct"

# Which mutation produces which class of non-match, once the arithmetic has established that there
# is one. A kind absent from here is not an oversight: a control fixture must not produce a
# non-match at all, a deletion produces a structural absence the set difference already classifies,
# and a relabelling's outcome depends on reference data an agent reads.
CLASS_BY_KIND: Final[dict[MutationKind, str]] = {
    MutationKind.TRANSPOSE_DIGITS: "V1",
    MutationKind.RECOMPUTE_UNDER: "V2",
}

# The metric whose computed side cites the inventory reference rather than a PDF page (D-RNA-01).
INVENTORY_SOURCED: Final = "room_nights_available"

# What `tda.excel.run._unmappable_label_finding` keys an unresolvable label to, restated here so the
# two sides can be compared. See `_unresolvable_relabel` below for why this key is asserted directly
# rather than reached by the set-difference walk every other fixture goes through.
ROOM_NIGHTS_SOLD: Final = "room_nights_sold"

PDF_CITATION: Final = "pdf_page"
INVENTORY_CITATION: Final = "inventory_ref"

DEFINITIONAL: Final = "V2"
EXTRACTION_LIMIT: Final = "V7"
BLOCKING: Final = "blocking"


@dataclass(frozen=True, slots=True)
class ClassificationView:
    """Exactly the policy this derivation reads, and nothing else.

    Narrow for the reason `datagen.aggregate.PolicyView` is narrow: the coupling between an
    expectation and the ruleset it was derived under should be one dataclass a reviewer can read,
    not a whole document somebody has to search.
    """

    severity_by_class: dict[str, str]
    escalates_to: dict[str, str]
    proposes_correction: dict[str, bool]
    missing_claim: tuple[str, str]
    orphan_claim: tuple[str, str]
    count_tolerance: Decimal | None
    percentage_tolerance: Decimal | None

    def severity_of(self, variance_class: str) -> str:
        severity = self.severity_by_class.get(variance_class)
        if severity is None:
            raise FixtureError(
                f"policy.yaml materiality.severity_by_class has no {variance_class}, so this "
                "derivation would have to invent a severity for it"
            )
        return severity

    def escalation_of(self, variance_class: str) -> str:
        target = self.escalates_to.get(variance_class)
        if target is None:
            raise FixtureError(
                f"policy.yaml classification.classes has no {variance_class}, so this derivation "
                "would have to invent an escalation target for it"
            )
        return target

    def tolerance_for(self, metric: str) -> Decimal | None:
        """The band a difference has to clear to become a finding. `None` means exact.

        Percentages take the band and everything else is a count, which is the same split
        `tda.policy.loader.Tolerances.for_metric` makes. Restated rather than imported, because
        this package may not import the product: if the two ever disagree, the fixture expects a
        finding the pipeline does not raise and the eval says so in one line.
        """
        return (
            self.percentage_tolerance
            if metric.endswith(PERCENTAGE_SUFFIX)
            else (self.count_tolerance)
        )


def _tolerance(document: dict[str, object], metric_type: str) -> Decimal | None:
    kind = policy_rule(document, f"tolerances.by_metric_type.{metric_type}.type", str)
    if kind == "exact":
        return None
    if kind != "absolute":
        raise FixtureError(
            f"policy.yaml tolerances.by_metric_type.{metric_type}.type is {kind!r}. This builder "
            "reproduces `exact` and `absolute`; a relative tolerance would need a second reading "
            "of D-TOL-02 and is not one this file may guess at."
        )
    return Decimal(
        str(policy_rule(document, f"tolerances.by_metric_type.{metric_type}.value", float))
    )


def _pairing_rule(document: dict[str, object], name: str) -> tuple[str, str]:
    block = policy_rule(document, f"materiality.{name}", dict)
    return str(block["class"]), str(block["severity"])


def load_classification_view(document: dict[str, object] | None = None) -> ClassificationView:
    """The classification, materiality and tolerance rules, read straight out of `policy.yaml`."""
    policy = document if document is not None else load_policy_document()
    classes = policy_rule(policy, "classification.classes", dict)
    severities = policy_rule(policy, "materiality.severity_by_class", dict)

    return ClassificationView(
        severity_by_class={str(k): str(v) for k, v in severities.items()},
        escalates_to={str(k): str(v["escalates_to"]) for k, v in classes.items()},
        proposes_correction={str(k): bool(v["proposes_correction"]) for k, v in classes.items()},
        missing_claim=_pairing_rule(policy, "missing_claim"),
        orphan_claim=_pairing_rule(policy, "orphan_claim"),
        count_tolerance=_tolerance(policy, "count"),
        percentage_tolerance=_tolerance(policy, "percentage_points"),
    )


# ── values ───────────────────────────────────────────────────────────────────


def _metric_of(key: str) -> str:
    return key.split(":", maxsplit=1)[0]


def _claimed_decimal(value: float) -> Decimal:
    """What the claim reader will make of the cell, stated the same way it states it.

    `Decimal(str(...))` and never `Decimal(float)`, which `tda.excel.read` records the reason for:
    the second turns `81.7` into `81.7000000000000028421709430404007434844970703125` and the
    expectation would then compare a claimed value against a number no workbook contains.
    """
    return Decimal(str(value))


def _computed_decimal(metric: str, value: float, policy: PolicyView) -> Decimal:
    """What the metric library will compute, at the precision it computes it.

    Percentages are quantised because rounding happens once, at the presentation boundary
    (D-OCC-04), and `truth_metrics.json` carries the value as JSON: `81.70` serialises to `81.7`
    and has to come back with its second decimal place, or the expectation's `computed` string
    disagrees with the pipeline's by a character.
    """
    stated = Decimal(str(value))
    if metric.endswith(PERCENTAGE_SUFFIX):
        return stated.quantize(Decimal(1).scaleb(-policy.occupancy_decimal_places))
    return stated


def _source_of(metric: str) -> str:
    """What the reconciliation engine will cite for this metric's computed side.

    `room_nights_available` is the one metric whose source is not a PDF, and D-RNA-01 is why: rooms
    available is a property attribute rather than a reservation attribute, so it does not appear in
    the reservation export at all and arrives in a separate CSV. `InventoryRef` carries no page for
    that reason, and inventing one to satisfy a type would put a false citation in front of a
    reviewer.

    Occupancy cites **both**, and the order decides this: `tda.metrics.compute._occupancy` appends
    the inventory days after the reservation rows, and `Finding.source_ref` takes
    `ComputedValue.source_rows[0]`. So the citation the engine attaches to an occupancy finding is a
    PDF page, even though the denominator is not on it. Read off the engine rather than reasoned
    from the metric: the denominator being absent from the report is a fact about the number, not
    about which citation is shown first.
    """
    return INVENTORY_CITATION if metric == INVENTORY_SOURCED else PDF_CITATION


# ── the four outcomes ────────────────────────────────────────────────────────


def _finding(
    key: str,
    variance_class: str,
    view: ClassificationView,
    *,
    claimed: str | None,
    computed: str | None,
    difference: str | None,
    excel: object,
    source: str,
    severity: str | None = None,
    proposed_correction: str | None = None,
    explaining_permutation: str | None = None,
) -> dict[str, object]:
    return {
        "key": key,
        "variance_class": variance_class,
        "severity": severity or view.severity_of(variance_class),
        "escalates_to": view.escalation_of(variance_class),
        "claimed": claimed,
        "computed": computed,
        "difference": difference,
        "proposed_correction": proposed_correction,
        "explaining_permutation": explaining_permutation,
        "evidence": {"excel": excel, "source": source},
    }


def _missing_claim(key: str, computed: Decimal, view: ClassificationView) -> dict[str, object]:
    """D-MAT-04. The workbook omitted a figure the source data supports."""
    variance_class, severity = view.missing_claim
    return _finding(
        key,
        variance_class,
        view,
        claimed=None,
        computed=str(computed),
        difference=None,
        # A typed absence rather than a blank. There is no cell: the row is not in the workbook,
        # and citing a coordinate would send a reviewer to whatever moved up into it.
        excel="not_reached",
        source=_source_of(_metric_of(key)),
        severity=severity,
    )


def _orphan_claim(
    claim: ClaimCell, claimed: Decimal, view: ClassificationView
) -> dict[str, object]:
    """D-MAT-05. The workbook asserts a figure the source data supports no value for."""
    variance_class, severity = view.orphan_claim
    return _finding(
        claim.key,
        variance_class,
        view,
        claimed=str(claimed),
        computed=None,
        difference=None,
        excel={"sheet": claim.sheet, "cell": claim.cell},
        source="not_reached",
        severity=severity,
    )


def _mismatch(
    mutation: Mutation,
    claim: ClaimCell,
    claimed: Decimal,
    computed: Decimal,
    view: ClassificationView,
) -> dict[str, object]:
    """A difference outside tolerance, classified by what the fixture did to produce it.

    This is the only place the mutation is consulted, and it is consulted after the non-match has
    already been established. The numbers cannot tell a mistyped count from a figure computed under
    a different definition: both are a claim that disagrees with ground truth by a lot. What
    separates them is whether a committed permutation reproduces the claim, and the fixture knows
    that because it planted it.
    """
    variance_class = CLASS_BY_KIND.get(mutation.kind)
    if variance_class is None:
        raise FixtureError(
            f"{claim.key} claims {claimed} against a computed {computed}, under a "
            f"{mutation.kind} mutation that cannot produce a value mismatch. Either the corpus "
            "and its ground truth have diverged, or the renderer wrote figures the claim table "
            "does not carry. A control fixture in particular must reach this line never."
        )

    explaining = mutation.permutation if variance_class == DEFINITIONAL else None
    if variance_class == DEFINITIONAL and explaining is None:
        raise FixtureError(
            f"{claim.key} is definitional and names no permutation, which D-CLS-07 forbids: a "
            "definitional finding that cannot say which reading reproduces the figure is an "
            "accusation with the evidence left out"
        )

    correction = str(computed) if view.proposes_correction.get(variance_class) else None
    return _finding(
        claim.key,
        variance_class,
        view,
        claimed=str(claimed),
        computed=str(computed),
        difference=str(claimed - computed),
        excel={"sheet": claim.sheet, "cell": claim.cell},
        source=_source_of(claim.metric),
        proposed_correction=correction,
        explaining_permutation=explaining,
    )


def _unresolvable_relabel(claim: ClaimCell, view: ClassificationView) -> dict[str, object]:
    """The one finding a relabelling to a label the lookup cannot resolve produces (D-NAT-12).

    Asserted directly rather than reached by the set-difference walk, because nothing about the
    walk would ever produce it: `mutate.relabelled` changes only the printed label, and the claim
    table still carries the correct value and value_key for this row, so `claimed == computed` for
    every key it touches. The real parser never gets that far. `tda.excel.read` resolves the label
    first, drops the row on failure, and `tda.excel.run._unmappable_label_finding` promotes the
    defect before any value is compared. This function mirrors that finding's shape exactly, key
    for key, because the two are asserting the same thing from opposite sides of the wall: this one
    from a claim table that still knows the truth, that one from a parser that never got to see it.

    Keyed to `room_nights_sold` for the quarter, matching the approximation the real finding makes:
    an unresolved dimension value has no `MetricKey` that can name it, so both sides pick the same
    stand-in rather than one inventing a key the other does not produce.
    """
    return _finding(
        f"{ROOM_NIGHTS_SOLD}:{QUARTER}",
        EXTRACTION_LIMIT,
        view,
        claimed=None,
        computed=None,
        difference=None,
        excel={"sheet": claim.sheet, "cell": claim.label_cell},
        source="not_reached",
    )


# ── the expectation ──────────────────────────────────────────────────────────


def _halt_at_first_blocking(findings: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """A blocking finding stops the run, so nothing downstream of it is expected.

    Returns the blocking finding alone. Keeping the reconciliation findings beside it would
    describe a run that both halted and completed, and no correct pipeline can produce that.
    """
    blocking = [f for f in findings if f["severity"] == BLOCKING]
    return [blocking[0]] if blocking else list(findings)


def _status(findings: Sequence[dict[str, object]], definitional: Sequence[object]) -> str:
    """The verdict status, restated from the rules `tda.graph.nodes.decide_status` applies.

    Most serious first: a run that could not read something is HALTED; definitional items go to
    the policy owner and are ESCALATED rather than filed against the hotel; real findings are
    FAIL; and PASS means zero of both rather than zero worth mentioning.
    """
    if any(f["severity"] == BLOCKING for f in findings):
        return "HALTED"
    if definitional:
        return "ESCALATED"
    if findings:
        return "FAIL"
    return "PASS"


def validate(payload: dict[str, object]) -> None:
    """Check a derived expectation against the contract, and refuse to write a broken one.

    Validated on write here and on read in `tda.eval`, which is what makes the schema a seam rather
    than a document: neither package can drift from it without one of the two failing loudly. The
    errors are reported together rather than one at a time, because a derivation that is wrong is
    usually wrong in a shape rather than in a field.
    """
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        detail = "\n".join(
            f"  {'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise FixtureError(
            f"the derived expectation does not satisfy {SCHEMA_PATH.name}:\n{detail}"
        )


def expectation(
    spec: FixtureSpec,
    table: tuple[ClaimCell, ...],
    truth: dict[str, float],
    policy: PolicyView,
    view: ClassificationView,
) -> dict[str, object]:
    """What this fixture should produce, derived from the mutated table and ground truth.

    The walk is over the union of the keys on both sides, sorted, so the output is deterministic
    and so a key absent from one side is a case rather than an omission. `spec.mutation` is read
    once, inside `_mismatch`, and never to decide whether a finding exists.

    One mutation bypasses the walk entirely: a relabelling to a label the lookup cannot resolve
    produces a finding the walk structurally cannot reach, because the claim table it operates on
    still carries the correct value under the correct key. See `_unresolvable_relabel`.
    """
    claimed = by_key(table)
    findings: list[dict[str, object]] = []

    if spec.mutation.kind is MutationKind.RELABEL and spec.mutation.resolvable is False:
        assert spec.mutation.target is not None  # guaranteed by Mutation.__post_init__
        value_key = spec.mutation.target.value_key
        assert value_key is not None  # guaranteed by Mutation.__post_init__
        relabelled_claim = next((cell for cell in table if cell.value_key == value_key), None)
        if relabelled_claim is None:
            raise FixtureError(
                f"{spec.fixture_id} relabels {value_key!r}, which names no claim in the mutated "
                "table. Either the corpus no longer carries that country or the spec is wrong."
            )
        findings.append(_unresolvable_relabel(relabelled_claim, view))
    else:
        for key in sorted(set(claimed) | set(truth)):
            claim = claimed.get(key)
            source_value = truth.get(key)

            if claim is None:
                assert source_value is not None  # a key in neither side cannot be in the union
                findings.append(
                    _missing_claim(
                        key, _computed_decimal(_metric_of(key), source_value, policy), view
                    )
                )
                continue

            claimed_value = _claimed_decimal(claim.value)
            if source_value is None:
                findings.append(_orphan_claim(claim, claimed_value, view))
                continue

            computed_value = _computed_decimal(claim.metric, source_value, policy)
            tolerance = view.tolerance_for(claim.metric)
            difference = abs(claimed_value - computed_value)
            if difference == 0 or (tolerance is not None and difference <= tolerance):
                continue

            findings.append(_mismatch(spec.mutation, claim, claimed_value, computed_value, view))

    reported = _halt_at_first_blocking(findings)
    definitional = [f for f in reported if f["variance_class"] == DEFINITIONAL]
    plain = [f for f in reported if f["variance_class"] != DEFINITIONAL]

    payload: dict[str, object] = {
        "fixture_set_version": FIXTURE_SET_VERSION,
        "fixture_id": spec.fixture_id,
        "why": spec.why,
        "mutation": spec.mutation.as_payload(),
        "status": _status(plain, definitional),
        "exhaustive": True,
        "findings": plain,
        "definitional_items": definitional,
    }
    validate(payload)
    return payload
