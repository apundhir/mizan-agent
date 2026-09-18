"""The policy loader must reject a policy it cannot vouch for, and permute without leaking.

The permutation tests carry most of the weight. A permutation that silently fails to change
anything is the worst bug available in this system: it reproduces the baseline, never matches a
claim, is never named in a finding, and is indistinguishable from a permutation correctly tried
that did not match. The visible consequence is a **definitional variance reported as a clerical
error** — a correct finding delivered as a false accusation against a hotel.

That bug was found for real while wiring this loader: `P-COMP-EXCLUDED` set
`rate_code.excluded` but left `COMP` in `rate_code.included`, which `qualifies()` reads. Every
override path resolved perfectly; only the meaning was wrong. Hence
`test_every_permutation_actually_changes_behaviour`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from tda.contracts import Metric, RateCode, Status
from tda.policy import (
    Denominator,
    MonthBasis,
    NationalityCounts,
    Policy,
    PolicyError,
    ToleranceType,
    apply_permutation,
    load_policy,
    permuted_policies,
)
from tda.policy.loader import DEFAULT_POLICY_PATH, DEFAULT_SCHEMA_PATH

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


def write_policy(tmp_path: Path, mutate: Any) -> Path:
    """Copy the real policy, apply one mutation, write it out."""
    raw = yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


# ── loading ──────────────────────────────────────────────────────────────────


def test_the_committed_policy_loads(policy: Policy) -> None:
    assert policy.version == "1.3.1"


def test_the_model_is_pinned_and_the_choice_is_deliberate(policy: Policy) -> None:
    """Sonnet, and this test is where changing it becomes a conversation.

    The reasoning is the architecture's own: model capability is not load-bearing for correctness
    here. Code performs every calculation, agents return keys and references, and the agent schema
    lint fails the build if a numeric field appears on an agent contract — so a POC that needed a
    frontier model to get its numbers right would have falsified its central claim.

    Pinned rather than left to a call site because the id is stamped into every verdict (D-EV-04)
    **and** hashed into every cassette key. Changing it re-records every cassette, which is the
    intended cost: a number produced under a different model is a different number.
    """
    assert policy.model.model_id == "claude-sonnet-5"
    assert policy.model.provider == "replay", (
        "the committed default must be replay - offline, free, and byte-reproducible. A committed "
        "`anthropic` default would make every test run cost money and need a key."
    )


def test_the_committed_baseline_is_the_documented_one(policy: Policy) -> None:
    """Pin the six contestable defaults. If somebody changes one, this test is where the
    change becomes a conversation instead of a surprise in a fixture two stories later."""
    occupancy = policy.metrics.occupancy_pct
    nationality = policy.metrics.guests_by_nationality

    assert occupancy.denominator is Denominator.ROOM_NIGHTS_AVAILABLE  # D-OCC-02
    assert occupancy.month_basis is MonthBasis.OCCUPIED_NIGHT  # D-RNS-03
    assert occupancy.exclude_out_of_order is True  # A-08
    assert occupancy.cap_at_100 is False  # D-OCC-05
    assert nationality.counts is NationalityCounts.GUESTS  # A-06
    assert nationality.include_children is True  # A-05
    assert nationality.month_basis is MonthBasis.ARRIVAL_MONTH  # A-04
    assert policy.qualifying.day_use.counts_room_nights is False  # A-03
    assert policy.qualifying.day_use.counts_guests is True  # A-03


def test_missing_policy_file_raises_policy_error(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read policy"):
        load_policy(tmp_path / "nope.yaml")


def test_malformed_yaml_raises_policy_error(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("version: [unclosed\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(path)


def test_schema_violation_is_reported_before_typed_validation(tmp_path: Path) -> None:
    """The schema runs first, so its message is what a user sees. It is the layer that knows
    about `const` pins, and a run under an unvalidated ruleset must not start."""
    path = write_policy(
        tmp_path, lambda raw: raw["evidence"].__setitem__("require_excel_ref", False)
    )
    with pytest.raises(PolicyError, match=re.escape("does not satisfy policy.schema.json")):
        load_policy(path, schema_path=DEFAULT_SCHEMA_PATH)


def test_a_status_left_unruled_is_rejected(tmp_path: Path) -> None:
    """An unlisted status would be silently dropped from every metric, which is the same bug
    as guessing — just quieter."""
    path = write_policy(
        tmp_path, lambda raw: raw["qualifying"]["status"].__setitem__("excluded", ["CANCELLED"])
    )
    with pytest.raises(PolicyError, match="neither included nor excluded"):
        load_policy(path)


def test_a_reordered_classification_ladder_is_rejected(tmp_path: Path) -> None:
    """D-CLS-01..05. Swapping V1 before V2 keeps every finding technically correct while
    reporting policy disagreements as clerical mistakes — the report becomes unusable and
    nothing fails."""
    path = write_policy(
        tmp_path,
        lambda raw: raw["classification"].__setitem__("order", ["V7", "V1", "V5", "V2", "V6"]),
    )
    with pytest.raises(PolicyError, match="order is a correctness requirement"):
        load_policy(path)


# ── tolerances ───────────────────────────────────────────────────────────────


def test_percentage_tolerance_is_the_published_band(policy: Policy) -> None:
    """D-TOL-02: ±0.10 percentage points."""
    tolerance = policy.tolerances.for_metric(Metric.OCCUPANCY_PCT)
    assert tolerance.type is ToleranceType.ABSOLUTE
    assert tolerance.value == pytest.approx(0.10)
    assert tolerance.accepts(0.10), "the boundary is inclusive"
    assert tolerance.accepts(0.05)
    assert not tolerance.accepts(0.11)


def test_counts_are_exact(policy: Policy) -> None:
    """D-TOL-01. A difference of one guest is a finding."""
    tolerance = policy.tolerances.for_metric(Metric.GUESTS_BY_NATIONALITY)
    assert tolerance.type is ToleranceType.EXACT
    assert tolerance.accepts(0)
    assert not tolerance.accepts(1)


def test_tolerance_is_symmetric(policy: Policy) -> None:
    """D-TOL-05. A claim below the computed value is treated exactly as one above it."""
    tolerance = policy.tolerances.for_metric(Metric.OCCUPANCY_PCT)
    assert tolerance.accepts(-0.05) == tolerance.accepts(0.05)
    assert tolerance.accepts(-0.20) == tolerance.accepts(0.20)


# ── the qualifying set ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "rate_code", "expected", "clause"),
    [
        (Status.CHECKED_OUT, RateCode.BAR, True, "D-QUAL-01"),
        (Status.IN_HOUSE, RateCode.BAR, True, "D-QUAL-01"),
        (Status.CANCELLED, RateCode.BAR, False, "D-QUAL-02 / A-01"),
        (Status.NO_SHOW, RateCode.BAR, False, "D-QUAL-02 / A-01"),
        (Status.CHECKED_OUT, RateCode.COMP, True, "D-QUAL-05 / A-02"),
        (Status.CHECKED_OUT, RateCode.HOUSE, False, "D-QUAL-04 / A-02"),
    ],
)
def test_qualifying_set(
    policy: Policy, status: Status, rate_code: RateCode, expected: bool, clause: str
) -> None:
    assert policy.qualifies(status, rate_code) is expected, f"contradicts {clause}"


# ── permutations ─────────────────────────────────────────────────────────────


def test_permutation_does_not_mutate_the_baseline(policy: Policy) -> None:
    """A permutation that mutated the live policy would leak into every later comparison in
    the run, making findings depend on evaluation order."""
    before = policy.metrics.occupancy_pct.denominator
    permutations = policy.permutations_for(Metric.OCCUPANCY_PCT)
    for permutation in permutations:
        apply_permutation(policy, permutation)
    assert policy.metrics.occupancy_pct.denominator is before


def test_denominator_permutation_produces_the_f3_error(policy: Policy) -> None:
    """`P-OCC-DENOM-ROOMS` is the canonical definitional error fixture F3 plants."""
    permutation = next(
        p for p in policy.permutations_for(Metric.OCCUPANCY_PCT) if p.id == "P-OCC-DENOM-ROOMS"
    )
    permuted = apply_permutation(policy, permutation)
    assert permuted.metrics.occupancy_pct.denominator is Denominator.ROOMS_AVAILABLE
    assert policy.metrics.occupancy_pct.denominator is Denominator.ROOM_NIGHTS_AVAILABLE


def test_every_permutation_actually_changes_behaviour(policy: Policy) -> None:
    """The regression test for the real bug this loader surfaced.

    A permutation whose resulting policy is identical to the baseline is a silent no-op: it can
    never reproduce a claim, so it is never named, and the definitional variance it was meant to
    explain gets reported as a clerical error instead.

    `P-COMP-EXCLUDED` was exactly this. It set `rate_code.excluded` to `[HOUSE, COMP]` and left
    `COMP` in `rate_code.included`, which is the list `qualifies()` reads. Every override path
    resolved; only the meaning was wrong, so no path-existence check could have caught it.
    """
    baseline = policy.model_dump(mode="python")
    unchanged: list[str] = []

    for metric in Metric:
        for permutation, permuted in permuted_policies(policy, metric):
            if permuted.model_dump(mode="python") == baseline:
                unchanged.append(f"{permutation.id} (on {metric.value})")

    assert not unchanged, (
        "these permutations produce a policy identical to the baseline, so they can never "
        f"explain a variance and will never be named in a finding: {sorted(set(unchanged))}"
    )


def test_partition_permutations_change_the_qualifying_decision(policy: Policy) -> None:
    """The specific form the bug took: the lists must move together, and `qualifies()` must
    actually give a different answer afterwards."""
    cases = [
        ("P-COMP-EXCLUDED", Status.CHECKED_OUT, RateCode.COMP, True, False),
        ("P-HOUSE-INCLUDED", Status.CHECKED_OUT, RateCode.HOUSE, False, True),
        ("P-STATUS-INCLUDE-NOSHOW", Status.NO_SHOW, RateCode.BAR, False, True),
        ("P-STATUS-INCLUDE-CANCELLED", Status.CANCELLED, RateCode.BAR, False, True),
    ]
    by_id = {p.id: p for p in policy.permutations.ordered}

    for permutation_id, status, rate_code, baseline_answer, permuted_answer in cases:
        permuted = apply_permutation(policy, by_id[permutation_id])
        assert policy.qualifies(status, rate_code) is baseline_answer
        assert permuted.qualifies(status, rate_code) is permuted_answer, (
            f"{permutation_id} did not change the qualifying decision for "
            f"{status.value}/{rate_code.value} - it is a silent no-op"
        )


def test_permutation_order_is_the_committed_order(policy: Policy) -> None:
    """D-CLS-09. Where several permutations explain a claim, the first is named. So the order
    is part of the output, not an implementation detail."""
    occupancy = [p.id for p in policy.permutations_for(Metric.OCCUPANCY_PCT)]
    assert occupancy[0] == "P-OCC-DENOM-ROOMS", "the biggest mover is tried first"
    assert occupancy.index("P-MONTH-ARRIVAL") < occupancy.index("P-DAYUSE-COUNTS-RN")


def test_permutations_only_apply_to_metrics_they_affect(policy: Policy) -> None:
    """A permutation offered for a metric it cannot change wastes a comparison and, worse,
    could be reported as having been "tried and ruled out"."""
    for permutation in policy.permutations_for(Metric.GUESTS_BY_NATIONALITY):
        assert Metric.GUESTS_BY_NATIONALITY in permutation.applies_to


def test_every_permutation_explains_itself_in_a_sentence(policy: Policy) -> None:
    """The `explains` text is what a reviewer reads in the finding. If the cause cannot be
    stated plainly, the permutation is not usable in a finding (D-CLS-07)."""
    for permutation in policy.permutations.ordered:
        assert len(permutation.explains) >= 20, f"{permutation.id} has no usable explanation"
        assert permutation.explains[0].isupper(), f"{permutation.id}: explanation is not a sentence"


def test_nonexistent_override_path_raises(policy: Policy) -> None:
    """The silent-no-op guard, from the code side."""
    from tda.policy.loader import Permutation

    broken = Permutation(
        id="P-TYPO",
        explains="A permutation with a typo in its override path",
        applies_to=(Metric.OCCUPANCY_PCT,),
        override={"metrics.occupancy_pct.denominatorr": "rooms_available"},
    )
    with pytest.raises(PolicyError, match="override path does not exist"):
        apply_permutation(policy, broken)


def test_override_cannot_invent_a_new_key(policy: Policy) -> None:
    """Creating the path instead of refusing would make every typo a no-op."""
    from tda.policy.loader import Permutation

    broken = Permutation(
        id="P-INVENT",
        explains="A permutation inventing a setting that does not exist",
        applies_to=(Metric.OCCUPANCY_PCT,),
        override={"metrics.occupancy_pct.brand_new_setting": True},
    )
    with pytest.raises(PolicyError, match="override path does not exist"):
        apply_permutation(policy, broken)


def test_policy_is_frozen(policy: Policy) -> None:
    """Policy is a parameter on every metric function, never a global. A mutable one would
    make a definitional change invisible at the call site."""
    with pytest.raises(Exception, match=r"frozen"):
        policy.metrics.occupancy_pct.denominator = Denominator.ROOMS_AVAILABLE  # type: ignore[misc]
