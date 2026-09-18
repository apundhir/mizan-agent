"""Pipeline-level eval tests: the scorer proven on every push, the fixtures scored when they exist.

Separate from `tests/eval/agents/` because the two measure different things. That suite scores an
agent's answer; this one scores a `Verdict`, and the property it defends is the one an eval harness
most often fails to have: that its scorer can tell a right answer from a wrong one at all.
"""

from __future__ import annotations
