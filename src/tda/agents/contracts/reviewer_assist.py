"""The reviewer-assist agent's contract: an answer with citations, or a refusal. Never prose alone.

This agent sits on the one surface where a verification officer forms their opinion of whether the
tool can be trusted. It answers their actual questions — *"why is March occupancy flagged?"*,
*"which rule made this definitional?"*, *"show me where that number came from"* — and the whole
design rests on one decision:

**An uncited answer cannot be constructed.** Not discouraged in the prompt, not checked afterwards
by something that might be skipped: `Answered.citations` has `min_length=1`, so a model that
returns confident prose with nothing behind it fails schema validation and the officer sees "I
can't evidence that" instead. *"Always cite your sources"* is advice a model follows most of the
time, and on this surface most of the time is not good enough.

## Why a citation is a reference and never a quotation

A citation here is a **pointer to something already in the run** — rows of a report, a cell of the
submitted workbook, or a clause of `policy.yaml`. It is not a quoted passage the agent composed,
because a quotation is a thing a model can write and a pointer is a thing a reader can follow. The
reviewer clicks through to the same evidence pane the finding itself uses; nothing the agent says
becomes evidence by being said.

The kinds mirror the places an answer can come from — a report, the workbook, the inventory
reference, the ruleset — and the list is deliberately closed. An officer's question that needs
something outside them is a question this agent declines.

`InventoryCitation` is here for the reason `tda.contracts.refs` gives at length: `room_nights_available`
is an in-scope metric whose source is a per-date CSV rather than a page, because D-RNA-01 makes rooms
available a property attribute rather than a reservation one. Without it, an answer about the one
metric that has no page could cite only the workbook side or the clause — or, worse, a `PdfRef` with
an invented page. A citation a reader cannot follow is the failure this whole contract exists to
prevent, so the union covers every source a finding can actually carry.

## Why declining is an outcome rather than an error

An exception says the machinery broke; a sentinel string says the caller has to remember which one;
a stated `declined` outcome says *the agent considered this and could not evidence an answer*. An
assistant that answers everything is indistinguishable from one that makes things up — so the
ability to decline is part of the contract, and four of the eval cases exist to exercise it.

It was a discriminated union — `Answered | Declined` — until recording proved the API cannot
generate one. See `CitedAnswer` for what happened and what replaced it.

## No numbers, as always

A citation carries `page`, `row_start` and `row_end`, which the schema lint allows because they are
positions rather than quantities — nothing downstream does arithmetic on a page number. The agent
never returns a figure of its own: a claimed or computed value belongs to the verdict, and if an
answer needs one it points at the finding that already holds it.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from tda.agents.contracts.base import AgentOutput
from tda.contracts import ExcelRef, InventoryRef, PdfRef

# Clause ids as `policy.yaml` writes them: `D-MAT-06`, `D-SCOPE-02`. The same shape `Finding.clause`
# uses, so an answer's citation and a finding's citation are comparable without translation.
CLAUSE_PATTERN = r"^D-[A-Z]+-[0-9]{2}$"


class PdfCitation(AgentOutput):
    """Rows of a submitted report.

    Wraps `PdfRef` rather than restating its fields, so a citation inherits the rules that type
    already enforces — 1-indexed pages, ordered rows, and a bare filename rather than a path. An
    absolute path in a citation leaks the machine that ran the verification into an answer a hotel
    may eventually read.
    """

    kind: Literal["pdf"] = "pdf"
    ref: PdfRef

    @property
    def citation(self) -> str:
        return self.ref.citation


class ExcelCitation(AgentOutput):
    """A cell of the submitted workbook.

    Note for anyone grepping: `tda.contracts.ExcelCitation` is a **different** thing - the
    `ExcelRef | NotReached` alias a `Finding` carries. The two are related (this one wraps the
    `ExcelRef` half) and the collision is unfortunate; import the module rather than the name if
    both are in scope.
    """

    kind: Literal["excel"] = "excel"
    ref: ExcelRef

    @property
    def citation(self) -> str:
        return self.ref.citation


class InventoryCitation(AgentOutput):
    """Days of the room inventory reference.

    The one source with no page. See the module docstring, and `tda.contracts.refs.InventoryRef`
    for why dressing it as a PDF reference would put a citation in front of a reviewer that leads
    nowhere.
    """

    kind: Literal["inventory"] = "inventory"
    ref: InventoryRef

    @property
    def citation(self) -> str:
        return self.ref.citation


class ClauseCitation(AgentOutput):
    """A clause of the ruleset the verdict was produced under.

    The answer to *"which rule made this definitional?"* is a clause id, and the officer can read
    the clause in `policy.yaml` and `docs/01-definitions.md`. `policy_version` is carried because a
    clause without the version it was read from is a citation of a moving target — and this system
    stamps a policy version into every verdict precisely so that cannot happen.
    """

    kind: Literal["clause"] = "clause"
    clause: str = Field(pattern=CLAUSE_PATTERN)
    policy_version: str = Field(min_length=1)

    @property
    def citation(self) -> str:
        return f"policy {self.policy_version} clause {self.clause}"


type Citation = PdfCitation | ExcelCitation | InventoryCitation | ClauseCitation


class CitedAnswer(AgentOutput):
    """One question, and the agent's answer to it: prose with citations, or a stated refusal.

    ## Why this is one flat model rather than `Answered | Declined`

    It was a discriminated union, and that was the more expressive design. Recording it against
    the live API killed it: **every answered case validated and every declined case failed.**
    Asked to decline, the model emitted the union of *both* branches' fields — `outcome:
    "declined"` alongside `prose: ""` and a populated `citations`, with `reason` absent entirely.
    A `oneOf` with a discriminator is well-formed JSON Schema; constrained decoding flattened it
    anyway, and `extra="forbid"` then refused the result. The same thing happened to
    `LabelResolution`, which had carried the identical idiom since M4 and had never once been
    recorded.

    So the shape the API can actually produce is a flat object with a discriminator field, and the
    invariant moves from the type system to `_the_outcome_matches_its_evidence` below. **The
    guarantee is unchanged**: an answer that claims to be answered and carries no citation cannot
    be constructed, and the failure is still a validation error rather than something a later
    stage has to remember to check. What is lost is only that the type checker can no longer prove
    it — a real cost, recorded in ADR-0009 rather than papered over.

    The alternative was to let the adapter drop the fields the discriminator says do not belong.
    That would have kept the union, and it would have made the adapter edit what the model
    returned, which is the one thing this repository argues hardest against.

    `question` is echoed back for the reason `LabelResolution.raw_label` is: the record has to
    stand alone. An answer in a trace with no question attached is unreadable six weeks later.
    """

    question: str = Field(min_length=1)
    outcome: Literal["answered", "declined"] = Field(
        description="Which kind of answer this is. The one field that decides how to read the "
        "rest, so it is required rather than defaulted - a default would let a malformed answer "
        "arrive as the reassuring case."
    )
    prose: str | None = Field(
        default=None,
        description="The answer, in the officer's terms. Required when answered, forbidden when "
        "declined. Read beside the citations, never instead of them.",
    )
    citations: tuple[Citation, ...] = Field(
        default=(),
        description="What the answer rests on: report rows, workbook cells, policy clauses. "
        "Non-empty when answered - see the validator - and empty when declined, which is the "
        "honest answer: a refusal rests on nothing.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the agent could not evidence an answer. Required when declined, "
        "forbidden when answered. A sentence the officer can act on, not a code.",
    )

    @model_validator(mode="after")
    def _the_outcome_matches_its_evidence(self) -> Self:
        """The headline guarantee, enforced here rather than by the union that used to hold it.

        Four rules, and the first is the one this whole contract exists for: **an answered outcome
        with no citation cannot be constructed.** A model that returns confident prose with
        nothing behind it fails validation in the provider, and the officer reads "it cannot tell
        you anything it cannot show you" instead of the prose.

        The other three keep the two outcomes from blurring. A decline carrying prose would render
        as an answer; an answer carrying a refusal reason would render as both; and a decline
        carrying citations would put references under a sentence that makes no claim.
        """
        if self.outcome == "answered":
            if not self.citations:
                raise ValueError(
                    "an answered outcome must carry at least one citation. An answer with "
                    "nothing behind it is not a weaker answer; it is a different kind of object, "
                    "and this contract cannot hold one."
                )
            if not (self.prose or "").strip():
                raise ValueError("an answered outcome must carry the answer itself")
            if self.reason is not None:
                raise ValueError(
                    "an answered outcome must not carry a refusal reason; it did not refuse"
                )
        else:
            if not (self.reason or "").strip():
                raise ValueError(
                    "a declined outcome must say why, in a sentence the officer can act on. "
                    "An empty refusal renders as an empty box, which reads as the tool having "
                    "broken rather than having declined."
                )
            if self.prose is not None:
                raise ValueError(
                    "a declined outcome must not carry prose; prose beside a refusal is read as "
                    "an answer"
                )
            if self.citations:
                raise ValueError(
                    "a declined outcome must not carry citations; a refusal rests on nothing, "
                    "and a reference under it stands beneath a sentence making no claim"
                )
        return self

    @property
    def is_answer(self) -> bool:
        """True when the agent answered, False when it declined.

        A property rather than `__bool__`, for the reason `AgentOutput.is_answer` gives at length:
        a falsy pydantic model is dropped by the SDK's `if content.parsed_output:` check, which
        turns a correct refusal into "the model returned nothing parseable".
        """
        return self.outcome == "answered"

    @property
    def text(self) -> str:
        """What the officer reads, whichever way it went."""
        return (self.prose if self.outcome == "answered" else self.reason) or ""
