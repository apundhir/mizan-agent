#!/usr/bin/env python3
"""The agent schema lint: no agent may return a number.

The import guard stops a model *client* from reaching the deterministic core. This stops a
model *value* from reaching it by the other route — travelling as a field on an agent's output
contract. That route is the more dangerous of the two, because it looks completely innocent in
review: a `count: int` on a mapping agent's response is one word, passes every type check, and
quietly makes a model the source of a number that ends up on an invoice.

**The rule.** No class deriving from `AgentOutput` may declare a numeric field, except the
whitelisted reference integers below. Agents pass **keys and references, never numbers** — the
mapping agent returns a cell range, not the value in it; the resolution agent returns a country
code, not a guest count.

**Why `AgentOutput` and not "every model in the agents package".** Scanning one directory for
bare `BaseModel` subclasses gets both halves wrong. It misses an output contract declared
anywhere else, and it flags things that are not contracts at all: the provider layer legitimately
carries token counts, and a count of what a call cost is not an agent's answer. So the marker base
is the subject of the rule and the scan covers all of `src/`, wherever a contract is declared.

A second check closes the obvious dodge: a model inside `agents/contracts/` that does **not**
derive from `AgentOutput` fails the lint, so the marker cannot be quietly omitted to escape the
numeric rule. (Telemetry avoids both checks by living in `tda.obs`, which is where it belongs.)

**A number a contract *reaches* is a number it returns.** The rule follows nested models, so a
contract pointing at a plain `BaseModel` that carries an `int` fails just as one declaring the
`int` itself does. This closed a real hole: every nested model on a contract used to be an
`AgentOutput` too, so the recursion happened by rule — until `PdfCitation.ref: PdfRef` pointed a
contract at `tda.contracts.refs`, and the guarantee was being met by coincidence.

**The whitelist** is `page`, `row`, `row_start`, `row_end`. These are citations, not
quantities: a page number is a place to look, and nothing downstream does arithmetic on it.
Anything else numeric is a finding.

Booleans are allowed. A `refused: bool` or `is_total_row: bool` is a classification, and
classification is exactly what agents are for.

Run:  python tools/guard/agent_schema_lint.py [--package src] [--quiet]
Exit: 0 clean · 1 violation · 2 bad invocation
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PACKAGE = Path("src")
# Models declared here must be marked `AgentOutput`. Anywhere else, only the marker matters.
CONTRACTS_DIR = Path("src/tda/agents/contracts")
MARKER = "AgentOutput"

if TYPE_CHECKING:
    from collections.abc import Sequence

# Numeric annotations, by the name as written in source. Checked textually rather than by
# importing the models: the lint has to run in CI without the agents extra installed, and
# without executing code whose whole purpose is to talk to a model.
NUMERIC_NAMES: frozenset[str] = frozenset(
    {"int", "float", "Decimal", "complex", "PositiveInt", "NonNegativeInt", "PositiveFloat"}
)

# Citations, not quantities. Nothing downstream does arithmetic on a page number.
ALLOWED_FIELDS: frozenset[str] = frozenset({"page", "row", "row_start", "row_end"})

# `AgentOutput` itself and `Abstention` are the marker and its canonical implementation; neither
# declares a numeric field, and both are checked like any other subclass.
PYDANTIC_BASES: frozenset[str] = frozenset({"BaseModel", "TypedDict"})


class Kind(StrEnum):
    """Two distinct failures, which need two distinct explanations. Reporting the unmarked-class
    case with the numeric-field message sends a reader looking for a number that is not there."""

    NUMERIC_FIELD = "numeric field on an agent contract"
    UNMARKED_CONTRACT = f"model in the contracts package not derived from {MARKER}"
    NUMERIC_NESTED = "numeric field reachable from an agent contract"


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    model: str
    kind: Kind
    field: str = ""
    annotation: str = ""
    via: str = ""

    def render(self, root: Path) -> str:
        try:
            where = self.path.relative_to(root)
        except ValueError:
            where = self.path

        if self.kind is Kind.NUMERIC_NESTED:
            return (
                f"  {where}:{self.line}  {self.model}.{self.field}: {self.annotation}\n"
                f"      Reached from an agent contract by: {self.via}\n"
                f"      The contract itself declares no number, but an agent returning it\n"
                f"      returns this one. Point the contract at a reference type instead."
            )
        if self.kind is Kind.UNMARKED_CONTRACT:
            return (
                f"  {where}:{self.line}  {self.model} does not derive from {MARKER}\n"
                f"      A model in the contracts package must be marked, so the numeric rule\n"
                f"      applies to it. If this is not an agent output, move it out of\n"
                f"      agents/contracts/ - telemetry belongs in tda.obs."
            )
        return (
            f"  {where}:{self.line}  {self.model}.{self.field}: {self.annotation}\n"
            f"      An agent must not return a number. Return the key or reference that\n"
            f"      identifies it and let the metric library compute the value."
        )


def _annotation_names(node: ast.expr) -> set[str]:
    """Every bare name in an annotation, so `int | None`, `list[Decimal]` and
    `Annotated[float, Field(...)]` are all caught rather than only the simple case."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            # A stringified annotation, e.g. `x: "int"` or `from __future__ import annotations`
            # combined with a quoted form.
            names.add(child.value.strip().strip("'\""))
    return names


def _base_names(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for base in node.bases:
        names |= _annotation_names(base)
    return names


def _is_agent_output(node: ast.ClassDef, marked: set[str]) -> bool:
    """Whether a class is an agent output contract.

    `marked` accumulates the names already known to be `AgentOutput` descendants, so a subclass
    of a subclass is caught. Errs towards yes: a false positive costs a whitelist entry, a false
    negative costs the guarantee.
    """
    bases = _base_names(node)
    return bool(bases & ({MARKER} | marked))


def _is_bare_pydantic_model(node: ast.ClassDef) -> bool:
    return bool(_base_names(node) & PYDANTIC_BASES)


@dataclass(frozen=True)
class ModelFields:
    """One model's annotated fields, as written. Enough to recurse without importing anything."""

    path: Path
    name: str
    fields: tuple[tuple[str, str, int], ...]  # (field, unparsed annotation, line)
    referenced: tuple[str, ...]  # every bare name appearing in those annotations


def index_models(files: Sequence[Path]) -> dict[str, list[ModelFields]]:
    """Every class in the package, by name, with its annotated fields.

    Keyed by bare name and holding a **list**, because two files may legitimately declare the same
    name. On a collision every candidate is checked, which errs the way the rest of this lint does:
    a false positive costs a reference type being renamed, a false negative costs the guarantee.

    Indiscriminate about what counts as a model - any class with annotated fields is recorded. The
    recursion below only ever looks up names that actually appear in a contract's annotations, so
    an over-broad index costs memory and nothing else.
    """
    index: dict[str, list[ModelFields]] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            fields: list[tuple[str, str, int]] = []
            referenced: set[str] = set()
            for statement in node.body:
                if not isinstance(statement, ast.AnnAssign) or not isinstance(
                    statement.target, ast.Name
                ):
                    continue
                fields.append(
                    (
                        statement.target.id,
                        ast.unparse(statement.annotation),
                        statement.lineno,
                    )
                )
                referenced |= _annotation_names(statement.annotation)
            if fields:
                index.setdefault(node.name, []).append(
                    ModelFields(
                        path=path,
                        name=node.name,
                        fields=tuple(fields),
                        referenced=tuple(sorted(referenced)),
                    )
                )
    return index


def nested_violations(
    contract: ModelFields, index: dict[str, list[ModelFields]]
) -> list[Violation]:
    """Numeric fields reachable from an agent contract through a nested model.

    The hole this closes: `tools/guard/agent_schema_lint.py` checked the fields **declared on**
    an `AgentOutput` subclass and stopped there. Every nested model on a contract used to be an
    `AgentOutput` itself, so the recursion happened by rule - until `PdfCitation.ref: PdfRef`
    pointed a contract at a bare `BaseModel` in `tda.contracts.refs`. Those three ref types carry
    only whitelisted citation integers, so nothing was wrong; but the rule was being satisfied by
    coincidence, and `pyproject.toml`'s `runtime-evaluated-base-classes` entry exists precisely to
    make that nesting comfortable, so it will spread.

    Breadth-first with a visited set, because a model graph can be cyclic and a lint that hangs is
    a lint somebody removes from CI.
    """
    violations: list[Violation] = []
    seen: set[str] = {contract.name}
    # (model name, the path of field names that reached it)
    queue: list[tuple[str, str]] = [
        (name, f"{contract.name}.{field}")
        for field, annotation, _line in contract.fields
        for name in sorted(_annotation_names(ast.parse(annotation, mode="eval").body))
    ]

    while queue:
        name, via = queue.pop(0)
        if name in seen or name in NUMERIC_NAMES or name not in index:
            continue
        seen.add(name)
        for model in index[name]:
            for field, annotation, line in model.fields:
                inner = _annotation_names(ast.parse(annotation, mode="eval").body)
                if field not in ALLOWED_FIELDS and inner & NUMERIC_NAMES:
                    violations.append(
                        Violation(
                            path=model.path,
                            line=line,
                            model=model.name,
                            kind=Kind.NUMERIC_NESTED,
                            field=field,
                            annotation=annotation,
                            via=via,
                        )
                    )
                queue.extend(
                    (next_name, f"{via} -> {model.name}.{field}")
                    for next_name in sorted(inner - NUMERIC_NAMES)
                )
    return violations


def contracts_in(path: Path) -> list[ModelFields]:
    """Every `AgentOutput` subclass declared in one file, with its fields."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []
    marked: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _is_agent_output(node, marked):
            marked.add(node.name)

    found: list[ModelFields] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not _is_agent_output(node, marked):
            continue
        fields = tuple(
            (statement.target.id, ast.unparse(statement.annotation), statement.lineno)
            for statement in node.body
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        )
        found.append(ModelFields(path=path, name=node.name, fields=fields, referenced=()))
    return found


def check_file(path: Path, *, in_contracts_dir: bool = False) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    violations: list[Violation] = []
    # Two passes over classes in source order, so a subclass declared after its parent is seen
    # as an AgentOutput descendant.
    marked: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _is_agent_output(node, marked):
            marked.add(node.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # The dodge check: a model in the contracts package that is not marked.
        if (
            in_contracts_dir
            and node.name != MARKER
            and _is_bare_pydantic_model(node)
            and not _is_agent_output(node, marked)
        ):
            violations.append(
                Violation(path=path, line=node.lineno, model=node.name, kind=Kind.UNMARKED_CONTRACT)
            )
            continue

        if not _is_agent_output(node, marked):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(
                statement.target, ast.Name
            ):
                continue
            field = statement.target.id
            if field in ALLOWED_FIELDS:
                continue
            names = _annotation_names(statement.annotation)
            numeric = names & NUMERIC_NAMES
            if numeric:
                violations.append(
                    Violation(
                        path=path,
                        line=statement.lineno,
                        model=node.name,
                        kind=Kind.NUMERIC_FIELD,
                        field=field,
                        annotation=ast.unparse(statement.annotation),
                    )
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--package",
        type=Path,
        default=DEFAULT_PACKAGE,
        help="Package to lint, relative to --root.",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    package: Path = root / args.package
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    if not package.is_dir():
        if not args.quiet:
            print(f"agent schema lint ok: {args.package} does not exist yet")
        return 0

    contracts_dir = (root / CONTRACTS_DIR).resolve()
    files = sorted(package.rglob("*.py"))
    violations = [
        v
        for path in files
        for v in check_file(path, in_contracts_dir=contracts_dir in path.resolve().parents)
    ]

    # And the numbers a contract reaches rather than declares. See `nested_violations`.
    index = index_models(files)
    violations.extend(
        v
        for path in files
        for contract in contracts_in(path)
        for v in nested_violations(contract, index)
    )

    if violations:
        counts = Counter(v.kind for v in violations)
        summary = ", ".join(f"{n} x {kind.value}" for kind, n in sorted(counts.items()))
        print(f"agent schema lint FAILED: {summary}", file=sys.stderr)
        for violation in violations:
            print(violation.render(root), file=sys.stderr)
        print(
            "\nAgents pass keys and references, never numbers. The whitelist is "
            f"{sorted(ALLOWED_FIELDS)} - citations, not quantities.\n"
            "Telemetry (token counts, durations) belongs in tda.obs, not on a contract.\n"
            "See docs/adr/0001-deterministic-core-agentic-edges.md, decision point 3.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        contracts = sum(
            1
            for path in files
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ClassDef) and _is_agent_output(node, set())
        )
        print(
            f"agent schema lint ok: {contracts} {MARKER} contract(s) across "
            f"{len(files)} file(s) in {args.package}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
