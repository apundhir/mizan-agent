"""Load, validate and permute the policy.

Two layers, deliberately, because they guard different things.

**`policy.schema.json` is the completeness gate.** It has `additionalProperties: false`
throughout and pins the load-bearing rules to single values (`consults_model: false`,
`require_excel_ref: true`, …). It runs first, and a policy that fails it never reaches the
code below — a run under an unvalidated ruleset produces a number nobody can defend.

**The Pydantic models here are the typed access layer.** They cover the fields code actually
reads and ignore the rest (`clause:` and `assumption:` annotations, which are for humans and
for the validator). They deliberately do **not** re-state the schema's constraints: two
copies of the same rule is two places for it to drift, and the schema is the one with
`additionalProperties: false`, so nothing unknown gets past it anyway.

The permutation runner is the third piece. It applies an ordered, committed set of overrides
to a **copy** of the policy so the same metric can be recomputed under an alternative rule
(D-CLS-07). A permutation whose override path does not exist raises rather than no-ops: a
silent no-op permutation explains nothing, is never named in a finding, and is
indistinguishable from one correctly tried that did not match — which is how a definitional
variance gets reported as a clerical error.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from tda.contracts.metric_key import Metric
from tda.contracts.reservation import RateCode, Status
from tda.contracts.variance import EscalationTarget, Severity, VarianceClass

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY_PATH = REPO_ROOT / "policy.yaml"
DEFAULT_SCHEMA_PATH = REPO_ROOT / "policy.schema.json"


class PolicyError(Exception):
    """Raised when a policy cannot be loaded, validated, or permuted.

    Deliberately not a subclass of ValueError: a bad policy is not a bad value in a
    computation, it is a reason not to start one.
    """


class MonthBasis(StrEnum):
    """How a stay that spans a month boundary is apportioned.

    The single most likely source of live definitional variance (A-04).
    """

    OCCUPIED_NIGHT = "occupied_night"
    ARRIVAL_MONTH = "arrival_month"
    DEPARTURE_MONTH = "departure_month"


class Denominator(StrEnum):
    """`ROOMS_AVAILABLE` is the canonical definitional error (fixture F3). It is a legal
    permutation value and never a legal baseline — the schema enforces that asymmetry."""

    ROOM_NIGHTS_AVAILABLE = "room_nights_available"
    ROOMS_AVAILABLE = "rooms_available"


class NationalityCounts(StrEnum):
    """What the nationality table is counting. A party of 3 in 1 room for 4 nights is 3
    guests, 1 arrival, or 4 room-nights — three different answers to the same cell (A-06)."""

    GUESTS = "guests"
    ARRIVALS = "arrivals"
    ROOM_NIGHTS = "room_nights"


class ToleranceType(StrEnum):
    EXACT = "exact"
    ABSOLUTE = "absolute"
    RELATIVE = "relative"


class _Lenient(BaseModel):
    """Base for policy sections.

    `extra="ignore"` so human-facing annotations (`clause:`, `assumption:`) do not have to be
    mirrored as fields. Completeness is the schema's job, not this model's.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")


class StatusRule(_Lenient):
    included: tuple[Status, ...]
    excluded: tuple[Status, ...]

    @model_validator(mode="after")
    def _disjoint_and_total(self) -> Self:
        overlap = set(self.included) & set(self.excluded)
        if overlap:
            raise ValueError(f"status listed as both included and excluded: {sorted(overlap)}")
        missing = set(Status) - set(self.included) - set(self.excluded)
        if missing:
            raise ValueError(
                f"status values neither included nor excluded: {sorted(s.value for s in missing)}. "
                "Every status must be ruled on explicitly - an unlisted one would be silently "
                "dropped, which is the same bug as guessing"
            )
        return self


class RateCodeRule(_Lenient):
    included: tuple[RateCode, ...]
    excluded: tuple[RateCode, ...]

    @model_validator(mode="after")
    def _disjoint_and_total(self) -> Self:
        overlap = set(self.included) & set(self.excluded)
        if overlap:
            raise ValueError(f"rate code listed as both included and excluded: {sorted(overlap)}")
        missing = set(RateCode) - set(self.included) - set(self.excluded)
        if missing:
            raise ValueError(
                f"rate codes neither included nor excluded: {sorted(r.value for r in missing)}"
            )
        return self


class DayUseRule(_Lenient):
    counts_room_nights: bool
    counts_guests: bool


class Qualifying(_Lenient):
    status: StatusRule
    rate_code: RateCodeRule
    day_use: DayUseRule


class OccupancyRule(_Lenient):
    denominator: Denominator
    month_basis: MonthBasis
    exclude_out_of_order: bool
    presentation_decimal_places: int
    cap_at_100: bool


class NationalityRule(_Lenient):
    counts: NationalityCounts
    include_children: bool
    month_basis: MonthBasis


class Metrics(_Lenient):
    occupancy_pct: OccupancyRule
    guests_by_nationality: NationalityRule


class Scope(_Lenient):
    """What the system verifies, and what it records without verifying (D-SCOPE-01, D-SCOPE-02).

    Loaded rather than left to `extra="ignore"` because the Excel claim parser needs the *whole*
    vocabulary, not just the verifiable half. A hotel's workbook contains sheets this POC does not
    check — average daily rate, RevPAR, length of stay — and there are two very different reasons a
    header block might produce no claim:

    - it names a measure the policy has declared out of scope, or
    - nothing in the system recognises it at all.

    The first is a decision already taken and recorded (D-SCOPE-02: *silence would be mistaken for
    approval*). The second is a gap that needs a human. Collapsing them loses the distinction that
    makes the second actionable, and the only way to keep them apart is to have the out-of-scope
    names here, as configuration, rather than as a list inside the parser that would quietly stop
    matching the day somebody added a metric to `policy.yaml`.
    """

    metrics_in_scope: tuple[Metric, ...]
    metrics_out_of_scope: tuple[str, ...]

    @model_validator(mode="after")
    def _disjoint(self) -> Self:
        overlap = {m.value for m in self.metrics_in_scope} & set(self.metrics_out_of_scope)
        if overlap:
            raise PolicyError(
                f"metrics both in and out of scope: {sorted(overlap)}. A metric cannot be both "
                "verified and recorded-without-verification; the parser would have to pick one."
            )
        return self

    @property
    def vocabulary(self) -> frozenset[str]:
        """Every metric name the mapping agent may return.

        The agent maps a header block by naming a metric, and code checks the name against this
        set. A name outside it is never accepted - not as an in-scope metric, and not as an
        out-of-scope one either. That is D-KEY-03 (*a key is never constructed from a model
        output*) enforced at the one place a model gets to influence which metric a number
        belongs to.
        """
        return frozenset({m.value for m in self.metrics_in_scope} | set(self.metrics_out_of_scope))


class Tolerance(_Lenient):
    type: ToleranceType
    value: float | None = None

    def accepts(self, difference: float) -> bool:
        """Whether a difference is inside tolerance and therefore logged, never raised (D-TOL-04).

        Applied to the absolute difference: tolerance is symmetric, so a claim below the
        computed value is treated exactly as one above it (D-TOL-05).
        """
        magnitude = abs(difference)
        if self.type is ToleranceType.EXACT:
            return magnitude == 0
        if self.value is None:  # pragma: no cover - the schema forbids this combination
            raise PolicyError(f"{self.type} tolerance has no value")
        return magnitude <= self.value


class ToleranceSet(_Lenient):
    """Tolerance per metric *type*, not per metric — so adding a metric cannot accidentally
    arrive with no tolerance and be compared exactly by default, or loosely by default."""

    count: Tolerance
    percentage_points: Tolerance
    total: Tolerance


class Tolerances(_Lenient):
    symmetric: bool
    by_metric_type: ToleranceSet

    def for_metric(self, metric: Metric) -> Tolerance:
        """Percentages take the ±0.10pp band; everything else is a count and is exact."""
        return (
            self.by_metric_type.percentage_points
            if metric.is_percentage
            else self.by_metric_type.count
        )


class VarianceClassRule(_Lenient):
    name: str
    proposes_correction: bool
    escalates_to: EscalationTarget


class Classification(_Lenient):
    order: tuple[VarianceClass, ...]
    classes: dict[VarianceClass, VarianceClassRule]
    consults_model: bool = False

    @model_validator(mode="after")
    def _order_is_the_documented_one(self) -> Self:
        expected = (
            VarianceClass.EXTRACTION_LIMIT,
            VarianceClass.DEFINITIONAL,
            VarianceClass.COMPLETENESS,
            VarianceClass.TRANSCRIPTION,
            VarianceClass.ROUNDING,
        )
        if self.order != expected:
            raise ValueError(
                f"classification order is {[c.value for c in self.order]}, expected "
                f"{[c.value for c in expected]}. The order is a correctness requirement: V7 first "
                "so the system never accuses a hotel of an error it could not see, and V2 before "
                "V1 so a policy disagreement is never reported as a clerical mistake (D-CLS-01..05)"
            )
        if set(self.classes) != set(VarianceClass):
            raise ValueError("every variance class needs a rule")

        # D-CLS-10, pinned from the configuration side. The import guard says the same thing from
        # the other, by failing the build if `tda.reconcile` imports an agent or a model SDK. Both
        # exist because a single one is a claim and two that cannot disagree are an argument: a
        # policy that turned this on would be asking for a system the code cannot build.
        if self.consults_model:
            raise ValueError(
                "classification.consults_model must be false. The code finds the cause and the "
                "model only writes the sentence (D-CLS-10) - a model near classification is a "
                "model deciding whether a hotel made an error"
            )
        return self


class Materiality(_Lenient):
    """How severe a variance is, and where it goes — decided mechanically, never by a model.

    `thresholds` and the two definitional fields were being dropped by `extra="ignore"` until PRD-87
    needed them. They are loaded now because both say something the reconciliation engine must not
    be free to contradict, and a rule that is only in a comment is a rule nothing enforces.
    """

    severity_by_class: dict[VarianceClass, Severity]
    thresholds: dict[str, dict[str, float]] = {}
    definitional_items_separate_array: bool = True
    definitional_escalates_to: EscalationTarget = EscalationTarget.POLICY_OWNER

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if set(self.severity_by_class) != set(VarianceClass):
            raise ValueError("every variance class needs a severity")

        # D-MAT-06, from the configuration side. A count of "hotel errors" that included policy
        # disagreements would be a wrong number presented as a right one, and it is the number that
        # gets quoted. `Finding.is_hotel_error` excludes V2 by construction; this stops a policy
        # asking for the opposite and leaving the two halves of the system disagreeing.
        if not self.definitional_items_separate_array:
            raise ValueError(
                "materiality.definitional_items_separate_array must be true: definitional "
                "variances are never counted as hotel errors (D-MAT-06)"
            )
        if self.definitional_escalates_to is not EscalationTarget.POLICY_OWNER:
            raise ValueError(
                f"definitional variances escalate to the policy owner, not to "
                f"{self.definitional_escalates_to.value}. Reporting a rule disagreement as a hotel "
                "error is how correct findings discredit the system (D-MAT-06)"
            )
        return self


class Permutation(_Lenient):
    """One alternative ruleset, with the sentence a reviewer will read."""

    id: str
    explains: str
    applies_to: tuple[Metric, ...]
    override: dict[str, Any]


class Permutations(_Lenient):
    enabled: bool
    ordered: tuple[Permutation, ...]


class AgentSettings(_Lenient):
    effort: str
    prompt_version: str


class Budget(_Lenient):
    """The supervisor's per-run cap on model calls (PRD-88).

    Two numbers rather than one. The per-agent cap is what catches a loop, and a total-only budget
    would let one runaway agent spend every other agent's allowance before anything noticed.
    """

    max_calls_per_run: int
    max_calls_per_agent: int


class ModelSettings(_Lenient):
    provider: str
    model_id: str
    agents: dict[str, AgentSettings]
    # Optional on the Python side although the JSON schema requires it, so a policy file written
    # before PRD-88 still loads — `Supervisor` falls back to its own conservative defaults rather
    # than to no budget at all. A missing budget must never mean an unbounded run.
    budget: Budget | None = None


class Policy(_Lenient):
    """A validated ruleset. Passed as a parameter to every metric function — never a global.

    A global would make a definitional change invisible at the call site, and the whole design
    rests on a reader being able to see which rules a number was computed under.
    """

    version: str
    scope: Scope
    qualifying: Qualifying
    metrics: Metrics
    tolerances: Tolerances
    classification: Classification
    materiality: Materiality
    permutations: Permutations
    model: ModelSettings

    # ── the questions the metric library actually asks ───────────────────────

    def qualifies(self, status: Status, rate_code: RateCode) -> bool:
        """Whether a reservation is in the qualifying set (§2 of the definitions)."""
        return (
            status in self.qualifying.status.included
            and rate_code in self.qualifying.rate_code.included
        )

    def severity_for(self, variance_class: VarianceClass) -> Severity:
        return self.materiality.severity_by_class[variance_class]

    def escalation_for(self, variance_class: VarianceClass) -> EscalationTarget:
        return self.classification.classes[variance_class].escalates_to

    def permutations_for(self, metric: Metric) -> tuple[Permutation, ...]:
        """The permutations that could explain a variance on this metric, in committed order.

        Order matters: where several reproduce a claimed value, the first is named in the
        finding and the others are recorded (D-CLS-09).
        """
        if not self.permutations.enabled:
            return ()
        return tuple(p for p in self.permutations.ordered if metric in p.applies_to)


# ── loading ──────────────────────────────────────────────────────────────────


def _validate_against_schema(raw: dict[str, Any], schema_path: Path) -> None:
    import jsonschema

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"cannot read policy schema at {schema_path}: {exc}") from exc

    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        detail = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        raise PolicyError(f"policy does not satisfy {schema_path.name}:\n{detail}")


def load_policy(path: Path | None = None, *, schema_path: Path | None = None) -> Policy:
    """Load and validate a policy. The only supported way to obtain a `Policy`.

    There is no constructor-from-dict in the public surface, because every `Policy` in
    circulation should have been through the schema.
    """
    path = path or DEFAULT_POLICY_PATH
    schema_path = schema_path or DEFAULT_SCHEMA_PATH

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"cannot read policy at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy at {path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise PolicyError(f"policy at {path} must be a mapping, got {type(raw).__name__}")

    _validate_against_schema(raw, schema_path)

    try:
        return Policy.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError, or a section validator's ValueError
        raise PolicyError(f"policy at {path} failed typed validation:\n{exc}") from exc


# ── permutation ──────────────────────────────────────────────────────────────


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    """Set an existing dotted path, refusing to create one.

    Refusing is the point. A typo that creates `metrics.occupancy_pct.denominatorr` would
    leave the real setting untouched, so the permutation would reproduce the baseline, never
    match a claim, and never be named — a definitional variance silently demoted to a
    clerical error.
    """
    segments = dotted.split(".")
    node: Any = target
    for segment in segments[:-1]:
        if not isinstance(node, dict) or segment not in node:
            raise PolicyError(f"override path does not exist: {dotted} (at {segment!r})")
        node = node[segment]

    leaf = segments[-1]
    if not isinstance(node, dict) or leaf not in node:
        raise PolicyError(f"override path does not exist: {dotted} (at {leaf!r})")
    node[leaf] = value


def apply_permutation(policy: Policy, permutation: Permutation) -> Policy:
    """Return a new policy with the permutation's overrides applied.

    The original is untouched — a permutation that mutated the live policy would leak into
    every subsequent comparison in the run, and the resulting findings would depend on the
    order they were evaluated in.

    The result is re-validated through the typed layer but **not** through the JSON Schema:
    the schema pins several baseline-only values (a `rooms_available` denominator is exactly
    what `P-OCC-DENOM-ROOMS` exists to test), and a permutation is by construction not a
    baseline.
    """
    raw = policy.model_dump(mode="python")
    for dotted, value in permutation.override.items():
        _set_dotted(raw, dotted, value)

    try:
        return Policy.model_validate(raw)
    except Exception as exc:
        raise PolicyError(
            f"permutation {permutation.id} produced an invalid policy:\n{exc}"
        ) from exc


def permuted_policies(policy: Policy, metric: Metric) -> list[tuple[Permutation, Policy]]:
    """Every applicable permutation paired with its resulting policy, in committed order."""
    return [(p, apply_permutation(policy, p)) for p in policy.permutations_for(metric)]
