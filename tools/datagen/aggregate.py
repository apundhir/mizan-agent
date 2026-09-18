"""The independent aggregation — ground truth, written from the clauses rather than from the code.

This is the module the architecture guard exists to protect. `tools/datagen/` may not import `tda`
at all, so nothing here is shared with `src/tda/metrics/`: the apportionment, the qualifying filter
and the rounding are all re-derived from `docs/01-definitions.md`. When the product's metric
library agrees with this file, that is two independent readings of a written definition arriving at
the same number — which is evidence. One implementation agreeing with itself is not.

**Configuration is shared; implementation is not.** The qualifying rules, the month bases and the
rounding policy are read straight out of `policy.yaml`, because a truth value is only meaningful
with respect to a ruleset — a `truth_metrics.json` that hard-coded its own opinion of which rate
codes qualify would disagree with the pipeline the first time the policy was amended, and the
disagreement would look like a pipeline bug. Reading the same input is not sharing an implementation.

The keys are rendered by `_render_key` below rather than by `tda.contracts.MetricKey`, for the same
reason. A unit test asserts every key this module emits parses cleanly through `MetricKey.parse`,
which is the cross-check: the generator states the key format independently and the test proves the
two statements match.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

import yaml

from datagen.spec import MONTHS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from datagen.ledger import InventoryRow, LedgerRow

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
POLICY_PATH: Final = REPO_ROOT / "policy.yaml"


class PolicyReadError(RuntimeError):
    """`policy.yaml` does not carry a rule this module needs.

    Loud rather than defaulted: a generator that silently fell back to its own idea of which rate
    codes qualify would emit a `truth_metrics.json` that no longer described the configured policy,
    and every downstream comparison would then be measuring the fallback.
    """


@dataclass(frozen=True, slots=True)
class PolicyView:
    """Exactly the rules this module needs, and nothing else.

    A narrow view rather than the whole document, so that the coupling between the generator and
    `policy.yaml` is visible in one dataclass. Anything the generator does not read here, it does
    not depend on.
    """

    version: str
    status_included: frozenset[str]
    rate_code_included: frozenset[str]
    day_use_counts_room_nights: bool
    day_use_counts_guests: bool
    occupancy_month_basis: str
    occupancy_exclude_out_of_order: bool
    occupancy_decimal_places: int
    guests_month_basis: str
    guests_include_children: bool
    guests_counts: str


def _require(mapping: object, path: str) -> object:
    """Read a dotted path out of the policy document, or fail naming the path."""
    cursor = mapping
    for segment in path.split("."):
        if not isinstance(cursor, dict) or segment not in cursor:
            raise PolicyReadError(
                f"policy.yaml has no `{path}`. The corpus generator reads this rule to compute "
                "ground truth; it will not guess a default, because a guessed default produces a "
                "truth_metrics.json that describes a policy nobody configured."
            )
        cursor = cursor[segment]
    return cursor


def _require_type[T](mapping: object, path: str, expected: type[T]) -> T:
    """The typed read. The type is checked rather than coerced, because `bool("false")` is `True`.

    That coercion is not hypothetical: `exclude_out_of_order: "false"` is a plausible hand-edit of a
    YAML file, `bool()` would turn it into `True`, and the corpus would then leave out-of-order
    rooms in the denominator while claiming to exclude them. A wrong type here has to be an error.
    """
    value = _require(mapping, path)
    if not isinstance(value, expected) or (expected is not bool and isinstance(value, bool)):
        raise PolicyReadError(
            f"policy.yaml `{path}` is {type(value).__name__} {value!r}, expected "
            f"{expected.__name__}. Coercing it would be worse than failing: a string 'false' "
            "becomes True and the corpus would silently describe the opposite rule."
        )
    return value


def _require_codes(mapping: object, path: str) -> frozenset[str]:
    value = _require(mapping, path)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyReadError(f"policy.yaml `{path}` must be a list of strings, got {value!r}")
    return frozenset(str(item) for item in value)


def load_policy_view(path: Path = POLICY_PATH) -> PolicyView:
    """The subset of `policy.yaml` the aggregation depends on."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise PolicyReadError(f"{path} did not parse to a mapping")

    return PolicyView(
        version=_require_type(document, "version", str),
        status_included=_require_codes(document, "qualifying.status.included"),
        rate_code_included=_require_codes(document, "qualifying.rate_code.included"),
        day_use_counts_room_nights=_require_type(
            document, "qualifying.day_use.counts_room_nights", bool
        ),
        day_use_counts_guests=_require_type(document, "qualifying.day_use.counts_guests", bool),
        occupancy_month_basis=_require_type(document, "metrics.occupancy_pct.month_basis", str),
        occupancy_exclude_out_of_order=_require_type(
            document, "metrics.occupancy_pct.exclude_out_of_order", bool
        ),
        occupancy_decimal_places=_require_type(
            document, "metrics.occupancy_pct.presentation_decimal_places", int
        ),
        guests_month_basis=_require_type(
            document, "metrics.guests_by_nationality.month_basis", str
        ),
        guests_include_children=_require_type(
            document, "metrics.guests_by_nationality.include_children", bool
        ),
        guests_counts=_require_type(document, "metrics.guests_by_nationality.counts", str),
    )


# ── the qualifying set (D-QUAL-01, D-QUAL-04) ────────────────────────────────


def qualifies(row: LedgerRow, policy: PolicyView) -> bool:
    """Whether a reservation belongs to the qualifying set.

    Defined once and used by both metric families, which is the point of D-QUAL's structure: two
    filters would eventually drift, and the drift would show up as a variance between two of the
    hotel's own numbers rather than as a bug here.
    """
    return row.status in policy.status_included and row.rate_code in policy.rate_code_included


# ── room-nights (D-RNS-03, D-RNS-04, D-RNA-02) ───────────────────────────────


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _apportioned_room_nights(row: LedgerRow, month: str, basis: str) -> int:
    """Room-nights this reservation contributes to `month` under `basis`.

    `occupied_night` is the baseline (D-RNS-03) and the other two bases exist because they are what
    a hotel actually does when it gets this wrong — the whole stay booked to the arrival or the
    departure month. Implementing all three here means the permutation *expectations* can be
    derived from the ledger too, rather than being asserted by hand in S7.
    """
    if row.is_day_use:
        return 0  # zero nights, so there is nothing to apportion
    if basis == "occupied_night":
        return row.room_nights_in(month)
    if basis == "arrival_month":
        return row.room_nights if _month_key(row.arrival_date) == month else 0
    if basis == "departure_month":
        return row.room_nights if _month_key(row.departure_date) == month else 0
    raise PolicyReadError(f"unknown month_basis {basis!r} for room-nights")


def room_nights_sold(rows: Sequence[LedgerRow], month: str, policy: PolicyView) -> int:
    """D-RNS-04. Summed over the qualifying set only.

    Day-use reservations contribute nothing: zero nights means zero room-nights, so they are
    invisible to occupancy while still being guests of the destination (D-QUAL-09, D-QUAL-10).
    The `counts_room_nights` rule is honoured for the permutation case, where a hotel has counted
    each day-use booking as one room-night.
    """
    total = 0
    for row in rows:
        if not qualifies(row, policy):
            continue
        if row.is_day_use:
            if policy.day_use_counts_room_nights and _month_key(row.arrival_date) == month:
                total += row.rooms
            continue
        total += _apportioned_room_nights(row, month, policy.occupancy_month_basis)
    return total


def room_nights_available(inventory: Sequence[InventoryRow], month: str, policy: PolicyView) -> int:
    """D-RNA-02. Σ over the dates in the month of `rooms_total − rooms_out_of_order`.

    A month with no inventory rows is a **closed month** and returns 0 (D-RNA-05) — which is a
    different statement from "the reference was not supplied" (D-RNA-04), and the difference is
    why this returns a number rather than raising.
    """
    total = 0
    for row in inventory:
        if _month_key(row.day) != month:
            continue
        total += (
            row.rooms_total - row.rooms_out_of_order
            if policy.occupancy_exclude_out_of_order
            else row.rooms_total
        )
    return total


def occupancy_pct(sold: int, available: int, policy: PolicyView) -> Decimal:
    """D-OCC-01, rounded half-up at the presentation boundary only (D-OCC-04).

    Zero denominator returns zero and is a closed month, not an error (D-OCC-03). No cap at 100
    (D-OCC-05): an over-100 value means rooms were oversold or the inventory reference is wrong,
    and both are worth surfacing rather than hiding.

    Computed in `Decimal` rather than `float` because the value is written to a JSON file that must
    be byte-identical across runs, and half-up rounding of a binary float is a coin toss at the
    midpoint — `round()` would give 66.34 or 66.35 depending on representation.
    """
    if available == 0:
        return Decimal("0").quantize(_quantum(policy))
    raw = Decimal(100) * Decimal(sold) / Decimal(available)
    return raw.quantize(_quantum(policy), rounding=ROUND_HALF_UP)


def _quantum(policy: PolicyView) -> Decimal:
    return Decimal(1).scaleb(-policy.occupancy_decimal_places)


# ── guests by nationality (D-NAT-06, D-NAT-07) ───────────────────────────────


def _guest_count(row: LedgerRow, policy: PolicyView) -> int:
    """D-NAT-02/03. Adults plus children, unless the policy excludes children."""
    return row.adults + row.children if policy.guests_include_children else row.adults


def guests_by_nationality(
    rows: Sequence[LedgerRow], month: str, policy: PolicyView
) -> dict[str, int]:
    """D-NAT-07. Guests attributed to the month of arrival, by the reservation's single nationality.

    The arrival-month basis is the one that makes a month-spanning stay contribute room-nights to
    two months and guests to one (D-NAT-08). A day-use visitor is counted: they were a guest of the
    destination even though they occupied no night (D-QUAL-10).

    `counts` and `month_basis` are read from policy so the alternative readings a hotel might have
    used — counting reservations, or counting room-nights — are derivable from the same ledger.
    """
    totals: dict[str, int] = defaultdict(int)
    for row in rows:
        if not qualifies(row, policy):
            continue
        if row.is_day_use and not policy.day_use_counts_guests:
            continue

        if policy.guests_counts == "guests":
            contribution = _guest_count(row, policy)
        elif policy.guests_counts == "arrivals":
            contribution = 1
        elif policy.guests_counts == "room_nights":
            contribution = row.room_nights
        else:
            raise PolicyReadError(f"unknown guests_by_nationality.counts {policy.guests_counts!r}")

        if policy.guests_month_basis == "arrival_month":
            if _month_key(row.arrival_date) == month:
                totals[row.nationality_iso2] += contribution
        elif policy.guests_month_basis == "occupied_night":
            nights_in_month = sum(1 for day in row.occupied_nights() if _month_key(day) == month)
            if nights_in_month:
                totals[row.nationality_iso2] += contribution
        else:
            raise PolicyReadError(f"unknown month_basis {policy.guests_month_basis!r} for guests")

    return dict(totals)


# ── the truth table ──────────────────────────────────────────────────────────


def _render_key(metric: str, period: str, dimension: str = "", value: str = "") -> str:
    """The canonical metric-key string, restated here rather than imported.

    `tda.contracts.MetricKey` renders the same form. That duplication is the point — a unit test
    round-trips every key emitted here through `MetricKey.parse`, so the two statements of the
    format are checked against each other instead of one being assumed.
    """
    base = f"{metric}:{period}"
    return f"{base}:{dimension}={value}" if dimension else base


def compute_truth(
    rows: Sequence[LedgerRow], inventory: Sequence[InventoryRow], policy: PolicyView
) -> dict[str, int | float]:
    """Every metric this corpus establishes, keyed canonically and sorted.

    Sorted because the result is serialised to a committed file: an ordering that depended on
    dictionary insertion would produce a diff every time the generator was refactored, and a diff
    nobody can explain is a diff nobody reads.

    Quarter-level keys are included as well as monthly ones. A quarter is a separate key and never
    the sum of its months as far as the join is concerned (D-KEY-01) — but it *is* derivable from
    the ledger, and a workbook that claims a quarterly figure needs something to be checked against.
    """
    truth: dict[str, int | float] = {}

    quarter_sold = 0
    quarter_available = 0
    quarter_guests: dict[str, int] = defaultdict(int)

    for month in MONTHS:
        sold = room_nights_sold(rows, month, policy)
        available = room_nights_available(inventory, month, policy)
        truth[_render_key("room_nights_sold", month)] = sold
        truth[_render_key("room_nights_available", month)] = available
        truth[_render_key("occupancy_pct", month)] = float(occupancy_pct(sold, available, policy))

        for iso2, count in guests_by_nationality(rows, month, policy).items():
            truth[_render_key("guests_by_nationality", month, "nationality_iso2", iso2)] = count
            quarter_guests[iso2] += count

        quarter_sold += sold
        quarter_available += available

    quarter = _quarter_of(MONTHS[0])
    truth[_render_key("room_nights_sold", quarter)] = quarter_sold
    truth[_render_key("room_nights_available", quarter)] = quarter_available
    truth[_render_key("occupancy_pct", quarter)] = float(
        occupancy_pct(quarter_sold, quarter_available, policy)
    )
    for iso2, count in quarter_guests.items():
        truth[_render_key("guests_by_nationality", quarter, "nationality_iso2", iso2)] = count

    return dict(sorted(truth.items()))


def _quarter_of(month: str) -> str:
    year, month_number = month.split("-")
    return f"{year}-Q{(int(month_number) - 1) // 3 + 1}"


def month_label(month: str) -> str:
    """`2026-02` → `February 2026`, for document headings."""
    year, number = month.split("-")
    return f"{calendar.month_name[int(number)]} {year}"
