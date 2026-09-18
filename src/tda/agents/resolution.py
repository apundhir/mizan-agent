"""The resolution agent: code finds the candidates, the model picks one, a human confirms.

Read `tda.extract.normalise` first. It says an unmapped label is never guessed — *not by edit
distance, not by substring, not by a model's best effort* — and this module does not soften that.
What it adds is narrower and worth having: when code has already refused, the agent proposes which
of a code-supplied shortlist the document probably meant, and that proposal rides along with the
blocking finding for a human to accept or reject.

The distinction is the whole story, so it is worth being exact about it:

| | |
|---|---|
| What code does | refuses the label, raises `UnmappableLabelError`, produces a blocking V7 finding |
| What the agent does | suggests which candidate it means, or abstains |
| What changes if the agent is wrong | a human rejects a suggestion |
| What changes if the agent is absent | a human types the same answer from scratch |

**Nothing the agent returns reaches a metric.** The finding is blocking either way (D-NAT-13), and
the only way a country code enters a computation is through `country_lookup.yaml`, which is a
committed file changed by a pull request.

## The two tools, and why the shortlist comes from code

`fuzzy_candidates` is where the edit distance lives, and it lives in *code* on purpose. Code
proposes a bounded, ranked list; the model chooses from it or declines. The inverse arrangement —
the model proposes and code checks — sounds equivalent and is not: a model asked for a country
given only a garbled string will produce a country, including for strings that are not countries at
all, and a check that the answer is *a valid ISO code* passes for every one of them.

`iso_lookup` exists so the agent can confirm what a code it is considering actually denotes, rather
than reasoning from a two-letter string. Both tools go through a `ToolSession`, so the allowlist is
enforced and the trace records which of them ran.

## The answer is checked before it is believed

`resolve_label` rejects an answer whose `matched_candidate` was not offered, and one whose
`iso_alpha2` is not in the committed lookup. Both are fabrications, and both would otherwise reach
a human as a confident suggestion with nothing marking it as invented.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from tda.agents.contracts.resolution import LabelResolution
from tda.agents.provider.base import Message
from tda.agents.roster import RESOLUTION
from tda.agents.runtime import AgentError, AgentSpec
from tda.agents.tools import Tool, ToolRegistry
from tda.extract.normalise import load_lookups, normalise_key

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.agents.runtime import AgentResult, AgentRunner
    from tda.agents.tools import ToolSession
    from tda.extract.normalise import Lookups
    from tda.policy import Policy

# How many candidates the agent is shown. Small on purpose: a list of twenty is a list in which
# something plausible always appears, and the agent's abstention becomes unreachable. Five is
# enough to contain a real typo's target and short enough that "none of these" stays a live answer.
MAX_CANDIDATES = 5

# The similarity floor for a candidate to be offered at all. `difflib`'s ratio, not a character
# count: `Austria`/`Australia` scores 0.75 and both are offered, which is correct — the agent is
# shown a genuinely ambiguous pair and is expected to abstain on it rather than be spared the
# choice by a threshold that quietly picked one.
MIN_SIMILARITY = 0.6

# How close the top two candidates may score before code stops accepting a choice between them.
# `Austrlia` is one edit from `Austria` and one from `Australia` — different continents, and no
# amount of character counting distinguishes them. Below this margin the shortlist does not contain
# an answer, it contains a coin toss.
AMBIGUOUS_MARGIN = 0.05


@dataclass(frozen=True, slots=True)
class Candidate:
    """One country the label might mean, with the code it would resolve to.

    The name is what the agent reasons about and the code is what it returns; carrying both means
    the answer can be checked against the offer rather than only against the lookup.
    """

    name: str
    iso_alpha2: str
    similarity: float = 0.0

    def render(self) -> str:
        return f"- {self.name} ({self.iso_alpha2})"


def fuzzy_candidates(
    raw: str, lookups: Lookups | None = None, *, limit: int = MAX_CANDIDATES
) -> tuple[Candidate, ...]:
    """A bounded, ranked shortlist for a label the committed lookup does not contain.

    The ranking is `difflib.SequenceMatcher` over the normalised forms, so it uses the same
    case-and-punctuation rule as the lookup itself (D-NAT-10) and does not rank `U.S.A.` below
    `USA` for reasons that have nothing to do with meaning.

    Returns the aliases, not only the canonical names: a document saying `Korea, Rep of` is better
    served by being shown `Korea, Republic of` than by being shown `South Korea`, because the
    reviewer confirming it is comparing against the document in front of them.

    An empty tuple is a legitimate answer and the agent is told so explicitly. A label with no near
    neighbour is one nobody should be nudged into resolving.
    """
    tables = lookups or load_lookups()
    key = normalise_key(raw)
    if not key:
        return ()

    matcher = difflib.SequenceMatcher(a=key)
    scored: list[tuple[float, str, str]] = []
    for alias, code in tables.countries.items():
        matcher.set_seq2(alias)
        ratio = matcher.ratio()
        if ratio >= MIN_SIMILARITY:
            scored.append((ratio, alias, code))

    # Sorted by score then alias, so the shortlist is stable: this text is hashed into the cassette
    # key, and a tie broken by dict order would give the same label two different keys.
    scored.sort(key=lambda item: (-item[0], item[1]))

    seen: set[str] = set()
    candidates: list[Candidate] = []
    for _ratio, alias, code in scored:
        if code in seen:
            continue
        seen.add(code)
        candidates.append(Candidate(name=alias, iso_alpha2=code, similarity=_ratio))
        if len(candidates) >= limit:
            break
    return tuple(candidates)


def too_close_to_choose(candidates: Sequence[Candidate]) -> bool:
    """Whether the top two candidates are so near in score that choosing between them is a guess.

    **Code decides this, not the prompt.** Two rounds of prompt instruction — *"if more than one
    candidate could genuinely be what the document meant, abstain"*, in as many words — did not stop
    the agent resolving `Austrlia` to Australia on the reasoning that it was "a closer character
    match than Austria". It is not: one is an insertion and the other a deletion, and the two are a
    continent apart.

    That is this repository's own thesis arriving on schedule. A prompt instruction is a request; a
    check in the function that returns the answer is a guarantee, and only the second survives a
    model that has an opinion. `resolve_label` refuses a resolution whenever this is true, and the
    eval case that caught it is the reason the check exists.
    """
    if len(candidates) < 2:
        return False
    return abs(candidates[0].similarity - candidates[1].similarity) < AMBIGUOUS_MARGIN


def iso_lookup(code: str, lookups: Lookups | None = None) -> str | None:
    """What an alpha-2 code denotes, or `None` for one the committed table does not know.

    `None` rather than a raise: the agent may reasonably ask about a code that turns out not to
    exist, and "no such code" is the answer to that question rather than an error in asking it.
    """
    tables = lookups or load_lookups()
    wanted = code.strip().upper()
    if wanted not in tables.canonical_countries:
        return None
    # The canonical name is the alias that *is* the code's own entry key; fall back to the longest
    # alias, which is the official form rather than an abbreviation.
    names = [alias for alias, mapped in tables.countries.items() if mapped == wanted]
    return max(names, key=len) if names else None


def build_registry(lookups: Lookups | None = None) -> ToolRegistry:
    """The resolution agent's two tools, bound to the committed lookups.

    Per run rather than as a module global, for the reason in `tda.agents.tools`: a global registry
    holds state across runs, and that is how a second submission gets answered from the first one's
    data.
    """
    tables = lookups or load_lookups()
    return ToolRegistry(
        (
            Tool(
                name="fuzzy_candidates",
                description=(
                    "The committed country names most similar to a label, ranked by code. "
                    "Code produces this list; you choose from it or decline."
                ),
                fn=lambda raw: fuzzy_candidates(raw, tables),
            ),
            Tool(
                name="iso_lookup",
                description="What an ISO 3166-1 alpha-2 code denotes, or nothing if unknown.",
                fn=lambda code: iso_lookup(code, tables),
            ),
        )
    )


def render_candidates(raw: str, candidates: Sequence[Candidate]) -> str:
    """The message the agent is shown: one label, and the shortlist code produced for it.

    The empty case says so in words rather than rendering an empty list. A list header followed by
    nothing reads as a formatting failure, and an agent that thinks the tool broke will behave
    differently from one that knows there is genuinely nothing close.
    """
    if not candidates:
        return (
            f"Label from the document: {raw!r}\n\n"
            "Code found no country name similar enough to offer. There is nothing to choose "
            "from, so abstain and say what the label appears to be, if anything."
        )
    listing = "\n".join(candidate.render() for candidate in candidates)
    return (
        f"Label from the document: {raw!r}\n\n"
        f"Candidates code produced, most similar first:\n{listing}\n\n"
        "Choose one of these or abstain. A name not on this list is not an available answer."
    )


def resolve_label(
    raw: str,
    runner: AgentRunner,
    policy: Policy,
    *,
    lookups: Lookups | None = None,
    session: ToolSession | None = None,
) -> AgentResult[LabelResolution]:
    """Ask the resolution agent what a label means. The answer is a suggestion, never a resolution.

    Checks two things about the answer before returning it, and both are fabrication checks rather
    than quality judgements:

    - the matched candidate must be one that was offered — otherwise the agent invented a name and
      dressed it as a choice;
    - the code must be in the committed lookup — a well-formed `XX` satisfies the field's pattern
      and denotes nothing.

    Either failure raises. It does not downgrade to an abstention: an abstention is a considered
    refusal, and silently relabelling a fabrication as one would put the two in the same bucket in
    every eval and every trace that follows.
    """
    spec = AgentSpec.from_policy(RESOLUTION, LabelResolution, policy)
    turn = session if session is not None else runner.session(spec)
    candidates: tuple[Candidate, ...] = turn.call("fuzzy_candidates", raw)

    result = runner.run(
        spec, [Message(role="user", content=render_candidates(raw, candidates))], session=turn
    )
    resolution = result.output

    if resolution.raw_label != raw:
        raise AgentError(
            f"resolution agent was asked about {raw!r} and answered about "
            f"{resolution.raw_label!r}. The label is echoed back so a misrouted answer is "
            "detectable rather than merely unlikely; this one is misrouted."
        )

    if resolution.is_answer and too_close_to_choose(candidates):
        # Code refuses the choice and substitutes the abstention it would have produced itself.
        # Not a repair of the model's answer - a rejection of it, falling back to the conservative
        # outcome, which is the same shape as `tda.extract.normalise` never guessing an unmapped
        # label. The agent's own reasoning is kept, because the human clearing the blocking finding
        # wants to see what it thought and why that was not enough.
        return replace(
            result,
            output=LabelResolution(
                raw_label=raw,
                outcome="abstained",
                reason=(
                    f"{candidates[0].name} and {candidates[1].name} are too close to choose "
                    f"between for {raw!r}, so this was not resolved. The agent proposed "
                    f"{resolution.matched_candidate}: {resolution.reason}"
                ),
            ),
        )

    if resolution.is_answer:
        offered = {candidate.name for candidate in candidates}
        if resolution.matched_candidate not in offered:
            raise AgentError(
                f"resolution agent matched {raw!r} to {resolution.matched_candidate!r}, which was not "
                f"offered. Code produces the shortlist precisely so the answer is bounded; a name "
                "from outside it is invented, and an invented suggestion reaching a human as a "
                "confident one is the failure this check exists for."
            )
        tables = lookups or load_lookups()
        if resolution.iso_alpha2 not in tables.canonical_countries:
            raise AgentError(
                f"resolution agent returned {resolution.iso_alpha2!r}, which is well-formed and is not "
                "in the committed lookup. The field's pattern accepts any two capitals; only the "
                "table decides which of them denote a country."
            )
    return result
