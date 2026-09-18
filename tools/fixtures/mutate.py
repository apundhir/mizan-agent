"""The mutation engine: a declared target, resolved through the claim table, and damaged.

Nothing here decides what a mutation *means*. It produces a claim table, and a claim table is the
only thing it produces: the same structure `datagen.render_workbook` renders the demo submission
from, with some of the figures changed. The document and the expectation are then built from that
one artefact, which is what makes them unable to disagree.

## Aggregate propagation, and the finding it produces

The nationality sheet has three month columns and a quarter column, and the quarter column is a
claim in its own right. A hotel that mistypes January's Germany figure also files a quarter total
containing the mistype, because the spreadsheet adds its own column up. So a mutation of one
monthly figure rewrites the quarter figure too, and the derivation then finds **two** mismatches
against ground truth rather than one.

Getting this wrong is not cosmetic. Leaving the quarter alone would produce a workbook whose own
three months do not sum to its own quarter total, which `tda.excel.selfcheck` reports before any
comparison happens. The fixture would then be exercising the self-consistency check rather than
the reconciliation path it was written for, and it would look like it was working.

Occupancy is excluded from the roll-up for the reason `selfcheck.py` states about the same
arithmetic: a quarter's occupancy is a ratio over the quarter, not the sum of three monthly
percentages. Every other metric in the table is additive across the period, and that is checked
against ground truth rather than assumed, in
`test_every_additive_quarter_claim_is_the_sum_of_its_months`.

## Why the permutation overrides are read out of `policy.yaml`

`P-OCC-DENOM-ROOMS` is a committed id with a committed override, and the fixture reproduces the
override rather than a number somebody typed after running the pipeline once. Two consequences
worth having: a permutation renamed in policy fails the build here instead of producing a fixture
whose `explaining_permutation` names nothing, and a permutation whose override this module cannot
reproduce fails loudly rather than silently planting a mutation the expectation cannot explain.

The overrides reproduced here are the inventory-side ones, which need only
`datagen.ledger.generate_inventory`. A numerator-side permutation (`P-MONTH-ARRIVAL` and the rest)
would need the reservation ledger re-aggregated under a different month basis; that is a few more
lines in `_denominator`'s neighbour and is not written until a fixture asks for it, because
unexercised code in a fixture builder is code nobody has watched produce a document.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

import yaml

from datagen.aggregate import occupancy_pct
from datagen.claims import ClaimCell, relabelled, without
from datagen.ledger import generate_inventory
from datagen.spec import MONTHS, QUARTER
from fixtures.spec import FixtureError, FixtureSpec, MutationKind, Target

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from datagen.aggregate import PolicyView
    from datagen.ledger import InventoryRow

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
POLICY_PATH: Final = REPO_ROOT / "policy.yaml"

# The dotted override keys this module knows how to reproduce. Both act on the occupancy
# denominator and need nothing but the inventory reference.
DENOMINATOR_OVERRIDE: Final = "metrics.occupancy_pct.denominator"
OUT_OF_ORDER_OVERRIDE: Final = "metrics.occupancy_pct.exclude_out_of_order"
SUPPORTED_OVERRIDES: Final = frozenset({DENOMINATOR_OVERRIDE, OUT_OF_ORDER_OVERRIDE})

# Percentages are not additive across a period, so they are never rolled up. See the module
# docstring: the quarter's occupancy is a ratio over the quarter.
PERCENTAGE_SUFFIX: Final = "_pct"


def load_policy_document(path: Path = POLICY_PATH) -> dict[str, object]:
    """`policy.yaml`, parsed, for the two narrow views this package builds from it.

    Shared with `derive.py` rather than parsed twice. The views themselves are deliberately not
    shared: the mutation engine reads the permutation block and the derivation reads the
    classification block, so what each half of this package depends on is visible in one dataclass
    each. That is the same argument `datagen.aggregate.PolicyView` makes for its own narrowness.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise FixtureError(f"{path} did not parse to a mapping")
    return document


def policy_rule[T](document: dict[str, object], dotted: str, expected: type[T]) -> T:
    """Read a dotted path out of the policy document, type-checked, or fail naming the path.

    Checked rather than coerced, for the reason `datagen.aggregate._require_type` records:
    `bool("false")` is `True`, and a fixture that silently planted the opposite of a configured
    rule would derive an expectation nobody declared.
    """
    cursor: object = document
    for segment in dotted.split("."):
        if not isinstance(cursor, dict) or segment not in cursor:
            raise FixtureError(
                f"policy.yaml has no `{dotted}`. The fixture builder reads this rule to derive an "
                "expectation; it will not guess a default, because a guessed default produces an "
                "expected.json describing a policy nobody configured."
            )
        cursor = cursor[segment]
    if not isinstance(cursor, expected) or (expected is not bool and isinstance(cursor, bool)):
        raise FixtureError(
            f"policy.yaml `{dotted}` is {type(cursor).__name__} {cursor!r}, expected "
            f"{expected.__name__}"
        )
    return cursor


@dataclass(frozen=True, slots=True)
class Permutation:
    """One committed alternative reading of a definition, as policy declares it."""

    permutation_id: str
    explains: str
    applies_to: frozenset[str]
    override: dict[str, object]


@dataclass(frozen=True, slots=True)
class PermutationView:
    """The permutation block, indexed by id.

    Ordered in policy because the first permutation that reproduces a claim is the one named
    (D-CLS-09); indexed here because a fixture names one directly. The order is irrelevant to this
    module and load-bearing in `tda.reconcile`, which is why this is a dict and that is a tuple.
    """

    by_id: dict[str, Permutation]

    def require(self, permutation_id: str) -> Permutation:
        found = self.by_id.get(permutation_id)
        if found is None:
            known = ", ".join(sorted(self.by_id))
            raise FixtureError(
                f"policy.yaml declares no permutation {permutation_id!r}. A fixture naming one "
                f"that does not exist would derive an explaining_permutation the pipeline can "
                f"never produce. Declared: {known}"
            )
        return found


def load_permutation_view(document: dict[str, object] | None = None) -> PermutationView:
    ordered = policy_rule(document or load_policy_document(), "permutations.ordered", list)
    by_id: dict[str, Permutation] = {}
    for entry in ordered:
        if not isinstance(entry, dict):
            raise FixtureError(f"permutations.ordered carries a non-mapping entry: {entry!r}")
        permutation_id = str(entry["id"])
        by_id[permutation_id] = Permutation(
            permutation_id=permutation_id,
            explains=str(entry["explains"]),
            applies_to=frozenset(str(metric) for metric in entry["applies_to"]),
            override={str(key): value for key, value in dict(entry["override"]).items()},
        )
    return PermutationView(by_id=by_id)


# ── resolving a target through the claim table ───────────────────────────────


def resolve(table: Iterable[ClaimCell], target: Target) -> tuple[ClaimCell, ...]:
    """The claims a target names, in table order, or a failure naming the target.

    The failure is the load-bearing half. A target that resolves to nothing is a spec that has
    drifted from the corpus, and the quiet outcome is a fixture that materialises an unmutated
    workbook and derives an empty expectation: it would pass, in the same green line as the
    control fixture, and prove nothing at all.
    """
    found = tuple(
        cell for cell in table if target.describes(cell.metric, cell.period, cell.value_key)
    )
    if not found:
        raise FixtureError(
            f"{target.rendered} names no claim in the demo workbook. Either the corpus no longer "
            "carries that figure or the spec is wrong; a mutation of nothing is not a fixture."
        )
    return found


# ── the mutations ────────────────────────────────────────────────────────────


def transpose_digits(value: float) -> int:
    """Swap the first two digits of an integer count: 83 becomes 38.

    Refuses anything a transposition cannot sensibly happen to. A non-integral figure, a
    single-digit one, or one whose first two digits are equal would all produce a "mutation" that
    either changes nothing or changes the kind of number it is, and a fixture whose planted error
    is not an error is the failure mode this whole package is built to avoid.
    """
    if value != int(value):
        raise FixtureError(
            f"{value} is not a whole number; a digit transposition is a mistyped count, and "
            "applying one to a percentage would plant a different error than the fixture declares"
        )
    digits = str(abs(int(value)))
    if len(digits) < 2:
        raise FixtureError(f"{value} has one digit, so there is nothing to transpose")
    if digits[0] == digits[1]:
        raise FixtureError(
            f"transposing {value} would leave it unchanged. The fixture would then materialise a "
            "correct workbook and derive an empty expectation, which passes and proves nothing"
        )
    return int(digits[1] + digits[0] + digits[2:])


def _in_period(day: date, period: str) -> bool:
    """Whether a calendar day falls in a period key, for both `2026-02` and `2026-Q1`."""
    if "-Q" in period:
        year, quarter = period.split("-Q")
        return day.year == int(year) and (day.month - 1) // 3 + 1 == int(quarter)
    parts = period.split("-")
    if len(parts) != 2:
        raise FixtureError(f"{period!r} is neither a month nor a quarter key")
    return day.year == int(parts[0]) and day.month == int(parts[1])


def _available(row: InventoryRow, *, exclude_out_of_order: bool) -> int:
    """Rooms sellable on one day. Restated rather than read off `InventoryRow.rooms_available`,
    because the permutation that includes out-of-order rooms is exactly the case where the
    convenience property gives the wrong answer."""
    return row.rooms_total - row.rooms_out_of_order if exclude_out_of_order else row.rooms_total


def _denominator(
    inventory: Sequence[InventoryRow], period: str, permutation: Permutation, policy: PolicyView
) -> int:
    """The occupancy denominator the permutation implies, over the days of the period.

    `rooms_available` is the **maximum** over the period rather than the mean or the first day: a
    hotel making this mistake quotes its room count, and a room count is what somebody means by
    "we have 60 rooms". `tda.metrics.occupancy.rooms_available` states the same choice from the
    other side of the wall, which is the cross-check: two readings of D-OCC-02's failure mode
    arriving at the same denominator.
    """
    unsupported = set(permutation.override) - SUPPORTED_OVERRIDES
    if unsupported:
        raise FixtureError(
            f"{permutation.permutation_id} overrides {sorted(unsupported)}, which this builder "
            "cannot reproduce from the inventory reference alone. See the module docstring: a "
            "numerator-side permutation needs the reservation ledger re-aggregated, and planting "
            "a mutation whose value is not the permutation's value would derive an expectation "
            "naming a permutation that does not explain it."
        )

    exclude = policy.occupancy_exclude_out_of_order
    if OUT_OF_ORDER_OVERRIDE in permutation.override:
        exclude = bool(permutation.override[OUT_OF_ORDER_OVERRIDE])

    days = [
        _available(row, exclude_out_of_order=exclude)
        for row in inventory
        if _in_period(row.day, period)
    ]
    if not days:
        raise FixtureError(f"the inventory reference carries no day in {period}")

    if permutation.override.get(DENOMINATOR_OVERRIDE) == "rooms_available":
        return max(days)
    return sum(days)


def _recomputed(
    claim: ClaimCell,
    indexed: dict[str, ClaimCell],
    inventory: Sequence[InventoryRow],
    permutation: Permutation,
    policy: PolicyView,
) -> float:
    """The occupancy the hotel would have filed under this permutation.

    The numerator is the hotel's **own** stated room-nights sold, read off the claim table rather
    than recomputed. That is what the mistake actually is: the property divided a figure it also
    submitted by the wrong denominator, and every permutation reproduced here leaves the numerator
    alone. Taking the numerator from the same table the workbook is rendered from also means the
    figure on the Occupancy sheet is arithmetically consistent with its own two neighbours under
    the permutation, which is the only way a reader could ever agree with it.
    """
    if not claim.metric.endswith(PERCENTAGE_SUFFIX):
        raise FixtureError(
            f"{claim.key} is not a percentage, and the permutations reproduced here act on the "
            "occupancy denominator only"
        )
    if claim.metric not in permutation.applies_to:
        raise FixtureError(
            f"policy.yaml says {permutation.permutation_id} applies to "
            f"{sorted(permutation.applies_to)}, not to {claim.metric}"
        )

    sold = indexed.get(f"room_nights_sold:{claim.period}")
    if sold is None:
        raise FixtureError(
            f"no room_nights_sold claim for {claim.period}, so the numerator of the recomputed "
            "occupancy would have to be invented"
        )
    denominator = _denominator(inventory, claim.period, permutation, policy)
    return float(occupancy_pct(int(sold.value), denominator, policy))


# ── the roll-up ──────────────────────────────────────────────────────────────


def _rolled_up(
    table: tuple[ClaimCell, ...], changed: Iterable[ClaimCell], months: tuple[str, ...]
) -> tuple[ClaimCell, ...]:
    """Rewrite the quarter claim of every additive group a monthly mutation touched.

    A group is one metric and one dimension value. Only groups whose changed claims are all
    monthly are rolled up: a mutation aimed at the quarter figure itself is a hotel that mistyped
    its roll-up and nothing else, and recomputing it here would silently undo the fixture.
    """
    groups = {
        (cell.metric, cell.value_key)
        for cell in changed
        if cell.period in months and not cell.metric.endswith(PERCENTAGE_SUFFIX)
    }
    if not groups:
        return table

    monthly_totals = {
        group: sum(
            cell.value
            for cell in table
            if (cell.metric, cell.value_key) == group and cell.period in months
        )
        for group in groups
    }
    return tuple(
        replace(cell, value=monthly_totals[(cell.metric, cell.value_key)])
        if cell.period == QUARTER and (cell.metric, cell.value_key) in monthly_totals
        else cell
        for cell in table
    )


# ── the entry point ──────────────────────────────────────────────────────────


def mutated(
    spec: FixtureSpec,
    table: tuple[ClaimCell, ...],
    policy: PolicyView,
    permutations: PermutationView | None = None,
) -> tuple[ClaimCell, ...]:
    """The claim table this fixture's workbook is rendered from.

    One arm per `MutationKind`, and the arm is chosen here and nowhere else. `derive.py` never
    reads the kind to decide *whether* something is wrong: it compares this table against ground
    truth. That split is the acceptance criterion of PRD-94, and it is visible in the fact that
    this function returns a claim table rather than anything resembling a finding.
    """
    mutation = spec.mutation
    if mutation.kind is MutationKind.NONE:
        return table

    assert mutation.target is not None  # guaranteed by Mutation.__post_init__
    targets = resolve(table, mutation.target)

    if mutation.kind is MutationKind.DELETE_DIMENSION:
        value_key = mutation.target.value_key
        assert value_key is not None  # guaranteed by Mutation.__post_init__
        return without(table, value_key)

    if mutation.kind is MutationKind.RELABEL:
        value_key = mutation.target.value_key
        assert value_key is not None  # guaranteed by Mutation.__post_init__
        assert mutation.new_label is not None  # guaranteed by Mutation.__post_init__
        # Only the printed label moves. The value and the value_key are what the pipeline compares
        # against ground truth, and neither changes: a relabelling is a document that says a
        # different word for the same country, not a document that claims a different figure. What
        # decides whether that word resolves is the committed lookup, not this function, which is
        # why `derive.py` reads `mutation.resolvable` rather than this module guessing at it.
        return relabelled(table, value_key, mutation.new_label)

    # Annotated rather than inferred: a transposed count stays an `int` at runtime and lands in
    # the cell as one, which is what a hotel types. `38.0` would read back as a different string.
    new_values: dict[str, float]
    if mutation.kind is MutationKind.TRANSPOSE_DIGITS:
        new_values = {cell.key: transpose_digits(cell.value) for cell in targets}
    else:
        assert mutation.permutation is not None  # guaranteed by Mutation.__post_init__
        view = permutations if permutations is not None else load_permutation_view()
        permutation = view.require(mutation.permutation)
        inventory = generate_inventory()
        indexed = {cell.key: cell for cell in table}
        new_values = {
            cell.key: _recomputed(cell, indexed, inventory, permutation, policy) for cell in targets
        }

    applied = tuple(
        replace(cell, value=new_values[cell.key]) if cell.key in new_values else cell
        for cell in table
    )
    changed = [cell for cell in applied if cell.key in new_values]
    return _rolled_up(applied, changed, MONTHS)
