"""Writing the fixtures out, and proving the demo corpus was not touched while doing it.

A fixture is a **copy** of `corpus/demo/` with one document re-rendered. The PDFs and the inventory
reference are byte-copied, because a fixture that re-rendered them would be testing the renderer
rather than the mutation, and any drift in reportlab would show up as six fixtures changing at once.
Only the workbook is rebuilt, from the mutated claim table, through the same `render_workbook` the
clean submission comes out of.

## The demo corpus is never written to, and that is a digest rather than a sentence

Every file under `corpus/demo/` is digested against `manifest.json` before the run and against that
same snapshot afterwards. `tda.outputs.workbook._check_untouched` makes the identical argument about
a submitted workbook: under every reachable input the check cannot fire, because nothing here opens
a demo file for writing, and that is exactly why it is worth having. The failure it guards against
is one mistaken output path away, it would be silent, and the consequence is a corpus whose
`truth_metrics.json` no longer describes its own documents. "We only write to the copy" is an
intention. A digest taken twice is a fact.

## What a fixture directory does not contain

`ground_truth/` is not copied. The pipeline is pointed at a fixture's `submission/`, and a fixture
that carried the answers next to the questions would be one wrong path away from a verification
that had seen them. What a fixture needs to know about truth is already in `expected.json`, derived
and schema-checked, and that file is read by the scorer rather than by the run.

`manifest.json` is copied verbatim, so a fixture records the corpus it was mutated from. Its
digests therefore describe the demo files rather than the fixture's own, which the fixture README
says in as many words: a manifest that looked like it covered the mutated workbook would be the
more dangerous artefact.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from datagen.aggregate import load_policy_view
from datagen.claims import baseline_table
from datagen.render_workbook import render_workbook
from datagen.reproducible import digest, finalise_xlsx, write_json, write_text
from datagen.spec import LAYOUT, MONTHS
from fixtures.derive import ClassificationView, expectation, load_classification_view
from fixtures.mutate import PermutationView, load_permutation_view, load_policy_document, mutated
from fixtures.spec import FIXTURES, FixtureError, MutationKind

if TYPE_CHECKING:
    from datagen.aggregate import PolicyView
    from fixtures.spec import FixtureSpec

DEFAULT_DEMO: Final = Path("corpus/demo")
DEFAULT_OUT: Final = Path("corpus/fixtures")

EXPECTED_FILE: Final = "expected.json"

# Copied rather than re-rendered. See the module docstring.
BYTE_COPIED: Final[tuple[str, ...]] = (
    LAYOUT.inventory_csv,
    *(LAYOUT.pdf_name(month) for month in MONTHS),
)


@dataclass(frozen=True, slots=True)
class FixtureBuild:
    """One fixture on disk, with the digest of every file in it.

    The digests are what `--verify` compares. They are computed from the tree rather than written
    into it: a fixture tree is generated and gitignored, so there is no committed manifest for a
    hand edit to be checked against, and a manifest the same run wrote would be checking a file
    against itself.
    """

    fixture_id: str
    path: Path
    files: dict[str, str]


def load_truth(demo: Path) -> dict[str, float]:
    """The demo corpus's ground truth, as the claim table and the derivation read it.

    Read from the corpus rather than recomputed from the ledger. The fixture's documents are copies
    of the demo's, so the truth they are to be judged against is the demo's truth: recomputing it
    here would introduce a second aggregation that could drift from the one the PDFs were rendered
    from, and the fixtures would then plant errors against figures the reports do not contain.
    """
    path = demo / LAYOUT.ground_truth / LAYOUT.truth_metrics
    document = json.loads(path.read_text(encoding="utf-8"))
    metrics = document.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise FixtureError(f"{path} carries no `metrics` object")
    return {str(key): value for key, value in metrics.items()}


def manifest_digests(demo: Path) -> dict[str, str]:
    """The digests `corpus/demo/manifest.json` commits to, keyed by relative path."""
    path = demo / LAYOUT.manifest
    document = json.loads(path.read_text(encoding="utf-8"))
    files = document.get("files")
    if not isinstance(files, dict) or not files:
        raise FixtureError(f"{path} carries no `files` map; the demo corpus cannot be checked")
    return {str(name): str(value) for name, value in files.items()}


def demo_digests(demo: Path) -> dict[str, str]:
    """Every file under the demo corpus, digested now. `manifest.json` names all the others.

    Walked rather than read off the manifest, so a file *added* to the corpus is caught as well as
    a file changed. An extra input nobody meant to leave there is the quieter of the two failures.
    """
    return {
        str(path.relative_to(demo)): digest(path)
        for path in sorted(demo.rglob("*"))
        if path.is_file() and path.name != LAYOUT.manifest
    }


def _report(before: dict[str, str], after: dict[str, str]) -> list[str]:
    names = sorted(set(before) | set(after))
    return [
        f"  {name}: {before.get(name, '(absent)')} -> {after.get(name, '(absent)')}"
        for name in names
        if before.get(name) != after.get(name)
    ]


def check_demo_untouched(demo: Path, before: dict[str, str]) -> None:
    """The postcondition: the demonstration corpus is byte-identical to what we were handed."""
    moved = _report(before, demo_digests(demo))
    if moved:
        raise FixtureError(
            "the demonstration corpus changed while fixtures were being built:\n"
            + "\n".join(moved)
            + "\n\nNothing here writes to corpus/demo, so this is a path that escaped its copy. "
            "The corpus is the only thing every fixture is derived from, and a mutated corpus "
            "would make every expectation in the set describe documents that no longer exist."
        )


def _check_demo_matches_its_manifest(demo: Path) -> dict[str, str]:
    """Digest the corpus before anything is built, and refuse to build on a corpus that has moved.

    Checked first for a reason worth stating: if the demo corpus already disagrees with its own
    manifest, the fixtures derived from it would be correct with respect to nothing. `make datagen
    --verify` is the tool for that failure, and this points at it rather than repeating it.
    """
    on_disk = demo_digests(demo)
    drifted = _report(manifest_digests(demo), on_disk)
    if drifted:
        raise FixtureError(
            f"{demo} does not match its own manifest.json:\n"
            + "\n".join(drifted)
            + "\n\nThe fixtures are copies of this corpus and their expectations are derived from "
            "its ground truth, so a corpus that has drifted produces a fixture set that is "
            "correct with respect to nothing. Regenerate with `make datagen` first."
        )
    return on_disk


# ── one fixture ──────────────────────────────────────────────────────────────


README = """# Fixture {fixture_id}

{why}

## The mutation

{mutation}

## The derived expectation

Status **{status}**, with {findings} finding(s) and {definitional} definitional item(s). Every
line of it was derived from the mutated claim table and the corpus's ground truth by
`tools/fixtures/derive.py`; none of it was written by hand. `expected.json` is the machine-readable
form and is what `make eval` scores against.

{lines}

## What is in this directory

`submission/` is what the hotel sends and is the only thing the pipeline reads. The PDFs and the
inventory reference are byte-copies of `corpus/demo/`; the workbook was re-rendered from the
mutated claim table. `manifest.json` is a verbatim copy of the demo corpus's manifest, so this
fixture records which corpus it was mutated from: its digests describe the demo files, not the
mutated workbook beside it.

Generated by `python -m fixtures`. Regenerate rather than edit: `python -m fixtures --verify`
compares digests and a hand edit fails it.
"""


def _mutation_prose(spec: FixtureSpec) -> str:
    mutation = spec.mutation
    if mutation.kind is MutationKind.NONE:
        return "None. This is the control, and the submission is the clean one, unaltered."

    assert mutation.target is not None  # guaranteed by Mutation.__post_init__
    target = mutation.target
    where = f"`{target.metric}`"
    if target.period is not None:
        where += f" for {target.period}"
    else:
        where += " for every period it is claimed for"
    if target.value_key is not None:
        where += f", dimension value `{target.value_key}`"

    if mutation.kind is MutationKind.TRANSPOSE_DIGITS:
        return (
            f"The first two digits of {where} were transposed, and the quarter roll-up was "
            "recomputed from the mutated months, because a spreadsheet adds its own column up."
        )
    if mutation.kind is MutationKind.RECOMPUTE_UNDER:
        return (
            f"{where} was recomputed under the committed permutation "
            f"`{mutation.permutation}`, using the workbook's own stated numerator."
        )
    if mutation.kind is MutationKind.DELETE_DIMENSION:
        return f"The row for {where} was removed from the workbook entirely."
    assert mutation.kind is MutationKind.RELABEL  # the only kind left, once NONE returns early
    resolves = (
        "resolves through the committed lookup"
        if mutation.resolvable
        else "the committed lookup has no entry for"
    )
    return f"{where} was printed as `{mutation.new_label}`, which {resolves} it."


def _finding_lines(payload: dict[str, object]) -> str:
    lines: list[str] = []
    for name in ("findings", "definitional_items"):
        entries = payload[name]
        assert isinstance(entries, list)
        for entry in entries:
            assert isinstance(entry, dict)
            evidence = entry["evidence"]
            assert isinstance(evidence, dict)
            excel = evidence["excel"]
            where = f"{excel['sheet']}!{excel['cell']}" if isinstance(excel, dict) else str(excel)
            detail = (
                f"- `{entry['key']}` {entry['variance_class']} "
                f"({entry['severity']}, to {entry['escalates_to']}) at {where}: "
                f"claimed {entry['claimed']}, computed {entry['computed']}"
            )
            if entry["proposed_correction"] is not None:
                detail += f", correction {entry['proposed_correction']}"
            if entry["explaining_permutation"] is not None:
                detail += f", explained by {entry['explaining_permutation']}"
            lines.append(detail)
    return "\n".join(lines) if lines else "No findings, and none expected."


def materialise_one(
    spec: FixtureSpec,
    demo: Path,
    out: Path,
    truth: dict[str, float],
    policy: PolicyView,
    permutations: PermutationView,
    view: ClassificationView,
) -> FixtureBuild:
    """Build one fixture directory. The directory is recreated rather than written into."""
    table = mutated(spec, baseline_table(MONTHS, truth), policy, permutations)
    payload = expectation(spec, table, truth, policy, view)

    root = out / spec.fixture_id
    if root.exists():
        shutil.rmtree(root)
    submission = root / LAYOUT.submission
    submission.mkdir(parents=True)

    for name in BYTE_COPIED:
        shutil.copyfile(demo / LAYOUT.submission / name, submission / name)
    shutil.copyfile(demo / LAYOUT.manifest, root / LAYOUT.manifest)

    workbook = submission / LAYOUT.workbook
    render_workbook(workbook, MONTHS, dict(truth), claims=table)
    finalise_xlsx(workbook)

    write_json(root / EXPECTED_FILE, payload)
    write_text(
        root / LAYOUT.readme,
        README.format(
            fixture_id=spec.fixture_id,
            why=spec.why,
            mutation=_mutation_prose(spec),
            status=payload["status"],
            findings=len(payload["findings"]) if isinstance(payload["findings"], list) else 0,
            definitional=(
                len(payload["definitional_items"])
                if isinstance(payload["definitional_items"], list)
                else 0
            ),
            lines=_finding_lines(payload),
        ),
    )

    return FixtureBuild(
        fixture_id=spec.fixture_id,
        path=root,
        files={
            str(path.relative_to(root)): digest(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        },
    )


def materialise_all(
    demo: Path = DEFAULT_DEMO,
    out: Path = DEFAULT_OUT,
    specs: tuple[FixtureSpec, ...] = FIXTURES,
) -> tuple[FixtureBuild, ...]:
    """Build every fixture, with the corpus digested on both sides of the run."""
    before = _check_demo_matches_its_manifest(demo)

    document = load_policy_document()
    policy = load_policy_view()
    permutations = load_permutation_view(document)
    view = load_classification_view(document)
    truth = load_truth(demo)

    out.mkdir(parents=True, exist_ok=True)
    builds = tuple(
        materialise_one(spec, demo, out, truth, policy, permutations, view) for spec in specs
    )

    check_demo_untouched(demo, before)
    return builds
