"""The product's metric library against the corpus generator's independent aggregation.

This is the test the whole M2 milestone was built to make possible, and Gate 2 of the build plan.
Two implementations, written from the same clauses in `docs/01-definitions.md` and sharing no code —
`tools/guard/import_guard.py` rule 2 forbids `tools/datagen/` from importing any of `tda` — must agree
on every metric the corpus establishes, **exactly**.

Exactly, not within a tolerance. A tolerance here would be measuring how closely two readings of the
same sentence happen to land, which answers nothing: the point is that they land on the same number.
The ±0.10pp band in `policy.yaml` is for comparing a *hotel's* figure against ours, where a rounding
difference is a real and forgivable thing. Between two implementations of one definition there is
nothing to forgive.

**When they disagree, decide which one is wrong against the definitions.** Never adjust one to match
the other. That instruction is on the Linear issue and it is the entire reason S1 was written before
any code: without a written definition to appeal to, "make the test pass" is the only available move,
and whichever implementation happens to be under the cursor wins the argument. A disagreement here is
good news found cheaply. The same disagreement found after the reconciliation engine exists arrives
dressed as a hotel error.

**On reading `ground_truth/`.** `corpus/demo/README.md` says nothing in the pipeline may read that
directory, and this test reads it. That is not an exception being carved out — it is what the
directory is for. The rule is about the *system under test* never seeing the answers; a test whose job
is to check the answers obviously must. Nothing here is importable by `tda`.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tda.contracts import (
    InventoryDay,
    InventoryRef,
    Metric,
    MetricKey,
    PdfRef,
    Period,
    RateCode,
    ReservationRecord,
    Status,
)
from tda.metrics import compute_all
from tda.policy import Policy, apply_permutation, load_policy

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "demo"
GROUND_TRUTH = CORPUS / "ground_truth"
SUBMISSION = CORPUS / "submission"

# The corpus covers 2026-Q1. Both the three months and the quarter are verified: a quarter is a
# separate key (D-KEY-01), so agreeing on the months does not imply agreeing on the quarter.
PERIODS = [Period.parse(p) for p in ("2026-01", "2026-02", "2026-03", "2026-Q1")]


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


@dataclass(frozen=True, slots=True)
class Truth:
    """`truth_metrics.json`, parsed into typed fields.

    Typed rather than a raw dict so the comparison below indexes named attributes instead of string
    keys into `Any`. A renamed field in the generator then fails at parse, naming the field, rather
    than surfacing as an empty metric set that makes Gate 2 pass vacuously.
    """

    policy_version: str
    period: str
    reservations: int
    inventory_days: int
    metrics: dict[str, float]

    @classmethod
    def load(cls, path: Path) -> Truth:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), f"{path} did not parse to a mapping"
        metrics = raw["metrics"]
        assert isinstance(metrics, dict), "truth_metrics.json `metrics` is not a mapping"
        return cls(
            policy_version=str(raw["policy_version"]),
            period=str(raw["period"]),
            reservations=int(raw["reservations"]),
            inventory_days=int(raw["inventory_days"]),
            metrics={str(key): float(value) for key, value in metrics.items()},
        )


@pytest.fixture(scope="module")
def truth() -> Truth:
    """The generator's answers, not ours."""
    path = GROUND_TRUTH / "truth_metrics.json"
    assert path.exists(), f"{path} is missing; run `make datagen`"
    return Truth.load(path)


@pytest.fixture(scope="module")
def records() -> list[ReservationRecord]:
    """The ledger, as canonical records.

    Read from the committed CSV rather than by importing the generator, so this test exercises the
    *data* the documents were rendered from rather than the code that produced it. `source` is
    synthesised: real citations come from the PDF extractor, and this test is about values.
    The refs are distinct per row so the citation-collapsing logic gets a realistic input rather than
    four hundred identical references.
    """
    path = GROUND_TRUTH / "reservations.csv"
    assert path.exists(), f"{path} is missing; run `make datagen`"

    parsed: list[ReservationRecord] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            page, line = divmod(index, 30)
            parsed.append(
                ReservationRecord(
                    reservation_id=row["reservation_id"],
                    hotel_id=row["hotel_id"],
                    guest_ref=row["guest_ref"],
                    nationality_iso2=row["nationality_iso2"],
                    adults=int(row["adults"]),
                    children=int(row["children"]),
                    rooms=int(row["rooms"]),
                    nights=int(row["nights"]),
                    room_nights=int(row["room_nights"]),
                    arrival_date=date.fromisoformat(row["arrival_date"]),
                    departure_date=date.fromisoformat(row["departure_date"]),
                    status=Status(row["status"]),
                    rate_code=RateCode(row["rate_code"]),
                    source=PdfRef(
                        file="pms_2026-01.pdf",
                        page=page + 1,
                        row_start=line + 1,
                        row_end=line + 1,
                    ),
                )
            )
    return parsed


@pytest.fixture(scope="module")
def inventory() -> list[InventoryDay]:
    """The inventory reference, from the file the hotel actually submits.

    This one comes out of `submission/` rather than `ground_truth/`, because it *is* a submitted
    input (D-RNA-01) — which also means the `InventoryRef` row numbers here are real rather than
    synthesised.
    """
    path = SUBMISSION / "inventory_2026-Q1.csv"
    assert path.exists(), f"{path} is missing; run `make datagen`"

    parsed: list[InventoryDay] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=1):
            parsed.append(
                InventoryDay(
                    hotel_id=row["hotel_id"],
                    day=date.fromisoformat(row["day"]),
                    rooms_total=int(row["rooms_total"]),
                    rooms_out_of_order=int(row["rooms_out_of_order"]),
                    source=InventoryRef(file=path.name, row_start=index, row_end=index),
                )
            )
    return parsed


@pytest.fixture(scope="module")
def computed(
    records: list[ReservationRecord], inventory: list[InventoryDay], policy: Policy
) -> dict[str, Decimal]:
    """Our answers, keyed identically to the generator's."""
    results = compute_all(records, inventory, PERIODS, policy)
    assert not results.not_verifiable, (
        "the corpus should establish every in-scope metric for every period; "
        f"not verifiable: {sorted(results.not_verifiable)}"
    )
    return {key: value.value for key, value in results.computed.items()}


# ── Gate 2 ───────────────────────────────────────────────────────────────────


def test_the_corpus_is_present_and_was_generated_by_a_known_policy(
    truth: Truth, policy: Policy
) -> None:
    """The comparison is only meaningful if both sides used the same ruleset.

    `truth_metrics.json` records the policy version it was computed under precisely so this can be
    checked rather than assumed. Comparing numbers produced under two different policies and calling
    the difference a bug would waste a day, and calling it agreement would be worse.
    """
    assert truth.policy_version == policy.version
    assert truth.reservations == 1200
    assert truth.inventory_days == 90
    assert truth.period == "2026-Q1"


def test_every_truth_metric_is_reproduced_exactly(
    truth: Truth, computed: dict[str, Decimal]
) -> None:
    """Gate 2. Every value in `truth_metrics.json`, with zero deviation.

    The failure message lists every disagreement with both numbers rather than stopping at the first,
    because a systematic difference (every month out by the same shape) and a single odd month point
    at completely different causes, and seeing one row cannot tell them apart.
    """
    expected = truth.metrics
    assert expected, "truth_metrics.json carries no metrics"

    disagreements: list[str] = []
    for key, value in sorted(expected.items()):
        ours = computed.get(key)
        if ours is None:
            disagreements.append(f"  {key}: generator {value}, metric library produced no value")
        elif ours != Decimal(str(value)):
            disagreements.append(f"  {key}: generator {value}, metric library {ours}")

    assert not disagreements, (
        f"{len(disagreements)} of {len(expected)} metrics disagree between the generator's "
        "independent aggregation and the product metric library:\n"
        + "\n".join(disagreements)
        + "\n\nResolve this by deciding which implementation is wrong AGAINST "
        "docs/01-definitions.md - never by adjusting one to match the other. That is what S1 was "
        "written first for."
    )


def test_the_metric_library_produces_nothing_the_generator_did_not(
    truth: Truth, computed: dict[str, Decimal]
) -> None:
    """The other direction, which the test above cannot see.

    An extra key means we computed something the generator does not know about — most likely a
    nationality counted in a period it should not have been, which would be a silent over-report. A
    test that only iterated the expected keys would pass while the library invented figures.
    """
    extra = sorted(set(computed) - set(truth.metrics))

    assert not extra, (
        f"the metric library produced {len(extra)} key(s) absent from truth_metrics.json:\n"
        + "\n".join(f"  {key} = {computed[key]}" for key in extra)
        + "\n\nAn extra key is a figure nothing supports. Most likely a period boundary: check "
        "which month a stay's guests are counted in (D-NAT-06)."
    )


def test_both_sides_cover_the_same_periods_and_metrics(computed: dict[str, Decimal]) -> None:
    """A sanity check on the comparison itself.

    The exact-match tests above would both pass on an empty intersection, so this asserts the
    comparison actually spans what it claims to: all four metrics, all four periods.
    """
    keys = [MetricKey.parse(key) for key in computed]

    assert {key.metric for key in keys} == set(Metric)
    assert {key.period for key in keys} == {period.rendered for period in PERIODS}
    assert len(computed) > 80, f"only {len(computed)} keys compared; the corpus has 94"


def test_the_comparison_actually_bites(
    records: list[ReservationRecord],
    inventory: list[InventoryDay],
    truth: Truth,
    policy: Policy,
) -> None:
    """Prove the exact comparison can fail, by running it under a ruleset that must disagree.

    Gate 2 passed on the first attempt, which is the moment to distrust it. A comparison that agrees
    is indistinguishable from a comparison that is not looking — wrong keys on both sides, a
    dictionary compared against itself, an empty intersection — and every one of those failure modes
    is silent and permanent.

    So the library is re-run under `P-MONTH-ARRIVAL`, which books each whole stay to its arrival
    month. With 25+ month-spanning stays in the corpus that *must* move February and March, and the
    number of disagreements is asserted to be substantial rather than merely non-zero: one
    disagreement could come from a single boundary case, while a permutation this fundamental should
    move most of the monthly room-night and occupancy figures.

    The quarter is asserted too, and the number there corrected a wrong assumption of mine. I expected
    the quarter total to be *unchanged*, on the reasoning that apportionment redistributes room-nights
    between months without creating or destroying them. That is true only for stays wholly inside the
    period. At a period boundary a whole-stay basis does both:

        12 stays arrived in December 2025 and departed into January.
            occupied_night: their January nights count.  arrival_month: nothing counts.   -66
        22 stays arrived in March and departed in April.
            occupied_night: their March nights count.    arrival_month: the WHOLE stay.  +110
                                                                                        ----
                                                                                         +44

    Both edges are in the corpus deliberately, and the exact figures are asserted below rather than
    the direction, because "it moved" would have been satisfied by my wrong reasoning too.
    """
    for candidate in policy.permutations.ordered:
        if candidate.id == "P-MONTH-ARRIVAL":
            permuted = apply_permutation(policy, candidate)
            break
    else:  # pragma: no cover - policy.yaml is schema-validated to contain it
        raise AssertionError("policy.yaml has no P-MONTH-ARRIVAL permutation")

    under_permutation = compute_all(records, inventory, PERIODS, permuted)
    expected = truth.metrics

    disagreeing = {
        key
        for key, value in under_permutation.computed.items()
        if key in expected and value.value != Decimal(str(expected[key]))
    }

    assert len(disagreeing) >= 4, (
        "running the metric library under P-MONTH-ARRIVAL produced "
        f"{len(disagreeing)} disagreement(s) with truth_metrics.json. It should produce several: "
        "the corpus has 25+ month-spanning stays and this permutation moves every one of them. "
        "Too few means the comparison above is not sensitive to apportionment, and Gate 2 is "
        "passing for the wrong reason."
    )
    quarter = under_permutation.value("room_nights_sold:2026-Q1")
    baseline_quarter = Decimal(str(expected["room_nights_sold:2026-Q1"]))
    assert quarter - baseline_quarter == 44, (
        f"the quarter total moved by {quarter - baseline_quarter} under P-MONTH-ARRIVAL, expected "
        "+44 (+110 from the 22 stays departing after 31 March, -66 from the 12 arriving before "
        "1 January). A different figure means the corpus's boundary edges have changed - which is "
        "worth knowing, because those edges are what make apportionment testable at all."
    )


# ── the properties the agreement rests on ────────────────────────────────────


def test_the_quarter_is_not_computed_as_the_sum_of_the_months(
    computed: dict[str, Decimal],
) -> None:
    """D-KEY-01. The quarter is summed over its own days, and that is a different statement.

    For the two count metrics it lands on the same number, and the test asserts that — it is a real
    cross-check on the apportionment. For occupancy it does **not**: a quarter's occupancy is its own
    ratio, not the mean of three monthly ratios. Asserting the mean would encode a bug.
    """
    for metric in ("room_nights_sold", "room_nights_available"):
        monthly = sum(computed[f"{metric}:2026-{month}"] for month in ("01", "02", "03"))
        assert computed[f"{metric}:2026-Q1"] == monthly

    quarter = computed["occupancy_pct:2026-Q1"]
    mean_of_months = sum(computed[f"occupancy_pct:2026-{m}"] for m in ("01", "02", "03")) / 3
    assert quarter != mean_of_months, (
        "the quarterly occupancy coincides with the mean of the monthly ones. That is arithmetically "
        "possible but would mean the months have equal denominators, which they do not here (one has "
        "an out-of-order window) - so it points at the quarter being computed as an average."
    )


def test_the_month_spanning_stays_are_what_make_the_agreement_non_trivial(
    records: list[ReservationRecord],
) -> None:
    """Without them, both implementations reduce to "sum room_nights by arrival month" and agreeing
    proves nothing about D-RNS-03 — the clause most likely to be read two ways.

    Asserted here rather than trusted to the corpus generator's own quota check, because this test is
    the one whose value depends on it.
    """
    spanning = [
        record
        for record in records
        if not record.is_day_use
        and {(night.year, night.month) for night in record.occupied_nights()}.__len__() > 1
    ]
    assert len(spanning) >= 25, (
        f"only {len(spanning)} month-spanning stays in the corpus. Below that, this comparison stops "
        "testing apportionment and the agreement becomes trivial."
    )


def test_occupancy_is_reported_to_two_decimal_places(computed: dict[str, Decimal]) -> None:
    """D-OCC-04. The compared value is the presentation-rounded one, so its exponent is fixed."""
    for key, value in computed.items():
        if key.startswith("occupancy_pct:"):
            assert value.as_tuple().exponent == -2, f"{key} = {value} is not at 2 decimal places"


def test_no_computed_value_is_negative(computed: dict[str, Decimal]) -> None:
    """Nothing in this domain can be negative, and a negative here would mean a sign error in
    apportionment that a total might otherwise absorb."""
    negative = {key: value for key, value in computed.items() if value < 0}
    assert not negative, f"negative values: {negative}"
