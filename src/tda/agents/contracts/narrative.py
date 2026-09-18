"""The narrative agent's contract: the sentence an officer reads, and nothing that could be wrong.

`make eval` covers the numbers. **It does not cover the prose, and the prose is what the officer
reads.** A finding whose figures are correct and whose sentence says "the hotel over-reported
occupancy" when the classifier found a definitional difference has accused somebody of something,
and no amount of correct arithmetic underneath undoes that.

So this contract is shaped around the two failures that matter, and it forecloses one of them
structurally:

**A number in the prose.** There is no field here that can carry one, and the schema lint would
reject one if there were. The narrative refers to a finding by id; the figures are rendered beside
it from `Finding.claimed`, `Finding.computed` and `Finding.difference`, which were computed by
`tda.metrics` from typed records. A model never restates a number, so a model can never restate one
wrongly. What remains possible is a *word* implying a magnitude — "substantially", "a handful of" —
and that is the critic's job (PRD-95), not a type's.

**A cause the classifier did not assign.** `cites_permutation` names the permutation the narrative
leans on, and the caller checks it against `Finding.explaining_permutation` before the sentence is
published. A narrative that names a cause nobody assigned is discarded rather than shown, which is
the only version of that check worth having: a warning would be read once and then ignored.

## Why the sentence is one field and not three

An earlier shape had `summary`, `cause` and `recommendation`. It produced prose that read as a
form, and it invited the model to fill a `recommendation` for a finding that needs none. One field,
with the prompt saying what belongs in it, produces a sentence a person wrote rather than a
template a person completed.
"""

from __future__ import annotations

from pydantic import Field

from tda.agents.contracts.base import AgentOutput


class FindingNarrative(AgentOutput):
    """One finding, explained in prose, with every number left where it was computed.

    `finding_id` is the handoff: keys and references, never values. The agent is shown the finding
    and returns the id it was shown, which is checkable — an id the agent was not given is a
    fabrication, and `tda.agents.narrative` discards the answer rather than publishing it under
    whatever finding happens to have that id.
    """

    finding_id: str = Field(
        pattern=r"^F-[0-9]{4}$",
        description="The finding this explains. Echoed back so a misrouted narrative is "
        "detectable rather than merely unlikely.",
    )
    sentence: str = Field(
        min_length=1,
        max_length=600,
        description="What happened and why, in prose an officer can act on. No figures: they are "
        "rendered beside this from the finding, where code put them.",
    )
    cites_permutation: str | None = Field(
        default=None,
        description="The permutation id the explanation rests on, or null where the finding has "
        "none. Checked against the finding's own `explaining_permutation` before publication - a "
        "narrative naming a cause the classifier did not assign is discarded, not shown.",
    )
    is_definitional: bool = Field(
        description="Whether the agent read this as a definitional difference rather than a "
        "clerical error. A classification, which is what agents are for - and a disagreement with "
        "the classifier is caught by comparing it, which is the point of asking.",
    )
