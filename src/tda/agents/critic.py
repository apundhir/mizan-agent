"""The critic: grades a narrative against the finding it claims to explain, with no tools at all.

An empty allowlist, and it is the most deliberate entry in the roster. The critic judges exactly
what it is shown — one finding, one sentence — and may not go looking for more. An agent that can
fetch its own evidence can find something that makes an ungrounded sentence look grounded, and it
will, because that is what looking for supporting evidence does. Withholding the tools is what
keeps the judgement about the pair in front of it.

## The critic and the narrative agent are separate agents on purpose

Same model, different prompts, different contracts, separate calls. Asking one agent to write and
then grade its own sentence produces a grader that agrees with itself, which is worse than no
grader because it reports a number that looks like evidence. Two prompts do not make the blind
spots independent — they share a model — but they do make them *different*, and the thing being
caught here is ungrounded prose rather than a subtle inferential error.

## The verdict is computed, not asserted

`CriticVerdict` carries four observations; `CriticVerdict.passed` derives the pass/fail from them.
So a prompt change is measured against a fixed rule rather than against a judge whose standards
moved at the same time. That is the property that makes a judge loop scorable at all, and it is why
the contract has no `score` field for the model to fill in.

Turning a set of verdicts into a grade for a prompt version is narrative grading's job, in the eval harness
where the thresholds sit next to the results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tda.agents.contracts.critic import CriticVerdict
from tda.agents.narrative import read_finding
from tda.agents.provider.base import Message
from tda.agents.roster import CRITIC
from tda.agents.runtime import AgentError, AgentSpec

if TYPE_CHECKING:
    from tda.agents.contracts.narrative import FindingNarrative
    from tda.agents.runtime import AgentResult, AgentRunner
    from tda.contracts import Finding
    from tda.policy import Policy


def render_case(finding: Finding, narrative: FindingNarrative) -> str:
    """The pair to be graded: the finding as the narrative agent saw it, and what it wrote.

    Uses `read_finding` — the *same* rendering the narrative agent was given — rather than a fuller
    one. Showing the critic more than the writer saw would have it marking a sentence ungrounded
    for omitting something the writer was never told, which grades the wiring instead of the prose.
    """
    return "\n\n".join(
        [
            "# the finding, exactly as the narrative agent was shown it",
            read_finding(finding),
            "# the sentence it wrote",
            narrative.sentence,
            f"# it declared this finding definitional: {narrative.is_definitional}",
            f"# it cited permutation: {narrative.cites_permutation or 'none'}",
            "Answer the four questions about this sentence.",
        ]
    )


def grade(
    finding: Finding,
    narrative: FindingNarrative,
    runner: AgentRunner,
    policy: Policy,
) -> AgentResult[CriticVerdict]:
    """Grade one narrative. Raises if the verdict is filed against a different finding.

    No tool session is created: the critic's allowlist is empty, so there is nothing to bind and
    nothing to record. That absence is itself visible in the trace, where the critic's records
    carry no tool calls while every other agent's do.
    """
    if narrative.finding_id != finding.finding_id:
        raise AgentError(
            f"cannot grade narrative for {narrative.finding_id} against finding "
            f"{finding.finding_id}. Grading a sentence against a finding it does not describe "
            "produces a verdict about nothing, and it would count in the score."
        )

    spec = AgentSpec.from_policy(CRITIC, CriticVerdict, policy)
    result = runner.run(spec, [Message(role="user", content=render_case(finding, narrative))])

    if result.output.finding_id != finding.finding_id:
        raise AgentError(
            f"critic graded {finding.finding_id} and filed the verdict against "
            f"{result.output.finding_id}. A misfiled verdict counts towards the wrong narrative's "
            "score in both directions."
        )
    return result
