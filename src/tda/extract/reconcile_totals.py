"""Reconciling what we read against what the report says about itself.

Five checks, and each one catches a failure the others cannot see. That matters because the tempting
version of this module does only the first — compare the row count — which is the check most likely to
pass while extraction is wrong: losing a row is rare, misreading a *column* is common, and a misread
column leaves the count intact.

| Check | Catches | Invisible to |
|---|---|---|
| Row count | a lost or duplicated row | everything else |
| Σ printed `RN Month` | a misread numeric column | the row count |
| Σ qualifying room-nights | a misread status or rate code | both of the above |
| Exclusion counts | a status normalised to the wrong side of the qualifying line | the sums, when two errors cancel |
| Guests per nationality | a misread nationality code | every room-night check |

The third and fourth overlap deliberately. A row misread from `CANCELLED` to `CHECKED_OUT` *and*
another misread the other way leaves the qualifying total correct and the exclusion counts wrong, and
only the fourth check sees it. Two compensating errors is not a hypothetical failure mode for a
positional parser: a boundary that shifts by a point moves a whole column at once.

**Every disagreement is a V7 blocking finding and halts the run.** Never a hotel error. The report is
internally consistent with itself, so a disagreement means *we* misread it — and the difference between
"your March figure is wrong" and "we could not read your March report" is the difference between an
accusation and a question.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tda.metrics import guests_by_nationality, qualifies, room_nights_in
from tda.policy import MonthBasis

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.contracts import Period, ReservationRecord
    from tda.extract.totals import PrintedTotals
    from tda.policy import Policy

# How the report labels each exclusion in its printed breakdown. Mapped rather than matched loosely,
# so a renamed label in the document is a reconciliation failure rather than a silently skipped check.
EXCLUSION_LABELS: dict[str, str] = {
    "cancelled": "cancelled",
    "no-show": "no_show",
    "house use": "house_use",
    "day use (zero room-nights)": "day_use",
}


@dataclass(frozen=True, slots=True)
class Disagreement:
    """One reconciliation check that failed, in the terms a reviewer reads.

    `printed` and `extracted` are both carried because the *direction* is diagnostic: extracted below
    printed means rows or values were lost, and extracted above means something was counted twice.
    A single "mismatch" flag would throw that away.
    """

    check: str
    month: str
    printed: str
    extracted: str
    detail: str
    # The metric the disagreement makes untrustworthy, and the nationality code where there is one.
    # Carried as fields rather than recovered from `check` by the caller: parsing a human-readable
    # label back into data is how a reworded message silently changes a finding's key.
    metric: str = "room_nights_sold"
    dimension_value: str | None = None
    clause: str = "D-RNS-04"

    def __str__(self) -> str:
        return (
            f"{self.month} {self.check}: the report prints {self.printed}, we read "
            f"{self.extracted}. {self.detail}"
        )


def _exclusion_counts(records: Sequence[ReservationRecord], policy: Policy) -> dict[str, int]:
    """Our count of each exclusion, in the categories the report prints.

    The order of the branches is the report's order and it is not arbitrary: a cancelled house-use row
    is counted once, as cancelled, because that is what the report does. Counting it in both would make
    our total exceed the printed one on a document that is entirely correct.
    """
    counts = dict.fromkeys(EXCLUSION_LABELS.values(), 0)
    for record in records:
        if record.status.value == "CANCELLED":
            counts["cancelled"] += 1
        elif record.status.value == "NO_SHOW":
            counts["no_show"] += 1
        elif record.rate_code.value == "HOUSE":
            counts["house_use"] += 1
        elif record.is_day_use and qualifies(record, policy):
            counts["day_use"] += 1
    return counts


def reconcile(
    records: Sequence[ReservationRecord],
    printed_room_nights_month: dict[str, int],
    printed: PrintedTotals,
    period: Period,
    policy: Policy,
) -> list[Disagreement]:
    """Every check, run together. An empty list means the read is trustworthy.

    All five run even after one fails, rather than returning on the first. A reviewer given one
    disagreement will assume it is the only one; given four, they can see the shape — and the shape is
    what says whether a column shifted or a page was lost.
    """
    found: list[Disagreement] = []
    month = printed.month

    if len(records) != printed.listed_rows:
        found.append(
            Disagreement(
                check="row count",
                month=month,
                printed=f"{printed.listed_rows} listed rows",
                extracted=f"{len(records)} rows",
                detail=(
                    "Fewer means rows were lost or rejected; more means something was read twice. "
                    "Either way no total below this is trustworthy."
                ),
            )
        )

    extracted_listed = sum(printed_room_nights_month.values())
    if extracted_listed != printed.listed_room_nights:
        found.append(
            Disagreement(
                check="sum of the printed RN Month column",
                month=month,
                printed=str(printed.listed_room_nights),
                extracted=str(extracted_listed),
                detail=(
                    "This compares the column as printed against the report's own sum of it, so a "
                    "disagreement is a misread numeric column rather than a disputed definition."
                ),
            )
        )

    computed_sold = sum(
        room_nights_in(record, period, MonthBasis.OCCUPIED_NIGHT)
        for record in records
        if qualifies(record, policy)
    )
    if computed_sold != printed.room_nights_sold:
        found.append(
            Disagreement(
                check="qualifying room-nights sold",
                month=month,
                printed=str(printed.room_nights_sold),
                extracted=str(computed_sold),
                detail=(
                    "Derived from the dates and the qualifying rules, not from the printed column. A "
                    "disagreement here with the RN Month sums agreeing means a status or rate code "
                    f"was read wrong (policy {policy.version})."
                ),
            )
        )

    ours = _exclusion_counts(records, policy)
    for label, key in EXCLUSION_LABELS.items():
        if label not in printed.excluded:
            found.append(
                Disagreement(
                    check=f"exclusion breakdown ({label})",
                    month=month,
                    printed="<the label is absent from the printed breakdown>",
                    extracted=str(ours[key]),
                    clause="D-QUAL-01",
                    detail=(
                        "The reconciliation depends on the report naming each exclusion. A missing "
                        "label is a changed report, not a passed check."
                    ),
                )
            )
        elif printed.excluded[label] != ours[key]:
            found.append(
                Disagreement(
                    check=f"exclusion breakdown ({label})",
                    month=month,
                    printed=str(printed.excluded[label]),
                    extracted=str(ours[key]),
                    clause="D-QUAL-01",
                    detail=(
                        "This is the check that catches two compensating errors: a row misread into "
                        "the qualifying set and another misread out of it leave the totals correct "
                        "and this count wrong."
                    ),
                )
            )

    found.extend(_reconcile_nationalities(records, printed, period, policy))
    return found


def _reconcile_nationalities(
    records: Sequence[ReservationRecord],
    printed: PrintedTotals,
    period: Period,
    policy: Policy,
) -> list[Disagreement]:
    """Guests per nationality, against the printed table.

    Compared per code rather than only on the total. A total-only check passes whenever two codes are
    swapped, and swapping two codes is precisely what a misread `Nat` column does — the column is two
    characters wide and its neighbours are a reservation id and a date.
    """
    ours = guests_by_nationality(records, period, policy)
    found: list[Disagreement] = []

    for code in sorted(set(ours) | set(printed.guests_by_nationality)):
        mine = ours.get(code, 0)
        theirs = printed.guests_by_nationality.get(code, 0)
        if mine != theirs:
            found.append(
                Disagreement(
                    check=f"guests ({code})",
                    month=printed.month,
                    printed=str(theirs) if theirs else "<no row>",
                    extracted=str(mine) if mine else "<none>",
                    metric="guests_by_nationality",
                    dimension_value=code,
                    clause="D-NAT-07",
                    detail=(
                        "Compared per code rather than on the total, because a swapped pair of codes "
                        "leaves the total right and both rows wrong."
                    ),
                )
            )

    total_ours = sum(ours.values())
    if total_ours != printed.total_guests:
        found.append(
            Disagreement(
                check="total guests",
                month=printed.month,
                printed=str(printed.total_guests),
                extracted=str(total_ours),
                detail="The report's own total of the table above it.",
                clause="D-NAT-07",
            )
        )
    return found
