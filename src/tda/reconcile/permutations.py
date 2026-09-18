"""The permutation runner: how a definitional cause is established mechanically (D-CLS-07).

This is the module the POC exists to demonstrate. Detecting that a hotel's figure differs from ours
is arithmetic; saying **why** is the part that makes a report usable, and the difference between
"your February room-nights are 44 too high" and "your February room-nights match ours exactly if the
whole stay is apportioned to the arrival month rather than by occupied night" is the difference
between an accusation and a conversation.

And it is established **mechanically, not interpretively**. The engine re-runs the metric under a
small, committed, ordered set of alternative rulesets. If one reproduces the claimed value within
tolerance, the variance is definitional and that permutation is named. No model is consulted, no
heuristic search happens, and nothing is generated at run time (D-CLS-08). The set lives in
`policy.yaml`, which means adding a hypothesis is a configuration change reviewed like any other.

## Computed once, not once per finding

The naive shape recomputes the metric library for every variance, which for a quarter of nationality
keys is several hundred full passes. Instead every permutation is run **once** over the whole record
set at construction, and the lookups are dictionary reads. Thirteen extra passes total.

## Why the whole `ComputedValue` is kept, not just the number

A definitional finding must cite the rows it was reproduced from — `Finding.source_ref` is required
and a V2 may not carry a typed absence, because a definitional variance asserts that a *computation*
came out a particular way and a reviewer has to be able to check it. The permutation's own
`ComputedValue` carries exactly those citations. Keeping only the `Decimal` would force the finding
to cite the baseline's rows instead, which are the rows of a different computation.

That also makes one otherwise impossible case work: a claim the **baseline computes as absent** and a
permutation produces. A nationality table counting arrivals rather than guests can contain a country
the guest-counting basis never produces a key for. The baseline sees an orphan claim; the permutation
explains it exactly, and cites its own rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tda.metrics import compute_all
from tda.policy import apply_permutation

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

    from tda.contracts import (
        ComputedValue,
        InventoryDay,
        Metric,
        MetricKey,
        Period,
        ReservationRecord,
    )
    from tda.metrics import MetricResults
    from tda.policy import Policy


@dataclass(frozen=True, slots=True)
class Explanation:
    """One permutation that reproduces a claim, and the computation that did it."""

    permutation_id: str
    explains: str
    computed: ComputedValue

    @property
    def value(self) -> Decimal:
        return self.computed.value


class PermutationIndex:
    """Every committed permutation's results, computed once and looked up per variance.

    Construction runs the metric library once per permutation. Nothing here is lazy: a lazily
    populated index would make the cost of a run depend on how many variances happened to be
    checked, and the reproducibility argument is easier to make about a fixed amount of work.
    """

    def __init__(
        self,
        records: Sequence[ReservationRecord],
        inventory: Sequence[InventoryDay] | None,
        periods: Sequence[Period],
        policy: Policy,
    ) -> None:
        self._policy = policy
        self._results: dict[str, MetricResults] = {}
        self._explains: dict[str, str] = {}

        if not policy.permutations.enabled:
            # Disabled is a legitimate configuration - a run that wants only clerical findings. It
            # means every variance falls through the V2 rung, which is correct rather than broken.
            self._order: tuple[str, ...] = ()
            return

        self._order = tuple(p.id for p in policy.permutations.ordered)
        for permutation in policy.permutations.ordered:
            self._explains[permutation.id] = permutation.explains
            self._results[permutation.id] = compute_all(
                records, inventory, periods, apply_permutation(policy, permutation)
            )

    def applicable(self, metric: Metric) -> tuple[str, ...]:
        """The permutation ids that could explain a variance on this metric, in committed order.

        Committed order, because D-CLS-09 names the *first* permutation that fits and records the
        others. An order that depended on dictionary iteration would make the named cause vary
        between runs on identical input, which is the opposite of what this system is claiming.
        """
        if not self._order:
            return ()
        applies = {p.id for p in self._policy.permutations_for(metric)}
        return tuple(pid for pid in self._order if pid in applies)

    def explanations(self, key: MetricKey, claimed: Decimal) -> list[Explanation]:
        """Every applicable permutation that reproduces `claimed` within tolerance, in order.

        Tolerance is the metric's own, from policy (D-TOL-01..03) — the same band the baseline
        comparison uses. Using a looser one here would let a permutation "explain" a variance it does
        not actually account for, which is the most expensive possible way to be wrong: the finding
        would be routed to the policy owner, and the hotel's real clerical error would go unreported.
        """
        tolerance = self._policy.tolerances.for_metric(key.metric)
        rendered = key.rendered
        found: list[Explanation] = []

        for permutation_id in self.applicable(key.metric):
            computed = self._results[permutation_id].computed.get(rendered)
            if computed is None:
                continue
            if tolerance.accepts(float(claimed - computed.value)):
                found.append(
                    Explanation(
                        permutation_id=permutation_id,
                        explains=self._explains[permutation_id],
                        computed=computed,
                    )
                )
        return found
