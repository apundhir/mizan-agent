"""The resolution agent's contract: a country code, or a typed refusal. Never a guess.

`tda.extract.normalise` states the rule this contract serves, and it is worth restating because the
whole design of this file follows from it: **an unmapped label is never guessed** — not by edit
distance, not by substring, not by a model's best effort. `Austria` and `Australia` differ by three
characters and by eleven thousand kilometres, and a guest counted under the wrong country is a wrong
number presented as a right one that nothing downstream can detect.

So what is this agent for, if code refuses to guess?

**It proposes, and a human disposes.** Code finds the label it cannot map and offers candidates.
The agent picks one *from that list* or says it cannot. Either way the label still produces a
blocking V7 finding (D-NAT-13) — the agent's answer is a suggestion attached to that finding, which
turns a human's job from "work out what `Cote d'Ivoir` is meant to be" into "confirm or reject
`CI`". That is a real saving and it is not the same thing as automation.

## Why abstention is a typed outcome rather than an exception or a sentinel

An exception says the machinery broke. A sentinel string says the caller has to remember which one.
A typed `Abstained` in a discriminated union says *the agent considered this and declined*, and
makes the caller handle it — `extra="forbid"` and the discriminator between them mean there is no
third shape to fall into. This is the type behind demo scene three.

## Why the union is nested inside one contract rather than being the contract

The provider takes one output schema per call: `complete(request, output_type)`. A bare
`Resolved | Abstained` is not a class and cannot be that argument. Wrapping the two in
`LabelResolution.answer` with a Pydantic discriminator gives a single JSON schema that still makes
the two outcomes structurally distinct, rather than collapsing them into one model with a
`resolved: bool` flag and an `iso2` that is sometimes null and sometimes meaningful.

## No numbers, as always

`iso_alpha2` is a code, not a quantity. The agent never sees or returns a guest count — the count
belongs to whichever reservation rows carry the label, and `tda.metrics` computes it from typed
records after the code is settled by a human.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from tda.agents.contracts.base import AgentOutput


class LabelResolution(AgentOutput):
    """One label, and the agent's answer about it: a matched country, or a stated abstention.

    ## Why this is one flat model rather than `Resolved | Abstained`

    It was a discriminated union from M4 until the first cassette recording ran against the live
    API, and that recording is what killed it: **every resolved case validated and every abstained case
    failed.** Asked to abstain, the model emitted the union of both branches' fields — `outcome:
    "abstained"` alongside `iso_alpha2: ""` and `matched_candidate: ""`. A `oneOf` with a
    discriminator is well-formed JSON Schema; constrained decoding flattened it anyway, and
    `extra="forbid"` refused the result.

    The idiom had been reviewed, type-checked and unit-tested since M4 and had never once been
    executed against a real model, because no cassette had ever been recorded. That is the finding,
    more than the fix: **a contract nothing has ever produced is a contract nobody has tested**,
    and every test that exercised this one built the object by hand.

    `tda.agents.contracts.reviewer_assist.CitedAnswer` carries the same change for the same
    reason, and ADR-0009 records the cost: the invariant is still enforced, but by a validator
    rather than by the type checker.

    `raw_label` is echoed back so the record stands alone. A resolution that says only `CI` is
    unreadable six weeks later in a review screen; one that says `'Cote dIvoir' -> CI` is not.
    """

    raw_label: str = Field(min_length=1)
    outcome: Literal["resolved", "abstained"] = Field(
        description="Which kind of answer this is. Required rather than defaulted: a default "
        "would let a malformed answer arrive as the confident case."
    )
    iso_alpha2: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
        description="ISO 3166-1 alpha-2. Required when resolved, forbidden when abstained. "
        "Validated against the committed lookup by the caller; a well-formed code no lookup "
        "knows is still rejected.",
    )
    matched_candidate: str | None = Field(
        default=None,
        description="The candidate name this matches, copied from the list that was offered. "
        "Required when resolved. Not redundant with `iso_alpha2`: a reviewer needs to see which "
        "offered name the agent thought the document meant, because `CI` tells them nothing and "
        "`Côte d'Ivoire` tells them everything. A name that was not offered is a fabrication and "
        "`tda.agents.resolution` discards the answer on that ground alone.",
    )
    reason: str = Field(
        min_length=1,
        description="Why this code, or why not. Required in **both** outcomes, because both need "
        "explaining to the human who reads the blocking finding.",
    )

    @model_validator(mode="after")
    def _the_outcome_matches_its_answer(self) -> Self:
        """A resolution names a country or it does not. Never half of each.

        The abstention is the load-bearing outcome here, not the fallback: `tda.extract.normalise`
        is explicit that an unmapped label is never guessed, and the cost of a wrong guess falls
        on a hotel that did nothing wrong. So an abstention carrying a code would be a guess
        wearing a refusal's clothes, and it is refused here.
        """
        if self.outcome == "resolved":
            if self.iso_alpha2 is None:
                raise ValueError("a resolved outcome must carry the country code it resolved to")
            if not (self.matched_candidate or "").strip():
                raise ValueError(
                    "a resolved outcome must name the offered candidate it matched, so a reviewer "
                    "can see what the agent thought the document meant"
                )
        else:
            if self.iso_alpha2 is not None:
                raise ValueError(
                    "an abstained outcome must not carry a country code. An abstention with a "
                    "code is a guess wearing a refusal's clothes, and the cost of a wrong guess "
                    "falls on a hotel that did nothing wrong."
                )
            if self.matched_candidate is not None:
                raise ValueError("an abstained outcome must not name a matched candidate")
        return self

    @property
    def is_answer(self) -> bool:
        """True when the agent named a country, False when it abstained.

        A property rather than `__bool__` - see `AgentOutput.is_answer` and `Abstention` for the
        recording run that established why. A falsy contract is silently dropped by the SDK's
        `if content.parsed_output:` check, and the caller is told the model returned nothing.
        """
        return self.outcome == "resolved"

    @property
    def abstained(self) -> bool:
        """Kept so the eval harness and the trace read the same word they always have, now that
        there is no `Abstention` subclass carrying the field."""
        return self.outcome == "abstained"
