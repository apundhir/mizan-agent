"""The narrative agent: the sentence beside a finding, and the checks that decide whether it runs.

Two tools, `read_finding` and `read_evidence`, and they are the agent's whole view. It is not given
the verdict, the other findings, or the corpus. That is a narrow surface for a narrow job — explain
*this* finding — and it forecloses the failure where a narrative about March quietly borrows a fact
from April.

## The figures are rendered, not written

`read_finding` gives the agent the finding's classification, its clause, its permutation and its
references. **It does not give it the figures**, and the prompt says why: a sentence containing a
number is a number a model produced, and the officer cannot tell by looking which numbers in a
report came from `tda.metrics` and which came from prose. So the claimed and computed values are
rendered beside the sentence by the publishing code, from the `Finding`, where code put them.

This is the same argument as `tda.excel.tools` makes about the mapping agent, applied to the other
end of the pipeline: not "please do not state a figure" but "there is no figure here to state".

## What is checked before a sentence is published

`narrate` rejects an answer whose `finding_id` is not the one it asked about, and one whose
`cites_permutation` is not the permutation the classifier assigned. The second is the load-bearing
check. A narrative that names a cause nobody assigned is a plausible sentence attached to a correct
finding, which is the most expensive kind of wrong this system can produce — the figures survive
review and the explanation does not.

Grading the prose itself is the critic's job, and the critic is a separate agent with a separate
prompt precisely so that the thing being graded and the thing doing the grading cannot share a
blind spot.

## Most classes need one more fact than a class name and a clause

A V2 has a permutation to lean on. A V1, a V5 and a V7 have none, and a class name alone does not
say *what* two things disagree, *which side* is missing a figure, or *why* nothing could be
compared. Asked for a sentence anyway, the agent filled that gap on its own: an invented pair of
source systems for a plain transcription mismatch, a directional claim about which document holds
a missing value, a specific mechanism for an unresolved label. narrative grading's own critic caught every one
of these as ungrounded, on real fixture findings, the first time anything graded one. See
`_structural_fact` below: one line per class, derived from field presence rather than a value, so
the fix adds a fact without adding a figure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tda.agents.contracts.narrative import FindingNarrative
from tda.agents.provider.base import Message
from tda.agents.roster import NARRATIVE
from tda.agents.runtime import AgentError, AgentSpec
from tda.agents.tools import Tool, ToolRegistry
from tda.contracts import VarianceClass

if TYPE_CHECKING:
    from tda.agents.runtime import AgentResult, AgentRunner
    from tda.agents.tools import ToolSession
    from tda.contracts import Finding
    from tda.policy import Policy


def _structural_fact(finding: Finding) -> str | None:
    """The one fact a class name and a clause do not supply on their own, for the three classes
    that carry no permutation to lean on: that two sides disagree, which side is missing a figure,
    or that nothing could be compared at all.

    No branch states a value. `claimed is None` is a fact about presence, checkable against the
    contract's own invariant that a V5 states exactly one of `claimed`/`computed`, not a number read
    out of either field.
    """
    if finding.variance_class is VarianceClass.TRANSCRIPTION:
        return (
            "the workbook states a figure for this key that does not match the figure this system "
            "computed from the source data; both sides state one"
        )
    if finding.variance_class is VarianceClass.COMPLETENESS:
        if finding.claimed is None:
            return "the workbook states no figure for this key; a source record does hold one"
        return "a source record holds no figure for this key; the workbook does state one"
    if finding.variance_class is VarianceClass.EXTRACTION_LIMIT:
        return (
            "part of the submission could not be read or matched against the reference data, so "
            "nothing here was compared against anything; that is the whole of what is established"
        )
    return None


def read_finding(finding: Finding) -> str:
    """Everything about a finding except its figures.

    The omission is the design. `claimed`, `computed`, `difference` and `proposed_correction` are
    all deliberately absent — see the module docstring. What is here is what the sentence needs:
    which metric, which period, what kind of difference, which rule, and what explained it.
    """
    lines = [
        f"finding_id: {finding.finding_id}",
        f"metric: {finding.key.metric.value}",
        f"period: {finding.key.period}",
        f"variance_class: {finding.variance_class.value}",
        f"severity: {finding.severity.value}",
        f"escalates_to: {finding.escalates_to.value}",
        f"clause: {finding.clause}",
    ]
    if finding.key.dimension is not None:
        lines.append(f"dimension: {finding.key.dimension.value} = {finding.key.value}")
    if finding.explaining_permutation is not None:
        lines.append(f"explaining_permutation: {finding.explaining_permutation}")
    if finding.also_explained_by:
        lines.append(f"also_explained_by: {', '.join(finding.also_explained_by)}")
    fact = _structural_fact(finding)
    if fact is not None:
        lines.append(fact)
    lines.append(
        "The figures are deliberately not shown. They are rendered beside your sentence from the "
        "finding itself, so that no number in the report came from prose."
    )
    return "\n".join(lines)


def read_evidence(finding: Finding) -> str:
    """Where the finding's two sides came from, as citations rather than as content.

    A page and a cell, not the text on the page or the value in the cell. The agent's sentence
    should be checkable against the evidence; it does not need to restate it, and restating it is
    where an invented quotation would come from.
    """
    return "\n".join(
        [
            f"source: {finding.source_ref!r}",
            f"excel: {finding.excel_ref!r}",
        ]
    )


def build_registry(finding: Finding) -> ToolRegistry:
    """The narrative agent's tools, bound to one finding.

    Bound to *one* finding rather than to the verdict, which is what makes "it cannot borrow a fact
    from another finding" a property of the wiring rather than of the prompt.
    """
    return ToolRegistry(
        (
            Tool(
                name="read_finding",
                description="This finding's classification, clause and cause. No figures.",
                fn=lambda: read_finding(finding),
            ),
            Tool(
                name="read_evidence",
                description="Where each side of this finding came from: a PDF page and an Excel cell.",
                fn=lambda: read_evidence(finding),
            ),
        )
    )


def narrate(
    finding: Finding,
    runner: AgentRunner,
    policy: Policy,
    *,
    session: ToolSession | None = None,
) -> AgentResult[FindingNarrative]:
    """Write the sentence for one finding, and refuse an answer that describes a different one.

    Both checks are fabrication checks. A wrong `finding_id` means the sentence would be published
    under a finding it does not describe; a `cites_permutation` the classifier did not assign means
    the sentence names a cause that was never found. Either raises rather than being logged and
    shown, because a warning beside a plausible sentence is read once and then never again.
    """
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)
    turn = session if session is not None else runner.session(spec)

    body = "\n\n".join(
        [
            str(turn.call("read_finding")),
            str(turn.call("read_evidence")),
            "Write the sentence an officer reads beside this finding.",
        ]
    )
    result = runner.run(spec, [Message(role="user", content=body)], session=turn)
    narrative = result.output

    if narrative.finding_id != finding.finding_id:
        raise AgentError(
            f"narrative agent was asked about {finding.finding_id} and answered about "
            f"{narrative.finding_id}. Publishing it would attach this sentence to a finding it "
            "does not describe."
        )
    if narrative.cites_permutation is not None:
        assigned = {finding.explaining_permutation, *finding.also_explained_by} - {None}
        if narrative.cites_permutation not in assigned:
            raise AgentError(
                f"narrative for {finding.finding_id} cites permutation "
                f"{narrative.cites_permutation!r}, which the classifier did not assign "
                f"(assigned: {sorted(str(a) for a in assigned) or 'none'}). A plausible sentence "
                "naming a cause nobody found is the most expensive wrong answer here: the figures "
                "survive review and the explanation does not."
            )
    return result
