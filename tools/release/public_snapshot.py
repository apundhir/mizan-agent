"""Publish a tagged tree to the public showcase repository, minus what stays private.

`apundhir/mizan-agent` is the public face of this project. It is **not** a mirror: the private
repository's history carries branch names, PR titles and backlog ids that are ours rather than the
reader's, so this exports a *tree* and commits it as one squashed commit on the public `main`. A
reader of the public repo gets the code and the reasoning, and none of the scaffolding.

## What stays private, and why each one

`docs/00-BUILD-PLAN.md` is a milestone roadmap with a table of deviations from the original PRD.
That is the one category named outright in this project's own publishing rules, and a roadmap tells
a reader what has not been built yet rather than what has.

`docs/07-git-workflow.md` and `.github/pull_request_template.md` are internal process: branch
prefixes, ruleset behaviour, the Definition of Done checklist. Useful to whoever works here,
noise to whoever is evaluating whether the thing works.

Everything else ships, deliberately, including `docs/02-assumption-register.md` and
`docs/05-onboarding-asks.md`. Both read as rigour rather than as planning: one records which
choices were made under uncertainty and what each would cost to revisit, the other states what a
real deployment would still have to decide. For an audience judging whether this project is honest
about its own limits, they are the strongest material in the repository, and `docs/06-walkthrough.md`
links to the second of them as the answer to the gap it admits.

Issue references (`PRD-115` and friends) are **not** stripped. They are traceability, not
documents, and the sentences carrying them explain why the code is shaped the way it is.

## What this refuses to do

Push a dirty tree, push from anywhere but a tag, or publish a file the exclusion list names. Each
is checked before the remote is touched, because a public push is the one operation here with no
undo: a private mistake is a force-push away from gone, and a public one is in somebody's clone.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
PUBLIC_REMOTE: Final = "https://github.com/apundhir/mizan-agent.git"

# Paths that never leave this repository. Checked twice: removed from the export, then asserted
# absent before the push, so a future edit to the copy step cannot silently reintroduce one.
EXCLUDED: Final = (
    "docs/00-BUILD-PLAN.md",
    "docs/07-git-workflow.md",
    ".github/pull_request_template.md",
)

# Rows in README.md's own documentation table that point at the files above. Left in place they
# would ship a table of broken links, which reads as carelessness in the first file anyone opens.
_EXCLUDED_LINK = re.compile(
    r"^\|\s*\[[^\]]+\]\(docs/(?:00-BUILD-PLAN|07-git-workflow)\.md\).*\|\s*$", re.MULTILINE
)

OK: Final = 0
REFUSED: Final = 2


class SnapshotError(RuntimeError):
    """A precondition failed. Nothing has been pushed."""


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _checked_tag(tag: str) -> str:
    """The tag must exist, and the working tree must be clean.

    A snapshot taken from a dirty tree is a snapshot nobody can reproduce, and the whole point of
    exporting from a tag is that `git archive` and the reader are looking at the same bytes.
    """
    if _git("status", "--porcelain", cwd=REPO_ROOT):
        raise SnapshotError(
            "the working tree has uncommitted changes. A snapshot must come from a tagged, "
            "committed tree so that what is published can be reproduced from the tag."
        )
    try:
        return _git("rev-list", "-n", "1", tag, cwd=REPO_ROOT)
    except SnapshotError as exc:
        raise SnapshotError(
            f"no such tag {tag!r}. Tag the release before snapshotting it."
        ) from exc


def export_tree(tag: str, destination: Path) -> None:
    """The tagged tree, on disk, with the excluded paths removed and README's links to them cut."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", tag],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive.stdout, check=True)

    for relative in EXCLUDED:
        target = destination / relative
        if target.is_file():
            target.unlink()

    readme = destination / "README.md"
    if readme.is_file():
        trimmed = _EXCLUDED_LINK.sub("", readme.read_text(encoding="utf-8"))
        readme.write_text(re.sub(r"\n{3,}", "\n\n", trimmed), encoding="utf-8")


def audit(tree: Path) -> list[str]:
    """Every reason this tree must not be published, or an empty list.

    Runs against the exported directory rather than against the plan, so it catches a file the
    copy step brought in by a route the exclusion list did not anticipate.
    """
    problems = [f"excluded path present: {rel}" for rel in EXCLUDED if (tree / rel).is_file()]
    for secret in (".env", ".streamlit/secrets.toml"):
        if (tree / secret).exists():
            problems.append(f"credential file present: {secret}")
    for markdown in tree.rglob("*.md"):
        text = markdown.read_text(encoding="utf-8", errors="replace")
        for relative in EXCLUDED:
            if f"]({relative})" in text or f"]({Path(relative).name})" in text:
                problems.append(f"{markdown.relative_to(tree)} links to excluded {relative}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/release/public_snapshot.py")
    parser.add_argument("tag", help="The tag to publish, e.g. v0.6.0.")
    parser.add_argument(
        "--remote", default=PUBLIC_REMOTE, help=f"Public repository. Default: {PUBLIC_REMOTE}"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Export and audit, print what would be published, and push nothing.",
    )
    args = parser.parse_args(argv)

    try:
        commit = _checked_tag(args.tag)
    except SnapshotError as exc:
        print(f"public snapshot refused: {exc}", file=sys.stderr)
        return REFUSED

    with tempfile.TemporaryDirectory(prefix="mizan-snapshot-") as scratch:
        tree = Path(scratch) / "tree"
        export_tree(args.tag, tree)

        problems = audit(tree)
        if problems:
            print("public snapshot refused, nothing was pushed:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return REFUSED

        files = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file())
        print(f"{args.tag} ({commit[:12]}) exports {len(files)} file(s)")
        for relative in EXCLUDED:
            print(f"  withheld: {relative}")
        if args.dry_run:
            print("dry run: nothing pushed")
            return OK

        _git("init", "-q", "-b", "main", cwd=tree)
        _git("add", "-A", cwd=tree)
        _git(
            "-c",
            "user.name=Ajay Pundhir",
            "-c",
            "user.email=ajaypratap.iiitb@gmail.com",
            "commit",
            "-q",
            "-m",
            f"snapshot {args.tag}",
            cwd=tree,
        )
        _git("remote", "add", "origin", args.remote, cwd=tree)
        _git("push", "--force", "origin", "main", cwd=tree)
        print(f"pushed {args.tag} to {args.remote}")
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
