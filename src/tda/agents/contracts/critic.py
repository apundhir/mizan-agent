"""The critic's contract: four booleans and a reason, scored by code.

A critic that returns "this narrative is good, 8/10" has moved the problem rather than solved it,
because now somebody has to decide what 8 means and whether it moved. So the critic answers four
specific yes/no questions about one narrative, and **the pass/fail is computed from the answers by
`verdict` below rather than asserted by the model.** That is what makes a judge loop scorable: the
aggregation is deterministic, the model only supplies the observations, and a prompt change can be
measured against the same rule rather than against a different judge's mood.

The four questions are the four ways a narrative goes wrong, and each is checkable by a human
reading the same two documents the critic was shown:

| Field | The failure it catches |
|---|---|
| `grounded` | prose that describes something the finding does not say |
| `leaks_a_number` | a figure in the sentence — which the contract cannot carry, so this catches one spelled out in words |
| `names_an_unassigned_cause` | a cause the classifier did not assign |
| `reads_as_an_accusation` | clerical blame for a definitional difference |

The last is the one that costs the most and is the easiest to miss. A definitional variance means
the hotel and the regulator counted different things, both correctly. Prose that calls it an error is a
correct finding that discredits the system, and one such accusation costs more trust than a hundred
right answers earn.

## Scoring is not here

This contract is what the critic returns. Turning a set of these into a grade for a prompt version
is narrative grading's job, and it lives in the eval harness where the thresholds are visible next to the
results. A `passed` property computed from the four observations is the most this file should own,
and it is deliberately the strictest possible rule — any one failure fails the narrative — so that
a later relaxation has to be written down somewhere a reviewer will see it.

## No numbers, and the usual reason

Booleans are classifications, and classification is what agents are for. A `score: int` here would
be a model-produced number that a threshold then acts on, which is exactly the route
`tools/guard/agent_schema_lint.py` exists to close.
"""

from __future__ import annotations

from pydantic import Field

from tda.agents.contracts.base import AgentOutput


class CriticVerdict(AgentOutput):
    """One narrative, graded against the finding it claims to explain."""

    finding_id: str = Field(
        pattern=r"^F-[0-9]{4}$",
        description="The finding whose narrative was graded. Echoed back so a verdict filed "
        "against the wrong narrative is detectable.",
    )
    grounded: bool = Field(
        description="Every claim in the sentence is supported by the finding it was shown. "
        "False for prose that is plausible but unsupported, which is the common case."
    )
    leaks_a_number: bool = Field(
        description="The sentence states a figure. The contract cannot carry one as a field, so "
        "this catches a number spelled out in words - 'about forty room-nights'."
    )
    names_an_unassigned_cause: bool = Field(
        description="The sentence names a cause the classifier did not assign to this finding."
    )
    reads_as_an_accusation: bool = Field(
        description="The sentence reads as clerical blame where the cause is definitional. The "
        "most expensive failure of the four: a correct finding that discredits the system."
    )
    reason: str = Field(
        min_length=1,
        description="What the critic saw, in one or two sentences. Read by whoever is deciding "
        "whether to change the narrative prompt, so 'fails' is not an answer.",
    )

    @property
    def passed(self) -> bool:
        """Deterministic, and deliberately unforgiving: any one failure fails the narrative.

        Computed here rather than returned by the model, so that two runs of the critic on the same
        observations grade identically. A later decision to tolerate one of these has to change
        this line, which is a diff a reviewer sees.
        """
        return (
            self.grounded
            and not self.leaks_a_number
            and not self.names_an_unassigned_cause
            and not self.reads_as_an_accusation
        )

    def failures(self) -> tuple[str, ...]:
        """The named failures, for a report that has to say *why* rather than just how many."""
        found = []
        if not self.grounded:
            found.append("ungrounded")
        if self.leaks_a_number:
            found.append("leaks a number")
        if self.names_an_unassigned_cause:
            found.append("names an unassigned cause")
        if self.reads_as_an_accusation:
            found.append("reads as an accusation")
        return tuple(found)
