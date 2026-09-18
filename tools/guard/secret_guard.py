#!/usr/bin/env python3
"""The secret guard: no credential may reach a tracked file.

This exists **before** any key does, which is the only useful order. A guard added after the first
recording session is a guard that was absent for the one commit that mattered.

`.env` is gitignored and is read only by `make record`, so the obvious leak is
already closed. This is for the non-obvious ones, and they are the realistic ones:

- a key pasted into a test fixture "just to check something works"
- a cassette recorded by a future version of the recorder that serialises request headers
- a `.env` copied to `.env.local.bak`, which no gitignore pattern anticipates
- a key in a docstring, a runbook example, or a commit message body

Scope is **tracked files only** (`git ls-files`). Scanning the working tree would flag the developer's
own gitignored `.env` on every run, and a guard that cries wolf on correct setup is a guard somebody
removes. What is tracked is what leaves the machine.

The patterns are Anthropic's key formats plus the generic shapes that show up in this repo's
neighbourhood. It is not a general-purpose scanner and does not pretend to be: GitHub's own secret
scanning runs on the remote and catches what this misses. This catches it one step earlier, when the
fix is still `git reset` rather than key rotation.

Run:  python tools/guard/secret_guard.py [--root .] [--quiet]
Exit: 0 clean · 1 a secret is tracked · 2 bad invocation
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Pattern:
    """One credential shape, and what to say when it is found."""

    name: str
    regex: re.Pattern[str]
    advice: str


PATTERNS: Final[tuple[Pattern, ...]] = (
    Pattern(
        name="Anthropic API key",
        # `sk-ant-` then the account/version segment and the body. Deliberately requires the real
        # prefix rather than matching any `sk-`: a loose pattern flags every mention of the word
        # "sk-ant-..." in prose, and a guard that fires on documentation gets switched off.
        regex=re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
        advice=(
            "Revoke it in the Anthropic Console immediately - a key in git history is a burned key, "
            "and rewriting history does not unpublish it. Then set it as an environment variable "
            "rather than a file: `make record` reads $ANTHROPIC_API_KEY and nothing else needs it."
        ),
    ),
    Pattern(
        name="Anthropic admin key",
        regex=re.compile(r"sk-ant-admin[A-Za-z0-9_-]{10,}"),
        advice="Revoke immediately. An admin key is broader than an API key and is never needed here.",
    ),
    Pattern(
        name="AWS access key id",
        regex=re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        advice=(
            "Nothing in this repo needs AWS - the Bedrock path was dropped for the Console API "
            "(ADR-0002). If this is real, rotate it; if it is an example, make it obviously fake."
        ),
    ),
    Pattern(
        name="private key block",
        regex=re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        advice="Remove it and rotate the keypair. Nothing here needs a private key.",
    ),
    Pattern(
        name="assigned Anthropic key in a tracked file",
        # `ANTHROPIC_API_KEY=<something that is not obviously a placeholder>`. The placeholder set is
        # narrow on purpose: `.env.example` should be committed and readable, and it will contain
        # exactly this line with a stand-in value.
        regex=re.compile(
            r"ANTHROPIC_API_KEY\s*[=:]\s*"
            r"(?!\s*$|['\"]?(?:sk-ant-xxx|xxx|your[-_]key|changeme|<|\$|\"\"|''))"
            r"['\"]?[A-Za-z0-9_\-]{12,}"
        ),
        advice=(
            "A committed assignment is a leak even if the value looks harmless. Keep the key in the "
            "environment; `.env.example` may name the variable but must not carry a value."
        ),
    ),
)

# Binary and generated files where a match would be a false positive or unreadable anyway.
SKIP_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".pdf", ".xlsx", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip"}
)

# This file necessarily contains every pattern it searches for.
SELF: Final = Path("tools/guard/secret_guard.py")


@dataclass(frozen=True, slots=True)
class Finding:
    path: Path
    line: int
    pattern: Pattern

    def render(self) -> str:
        return f"  {self.path}:{self.line}  {self.pattern.name}\n      {self.pattern.advice}"


def tracked_files(root: Path) -> list[Path]:
    """Every file git tracks. Fails loudly outside a repository rather than scanning nothing.

    A guard that silently finds no files to check is worse than no guard: it reports success.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as problem:
        raise RuntimeError(
            f"cannot list tracked files in {root}: {problem}. This guard scans what git tracks, "
            "because that is what leaves the machine - it needs a repository to do that."
        ) from problem

    return [Path(name) for name in result.stdout.split("\0") if name]


def scan(root: Path) -> list[Finding]:
    findings: list[Finding] = []

    for relative in tracked_files(root):
        if relative == SELF or relative.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = (root / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable; a credential here would not be usable as text anyway

        for number, line in enumerate(text.splitlines(), start=1):
            findings.extend(
                Finding(path=relative, line=number, pattern=pattern)
                for pattern in PATTERNS
                if pattern.regex.search(line)
            )

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    try:
        findings = scan(root)
    except RuntimeError as problem:
        print(str(problem), file=sys.stderr)
        return 2

    if findings:
        print(
            f"secret guard FAILED: {len(findings)} credential(s) in tracked files", file=sys.stderr
        )
        for finding in findings:
            print(finding.render(), file=sys.stderr)
        print(
            "\nA credential in a tracked file is compromised the moment it is pushed, and removing "
            "it from a later commit does not unpublish it. Rotate first, tidy second.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"secret guard ok: {len(tracked_files(root))} tracked file(s), no credentials")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
