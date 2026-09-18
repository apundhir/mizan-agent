"""`python -m fixtures` - build the scored fixtures, then prove they are reproducible.

Mirrors `python -m datagen` deliberately, down to the exit codes, because the two are the same
kind of thing: a build that writes documents whose bytes are the contract. What differs is what
`--verify` compares against, and the difference is worth stating.

`datagen` compares a rebuild against the digests committed in `corpus/demo/manifest.json`, because
that corpus is tracked. `corpus/fixtures/` is gitignored: the fixtures are generated, and only the
spec is tracked. So there is no committed digest to compare against, and writing one during the
same run would be checking a file against a number the same run produced. Instead the whole tree is
rebuilt into a temporary directory and compared file by file against what is on disk. That catches
both halves of what can go wrong: a builder change nobody meant to make, and a fixture somebody
edited by hand to make an eval pass.

Exit codes: 0 wrote (or verified) the fixtures - 1 the tree on disk does not match - 2 bad
invocation or a fixture that could not be built.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from datagen.reproducible import digest
from fixtures import FIXTURE_SET_VERSION
from fixtures.materialise import DEFAULT_DEMO, DEFAULT_OUT, FixtureBuild, materialise_all
from fixtures.spec import FIXTURES, FixtureError


def _on_disk(out: Path, builds: tuple[FixtureBuild, ...]) -> dict[str, str]:
    """Every file under each fixture's directory on disk, digested, keyed by `F2/expected.json`.

    Keyed across the whole tree rather than per fixture so that a fixture directory which exists on
    disk and is no longer produced shows up as an extra key. A stale fixture left behind by an
    earlier version of the spec is a fixture the eval would go on scoring.
    """
    found: dict[str, str] = {}
    for build in builds:
        root = out / build.fixture_id
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                found[f"{build.fixture_id}/{path.relative_to(root)}"] = digest(path)
    return found


def _flatten(builds: tuple[FixtureBuild, ...]) -> dict[str, str]:
    return {
        f"{build.fixture_id}/{name}": value
        for build in builds
        for name, value in build.files.items()
    }


def _verify(demo: Path, out: Path) -> int:
    if not out.is_dir():
        print(f"no fixtures at {out}; run `python -m fixtures` first", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as temporary:
        rebuilt = materialise_all(demo, Path(temporary) / "fixtures")
        expected = _flatten(rebuilt)
    actual = _on_disk(out, rebuilt)

    differences = [
        f"  {name}\n      on disk  {actual.get(name, '(absent)')}\n      rebuilt  {value}"
        for name, value in sorted(expected.items())
        if actual.get(name) != value
    ]
    extra = sorted(set(actual) - set(expected))

    if differences or extra:
        print("the fixture tree is NOT reproducible from the current builder:", file=sys.stderr)
        for line in differences:
            print(line, file=sys.stderr)
        for name in extra:
            print(f"  {name}: on disk, no longer produced", file=sys.stderr)
        print(
            "\nEither the spec or the builder changed and the fixtures need rebuilding "
            "(`python -m fixtures`), or a fixture was edited by hand. The second is the worse "
            "case: an edited expected.json scores the pipeline against something nobody derived.",
            file=sys.stderr,
        )
        return 1

    print(f"fixtures verified: {len(expected)} files across {len(rebuilt)} fixtures match {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--demo", type=Path, default=DEFAULT_DEMO)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Rebuild into a temporary directory and diff digests against --out.",
    )
    args = parser.parse_args(argv)

    demo: Path = args.demo
    out: Path = args.out
    if not demo.is_dir():
        print(f"no demonstration corpus at {demo}; run `make datagen` first", file=sys.stderr)
        return 2

    try:
        if args.verify:
            return _verify(demo, out)
        builds = materialise_all(demo, out)
    except FixtureError as error:
        print(f"fixtures failed: {error}", file=sys.stderr)
        return 2

    print(f"fixture set {FIXTURE_SET_VERSION} written to {out}  ({len(builds)} fixtures)")
    for build in builds:
        print(f"  {build.fixture_id}/  ({len(build.files)} files)")
    if len(builds) < 6:
        print(
            f"\n{len(FIXTURES)} of 6 fixtures are specified. F4 to F6 turn on an unresolvable "
            "country label, a deleted row and a blocking halt, and each needs a model recording "
            "before it can be scored."
        )
    print("\nVerify byte-reproducibility with:  python -m fixtures --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
