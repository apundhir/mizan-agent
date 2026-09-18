"""The fixture declarations: what is mutated, and nothing about what should come out.

Every field a spec may carry is here, and the set is deliberately small. A spec names a **claim**
and a **kind of damage**; it never names a cell, a value, a variance class, a severity or an
escalation target. Those are all derived (`derive.py`), and the reason is the defect PRD-94 exists
to prevent: a hand-written expectation is a second source of truth, and when the two disagree the
test keeps passing while asserting the wrong thing.

The rule is mechanical, so it is checkable by reading this file. If you can find a cell reference,
a number that should come out, or the word `material` below, the design has been broken.

## Why a target names a claim rather than a cell

`Target("guests_by_nationality", "2026-01", "DE")` resolves through `datagen.claims` to
`Nationality!B10`. Move the sheet layout and the spec still points at Germany's January figure; a
spec carrying `B10` would quietly start pointing at whatever moved into that cell. The claim table
is the same one the renderer writes from, so the mutation, the document and the expectation cannot
disagree about where a figure lives.

`period=None` names every period the metric is claimed for. That is not a shortcut: a hotel whose
occupancy formula divides by the wrong thing has the wrong formula in every row of the column, and
a fixture that mutated one month would be a fixture of a different, less likely mistake.

## Six fixtures, in two waves

F1 to F3 need no model recording: nothing about a value-only mutation changes the mapping agent's
digest, so they replay against the cassette already committed for the clean demo workbook. F4 to F6
each move a label or a row's coordinates, which does change the digest, so each needed its own
recording before it could be scored. F6 needed one more thing besides: an unresolvable label
produced no finding at all until `tda.excel.run` was taught to promote it (D-NAT-12).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class FixtureError(RuntimeError):
    """A fixture cannot be built or derived, and the build must stop rather than guess.

    Every raise in this package is loud on purpose. The alternative, a fixture that materialises
    with a silently degraded expectation, is worse than no fixture: the eval would report a score
    against something nobody declared, and a green scorecard is exactly the artefact a reviewer
    stops reading carefully.
    """


class MutationKind(StrEnum):
    """The kinds of damage a fixture may plant.

    Named after what the hotel *did*, never after what the system should *say*. `transpose_digits`
    is a clerical act; V1 is a conclusion about one, and the conclusion is the derivation's to
    reach from the numbers. Keeping the vocabularies apart is what stops a spec from asserting a
    class the arithmetic does not support.
    """

    NONE = "none"
    TRANSPOSE_DIGITS = "transpose_digits"
    RECOMPUTE_UNDER = "recompute_under"
    DELETE_DIMENSION = "delete_dimension"
    RELABEL = "relabel"


@dataclass(frozen=True, slots=True)
class Target:
    """The claim a mutation lands on, in the claim table's own vocabulary.

    `period` and `value_key` are optional and widen the target rather than defaulting it:
    `Target("occupancy_pct")` is every occupancy figure on the sheet, and
    `Target("guests_by_nationality", value_key="IS")` is Iceland in every month it appears.
    """

    metric: str
    period: str | None = None
    value_key: str | None = None

    def describes(self, metric: str, period: str, value_key: str | None) -> bool:
        return (
            metric == self.metric
            and (self.period is None or period == self.period)
            and (self.value_key is None or value_key == self.value_key)
        )

    @property
    def rendered(self) -> str:
        parts = [self.metric, self.period or "*"]
        if self.value_key is not None:
            parts.append(self.value_key)
        return ":".join(parts)


@dataclass(frozen=True, slots=True)
class Mutation:
    """One declared change to the claim table, checked for coherence at construction.

    The validation matters more than it looks. `RECOMPUTE_UNDER` without a permutation id, or
    `RELABEL` without a label, would otherwise reach the mutation engine and be discovered there,
    by which point the error names an internal function rather than the spec entry that is wrong.
    """

    kind: MutationKind
    target: Target | None = None
    permutation: str | None = None
    new_label: str | None = None
    resolvable: bool | None = None

    def __post_init__(self) -> None:
        if self.kind is MutationKind.NONE:
            if self.target is not None:
                raise FixtureError("a control fixture mutates nothing and must name no target")
            return

        if self.target is None:
            raise FixtureError(f"{self.kind} must name the claim it lands on")
        if (self.kind is MutationKind.RECOMPUTE_UNDER) != (self.permutation is not None):
            raise FixtureError(
                f"{self.kind} and permutation={self.permutation!r} disagree: a recomputation needs "
                "the id of the permutation that explains it, and nothing else may carry one"
            )
        if self.kind is MutationKind.RELABEL and (
            self.new_label is None or self.resolvable is None
        ):
            raise FixtureError(
                "a relabelling must state the label it writes and whether the reference data can "
                "resolve it, because those two decide whether the outcome is silence or a halt"
            )
        if self.kind in (MutationKind.DELETE_DIMENSION, MutationKind.RELABEL) and (
            self.target.value_key is None
        ):
            raise FixtureError(f"{self.kind} acts on a dimension value and must name one")

    def as_payload(self) -> dict[str, object]:
        """The mutation as the report echoes it. Never read back by the scorer.

        Present so a failing fixture can be read without opening this file. The schema keeps it a
        flat echo rather than a nested spec for the same reason: anything a scorer could reason
        from would be a second place the expectation lives.
        """
        return {
            "kind": str(self.kind),
            "metric": self.target.metric if self.target else None,
            "period": self.target.period if self.target else None,
            "value_key": self.target.value_key if self.target else None,
            "permutation": self.permutation,
            "new_label": self.new_label,
            "resolvable": self.resolvable,
        }


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """One scored fixture, as declared.

    `why` is carried into `expected.json` and from there into the scorecard, so a reader of a
    failing line does not have to find this file to learn what was being defended.
    """

    fixture_id: str
    why: str
    mutation: Mutation


F1: Final = FixtureSpec(
    fixture_id="F1",
    why=(
        "The control. An unmutated submission must come back with an empty finding list, because "
        "precision is the half a reviewer feels: a system that reports something on every return "
        "teaches an officer to skim the report, and then the one real finding is skimmed too."
    ),
    mutation=Mutation(kind=MutationKind.NONE),
)

F2: Final = FixtureSpec(
    fixture_id="F2",
    why=(
        "The commonest real error, a transposition in a hand-keyed count. It also proves the "
        "aggregate path: the quarter column is a claim in its own right, so one mistyped month "
        "produces two findings, and a system that reported only the cell that was typed would "
        "leave the roll-up standing."
    ),
    mutation=Mutation(
        kind=MutationKind.TRANSPOSE_DIGITS,
        target=Target(metric="guests_by_nationality", period="2026-01", value_key="DE"),
    ),
)

F3: Final = FixtureSpec(
    fixture_id="F3",
    why=(
        "The canonical definitional disagreement, occupancy divided by the room count rather than "
        "by room-nights. The hotel is not wrong in the way a typo is wrong, so the finding must "
        "reach the policy owner rather than the property, and it must name the permutation that "
        "reproduces the figure. Filed as a clerical error it would be an accusation."
    ),
    mutation=Mutation(
        kind=MutationKind.RECOMPUTE_UNDER,
        target=Target(metric="occupancy_pct"),
        permutation="P-OCC-DENOM-ROOMS",
    ),
)

F4: Final = FixtureSpec(
    fixture_id="F4",
    why=(
        "A country dropped from the workbook entirely, rather than mistyped. The row is gone from "
        "every period it appeared in, so the submission is incomplete rather than merely wrong, "
        "and there is no cell to cite because the cell does not exist: the absence is the finding."
    ),
    mutation=Mutation(
        kind=MutationKind.DELETE_DIMENSION,
        target=Target(metric="guests_by_nationality", value_key="KZ"),
    ),
)

F5: Final = FixtureSpec(
    fixture_id="F5",
    why=(
        "A country the workbook prints under a different name for the same code: `Czechia` where "
        "the demo corpus prints `Czech Republic`. Both resolve to CZ in the committed lookup, so "
        "this must come back with nothing to say. A system that raised a finding on vocabulary "
        "would be indistinguishable from one that raised it on substance, and precision is the "
        "half of this a reviewer actually feels."
    ),
    mutation=Mutation(
        kind=MutationKind.RELABEL,
        target=Target(metric="guests_by_nationality", value_key="CZ"),
        new_label="Czechia",
        resolvable=True,
    ),
)

F6: Final = FixtureSpec(
    fixture_id="F6",
    why=(
        "A country label the committed lookup has never seen. Not a typo and not a variant form: "
        "nothing in D-NAT-09 through D-NAT-11 resolves it, so the row cannot be placed against "
        "anything and the submission refuses rather than the parser guessing the nearest country. "
        "Blocking, escalated to a human, and never a hotel error, because the property did not "
        "cause a parser's vocabulary gap."
    ),
    mutation=Mutation(
        kind=MutationKind.RELABEL,
        target=Target(metric="guests_by_nationality", value_key="PL"),
        new_label="Mongolia",
        resolvable=False,
    ),
)

FIXTURES: Final[tuple[FixtureSpec, ...]] = (F1, F2, F3, F4, F5, F6)


def fixture(fixture_id: str) -> FixtureSpec:
    """The spec with this id, or a failure naming what exists.

    Used by the CLI and by the tests. A `KeyError` on a typo would name the dictionary rather than
    the fixture set, which is one indirection more than the reader needs.
    """
    for spec in FIXTURES:
        if spec.fixture_id == fixture_id:
            return spec
    known = ", ".join(spec.fixture_id for spec in FIXTURES)
    raise FixtureError(f"no fixture {fixture_id!r}; this set carries {known}")
