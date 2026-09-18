"""The marker base every agent output contract derives from.

`AgentOutput` exists so the schema lint has something unambiguous to look for. Scanning a
directory for bare `BaseModel` subclasses catches contracts that happen to live in the right
folder and misses one defined anywhere else; scanning for this base catches it wherever it is.

**The rule it marks: an agent output may not carry a number.** Agents pass keys and references —
a cell range, a country code, a finding id — and the metric library computes values from typed
records. The whitelisted exceptions are `page`, `row`, `row_start`, `row_end`, which are
citations rather than quantities: nothing downstream does arithmetic on a page number.

Booleans are fine. `refused: bool`, `is_total_row: bool` — those are classifications, and
classification is what agents are for.

Telemetry is deliberately **not** an `AgentOutput`. Token counts are integers and belong in
`tda.obs`, because they describe what a call cost rather than what the agent answered. Putting
them here would force a choice between weakening the lint and mislabelling the data.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AgentOutput(BaseModel):
    """Base for every agent's structured output.

    Frozen and `extra="forbid"`: a model that returns an unexpected field is a contract
    violation, not a bonus. Silently accepting it is how an undeclared numeric field would get
    past the lint — the lint reads declarations, so an undeclared field is invisible to it, and
    `extra="forbid"` is what makes that gap unreachable at runtime.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def is_answer(self) -> bool:
        """Whether this output is an answer rather than a considered refusal.

        True for every contract that has only one outcome. Overridden by the ones that can decline
        — see `Abstention` below for why this is a property and emphatically not `__bool__`.
        """
        return True


class Abstention(AgentOutput):
    """A first-class "I cannot answer this".

    Every agent that could be asked something unanswerable returns a union including this, so
    abstention is a *typed outcome* rather than an exception, a sentinel string, or — worst —
    a confident guess.

    This is the type behind demo scene three. A system that guesses at an unmappable country
    label will eventually accuse a hotel of an error that does not exist, and one such
    accusation costs more trust than a hundred correct findings earn. `reason` is what the
    officer reads when the system declines, so it has to be a sentence, not a code.
    """

    abstained: bool = True
    reason: str

    @property
    def is_answer(self) -> bool:
        """False: an abstention is a considered refusal, not an answer.

        A **property, not `__bool__`**. Making the model itself falsy is the obvious, idiomatic
        thing and it cost this repository a full recording run to discover: the Anthropic SDK
        populates `parsed_output` and then reads it back with `if content.parsed_output:`. A
        contract that is falsy when the agent declines is dropped by that check, and the caller is
        told the model returned nothing parseable — from a response whose JSON was exactly right.

        The rule this leaves behind: **a pydantic model that a library may test for truthiness
        must never be falsy.** `AgentResult.__bool__` is where `if result:` belongs, because an
        `AgentResult` never crosses a library boundary.
        """
        return False
