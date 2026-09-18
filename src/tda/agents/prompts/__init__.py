"""Versioned prompt files.

Layout: `prompts/<agent>/v<n>.md`. A version's text is immutable once a cassette exists against
it - to change a prompt, add the next version and point `policy.yaml` at it.
"""

from tda.agents.prompts.registry import (
    MANIFEST_PATH,
    PROMPTS_ROOT,
    Prompt,
    PromptError,
    PromptRegistry,
    parse_manifest,
    render_manifest,
)

__all__ = [
    "MANIFEST_PATH",
    "PROMPTS_ROOT",
    "Prompt",
    "PromptError",
    "PromptRegistry",
    "parse_manifest",
    "render_manifest",
]
