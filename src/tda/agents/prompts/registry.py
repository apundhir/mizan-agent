"""Versioned prompts, loaded from disk and recorded per call.

A prompt is an input to a number that ends up on an invoice, so it is versioned like code and
stamped into the verdict like the policy version. Three rules make that real:

**Prompts live in files, not in string literals.** A prompt embedded in a `.py` file gets edited
mid-debug and the change disappears into a commit about something else. A file under
`prompts/<agent>/v1.md` shows up in a diff as a prompt change, which is what it is.

**Versions are immutable once a cassette exists.** Editing `v1.md` invalidates every cassette
recorded against it — the cassette key hashes the prompt version, not its contents, so an edited
`v1` would keep the same key and serve a stale response for a prompt that no longer exists. That
is the worst failure mode this layer has, so `frozen()` asserts against a recorded manifest and
the fix for a prompt change is always a new version, never an edit.

**A missing prompt is an error at load, not a silent empty string.** An agent that runs with an
empty system prompt still returns *something*, and that something looks like an answer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_ROOT = Path(__file__).resolve().parent
_VERSION = re.compile(r"^v(\d+)$")


class PromptError(Exception):
    """A prompt could not be loaded, or a version rule was broken."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """One versioned prompt, with the digest that lets drift be detected."""

    agent: str
    version: str
    text: str
    path: Path

    @property
    def digest(self) -> str:
        """SHA-256 of the prompt text, first 16 hex chars.

        The cassette key hashes the *version*, not the text, because the version is what a human
        reasons about. The digest is how `frozen()` catches a version whose text changed
        underneath it — the one case where the key would stay stable while the prompt did not.
        """
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


class PromptRegistry:
    """Loads prompts from `prompts/<agent>/v<n>.md`."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or PROMPTS_ROOT

    def load(self, agent: str, version: str) -> Prompt:
        if not _VERSION.match(version):
            raise PromptError(f"version must look like v1, v2, ...; got {version!r}")

        path = self.root / agent / f"{version}.md"
        if not path.is_file():
            available = self.versions(agent)
            raise PromptError(
                f"no prompt at {path.relative_to(self.root)}. "
                f"Available for {agent!r}: {available or 'none'}. "
                "A missing prompt is an error rather than an empty system prompt, because an "
                "agent with no instructions still returns something that looks like an answer."
            )

        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise PromptError(f"{path} is empty")
        return Prompt(agent=agent, version=version, text=text, path=path)

    def versions(self, agent: str) -> list[str]:
        """Every version for an agent, newest last."""
        directory = self.root / agent
        if not directory.is_dir():
            return []
        versions = [p.stem for p in directory.glob("v*.md") if _VERSION.match(p.stem)]
        return sorted(versions, key=lambda v: int(v[1:]))

    def latest(self, agent: str) -> Prompt:
        """The highest version.

        Convenient for development and deliberately **not** what a run uses: a run loads the
        version named in `policy.yaml`, so adding `v2.md` cannot silently change what a
        verification does. Convenience that quietly changes behaviour is not convenience.
        """
        versions = self.versions(agent)
        if not versions:
            raise PromptError(f"no prompts for agent {agent!r} under {self.root}")
        return self.load(agent, versions[-1])

    def agents(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            d.name for d in self.root.iterdir() if d.is_dir() and not d.name.startswith("_")
        )

    def manifest(self) -> dict[str, str]:
        """`{"<agent>/<version>": "<digest>"}` for every prompt on disk.

        Committed as `prompts/MANIFEST.txt` and compared by a test. That test is what turns
        "prompt versions are immutable" from a convention into something the build enforces.
        """
        return {
            f"{agent}/{version}": self.load(agent, version).digest
            for agent in self.agents()
            for version in self.versions(agent)
        }


def render_manifest(manifest: dict[str, str]) -> str:
    """Stable text form, so the committed manifest diffs cleanly."""
    lines = [
        "# Prompt manifest - digests of every committed prompt version.",
        "#",
        "# A version's text is IMMUTABLE once a cassette has been recorded against it: the",
        "# cassette key hashes the version, not the text, so an edited v1 keeps its key and",
        "# serves a stale response for a prompt that no longer exists.",
        "#",
        "# To change a prompt, add v<n+1>.md and point policy.yaml at it. Regenerate with:",
        "#   python -m tda.agents.prompts.registry --write-manifest",
        "",
    ]
    lines.extend(f"{key}  {digest}" for key, digest in sorted(manifest.items()))
    return "\n".join(lines) + "\n"


def parse_manifest(text: str) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, digest = stripped.partition("  ")
        if not digest:
            raise PromptError(f"malformed manifest line: {line!r}")
        manifest[key] = digest.strip()
    return manifest


MANIFEST_PATH = PROMPTS_ROOT / "MANIFEST.txt"


def main() -> int:  # pragma: no cover - a small developer utility
    import argparse

    parser = argparse.ArgumentParser(description="Prompt registry utility")
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args()

    registry = PromptRegistry()
    manifest = registry.manifest()
    if args.write_manifest:
        MANIFEST_PATH.write_text(render_manifest(manifest), encoding="utf-8")
        print(f"wrote {MANIFEST_PATH} ({len(manifest)} prompt version(s))")
        return 0

    print(render_manifest(manifest), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
