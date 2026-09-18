"""`make bundle`: the archive is real, complete, and safe to build twice.

`bundle.py` lives under `tools/release/`, not a package, so it is imported the way
`tests/arch/test_guards.py` imports the guard scripts: `sys.path` gets the directory, then a bare
`import bundle`.

Three properties matter more than the rest, because each is a way this target could look green
while shipping something wrong:

- **Nothing required is silently missing.** A partial archive with no error is worse than no
  archive; see `test_archive_contains_every_required_file`.
- **A missing scorecard is a build failure, not an omission.** `test_ensure_scorecard_fails...`
  and its companion mutation (run manually, see the module docstring's discipline) are what
  distinguish "not measured" from "measured and left out".
- **The second build does not fall over on the first build's leftovers.** Every script this
  project ships gets re-run against non-empty prior state before it is trusted; see
  `test_running_bundle_twice_does_not_crash_on_a_pre_existing_dist`.

The tests that build a real archive are marked `eval`: each one runs `mizan run` against the demo
corpus, which is a few seconds of real pipeline, not a mock agreeing with itself.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "release"))

import bundle

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_RUN_FILES = frozenset(
    {
        "run/verdict.json",
        "run/memo.docx",
        "run/run.json",
        "run/trace.jsonl",
        "run/nodes.jsonl",
    }
)
REQUIRED_EVAL_FILES = frozenset({"eval/scorecard.json", "eval/report.md"})


@pytest.fixture(scope="module")
def built_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One real bundle, built once and inspected by several tests below.

    A module-scoped fixture rather than one build per test: each build runs the real pipeline
    (`mizan run` against the demo corpus), and there is nothing about the archive-contents check
    and the manifest-commit check that needs two separate runs to disagree about.
    """
    workdir = tmp_path_factory.mktemp("bundle")
    return bundle.build_bundle(dist_dir=workdir / "dist", artifacts_dir=workdir / "artifacts")


@pytest.mark.eval
def test_archive_contains_every_required_file(built_archive: Path) -> None:
    """Every file an officer or auditor would need is in the archive, and nothing required is
    missing: the verdict, the memo, the annotated workbook, the run ledger and trace, and the
    eval report and scorecard."""
    with tarfile.open(built_archive) as archive:
        names = set(archive.getnames())

    assert "MANIFEST.json" in names
    assert names >= REQUIRED_RUN_FILES
    assert names >= REQUIRED_EVAL_FILES
    workbooks = {
        name for name in names if name.startswith("run/annotated_") and name.endswith(".xlsx")
    }
    assert len(workbooks) == 1, f"expected exactly one annotated workbook, found {workbooks}"


@pytest.mark.eval
def test_manifest_names_the_git_commit_accurately(built_archive: Path) -> None:
    """The manifest's `git_commit` is the commit the checkout was actually on, checked against an
    independent `git rev-parse` rather than against `bundle.git_commit()` itself."""
    actual = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()

    manifest = _read_manifest(built_archive)
    assert manifest["git_commit"] == actual


@pytest.mark.eval
def test_manifest_file_list_matches_what_is_actually_in_the_archive(built_archive: Path) -> None:
    """`MANIFEST.json`'s own `files` list is not aspirational: it names exactly the archive's
    members, so a reader can trust the manifest without also running `tar -tzf`."""
    with tarfile.open(built_archive) as archive:
        names = set(archive.getnames())

    manifest = _read_manifest(built_archive)
    assert set(manifest["files"]) == names


@pytest.mark.eval
def test_running_bundle_twice_does_not_crash_on_a_pre_existing_dist(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The second build, against the first build's own output. `dist/` already holds an archive
    at the same path when the second call starts, and `w:gz` must overwrite it rather than choke
    on it or append to it."""
    workdir = tmp_path_factory.mktemp("bundle-twice")
    dist_dir = workdir / "dist"
    artifacts_dir = workdir / "artifacts"

    first = bundle.build_bundle(dist_dir=dist_dir, artifacts_dir=artifacts_dir)
    assert tarfile.is_tarfile(first)

    second = bundle.build_bundle(dist_dir=dist_dir, artifacts_dir=artifacts_dir)
    assert second == first
    assert tarfile.is_tarfile(second)

    manifest = _read_manifest(second)
    assert manifest["run_id"] != ""  # the second build minted its own run, not a reuse of the first


def test_ensure_scorecard_fails_loudly_when_it_cannot_produce_one(tmp_path: Path) -> None:
    """No scorecard, no report, and a `repo_root` with no Makefile to build one: `make eval` will
    fail instantly and the check afterward must raise rather than let the bundle proceed without
    eval evidence."""
    with pytest.raises(bundle.BundleError, match="scorecard"):
        bundle.ensure_scorecard(eval_out=tmp_path / "eval", repo_root=tmp_path)


def test_ensure_scorecard_does_not_shell_out_when_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`make eval` takes real seconds and, unlike `make run`, needs the fixture tree. It must run
    only when the scorecard or the report is actually missing."""
    # Ensure the real eval outputs exist first; harmless and idempotent if they already do.
    bundle.ensure_scorecard()

    def _must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("`make eval` ran even though the scorecard already existed")

    # Patched on the real `subprocess` module, not `bundle.subprocess`: `bundle.py` never
    # re-exports it, so patching the attribute it actually looks up at call time is both what
    # mypy's `--strict` (no implicit re-export) will accept and the more faithful patch anyway.
    monkeypatch.setattr(subprocess, "run", _must_not_run)

    scorecard, report = bundle.ensure_scorecard()
    assert scorecard.is_file()
    assert report.is_file()


def test_fresh_run_fails_loudly_when_mizan_run_could_not_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `mizan run` that exits 2 never reached a verdict. Bundling whatever was left in
    `artifacts/` from an earlier, unrelated run would ship the wrong evidence under the right
    label, so this must raise instead of quietly falling back to `latest_run`."""

    def _could_not_run(_argv: Sequence[str]) -> int:
        return bundle.COULD_NOT_BUILD  # 2, the same value tda.cli.COULD_NOT_RUN uses

    import tda.cli

    monkeypatch.setattr(tda.cli, "main", _could_not_run)

    with pytest.raises(bundle.BundleError, match="could not complete"):
        bundle.fresh_run(artifacts_dir=tmp_path)


def test_main_reports_a_bundle_error_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI wrapper turns a `BundleError` into a printed message and a non-zero exit rather
    than a traceback - the same contract every other `make` target in this repo keeps."""

    def _boom(**_kwargs: object) -> Path:
        raise bundle.BundleError("the eval scorecard is missing")

    monkeypatch.setattr(bundle, "build_bundle", _boom)

    exit_code = bundle.main([])
    assert exit_code == bundle.COULD_NOT_BUILD

    captured = capsys.readouterr()
    assert "the eval scorecard is missing" in captured.err


def _read_manifest(archive_path: Path) -> dict[str, Any]:
    with tarfile.open(archive_path) as archive:
        member = archive.extractfile("MANIFEST.json")
        assert member is not None
        payload: dict[str, Any] = json.loads(member.read().decode("utf-8"))
    return payload
