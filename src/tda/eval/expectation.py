"""Reading `expected.json`: schema-validated on the way in, typed on the way out.

The consumer's half of the seam described in `tools/fixtures/expected.schema.json`. The producer
validates on write and this validates on read, which is not redundant. A file is the contract
between two packages that may not import each other, so the only way either side learns that the
shape moved is by checking the shape it actually received. One-sided validation means the reader
trusts a writer it cannot see.

## Why the version is a copy rather than an import

`tools.fixtures.FIXTURE_SET_VERSION` is the authority, and `SUPPORTED_FIXTURE_SET_VERSION` below is
a copy of it. Importing it would be neater and is not available: `tools/` ships in no wheel
(`[tool.hatch.build.targets.wheel]` packages `src/tda` only), so `import tools.fixtures` inside
`src/tda` turns an installed package into one that fails at import, which is the same trap the
import guard's third rule records about `tools/datagen`.

A copy is also the honest shape for a version that two independent sides must agree on. The
mismatch is what matters, and it is detectable precisely because each side states its own.

## Why figures arrive as strings

Every figure in the file is a decimal string, and this module is where it becomes a `Decimal`. A
JSON number would be parsed as a float, and a float round trip reintroduces exactly the drift the
tolerance policy exists to make explicit. `Decimal("30.00") == Decimal("30")` compares numerically,
so the written form carries no meaning the scorer could accidentally depend on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from tda.contracts import EscalationTarget, Severity, VarianceClass, VerdictStatus

EXPECTED_FILE: Final = "expected.json"

# `src/tda/eval/expectation.py` -> repository root. The schema is development tooling and is read
# from the checkout, like `policy.schema.json`, rather than from package data.
REPO_ROOT: Final = Path(__file__).resolve().parents[3]
SCHEMA_PATH: Final = REPO_ROOT / "tools" / "fixtures" / "expected.schema.json"

# A copy of `tools.fixtures.FIXTURE_SET_VERSION`. See the module docstring for why it is a copy,
# and bump it here in the same commit that bumps it there.
SUPPORTED_FIXTURE_SET_VERSION: Final = "1.0.0"


class ExpectationError(Exception):
    """An expectation that cannot be read, cannot be validated, or was derived by another builder.

    One exception type for all three, because the caller does the same thing with each: refuse to
    report a score. A fixture scored against an expectation of unknown provenance produces a number
    that looks like a measurement.
    """


class MutationKind(StrEnum):
    """What was done to the copy of the demo corpus. Closed, so an unknown kind fails at load."""

    NONE = "none"
    TRANSPOSE_DIGITS = "transpose_digits"
    RECOMPUTE_UNDER = "recompute_under"
    DELETE_DIMENSION = "delete_dimension"
    RELABEL = "relabel"


class ExpectedAbsence(StrEnum):
    """The literal the schema allows where a cell reference cannot exist.

    A member of its own type rather than `None`, so `ExpectedCell | ExpectedAbsence` is a union the
    type checker forces the scorer to discriminate. `None` would make "the fixture expects a typed
    absence" indistinguishable from "the fixture said nothing about evidence", and those demand
    opposite behaviour: the first must match a `NotReached`, the second is a malformed file.
    """

    NOT_REACHED = "not_reached"


class ExpectedSource(StrEnum):
    """Which side the computed value was expected to come from.

    `PDF_PAGE` asserts a `PdfRef` carrying a page and a row range, and deliberately does not say
    which page. The page a figure derives from is the extractor's to determine, and pinning it here
    would make the expectation a second implementation of extraction.
    """

    PDF_PAGE = "pdf_page"
    NOT_REACHED = "not_reached"


@dataclass(frozen=True, slots=True)
class ExpectedCell:
    """A workbook cell a reviewer can open."""

    sheet: str
    cell: str

    def render(self) -> str:
        return f"{self.sheet}!{self.cell}"


@dataclass(frozen=True, slots=True)
class ExpectedEvidence:
    """What the finding must let a reviewer open, on each side."""

    excel: ExpectedCell | ExpectedAbsence
    source: ExpectedSource


@dataclass(frozen=True, slots=True)
class ExpectedFinding:
    """One finding the run must produce, keyed by `MetricKey.rendered`.

    The figure fields are optional in the schema *and* nullable. Both spellings mean the same
    thing here, which is safe only because the field is genuinely optional on the contract too:
    `Finding.claimed` is `Decimal | None`, and "the workbook omitted this figure" is a real state
    rather than a stand-in for one.
    """

    key: str
    variance_class: VarianceClass
    severity: Severity
    escalates_to: EscalationTarget
    claimed: Decimal | None
    computed: Decimal | None
    difference: Decimal | None
    proposed_correction: Decimal | None
    explaining_permutation: str | None
    evidence: ExpectedEvidence


@dataclass(frozen=True, slots=True)
class Mutation:
    """The mutation as declared, carried for the report and never reasoned from.

    Everything the scorer asserts lives in `status`, `findings` and `definitional_items`, already
    derived. This is here so a failing fixture can be read without opening the spec, and `is_control`
    is the one question the aggregate asks of it: a fixture that planted nothing must still come
    back empty, because precision is the half a reviewer feels.
    """

    kind: MutationKind
    metric: str | None = None
    period: str | None = None
    value_key: str | None = None
    permutation: str | None = None
    new_label: str | None = None
    resolvable: bool | None = None

    @property
    def is_control(self) -> bool:
        return self.kind is MutationKind.NONE

    def render(self) -> str:
        detail = ", ".join(
            f"{name}={value}"
            for name, value in (
                ("metric", self.metric),
                ("period", self.period),
                ("value_key", self.value_key),
                ("permutation", self.permutation),
                ("new_label", self.new_label),
                ("resolvable", self.resolvable),
            )
            if value is not None
        )
        return f"{self.kind.value}({detail})" if detail else self.kind.value


@dataclass(frozen=True, slots=True)
class Expectation:
    """One fixture's derived expectation, validated and typed.

    `exhaustive` is `const: true` in the schema and is carried anyway rather than assumed. The
    scorer asserts set equality on keys, and it should be able to say which field licensed that:
    an expectation that ever became non-exhaustive would turn every set-equality check into a
    containment check, and a false positive would stop being a failure.
    """

    fixture_set_version: str
    fixture_id: str
    why: str
    mutation: Mutation
    status: VerdictStatus
    exhaustive: bool
    findings: tuple[ExpectedFinding, ...]
    definitional_items: tuple[ExpectedFinding, ...]

    @property
    def rendered(self) -> str:
        """The expectation in one cell of the report table."""
        return (
            f"{self.status.value}, {len(self.findings)} finding(s), "
            f"{len(self.definitional_items)} definitional"
        )

    @property
    def material_keys(self) -> frozenset[str]:
        """Keys of the expected findings that carry a planted material error.

        The denominator of recall. Blocking and informational findings are excluded because they
        measure something else: a blocking finding says the run could not see, and an informational
        one is below the threshold at which anybody acts.
        """
        return frozenset(
            f.key
            for f in (*self.findings, *self.definitional_items)
            if f.severity is Severity.MATERIAL
        )


def load_expectation(
    path: Path,
    *,
    schema_path: Path | None = None,
    supported_version: str = SUPPORTED_FIXTURE_SET_VERSION,
) -> Expectation:
    """Read one `expected.json`, validate it, and refuse it if it came from another builder.

    The version check is first among the semantic checks and it is a refusal rather than a warning.
    A fixture tree derived before a change to the mutation engine describes outcomes that engine no
    longer produces, and scoring against it reports a defect in the pipeline for behaving correctly.
    """
    raw = _read_json(path)
    _validate_against_schema(raw, schema_path or SCHEMA_PATH, path)

    version = str(raw["fixture_set_version"])
    if version != supported_version:
        raise ExpectationError(
            f"{path} was derived by fixture set version {version}, and this scorer understands "
            f"{supported_version}. Rebuild the fixtures with `make datagen` rather than scoring "
            "against an expectation whose meaning may have moved."
        )

    findings = tuple(_finding(item, path) for item in raw["findings"])
    definitional = tuple(_finding(item, path) for item in raw["definitional_items"])
    _check_filing(findings, definitional, path)

    return Expectation(
        fixture_set_version=version,
        fixture_id=str(raw["fixture_id"]),
        why=str(raw["why"]),
        mutation=_mutation(raw["mutation"]),
        status=VerdictStatus(raw["status"]),
        exhaustive=bool(raw["exhaustive"]),
        findings=findings,
        definitional_items=definitional,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExpectationError(f"cannot read an expectation at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ExpectationError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExpectationError(f"{path} must hold a JSON object, got {type(payload).__name__}")
    return payload


def _validate_against_schema(raw: dict[str, Any], schema_path: Path, path: Path) -> None:
    """Every error at once, sorted by position, the way the policy loader reports them.

    A validator that stops at the first error turns fixing a drifted file into a guessing game with
    one answer per run.
    """
    import jsonschema

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ExpectationError(
            f"cannot read the expectation schema at {schema_path}: {exc}"
        ) from exc

    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        detail = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        raise ExpectationError(f"{path} does not satisfy {schema_path.name}:\n{detail}")


def _mutation(raw: dict[str, Any]) -> Mutation:
    resolvable = raw.get("resolvable")
    return Mutation(
        kind=MutationKind(raw["kind"]),
        metric=_optional_str(raw.get("metric")),
        period=_optional_str(raw.get("period")),
        value_key=_optional_str(raw.get("value_key")),
        permutation=_optional_str(raw.get("permutation")),
        new_label=_optional_str(raw.get("new_label")),
        resolvable=None if resolvable is None else bool(resolvable),
    )


def _finding(raw: dict[str, Any], path: Path) -> ExpectedFinding:
    return ExpectedFinding(
        key=str(raw["key"]),
        variance_class=VarianceClass(raw["variance_class"]),
        severity=Severity(raw["severity"]),
        escalates_to=EscalationTarget(raw["escalates_to"]),
        claimed=_decimal(raw.get("claimed"), "claimed", path),
        computed=_decimal(raw.get("computed"), "computed", path),
        difference=_decimal(raw.get("difference"), "difference", path),
        proposed_correction=_decimal(raw.get("proposed_correction"), "proposed_correction", path),
        explaining_permutation=_optional_str(raw.get("explaining_permutation")),
        evidence=_evidence(raw["evidence"]),
    )


def _evidence(raw: dict[str, Any]) -> ExpectedEvidence:
    excel: Any = raw["excel"]
    return ExpectedEvidence(
        excel=(
            ExpectedAbsence(excel)
            if isinstance(excel, str)
            else ExpectedCell(sheet=str(excel["sheet"]), cell=str(excel["cell"]))
        ),
        source=ExpectedSource(raw["source"]),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _decimal(value: Any, field: str, path: Path) -> Decimal | None:
    """A figure, as a `Decimal` built from its written form.

    `Decimal(str)` rather than `Decimal(float)`: the schema forbids JSON numbers here, and this is
    the second half of that decision. A file that slipped a number past the schema would arrive as
    a float and reintroduce the drift the tolerance policy exists to make explicit, so it is
    refused rather than coerced.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExpectationError(
            f"{path}: {field} is {value!r}, a JSON {type(value).__name__}. Every figure in an "
            "expectation is a decimal string, because a float round trip changes the value."
        )
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ExpectationError(f"{path}: {field}={value!r} is not a decimal") from exc


def _check_filing(
    findings: tuple[ExpectedFinding, ...],
    definitional: tuple[ExpectedFinding, ...],
    path: Path,
) -> None:
    """The two rules the scorer's own logic branches on, checked against the expectation itself.

    `Verdict` refuses a V2 in `findings` and refuses a V2 without an explaining permutation, so an
    expectation that asserts either describes a verdict that cannot be constructed. Scoring against
    it would report the contract working correctly as a mismatch, and the mismatch would be
    unfixable in the pipeline.
    """
    misfiled = [f.key for f in findings if f.variance_class is VarianceClass.DEFINITIONAL]
    if misfiled:
        raise ExpectationError(
            f"{path}: V2 keys in `findings`: {misfiled}. They belong in `definitional_items`, and "
            "`Verdict` refuses the arrangement this expectation describes (D-MAT-06)."
        )
    not_definitional = [
        f.key for f in definitional if f.variance_class is not VarianceClass.DEFINITIONAL
    ]
    if not_definitional:
        raise ExpectationError(f"{path}: non-V2 keys in `definitional_items`: {not_definitional}.")
    for expected in (*findings, *definitional):
        is_v2 = expected.variance_class is VarianceClass.DEFINITIONAL
        if is_v2 and expected.explaining_permutation is None:
            raise ExpectationError(
                f"{path}: {expected.key} is a V2 with no explaining_permutation. `Finding` refuses "
                "one, so no run can satisfy this expectation (D-CLS-07)."
            )
        if not is_v2 and expected.explaining_permutation is not None:
            raise ExpectationError(
                f"{path}: {expected.key} is a {expected.variance_class.value} carrying "
                "explaining_permutation, which only a V2 may (D-CLS-07)."
            )
