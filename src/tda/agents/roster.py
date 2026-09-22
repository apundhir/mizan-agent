"""The six agents, their tool allowlists and where each one runs.

Declared as data, in one file, deliberately. An allowlist assembled at the call site is an
allowlist nobody can audit: answering "what can the narrative agent reach?" would mean reading
every place it is constructed and hoping none of them differs. Here it is one table, and the
runtime reads the table rather than accepting a set from its caller.

**This module imports no contract types, and that is load-bearing.** The output contract for each
agent lives in the package that owns the problem — `WorkbookMapping` with the Excel parser,
`FindingNarrative` with the agents that write prose — and a roster that imported them all would
make every agent's module depend on every other's. So the roster carries names, permissions and
effort defaults; the caller supplies the type. `AgentSpec` is where the two meet, and it will not
construct without both.

## Effort

`low` for mapping and resolution: classification tasks with a small answer space, where the answer
is checked against a closed vocabulary afterwards anyway. `high` for narrative, reviewer-assist and
critic: writing and judgement, where quality is visible to the officer. The values here are
defaults for tests and tooling; a *run* takes effort from `policy.yaml`, so the cost profile of a
verification is legible in the same file as its rules rather than in Python.

## Reviewer-assist

Listed here with its allowlist, and its implementation lands in the reviewer-assist agent. The entry exists now
because the roster is the audited artefact: an agent that appears in the architecture diagram and
not in the table is an agent whose permissions nobody wrote down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from tda.agents.provider.base import Effort

# Agent names. String constants rather than an enum: they are also directory names under
# `prompts/`, keys in `policy.yaml`, and directory names under `tests/cassettes/`, and a StrEnum
# that has to be `.value`-ed at three of those four call sites buys nothing.
MAPPING: Final = "mapping"
RESOLUTION: Final = "resolution"
NARRATIVE: Final = "narrative"
REVIEWER_ASSIST: Final = "reviewer_assist"
CRITIC: Final = "critic"
SUPERVISOR: Final = "supervisor"


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One agent's declared surface.

    `tools` is the allowlist, and it is the whole allowlist: `ToolRegistry.session()` is given
    exactly this set and refuses anything outside it at call time.
    """

    name: str
    job: str
    runs_in: str
    tools: frozenset[str]
    default_effort: Effort


# The supervisor is absent from this table on purpose. It is code — it has no prompt, no output
# contract and no tools — and listing it here would invite someone to give it one of the three.
# See docs/adr/0004-agent-runtime.md.
ROSTER: Final[dict[str, RosterEntry]] = {
    MAPPING: RosterEntry(
        name=MAPPING,
        job="Which sheet and header block is which metric and axis",
        runs_in="claim_parse",
        # Neither tool can emit a cell's value; see `tda.excel.tools`. The allowlist is the second
        # lock on that door, not the first.
        tools=frozenset({"list_sheets", "peek_headers"}),
        default_effort=Effort.LOW,
    ),
    RESOLUTION: RosterEntry(
        name=RESOLUTION,
        job="Resolve a label variant to a canonical code, or abstain",
        runs_in="extract, claim_parse",
        tools=frozenset({"iso_lookup", "fuzzy_candidates"}),
        default_effort=Effort.LOW,
    ),
    NARRATIVE: RosterEntry(
        name=NARRATIVE,
        job="Write the sentence that explains one finding",
        runs_in="publish",
        tools=frozenset({"read_finding", "read_evidence"}),
        default_effort=Effort.HIGH,
    ),
    REVIEWER_ASSIST: RosterEntry(
        name=REVIEWER_ASSIST,
        job="Answer an officer's question, with citations",
        runs_in="review",
        tools=frozenset({"query_verdict", "get_evidence", "get_policy_clause"}),
        default_effort=Effort.HIGH,
    ),
    CRITIC: RosterEntry(
        name=CRITIC,
        job="Grade narratives and abstentions; catch ungrounded prose",
        runs_in="eval",
        # None. The critic judges what it is shown and may not go looking for more: an agent that
        # can fetch its own evidence can talk itself into a verdict the finding does not support.
        tools=frozenset(),
        default_effort=Effort.HIGH,
    ),
}


class UnknownAgentError(KeyError):
    """A name that is not in the roster.

    A `KeyError` subclass so `ROSTER[name]` and `entry_for(name)` fail the same way, and a distinct
    type so a caller can tell a roster miss from any other missing key.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"no agent named {name!r} in the roster; have {sorted(ROSTER)}. "
            "Add it here, with its tool allowlist, before wiring it - the roster is the audited "
            "record of what each agent may reach."
        )


def entry_for(name: str) -> RosterEntry:
    entry = ROSTER.get(name)
    if entry is None:
        raise UnknownAgentError(name)
    return entry


def allowlist_for(name: str) -> frozenset[str]:
    """The tools this agent may call. The runtime reads this rather than taking a set from its
    caller, so widening an allowlist is a diff in this file."""
    return entry_for(name).tools
