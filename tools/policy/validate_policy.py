#!/usr/bin/env python3
"""Validate policy.yaml against policy.schema.json, and prove the schema has teeth.

Run:  python tools/policy/validate_policy.py [policy.yaml] [policy.schema.json]

Four checks, in order of what they protect:

1. **Structural.** policy.yaml satisfies the JSON Schema. A policy that does not halts the run
   before any number is computed, because a number produced under an unvalidated ruleset is a
   number nobody can defend.

2. **Referential.** Every permutation `override` path resolves to a path that exists in the
   policy, and every permutation id is unique. A typo in an override path would otherwise
   produce a silent no-op permutation — one that explains nothing, is never named in a finding,
   and looks exactly like a permutation that was correctly tried and did not match. That is the
   worst failure mode available here: a definitional variance reported as a clerical error.

3. **Semantic.** Pairs of keys that must partition a closed enum (`included` / `excluded`)
   are consistent in the baseline *and* in every permutation that touches either side. This
   catches a second flavour of silent no-op that the referential check cannot see, because
   every override path resolves correctly and only the meaning is wrong.

4. **Adversarial.** A set of deliberately invalid policies that the schema MUST reject. A schema
   nobody has watched reject anything is not a guard, it is a comment. These cases are not
   hypothetical — each one is a loosening that someone under deadline pressure would plausibly
   make to get a run to pass.

This script is stdlib + pyyaml + jsonschema only, and deliberately does not import anything from
`src/tda/`. It has to be runnable before the package exists, and it must not be able to pass by
agreeing with the code it is checking.

Exit codes:  0 all checks pass · 1 a check failed · 2 a dependency or input file is missing
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "policy.yaml"
DEFAULT_SCHEMA = REPO_ROOT / "policy.schema.json"

Policy = dict[str, Any]


# ── 3. the adversarial cases ─────────────────────────────────────────────────
#
# Each entry loosens the policy in a way that would break a claim the POC makes out loud.
# The schema must refuse all of them. Keep the "why it matters" text: it is the reason the
# case is here, and without it a future reader will delete the ones that look pedantic.

NEGATIVE_CASES: list[tuple[str, str, Callable[[Policy], None]]] = [
    (
        "classification order drops V7",
        "Extraction limits must be tested first, or the system can accuse a hotel of an error it could not see.",
        lambda p: p["classification"].__setitem__("order", ["V2", "V5", "V1", "V6", "V6"]),
    ),
    (
        "classification consults a model",
        "The code finds the cause; the model only writes the sentence (D-CLS-10).",
        lambda p: p["classification"].__setitem__("consults_model", True),
    ),
    (
        "unclassified variance becomes non-blocking",
        "There is no 'other' bucket. An unexplained difference is the one thing a reviewer cannot act on.",
        lambda p: p["classification"].__setitem__("unclassified", "informational"),
    ),
    (
        "Excel reference made optional",
        "Every finding cites a PDF page AND an Excel cell, or it never reaches the output (D-EV-01).",
        lambda p: p["evidence"].__setitem__("require_excel_ref", False),
    ),
    (
        "PDF reference made optional",
        "Same as above, from the other side.",
        lambda p: p["evidence"].__setitem__("require_source_ref", False),
    ),
    (
        "unmappable country label guessed by nearest match",
        "An unmappable label is blocking and never guessed — not by edit distance, not by a model (D-NAT-12).",
        lambda p: p["metrics"]["guests_by_nationality"]["normalisation"].__setitem__(
            "unmappable_label", "nearest_match"
        ),
    ),
    (
        "occupancy divides by rooms in the BASELINE policy",
        "rooms_available is the canonical definitional error (F3). Valid as a permutation, never as a baseline.",
        lambda p: p["metrics"]["occupancy_pct"].__setitem__("denominator", "rooms"),
    ),
    (
        "inventory reference made optional",
        "Occupancy cannot be verified without it, and must say so rather than approximate (D-RNA-04).",
        lambda p: p["metrics"]["occupancy_pct"].__setitem__("inventory_reference", "optional"),
    ),
    (
        "room-nights adopted from the printed column",
        "Derive, never trust. A printed value is a cross-check, not an input (D-RNS-02).",
        lambda p: p["metrics"]["occupancy_pct"].__setitem__(
            "room_nights_derivation", "printed_column"
        ),
    ),
    (
        "exact tolerance given a value",
        "Contradictory. Reject it rather than silently pick one of the two meanings.",
        lambda p: p["tolerances"]["by_metric_type"]["count"].__setitem__("value", 5),
    ),
    (
        "absolute tolerance with no value",
        "A non-exact tolerance without a value silently accepts everything.",
        lambda p: p["tolerances"]["by_metric_type"].__setitem__(
            "percentage_points", {"type": "absolute"}
        ),
    ),
    (
        "unknown status silently coerced",
        "An unknown vendor status string is blocking, never mapped to CHECKED_OUT (D-QUAL-03).",
        lambda p: p["qualifying"]["status"].__setitem__("unknown_value", "coerce"),
    ),
    (
        "duplicate reservation ids de-duplicated",
        "A duplicate row means the export is wrong. Collapsing it hides that (D-QUAL-07).",
        lambda p: p["qualifying"].__setitem__("duplicate_reservation_id", "deduplicate"),
    ),
    (
        "guest names permitted in state and logs",
        "No guest name enters state, output or logs (D-EV-03). This is a data-protection boundary.",
        lambda p: p["evidence"].__setitem__("guest_name_in_state_or_logs", "allowed"),
    ),
    (
        "definitional items merged with hotel errors",
        "A count of hotel errors that includes policy disagreements is a wrong number presented as a right one (D-MAT-06).",
        lambda p: p["materiality"].__setitem__("definitional_items_separate_array", False),
    ),
    (
        "V7 downgraded from blocking",
        "Blocking outranks every other severity. A V7 is the system saying it could not see (D-MAT-01).",
        lambda p: p["materiality"]["severity_by_class"].__setitem__("V7", "material"),
    ),
    (
        "model falls back to free text on validation failure",
        "A validation failure is an error. Free text must never enter the numeric path.",
        lambda p: p["model"].__setitem__("validation_failure", "fallback_to_text"),
    ),
    (
        "replay miss silently makes a live call",
        "A replay run is offline and reproducible. A live fallback destroys both properties invisibly.",
        lambda p: p["model"].__setitem__("replay_miss", "live_call"),
    ),
    (
        "temperature reintroduced",
        "Rejected with HTTP 400 on current models, and it never guaranteed determinism. See ADR-0002.",
        lambda p: p["model"].__setitem__("temperature", 0),
    ),
    (
        "agent budget removed",
        "A run with no cap is a run a looping agent can bill without limit.",
        lambda p: p["model"].pop("budget"),
    ),
    (
        "agent budget of zero calls",
        "A budget of nothing refuses the first call, which is a broken run dressed as a spend control.",
        lambda p: p["model"]["budget"].__setitem__("max_calls_per_agent", 0),
    ),
    (
        "permutation id with an unusable format",
        "Permutation ids are named in findings, so they must be stable and human-legible.",
        lambda p: p["permutations"]["ordered"][0].__setitem__("id", "occ denom rooms"),
    ),
    (
        "permutation with no stated explanation",
        "If the cause cannot be stated plainly, the permutation is not usable in a finding (D-CLS-07).",
        lambda p: p["permutations"]["ordered"][0].__setitem__("explains", "n/a"),
    ),
    (
        "unknown top-level key (a typo, silently ignored)",
        "A misspelled 'tolerances' would leave the real tolerances at their defaults and nobody would know.",
        lambda p: p.__setitem__("tolerence", {}),
    ),
]


def _display(path: Path) -> str:
    """Repo-relative when it can be, absolute otherwise — a path outside the repo is a
    legitimate argument (validating a candidate policy before committing it), not a crash."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _fail(message: str) -> None:
    print(f"  FAIL  {message}")


def _ok(message: str) -> None:
    print(f"  ok    {message}")


def resolve_path(root: Policy, dotted: str) -> bool:
    """True if a dotted path resolves to an existing key in the policy."""
    node: Any = root
    for segment in dotted.split("."):
        if not isinstance(node, dict) or segment not in node:
            return False
        node = node[segment]
    return True


def check_structural(policy: Policy, validator: Any) -> bool:
    errors = sorted(validator.iter_errors(policy), key=lambda e: list(e.path))
    if errors:
        for error in errors:
            location = "/".join(str(part) for part in error.path) or "<root>"
            _fail(f"{location}: {error.message}")
        return False
    _ok(f"policy v{policy['version']} satisfies the schema")
    return True


# Pairs of keys that must partition a closed enum. `qualifies()` reads the `included` side,
# so a permutation that edits only `excluded` changes nothing: it reproduces the baseline,
# never matches a claim, and is never named — the silent no-op again, this time with every
# override path resolving perfectly. Found the hard way while wiring the typed loader.
PARTITIONED: tuple[tuple[str, str], ...] = (
    ("qualifying.status.included", "qualifying.status.excluded"),
    ("qualifying.rate_code.included", "qualifying.rate_code.excluded"),
)


def check_partitions(policy: Policy) -> bool:
    """A permutation touching one side of a partition must touch both."""
    passed = True
    for perm in policy["permutations"]["ordered"]:
        paths = set(perm["override"])
        for left, right in PARTITIONED:
            touched = paths & {left, right}
            if touched and touched != {left, right}:
                missing = ({left, right} - touched).pop()
                _fail(
                    f"permutation {perm['id']} sets {touched.pop()} but not {missing}. "
                    "These must partition a closed enum, and the qualifying check reads the "
                    "`included` side - a one-sided override is a silent no-op permutation"
                )
                passed = False

    # And the baseline itself must partition, or the same hole exists without any permutation.
    for left, _right in PARTITIONED:
        section = left.rsplit(".", 1)[0]
        node = policy
        for segment in section.split("."):
            node = node[segment]
        included, excluded = set(node["included"]), set(node["excluded"])
        if included & excluded:
            _fail(f"{section}: values in both included and excluded: {sorted(included & excluded)}")
            passed = False

    if passed:
        _ok(
            f"{len(PARTITIONED)} partitioned rules consistent in the baseline and every permutation"
        )
    return passed


def check_referential(policy: Policy) -> bool:
    permutations = policy["permutations"]["ordered"]
    passed = True

    unresolved = [
        (perm["id"], path)
        for perm in permutations
        for path in perm["override"]
        if not resolve_path(policy, path)
    ]
    for perm_id, path in unresolved:
        _fail(f"permutation {perm_id}: override path does not exist -> {path}")
        passed = False

    ids = [perm["id"] for perm in permutations]
    duplicates = {i for i in ids if ids.count(i) > 1}
    for duplicate in sorted(duplicates):
        _fail(f"duplicate permutation id: {duplicate}")
        passed = False

    # A permutation must apply to a metric that is actually in scope, or it can never fire.
    in_scope = set(policy["scope"]["metrics_in_scope"])
    for perm in permutations:
        out_of_scope = set(perm["applies_to"]) - in_scope
        if out_of_scope:
            _fail(
                f"permutation {perm['id']} applies to out-of-scope metrics: {sorted(out_of_scope)}"
            )
            passed = False

    if passed:
        override_count = sum(len(p["override"]) for p in permutations)
        _ok(
            f"{len(ids)} permutations: ids unique, {override_count} override paths resolve, all in scope"
        )
    return passed


def check_adversarial(policy: Policy, validator: Any) -> bool:
    accepted: list[tuple[str, str]] = []
    for name, why, mutate in NEGATIVE_CASES:
        candidate = copy.deepcopy(policy)
        mutate(candidate)
        if validator.is_valid(candidate):
            accepted.append((name, why))

    if accepted:
        _fail(f"the schema accepted {len(accepted)} policies it must reject:")
        for name, why in accepted:
            print(f"          · {name}")
            print(f"            why it matters: {why}")
        return False
    _ok(f"schema rejects all {len(NEGATIVE_CASES)} deliberately invalid policies")
    return True


def _load(policy_path: Path, schema_path: Path) -> tuple[Policy, dict[str, Any], Any]:
    try:
        import jsonschema
        import yaml
    except ImportError as exc:  # pragma: no cover - environment problem, not a policy problem
        print(
            f"missing dependency: {exc}. Install with: pip install pyyaml jsonschema",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    for path in (policy_path, schema_path):
        if not path.is_file():
            print(f"not found: {path}", file=sys.stderr)
            raise SystemExit(2)

    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return policy, schema, jsonschema.Draft202012Validator(schema)


def main(argv: list[str]) -> int:
    policy_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_POLICY
    schema_path = Path(argv[2]) if len(argv) > 2 else DEFAULT_SCHEMA

    policy, _schema, validator = _load(policy_path, schema_path)

    print(f"validating {_display(policy_path)} against {_display(schema_path)}")

    results = [
        check_structural(policy, validator),
        check_referential(policy),
        check_partitions(policy),
        check_adversarial(policy, validator),
    ]

    if all(results):
        print("policy OK")
        return 0
    print("policy INVALID", file=sys.stderr)
    return 1


def iter_negative_case_names() -> Iterator[str]:
    """Exposed so the S2 test suite can assert the case list has not shrunk."""
    for name, _why, _mutate in NEGATIVE_CASES:
        yield name


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
