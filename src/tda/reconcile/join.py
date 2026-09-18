"""The join: every claim beside every computed value, on the canonical key.

Exact, never fuzzy. `MetricKey` exists so that this join is a dictionary lookup rather than a
similarity match (D-KEY-01..03), and the whole reconciliation rests on that: a fuzzy join would
silently pair a claim about German guests in February with a computed figure for January, and the
resulting finding would be confidently wrong about a figure nobody got wrong.

**Both directions of a non-match are failures, and they are not the same failure.** The story is
explicit and so is the policy:

- A **claim with no computed counterpart** (D-MAT-05) means the workbook asserts something the
  source data cannot support. This is the more serious direction: the hotel has stated a figure that
  nothing in its own export produces.
- A **computed value with no claim** (D-MAT-04) means the workbook is incomplete. The source supports
  a figure the submission is silent about.

Conflating them into "mismatch" would lose the distinction that tells a reviewer which of those two
letters to send.

There is a third non-match, and it is the one that must never be blamed on the hotel: a key the
system **could not establish a value for at all** — occupancy with no inventory reference (D-RNA-04),
or a period with no extracted rows. The claim may be perfectly correct; we cannot say. That is a V7,
and `Finding.is_hotel_error` is false for V7 by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.contracts import Claim, ComputedValue, MetricKey, NotVerifiable
    from tda.metrics import MetricResults


class Pairing(StrEnum):
    """What the join found for one key."""

    MATCHED = "matched"
    ORPHAN_CLAIM = "orphan_claim"
    MISSING_CLAIM = "missing_claim"
    NOT_VERIFIABLE = "not_verifiable"


@dataclass(frozen=True, slots=True)
class Pair:
    """One key, with whichever of the two sides exist.

    `key` is carried separately rather than read off whichever side is present, because for three of
    the four pairings one side is absent and a caller that reached for `pair.claim.key` would be
    right only by luck.
    """

    key: MetricKey
    pairing: Pairing
    claim: Claim | None = None
    computed: ComputedValue | None = None
    not_verifiable: NotVerifiable | None = None

    @property
    def rendered(self) -> str:
        return self.key.rendered

    @property
    def is_exact_match(self) -> bool:
        """Both sides present and identical. Nothing to report, and the ladder is never entered."""
        return (
            self.pairing is Pairing.MATCHED
            and self.claim is not None
            and self.computed is not None
            and self.claim.value == self.computed.value
        )


def join(claims: Sequence[Claim], results: MetricResults) -> list[Pair]:
    """Pair claims with computed values, in a deterministic order.

    Sorted by rendered key so that finding ids are stable across runs. Two runs over the same inputs
    that numbered their findings differently would make a verdict impossible to diff, and a reviewer's
    recorded decision references a finding id.

    A key that is both claimed and *not verifiable* pairs as `NOT_VERIFIABLE` rather than as an
    orphan. The difference matters more than it looks: an orphan claim says the hotel asserted
    something unsupportable, and this says we could not check. Reporting the second as the first is
    the specific accusation D-MAT-01 exists to prevent.
    """
    by_key = {claim.key.rendered: claim for claim in claims}
    pairs: list[Pair] = []

    for rendered in sorted(set(by_key) | set(results.computed) | set(results.not_verifiable)):
        claim = by_key.get(rendered)
        computed = results.computed.get(rendered)
        unverifiable = results.not_verifiable.get(rendered)

        if unverifiable is not None:
            pairs.append(
                Pair(
                    key=unverifiable.key,
                    pairing=Pairing.NOT_VERIFIABLE,
                    claim=claim,
                    not_verifiable=unverifiable,
                )
            )
        elif claim is not None and computed is not None:
            pairs.append(
                Pair(key=claim.key, pairing=Pairing.MATCHED, claim=claim, computed=computed)
            )
        elif claim is not None:
            pairs.append(Pair(key=claim.key, pairing=Pairing.ORPHAN_CLAIM, claim=claim))
        else:
            assert computed is not None  # the key came from one of the three dictionaries
            pairs.append(Pair(key=computed.key, pairing=Pairing.MISSING_CLAIM, computed=computed))

    return pairs
