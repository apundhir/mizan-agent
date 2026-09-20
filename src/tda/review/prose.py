"""Grade the prose: narrate and grade every finding in a verdict, on demand, for the console.

The same two-agent sequence `tda.eval.narrative.grade_finding` runs for `make eval` - narrate a
finding, grade the narrative, both through fresh runners bound to that one finding - repeated here
rather than called, for one reason: this module needs the two trace records the console's agent
cards render (what the narrative agent was asked, what the critic was asked), and `grade_finding`
returns only the plain `NarrativeResult` those calls produced, not the calls themselves. Nothing
about the grading rules changes; the sequence is identical, `tda.agents.narrative.narrate` and
`tda.agents.critic.grade` are the same two functions `make eval` calls.

## One row per finding, never a blank panel for one miss

`grade_finding` lets a `CassetteMissError` propagate, correct for `make eval` where a fixture with
no recorded cassette is a fixture the whole run fails on. A console showing five findings at once
must not blank the entire panel because one finding's cassette is missing - `grade_verdict` catches
per finding, so four graded rows and one `not_recorded` row is what a viewer sees, not nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tda.agents.critic import grade
from tda.agents.narrative import build_registry, narrate
from tda.agents.provider import CassetteMissError
from tda.agents.runtime import AgentRunner
from tda.agents.tools import ToolRegistry
from tda.obs.redact import redact
from tda.obs.trace import TraceLog

if TYPE_CHECKING:
    from tda.agents.provider.base import LLMProvider
    from tda.contracts import Finding, Verdict
    from tda.obs.trace import TraceRecord
    from tda.obs.usage import UsageLedger
    from tda.policy import Policy

Status = Literal["graded", "not_recorded", "failed"]


@dataclass(frozen=True, slots=True)
class GradeRow:
    """One finding, narrated and graded - or the reason it could not be, without stopping the
    other findings from grading."""

    finding_id: str
    sentence: str | None
    passed: bool | None
    failures: tuple[str, ...]
    reason: str | None
    status: Status
    detail: str
    narrative_trace: TraceRecord | None
    critic_trace: TraceRecord | None


def _not_recorded(
    finding_id: str,
    detail: str,
    *,
    sentence: str | None = None,
    narrative_trace: TraceRecord | None = None,
) -> GradeRow:
    return GradeRow(
        finding_id=finding_id,
        sentence=sentence,
        passed=None,
        failures=(),
        reason=None,
        status="not_recorded",
        detail=redact(detail)[0],
        narrative_trace=narrative_trace,
        critic_trace=None,
    )


def _failed(
    finding_id: str,
    detail: str,
    *,
    sentence: str | None = None,
    narrative_trace: TraceRecord | None = None,
) -> GradeRow:
    return GradeRow(
        finding_id=finding_id,
        sentence=sentence,
        passed=None,
        failures=(),
        reason=None,
        status="failed",
        detail=redact(detail)[0],
        narrative_trace=narrative_trace,
        critic_trace=None,
    )


def grade_finding(
    finding: Finding, provider: LLMProvider, policy: Policy, *, usage: UsageLedger | None = None
) -> GradeRow:
    """`tda.eval.narrative.grade_finding`'s own sequence, with both trace records kept rather
    than discarded - narrate, then grade, each through a runner bound to nothing but this finding."""
    narrative_runner = AgentRunner(
        provider, policy=policy, registry=build_registry(finding), trace=TraceLog(), usage=usage
    )
    try:
        narrative_result = narrate(finding, narrative_runner, policy)
    except CassetteMissError as exc:
        return _not_recorded(finding.finding_id, str(exc))
    except Exception as exc:
        return _failed(finding.finding_id, f"{type(exc).__name__}: {exc}")

    narrative_trace = narrative_runner.trace[-1] if len(narrative_runner.trace) else None
    sentence = narrative_result.output.sentence

    critic_runner = AgentRunner(
        provider, policy=policy, registry=ToolRegistry(), trace=TraceLog(), usage=usage
    )
    try:
        critic_result = grade(finding, narrative_result.output, critic_runner, policy)
    except CassetteMissError as exc:
        return _not_recorded(
            finding.finding_id, str(exc), sentence=sentence, narrative_trace=narrative_trace
        )
    except Exception as exc:
        return _failed(
            finding.finding_id,
            f"{type(exc).__name__}: {exc}",
            sentence=sentence,
            narrative_trace=narrative_trace,
        )

    critic_trace = critic_runner.trace[-1] if len(critic_runner.trace) else None
    verdict = critic_result.output
    return GradeRow(
        finding_id=finding.finding_id,
        sentence=sentence,
        passed=verdict.passed,
        failures=verdict.failures(),
        reason=redact(verdict.reason)[0],
        status="graded",
        detail="",
        narrative_trace=narrative_trace,
        critic_trace=critic_trace,
    )


def grade_verdict(
    verdict: Verdict, provider: LLMProvider, policy: Policy, *, usage: UsageLedger | None = None
) -> tuple[GradeRow, ...]:
    """Every finding and definitional item in `verdict`, graded independently - matching
    `tda.eval.narrative.grade_narratives`'s own coverage, since a narrative that reads as an
    accusation is exactly as expensive on a definitional item as on a material one."""
    return tuple(
        grade_finding(finding, provider, policy, usage=usage)
        for finding in (*verdict.findings, *verdict.definitional_items)
    )
