"""Prompt versions are immutable once recorded against, and the build has to enforce that.

The cassette key hashes the prompt *version*, not its text. That is the right choice — a version
is what a human reasons about — and it creates exactly one dangerous failure: editing `v1.md`
leaves every cassette recorded against `v1` with an unchanged key, serving a stale response for a
prompt that no longer exists. The number in the verdict would then come from a prompt nobody can
read any more.

`MANIFEST.txt` plus `test_committed_prompts_match_the_manifest` is what catches that. It is the
reason the manifest exists at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tda.agents.prompts import (
    MANIFEST_PATH,
    PromptError,
    PromptRegistry,
    parse_manifest,
    render_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def registry(tmp_path: Path) -> PromptRegistry:
    """A registry over a temporary tree, so these tests do not depend on prompts that land in
    the agent runtime and do not break when they do."""
    mapping = tmp_path / "mapping"
    mapping.mkdir()
    (mapping / "v1.md").write_text(
        "Map each sheet to a metric. Return mappings only.\n", encoding="utf-8"
    )
    (mapping / "v2.md").write_text(
        "Map each sheet and header block to a metric and axis.\n", encoding="utf-8"
    )

    narrative = tmp_path / "narrative"
    narrative.mkdir()
    (narrative / "v1.md").write_text("Explain one finding in one sentence.\n", encoding="utf-8")

    return PromptRegistry(root=tmp_path)


def test_loads_the_named_version(registry: PromptRegistry) -> None:
    prompt = registry.load("mapping", "v1")
    assert "Return mappings only" in prompt.text
    assert prompt.version == "v1"


def test_a_missing_prompt_is_an_error_not_an_empty_string(registry: PromptRegistry) -> None:
    """An agent that runs with an empty system prompt still returns something, and that something
    looks like an answer. So the failure has to be at load."""
    with pytest.raises(PromptError, match="no prompt at"):
        registry.load("mapping", "v9")


def test_a_missing_prompt_names_what_is_available(registry: PromptRegistry) -> None:
    """The error a developer actually hits is a typo'd version, so the message should answer the
    next question rather than requiring an `ls`."""
    with pytest.raises(PromptError, match=r"\['v1', 'v2'\]"):
        registry.load("mapping", "v3")


def test_an_empty_prompt_file_is_rejected(tmp_path: Path) -> None:
    agent = tmp_path / "mapping"
    agent.mkdir()
    (agent / "v1.md").write_text("   \n\n", encoding="utf-8")

    with pytest.raises(PromptError, match="is empty"):
        PromptRegistry(root=tmp_path).load("mapping", "v1")


@pytest.mark.parametrize("version", ["1", "V1", "v", "v1.1", "latest", ""])
def test_malformed_versions_are_rejected(registry: PromptRegistry, version: str) -> None:
    with pytest.raises(PromptError, match="version must look like"):
        registry.load("mapping", version)


def test_versions_sort_numerically_not_lexically(tmp_path: Path) -> None:
    """`v10` must come after `v9`. Lexical sorting puts it after `v1`, which would make `latest()`
    quietly return the wrong prompt the tenth time one is revised."""
    agent = tmp_path / "mapping"
    agent.mkdir()
    for n in (1, 2, 9, 10, 11):
        (agent / f"v{n}.md").write_text(f"version {n}\n", encoding="utf-8")

    assert PromptRegistry(root=tmp_path).versions("mapping") == ["v1", "v2", "v9", "v10", "v11"]
    assert PromptRegistry(root=tmp_path).latest("mapping").version == "v11"


def test_latest_is_a_developer_convenience_and_a_run_uses_policy(registry: PromptRegistry) -> None:
    """`latest()` exists, and a run must not use it: adding v3 would silently change what a
    verification does. Convenience that quietly changes behaviour is not convenience.

    This test documents the intent; the enforcement is that the agent runtime reads the version
    from `policy.yaml`.
    """
    assert registry.latest("mapping").version == "v2"
    assert registry.load("mapping", "v1").version == "v1"


def test_no_prompts_for_an_agent_raises(registry: PromptRegistry) -> None:
    with pytest.raises(PromptError, match="no prompts for agent"):
        registry.latest("critic")


# ── the manifest: how prompt immutability is enforced ────────────────────────


def test_digest_changes_when_the_text_changes(tmp_path: Path) -> None:
    agent = tmp_path / "mapping"
    agent.mkdir()
    path = agent / "v1.md"

    path.write_text("Map each sheet to a metric.\n", encoding="utf-8")
    before = PromptRegistry(root=tmp_path).load("mapping", "v1").digest

    path.write_text("Map each sheet to a metric, and also guess the values.\n", encoding="utf-8")
    after = PromptRegistry(root=tmp_path).load("mapping", "v1").digest

    assert before != after, (
        "the digest must move when the text does - it is the only signal that an edited v1 is "
        "serving cassettes recorded against a prompt that no longer exists"
    )


def test_manifest_round_trips(registry: PromptRegistry) -> None:
    manifest = registry.manifest()
    assert set(manifest) == {"mapping/v1", "mapping/v2", "narrative/v1"}
    assert parse_manifest(render_manifest(manifest)) == manifest


def test_rendered_manifest_is_sorted_and_explains_itself(registry: PromptRegistry) -> None:
    """A manifest whose order depends on filesystem iteration produces spurious diffs, and a
    manifest with no explanation gets deleted by the first person who does not know why it exists."""
    rendered = render_manifest(registry.manifest())
    entries = [line for line in rendered.splitlines() if line and not line.startswith("#")]

    assert entries == sorted(entries)
    assert "IMMUTABLE" in rendered
    assert "--write-manifest" in rendered


def test_a_malformed_manifest_line_is_rejected() -> None:
    with pytest.raises(PromptError, match="malformed manifest line"):
        parse_manifest("mapping/v1 deadbeef\n")  # single space, not the two-space separator


def test_committed_prompts_match_the_manifest() -> None:
    """The guard that makes prompt versions actually immutable.

    Editing a committed prompt without adding a new version fails here. That is the whole point:
    the cassette key hashes the version, so an edited `v1` keeps its key and serves a response
    recorded against text nobody can read any more.

    Vacuously true today — there are no prompts until the agent runtime — and the `exists()` branch is what
    stops that vacuity from becoming permanent silence.
    """
    on_disk = PromptRegistry().manifest()

    if not MANIFEST_PATH.exists():
        assert not on_disk, (
            f"{len(on_disk)} prompt(s) exist but MANIFEST.txt does not. Run:\n"
            "  python -m tda.agents.prompts.registry --write-manifest"
        )
        return

    committed = parse_manifest(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert on_disk == committed, (
        "committed prompts do not match MANIFEST.txt. If you edited a prompt, add the next "
        "version instead: an edited version keeps its cassette key and serves a stale response.\n"
        f"  on disk:   {on_disk}\n  manifest:  {committed}"
    )
