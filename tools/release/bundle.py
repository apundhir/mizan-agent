#!/usr/bin/env python3
"""`make bundle`: the release artefact an officer or auditor is actually handed.

Every other target in this repository produces evidence for somebody who already has the repo
open. This is the one target for somebody who does not: `dist/mizan-<version>.tar.gz`, holding a
real run of the demo corpus, the eval report that run's pipeline was measured against, and a
manifest naming exactly what is inside and which commit built it.

## Why a fresh run, not whatever is sitting in `artifacts/`

`artifacts/` accumulates every run anyone has done locally: old submissions, failed experiments,
runs against a policy version three commits old. Bundling "the latest directory" as found would
ship whatever happened to be there last, silently. This target runs the demo corpus itself,
through `mizan run`, and bundles that run and no other, so the bundle's contents are always
answerable to "how was this produced" with one command.

## Why `make run` runs in-process and `make eval` does not

`fresh_run` calls `tda.cli.main` directly rather than shelling out. A bare `mizan run` has no
build dependency of its own, so calling the same function `make run` calls is running `make run`,
and doing it in-process is what lets this module take an `artifacts_dir` a test can point at a
temporary directory instead of the repository's own `artifacts/`.

`ensure_scorecard` shells out to `make eval` instead, because `eval` is not a leaf. The Makefile
target depends on `fixtures`, which builds `corpus/fixtures/` if it is not already on disk. That
dependency graph belongs to the Makefile. Re-deriving it here (checking whether the fixture tree
exists, importing `fixtures` with `tools/` off `sys.path`) would just be a second copy of a rule
that already lives in one place.

## Why a scorecard is required rather than merely preferred

A release bundle with no eval evidence looks, to whoever opens it, exactly like a release bundle
whose eval passed and was left out to save space. Those are opposite facts. `ensure_scorecard`
tries to produce one before giving up, but if `artifacts/eval/scorecard.json` still is not there
afterwards, `make bundle` fails loudly rather than shipping an archive that cannot be told apart
from a measured one.

## Why `.tar.gz` and not `.zip`

The two files this bundle already carries, `memo.docx` and the annotated `.xlsx`, are themselves
zip containers. Nesting a zip inside a zip is the format most likely to confuse an officer's own
unzip tool ("why are there two files that look the same"). A `.tar.gz` reads as one wrapper around
ordinary files on every platform this system runs on, and `tarfile` needs no dependency beyond the
standard library that is not already in this repository's closure.

## The manifest

`MANIFEST.json` sits at the archive root, not inside `run/`, so it is the first thing a
`tar -tzf` or an unzip-and-look shows. It names the package version, the exact git commit the
bundle was built from (and whether the tree was dirty when it was), the run id inside, the eval
outcome summary, and the full file list. An auditor can then check what they were handed against
what the manifest says they were handed, without opening either the workbook or the memo.

Run:  python tools/release/bundle.py [--out dist]
Exit: 0 wrote the archive. 2 a required input was missing, or a build step did not produce it.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DIST_DIR: Final = REPO_ROOT / "dist"
ARTIFACTS_DIR: Final = REPO_ROOT / "artifacts"
EVAL_OUT: Final = ARTIFACTS_DIR / "eval"

SCORECARD_FILE: Final = "scorecard.json"
REPORT_FILE: Final = "report.md"
MANIFEST_FILE: Final = "MANIFEST.json"

# Every file `mizan run` writes that this bundle refuses to ship without. The annotated workbook's
# name depends on the submitted file, so it is found by pattern rather than listed here.
RUN_FILES: Final = (
    "verdict.json",
    "memo.docx",
    "run.json",
    "trace.jsonl",
    "nodes.jsonl",
    "routing.jsonl",
)

OK: Final = 0
COULD_NOT_BUILD: Final = 2


class BundleError(Exception):
    """A required input is missing, or a build step did not produce what it promised.

    `make bundle` exits non-zero on this rather than shipping an archive with a hole in it. The
    person opening the bundle has no way to notice that something was quietly left out, so the
    failure has to happen here, loudly, or it does not happen at all.
    """


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """One completed run of the demo corpus, and the files this bundle takes from it."""

    directory: Path
    files: tuple[Path, ...]


def package_version(repo_root: Path = REPO_ROOT) -> str:
    """`pyproject.toml`'s declared version.

    Not `tda.__version__`. Both name the same thing and `tests/unit/test_packaging.py` pins them
    together, but reading the file directly means this script names a version correctly even run
    against a checkout whose editable install is stale.
    """
    with (repo_root / "pyproject.toml").open("rb") as handle:
        version: str = tomllib.load(handle)["project"]["version"]
    return version


def git_commit(repo_root: Path = REPO_ROOT) -> str:
    """The commit this bundle is built from, read fresh rather than trusted from an environment
    variable. A stale `GIT_COMMIT` left over from an earlier CI step would name the wrong build."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BundleError(f"could not read the git commit at {repo_root}: {exc}") from exc
    return result.stdout.strip()


def git_is_dirty(repo_root: Path = REPO_ROOT) -> bool:
    """Whether the working tree differs from `git_commit()`.

    A bundle built from a dirty tree is not wrong, but the manifest naming a commit that is only
    *most* of what shipped is a fact worth recording rather than hiding.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BundleError(f"could not read git status at {repo_root}: {exc}") from exc
    return bool(result.stdout.strip())


def fresh_run(artifacts_dir: Path = ARTIFACTS_DIR) -> RunArtifacts:
    """Verify the demo corpus through the real pipeline and return what it wrote.

    Calls `tda.cli.main` in-process with the same subcommand `make run` invokes. See the module
    docstring on why this one is not shelled out to. Exit code 2 (`COULD_NOT_RUN`) means the run
    never reached a verdict at all; 0 and 1 both mean it did (a clean `PASS`, or a status a human
    would have to read), and either is something to bundle.
    """
    from tda.cli import COULD_NOT_RUN
    from tda.cli import main as run_cli
    from tda.obs import latest_run

    exit_code = run_cli(["run", "--artifacts", str(artifacts_dir)])
    if exit_code == COULD_NOT_RUN:
        raise BundleError(
            "`mizan run` against the demo corpus could not complete (exit 2). A release bundle "
            "with no run to ship is not a smaller bundle, it is a build that did not happen."
        )

    directory = latest_run(artifacts_dir)
    if directory is None:
        raise BundleError(
            f"`mizan run` reported exit {exit_code} but wrote no run directory under "
            f"{artifacts_dir}."
        )

    missing = [name for name in RUN_FILES if not (directory / name).is_file()]
    if missing:
        raise BundleError(
            f"{directory} is missing {missing}. Every one of these is part of what an officer or "
            "auditor is handed, so a partial run directory is never bundled."
        )

    workbooks = sorted(directory.glob("annotated_*.xlsx"))
    if not workbooks:
        raise BundleError(
            f"{directory} has no annotated workbook. A run against the demo corpus always "
            "produces one; its absence means this run did not reach a claim to annotate."
        )
    if len(workbooks) > 1:
        raise BundleError(
            f"{directory} has {len(workbooks)} annotated workbooks "
            f"({[w.name for w in workbooks]}). A run writes exactly one, so a second file means "
            "this directory is not holding a single run's output."
        )

    files = (*(directory / name for name in RUN_FILES), workbooks[0])
    return RunArtifacts(directory=directory, files=files)


def ensure_scorecard(eval_out: Path = EVAL_OUT, repo_root: Path = REPO_ROOT) -> tuple[Path, Path]:
    """The eval scorecard and report, running `make eval` if neither exists yet.

    See the module docstring on why `make eval` is shelled out to rather than reproduced here, and
    why this raises rather than bundling without them.
    """
    scorecard = eval_out / SCORECARD_FILE
    report = eval_out / REPORT_FILE
    if not (scorecard.is_file() and report.is_file()):
        subprocess.run(["make", "eval"], cwd=repo_root, check=False)
    if not (scorecard.is_file() and report.is_file()):
        raise BundleError(
            f"{eval_out} has no {SCORECARD_FILE} or {REPORT_FILE}, even after `make eval`. A "
            "release bundle with no eval evidence is a claim nobody can check. If `make eval` "
            "just failed above, `make fixtures` or `make record` is very likely what it is "
            "waiting on. See docs/04-runbook.md."
        )
    return scorecard, report


def _layout(run: RunArtifacts, eval_files: tuple[Path, Path]) -> tuple[tuple[Path, str], ...]:
    """Source path to archive path, for every file this bundle carries except the manifest.

    `run/` and `eval/` rather than the bare file names: two files named `report.md` in one flat
    archive would be indistinguishable the moment there is a second one, and there will be. The
    eval report and, eventually, a memo could easily end up sharing a name with something else.
    """
    scorecard_path, report_path = eval_files
    entries = [(path, f"run/{path.name}") for path in run.files]
    entries.append((scorecard_path, f"eval/{scorecard_path.name}"))
    entries.append((report_path, f"eval/{report_path.name}"))
    return tuple(entries)


def build_manifest(
    *,
    version: str,
    commit: str,
    dirty: bool,
    run: RunArtifacts,
    scorecard_path: Path,
    layout: tuple[tuple[Path, str], ...],
) -> dict[str, Any]:
    """What `MANIFEST.json` says. See the module docstring on why each field is in it."""
    scorecard: dict[str, Any] = json.loads(scorecard_path.read_text(encoding="utf-8"))
    return {
        "mizan_version": version,
        "git_commit": commit,
        "git_dirty": dirty,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_id": run.directory.name,
        "eval": {
            "complete": scorecard["complete"],
            "fixtures_scored": scorecard["fixtures_scored"],
            "outcomes": scorecard["outcomes"],
        },
        "files": sorted((MANIFEST_FILE, *(arcname for _, arcname in layout))),
    }


def assemble(
    dist_dir: Path,
    version: str,
    layout: tuple[tuple[Path, str], ...],
    manifest: dict[str, Any],
) -> Path:
    """Write `dist/mizan-<version>.tar.gz`.

    `w:gz` truncates a file already at that path rather than appending to it, so a second build of
    the same version overwrites cleanly. That is the one thing the idempotency test in
    `tests/unit/test_bundle.py` exists to hold onto.
    """
    dist_dir.mkdir(parents=True, exist_ok=True)
    archive_path = dist_dir / f"mizan-{version}.tar.gz"

    payload = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n"
    manifest_info = tarfile.TarInfo(name=MANIFEST_FILE)
    manifest_info.size = len(payload)
    manifest_info.mtime = int(time.time())

    with tarfile.open(archive_path, "w:gz") as archive:
        archive.addfile(manifest_info, io.BytesIO(payload))
        for source, arcname in layout:
            archive.add(source, arcname=arcname)

    return archive_path


def build_bundle(
    *,
    dist_dir: Path = DIST_DIR,
    artifacts_dir: Path = ARTIFACTS_DIR,
    eval_out: Path = EVAL_OUT,
    repo_root: Path = REPO_ROOT,
) -> Path:
    """Everything `make bundle` does, as one call a caller can hold a `Path` from.

    Order matters only in that the version, commit and dirty flag are cheap and checked first.
    There is no reason to run the pipeline for four seconds before discovering `git` is not on the
    `PATH`.
    """
    version = package_version(repo_root)
    commit = git_commit(repo_root)
    dirty = git_is_dirty(repo_root)

    run = fresh_run(artifacts_dir)
    scorecard_path, _report_path = ensure_scorecard(eval_out, repo_root)

    layout = _layout(run, (scorecard_path, eval_out / REPORT_FILE))
    manifest = build_manifest(
        version=version,
        commit=commit,
        dirty=dirty,
        run=run,
        scorecard_path=scorecard_path,
        layout=layout,
    )
    return assemble(dist_dir, version, layout, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/release/bundle.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--out", type=Path, default=DIST_DIR, help="Where the archive is written. Default: dist/"
    )
    args = parser.parse_args(argv)

    try:
        archive = build_bundle(dist_dir=args.out)
    except BundleError as exc:
        print(f"make bundle: {exc}", file=sys.stderr)
        return COULD_NOT_BUILD

    print(f"wrote {archive}")
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
