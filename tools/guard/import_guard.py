#!/usr/bin/env python3
"""The import guard: no model client may reach the deterministic core.

ADR-0001 claims that no path in this system lets a model output reach a metric function. A
claim like that is worth exactly as much as its enforcement, so this runs in CI on every push
and fails the build on violation. `tests/arch/` proves it works by feeding it code that
deliberately breaks each rule — a guard nobody has watched fail is a comment.

Five rules, each protecting a different promise:

1. **`src/tda/metrics/` and `src/tda/reconcile/` may not import the agents package or any
   model SDK.** This is the wall. The metric library takes typed records and a policy, and
   returns numbers; the reconciliation layer joins, applies tolerance, and classifies. Neither
   has any business talking to a model, and `classification.consults_model` in `policy.yaml`
   is pinned `false` to say the same thing from the configuration side.

2. **`tools/datagen/` may not import `tda` at all** — not the metric library, not even the
   contracts. The generator's aggregation has to be an independent implementation; if the two
   shared code, the POC would prove only that the code equals itself and `truth_metrics.json`
   would be a tautology rather than ground truth. The rule covers the whole package rather than
   just `tda.metrics` because of one specific method: `ReservationRecord.occupied_nights()` *is*
   D-RNS-03's month apportionment, so importing "just the contracts" would co-derive the exact
   thing under test. Configuration is legitimately shared — the generator reads `policy.yaml`
   directly, because ground truth is only meaningful with respect to a ruleset.

3. **`src/tda/` may not import `datagen`** — the same tautology as rule 2, from the other
   direction. An extractor that read its committed column positions out of the generator's own
   constants would be checking the renderer against itself, and would go on passing after the
   layout stopped matching any document a property would send. The generator is build tooling and
   ships in no wheel, so the rule also keeps an installed package importable.

4. **`tools/fixtures/` may not import `tda`** — the same tautology as rule 2, one layer up.
   The fixture builder derives the expected outcome of each scored fixture, so a builder that
   could reach `tda.reconcile` would compute the expectation with the code the expectation is
   used to score. The eval would then measure whether the system agrees with itself, which is
   the number it exists to avoid reporting.

5. **The deterministic core may not do I/O.** No `open`, no `pathlib.Path.read_*`, no
   `requests`. A pure function whose result depends on a file on disk is not reproducible, and
   reproducibility is the entire argument.

Analysis is AST-based rather than grep-based: a comment mentioning `anthropic` is fine, a
string containing it is fine, and `import anthropic` inside a function body is not — grep
cannot tell those apart, and the difference is exactly where a violation would hide.

Transitive reach is checked too. Importing a first-party module that itself imports the model
client is the same violation wearing a hat.

Run:  python tools/guard/import_guard.py [--roots src/tda] [--quiet]
Exit: 0 clean · 1 violation · 2 bad invocation
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Model SDKs and the in-repo agent layer. Prefix match on the dotted module path.
MODEL_MODULES: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "langchain",
        "langgraph",
        "tda.agents",
    }
)

# I/O that would make a "pure" function depend on the world.
IO_MODULES: frozenset[str] = frozenset(
    {"requests", "httpx", "httpx2", "urllib", "socket", "subprocess", "sqlite3", "boto3"}
)

# Directories that are never first-party source. The transitive check needs a module graph, and
# for a rule on `tools/` that graph is rooted at the repository — which means `.venv` unless it is
# excluded. Without this the guard spends forty seconds parsing site-packages to reach the same
# answer, and a guard slow enough to be irritating is a guard somebody eventually takes out of `ci`.
SKIP_DIRS: frozenset[str] = frozenset(
    {".venv", "venv", ".git", "__pycache__", "node_modules", "build", "dist", ".mypy_cache"}
)

IO_CALLS: frozenset[str] = frozenset({"open"})
IO_ATTRS: frozenset[str] = frozenset(
    {"read_text", "read_bytes", "write_text", "write_bytes", "open", "mkdir", "unlink"}
)


@dataclass(frozen=True)
class Rule:
    """One package, and what it is forbidden to touch."""

    package: str
    forbidden: frozenset[str]
    reason: str
    forbid_io: bool = False


RULES: tuple[Rule, ...] = (
    Rule(
        package="src/tda/metrics",
        forbidden=MODEL_MODULES | IO_MODULES,
        reason=(
            "the metric library is pure functions over typed records; a model near arithmetic "
            "destroys reproducibility, which is what the regulator is being asked to trust (ADR-0001)"
        ),
        forbid_io=True,
    ),
    Rule(
        package="src/tda/reconcile",
        forbidden=MODEL_MODULES | IO_MODULES,
        reason=(
            "classification decides whether a variance is a hotel error or a policy "
            "disagreement. The code finds the cause; the model only writes the sentence (D-CLS-10)"
        ),
        forbid_io=True,
    ),
    Rule(
        package="src/tda",
        forbidden=frozenset({"datagen"}),
        reason=(
            "the product may not import the corpus generator. The reverse rule below stops the "
            "generator co-deriving ground truth from the code under test; this stops the same "
            "tautology arriving from the other direction - an extractor that read its column "
            "positions out of tools/datagen/render_pdf.py would be checking the renderer against "
            "itself, and would keep passing after the layout stopped matching any real document. "
            "The generator is build tooling and ships in no wheel, so this is also what keeps "
            "`pip install mizan` from failing at import"
        ),
    ),
    Rule(
        package="tools/datagen",
        forbidden=frozenset({"tda"}),
        reason=(
            "the generator's aggregation must be an independent implementation, so it may not "
            "import ANY of tda - not even the contracts. ReservationRecord.occupied_nights() is "
            "exactly D-RNS-03's month apportionment, and a generator that called it would emit a "
            "truth_metrics.json co-derived with the code it is supposed to check. Configuration "
            "is legitimately shared (policy.yaml is read directly); implementation is not"
        ),
    ),
    Rule(
        package="tools/fixtures",
        forbidden=frozenset({"tda"}),
        reason=(
            "the fixture builder derives the expected outcome, so importing the system under test "
            "would score the system against itself - the same tautology the generator rule above "
            "prevents one layer up. An expectation computed by tda.reconcile would agree with "
            "tda.reconcile, and `make eval` would report that agreement as a score"
        ),
    ),
)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    imported: str
    rule: Rule
    via: str | None = None

    def render(self, root: Path) -> str:
        try:
            where = self.path.relative_to(root)
        except ValueError:
            where = self.path
        chain = f" (reached via {self.via})" if self.via else ""
        return f"  {where}:{self.line}  imports {self.imported}{chain}\n      why it matters: {self.rule.reason}"


def _module_name(path: Path, source_root: Path) -> str:
    """Dotted module name for a file under a source root, so `tda.metrics.occupancy` etc."""
    relative = path.relative_to(source_root).with_suffix("")
    parts = [p for p in relative.parts if p != "__init__"]
    return ".".join(parts)


def imports_of(tree: ast.AST) -> list[tuple[str, int]]:
    """Every module a file imports, with its line number.

    Walks the whole tree, so a deferred `import anthropic` inside a function body is caught —
    that is where a violation would realistically hide, added at speed to make one thing work.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import. Resolving it needs the package context, which the caller
                # supplies; recorded as-is and resolved there.
                found.append((f".{'.' * (node.level - 1)}{node.module or ''}", node.lineno))
            elif node.module:
                found.append((node.module, node.lineno))
    return found


def io_operations(tree: ast.AST) -> list[tuple[str, int]]:
    """Filesystem and network calls, for packages declared pure."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in IO_CALLS:
            found.append((f"{func.id}()", node.lineno))
        elif isinstance(func, ast.Attribute) and func.attr in IO_ATTRS:
            found.append((f".{func.attr}()", node.lineno))
    return found


def _matches(imported: str, forbidden: frozenset[str]) -> str | None:
    """Prefix match on dotted paths: `tda.agents` forbids `tda.agents.mapping`, and
    `anthropic` forbids `anthropic.types`, without `anthropic_helpers` matching either."""
    for bad in forbidden:
        if imported == bad or imported.startswith(f"{bad}."):
            return bad
    return None


def _python_files(root: Path) -> list[Path]:
    """Every `.py` file under `root`, skipping the directories that are never first-party."""
    return sorted(
        path for path in root.rglob("*.py") if not SKIP_DIRS & set(path.relative_to(root).parts)
    )


def _first_party_graph(source_root: Path) -> dict[str, set[str]]:
    """Map every first-party module to the modules it imports, for transitive checking."""
    graph: dict[str, set[str]] = {}
    for path in _python_files(source_root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # a deliberately broken fixture; the per-file pass reports it
        module = _module_name(path, source_root)
        graph[module] = {name for name, _ in imports_of(tree) if not name.startswith(".")}
    return graph


def _transitively_reaches(
    start: str, forbidden: frozenset[str], graph: dict[str, set[str]]
) -> tuple[str, str] | None:
    """Whether `start` reaches anything forbidden through first-party imports.

    Returns (the forbidden module, the first-party module that imports it) so the message can
    name the hop. "metrics/occupancy.py imports tda.helpers" is not actionable; "…via
    tda.helpers, which imports anthropic" is.
    """
    seen: set[str] = set()
    queue = [start]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for imported in graph.get(current, set()):
            hit = _matches(imported, forbidden)
            if hit is not None and current != start:
                return hit, current
            if imported.startswith("tda.") and imported in graph:
                queue.append(imported)
    return None


def check_package(rule: Rule, root: Path) -> list[Violation]:
    package_dir = root / rule.package
    if not package_dir.is_dir():
        return []

    source_root = root / "src" if rule.package.startswith("src/") else root
    graph = _first_party_graph(source_root) if source_root.is_dir() else {}
    violations: list[Violation] = []

    for path in _python_files(package_dir):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(
                Violation(path=path, line=exc.lineno or 0, imported="<unparseable>", rule=rule)
            )
            continue

        for imported, line in imports_of(tree):
            hit = _matches(imported, rule.forbidden)
            if hit is not None:
                violations.append(Violation(path=path, line=line, imported=imported, rule=rule))

        if rule.forbid_io:
            for operation, line in io_operations(tree):
                violations.append(
                    Violation(
                        path=path,
                        line=line,
                        imported=f"{operation} [filesystem I/O in a pure package]",
                        rule=rule,
                    )
                )

        if graph:
            module = _module_name(path, source_root)
            reach = _transitively_reaches(module, rule.forbidden, graph)
            if reach is not None:
                hit, via = reach
                violations.append(Violation(path=path, line=0, imported=hit, rule=rule, via=via))

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root to check. Tests point this at a fixture tree.",
    )
    parser.add_argument("--quiet", action="store_true", help="Print only on failure.")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    violations: list[Violation] = []
    checked: list[str] = []
    for rule in RULES:
        if (root / rule.package).is_dir():
            checked.append(rule.package)
        violations.extend(check_package(rule, root))

    if violations:
        print(f"import guard FAILED: {len(violations)} violation(s)", file=sys.stderr)
        for violation in violations:
            print(violation.render(root), file=sys.stderr)
        print(
            "\nThis guard is not the problem. If a package listed above needs something from an\n"
            "agent, the design is wrong - see docs/adr/0001-deterministic-core-agentic-edges.md.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        if checked:
            print(f"import guard ok: {', '.join(checked)}")
        else:
            print("import guard ok: no guarded packages present yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
