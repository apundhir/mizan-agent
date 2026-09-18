"""Read `.env` for the one target that needs a credential, and for no other.

`.env.example` has said *"Copy to `.env` and fill in"* since M1. Nothing read the file. A developer
following that instruction exactly would set `ANTHROPIC_API_KEY` in `.env`, run `make record`, and
be told the key is not set — which is true, and which reads as a bug in the recorder rather than as
a missing loader. This closes that gap.

## Three rules, and each one is a decision

**The real environment always wins.** A value already in `os.environ` is never overwritten. A file
on disk quietly shadowing an exported variable is how somebody records against the wrong account
and cannot work out why. `.env` is the fallback, not the authority.

**Only `make record` loads it.** Not the package, not a test, not a run. Every other target is
offline in replay and needs no credential, so a loader that fired on import would be reaching for a
secret in processes that have no business holding one — and would make `make ci`'s behaviour depend
on an untracked file, which is the opposite of what this repo claims about reproducibility.

**Names are returned; values never are.** `load_dotenv` reports which variables it set so the
recorder can say where the key came from. It cannot report what they contain, because the one thing
this function must never do is put a credential somewhere it can be logged.

## Why not python-dotenv

Forty lines of parsing against a runtime dependency in the shipped package, for a developer
convenience used by exactly one target. `tools/guard/secret_guard.py` already treats `.env` as the
expected home for a key; this just makes the repository read the file it has been telling people to
write.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

# `export FOO=bar` is what a shell-minded reader writes, and refusing it would mean a file that
# looks correct and silently sets nothing.
_EXPORT = "export "


def parse_env(text: str) -> dict[str, str]:
    """Parse `.env` text into a mapping. Tolerant of the forms people actually write.

    Blank lines and `#` comments are skipped. A line with no `=` is skipped rather than raising:
    `.env` is hand-edited under time pressure, and failing the whole load over one stray line would
    send somebody hunting in the wrong place for a missing key.

    Surrounding quotes are stripped, because `KEY="sk-ant-..."` is how half of all `.env` files are
    written and a key with literal quote marks in it fails at the API with an opaque 401.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(_EXPORT):
            stripped = stripped[len(_EXPORT) :].lstrip()
        name, separator, value = stripped.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_dotenv(path: Path | None = None, *, environ: dict[str, str] | None = None) -> list[str]:
    """Set any variable in `.env` that is not already in the environment. Returns the names set.

    **Names, not values.** The caller wants to tell a human where the credential came from, and a
    function that handed back the secret to do it would be one refactor away from printing it.

    A missing file returns `[]` rather than raising. Not having a `.env` is the normal case —
    exporting the variable in a shell is equally valid, and CI has neither.
    """
    env = os.environ if environ is None else environ
    target = path or DEFAULT_ENV_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return []

    loaded: list[str] = []
    for name, value in parse_env(text).items():
        # The real environment wins. See the module docstring: a file shadowing an exported
        # variable is how somebody records against the wrong account without noticing.
        if name in env or not value:
            continue
        env[name] = value
        loaded.append(name)
    return sorted(loaded)
