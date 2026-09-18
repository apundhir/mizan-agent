"""The qualifying set — one filter, used by both metric families.

§2 of the definitions defines the qualifying set once, and that is a design constraint rather than
tidiness. Two filters would eventually drift, and the drift would surface as a variance between two
of the *hotel's own* numbers — occupancy and the nationality table disagreeing about the same
reservation — which reads like a hotel error and is a bug here.

So `qualifies()` is the only place a reservation is included or excluded, `Policy.qualifies()` is
the only place the rule is read, and everything downstream filters through this module.

Day-use is deliberately **not** part of qualification. A day-use reservation qualifies like any
other; what differs is that it contributes zero room-nights and one set of guests (D-QUAL-09,
D-QUAL-10). Folding it into the filter would make it invisible to both metric families, which is
wrong in a way that would be very hard to see in a total.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from tda.contracts import ReservationRecord
    from tda.policy import Policy


class MetricError(Exception):
    """The records handed to the metric library cannot be aggregated as they are.

    Raised rather than worked around. Every case is one the policy marks `blocking`, and the
    blocking *finding* is constructed upstream where there is a citation to attach — the metric
    library's job is to refuse, not to report.

    Deliberately not a `ValueError`: this is not a bad argument to a computation, it is a reason not
    to run one.
    """


def qualifies(record: ReservationRecord, policy: Policy) -> bool:
    """Whether a reservation belongs to the qualifying set (D-QUAL-01, D-QUAL-04).

    Status and rate code only. Both are closed enums, so there is no unknown-value branch here: an
    unrecognised vendor string is blocking at extraction (D-QUAL-03) and never reaches a record.
    """
    return policy.qualifies(record.status, record.rate_code)


def qualifying(records: Iterable[ReservationRecord], policy: Policy) -> list[ReservationRecord]:
    """The qualifying subset, in the order given."""
    return [record for record in records if qualifies(record, policy)]


def reject_duplicate_ids(records: Sequence[ReservationRecord]) -> None:
    """Refuse to aggregate a set containing a repeated reservation id (D-QUAL-07).

    The policy says `duplicate_reservation_id: blocking`, and the reason it has to be enforced *here*
    as well as at extraction is that the metric library would otherwise do exactly the wrong thing
    silently: it sums over what it is given, so a duplicated row is simply counted twice. The total
    would be wrong by a plausible amount, with nothing anywhere saying so.

    Never de-duplicated. Two rows with one id might be a double export or might be two genuine
    bookings with a clerical collision, and the system has no way to tell — which is the whole
    reason the policy makes it blocking rather than choosing.
    """
    seen: dict[str, int] = {}
    for record in records:
        seen[record.reservation_id] = seen.get(record.reservation_id, 0) + 1

    duplicates = sorted(rid for rid, count in seen.items() if count > 1)
    if duplicates:
        shown = ", ".join(duplicates[:5])
        more = f" (and {len(duplicates) - 5} more)" if len(duplicates) > 5 else ""
        raise MetricError(
            f"duplicate reservation ids: {shown}{more}. D-QUAL-07 makes this blocking and it is "
            "never de-duplicated: the metric library sums what it is given, so a duplicated row "
            "is counted twice and the total is wrong by a plausible amount. Two rows with one id "
            "may be a double export or two bookings with a clerical collision, and nothing in the "
            "data distinguishes them."
        )
