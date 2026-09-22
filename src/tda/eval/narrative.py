"""Grading the prose beside each finding: narrative grading, kept outside the deterministic score.

`make eval` scores the numbers (`tda.eval.score`). This module is the other half: for every
finding a fixture's `Verdict` carries, write the sentence an officer would read (`narrate`) and
grade it (`grade`), then hand back a plain `NarrativeResult` rather than a `CriticVerdict` — see
`tda.eval.scoring.NarrativeResult` for why. Its output joins `score_fixture`'s checks and never
touches `Verdict` itself, matching narrative grading's own framing: the critic's output affects the eval
report, never a verdict.

## Why a fresh `AgentRunner` per finding

The narrative agent's tools are bound to one finding (`tda.agents.narrative.build_registry`), the
same rule `tda.agents.cases.registry_for` already follows for the hand-written eval cases. A
registry shared across findings would let one finding's narrative be answered from another's data,
which is exactly the bug per-run construction exists to prevent. The critic's allowlist is empty,
so its runner's registry is an empty `ToolRegistry()` for the same reason `cases.registry_for`'s
`CRITIC` branch uses one: the absence is the roster entry made concrete, not an oversight.

## Why a cassette miss is not caught here

Both `narrate` and `grade` raise `CassetteMissError` through the provider when replay finds nothing
recorded. Left uncaught, it reaches `tda.eval.run.score_fixture`'s existing exception handling,
which already walks any exception's cause chain looking for one and reports `Outcome.NOT_RECORDED`
rather than a failure. Catching it here would duplicate that classification in a second place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tda.agents.critic import grade
from tda.agents.narrative import build_registry, narrate
from tda.agents.runtime import AgentRunner
from tda.agents.tools import ToolRegistry
from tda.eval.scoring import NarrativeResult
from tda.obs import TraceLog

if TYPE_CHECKING:
    from tda.agents.provider.base import LLMProvider
    from tda.contracts import Finding, Verdict
    from tda.obs import UsageLedger
    from tda.policy import Policy


def grade_finding(
    finding: Finding,
    provider: LLMProvider,
    policy: Policy,
    *,
    usage: UsageLedger | None = None,
) -> NarrativeResult:
    """`narrate` then `grade` one finding, converted to the plain result `scoring.py` carries.

    `usage` is `None` for every call `make eval` makes (replay is free) and a shared ledger for
    `tda.eval.record_narratives`, which reports the same cost figure `make record` already does.
    """
    narrative_runner = AgentRunner(
        provider, policy=policy, registry=build_registry(finding), trace=TraceLog(), usage=usage
    )
    narrative_result = narrate(finding, narrative_runner, policy)

    critic_runner = AgentRunner(
        provider, policy=policy, registry=ToolRegistry(), trace=TraceLog(), usage=usage
    )
    verdict = grade(finding, narrative_result.output, critic_runner, policy).output

    return NarrativeResult(
        finding_id=finding.finding_id,
        sentence=narrative_result.output.sentence,
        passed=verdict.passed,
        failures=verdict.failures(),
        reason=verdict.reason,
    )


def grade_narratives(
    verdict: Verdict,
    provider: LLMProvider,
    policy: Policy,
    *,
    usage: UsageLedger | None = None,
) -> tuple[NarrativeResult, ...]:
    """Every finding and definitional item the verdict carries, narrated and graded.

    Both arrays, because a narrative that reads as an accusation is exactly as expensive on a
    definitional item as on a material one — it is the PRD's own motivating example. A verdict
    with no findings and no definitional items (F1, F5) yields nothing to grade, which is correct
    rather than a gap: there is no sentence for a reviewer to read.
    """
    return tuple(
        grade_finding(finding, provider, policy, usage=usage)
        for finding in (*verdict.findings, *verdict.definitional_items)
    )
