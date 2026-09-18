"""One orchestrated run: files in, a `Verdict` out, and a record of everything in between.

This is `make run`. It is the first thing in the repository that produces a `Verdict`, which means
it is also the first thing that has had to satisfy every one of that contract's invariants at once
— and two of them are load-bearing enough to restate:

- **A blocking finding forbids `PASS` and `FAIL`.** A run that could not read something has not
  found nothing; it has not looked. `Verdict` rejects the combination outright.
- **Definitional items are never in `findings`.** They go to the policy owner, and a hotel-error
  count that included policy disagreements would be a wrong number presented as a right one
  (D-MAT-06).

`decide_status` reads those rules forwards and the contract enforces them backwards, so a status
this module gets wrong fails loudly at construction rather than reaching a reviewer.

## The stamps

Every field that says *which rules produced this number* is filled here, from the one place that
owns it: `policy.version` from the loaded ruleset, `METRIC_LIBRARY_VERSION` from `tda.metrics`,
`model_id` from policy, `provider_mode` from the provider actually in use, and `prompt_versions`
from the trace. A number and its ruleset travel together, or the number is not defensible.

`METRIC_LIBRARY_VERSION` has existed since M2 and nothing has ever read it. This is the first
consumer.

## A gap this module inherits and cannot close

`Verdict.out_of_scope_claims` is typed `tuple[MetricKey, ...]`. Out-of-scope metrics — `revenue`,
`average_daily_rate`, `revpar` — are **by definition not members of the closed `Metric` enum**
(`policy.scope.metrics_out_of_scope` holds them as plain strings), so no `MetricKey` can be built
for one and the field cannot be filled from the only thing that produces out-of-scope claims.

It is left empty and the claims are carried on `RunState.out_of_scope` instead, so nothing is lost
in this run. But D-SCOPE-02 says silence must not be mistaken for approval, and an empty field in
the published verdict is exactly that silence. **Fixing it is a contract change** — the field needs
a type that can represent a metric name outside the enum — and that belongs with PRD-92, which owns
the verdict artefacts. Recorded here rather than worked around quietly.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from tda.contracts import Verdict, VerdictStatus
from tda.graph.build import build_graph
from tda.graph.context import RunContext
from tda.graph.state import Declaration, RunState, Submission
from tda.metrics import METRIC_LIBRARY_VERSION

if TYPE_CHECKING:
    from pathlib import Path

    from tda.agents.provider.base import LLMProvider
    from tda.contracts import Period
    from tda.obs.nodes import NodeLog
    from tda.policy import Policy


def new_run_id() -> str:
    """A run id that is unique and carries no meaning.

    Deliberately not derived from the submission, the time or the hotel. A meaningful id invites
    somebody to parse it, and then the format is a contract nobody wrote down. The run's *facts*
    live in the verdict, where they are typed.
    """
    return f"run-{uuid.uuid4().hex[:12]}"


def discover(directory: Path, period: Period) -> Submission:
    """The submission in a directory, by the naming convention the corpus and the parser share.

    A convenience for `make run` and the tests, not a contract. `intake` is what decides whether
    what was found is acceptable — this only says what is there, and says it without judgement so
    that a missing file reaches intake as a missing file rather than as a crash here.
    """
    reports = tuple(sorted(directory.glob("pms_*.pdf")))
    workbooks = sorted(directory.glob(f"claims_{period}.xlsx")) or sorted(directory.glob("*.xlsx"))
    inventories = sorted(directory.glob(f"inventory_{period}.csv")) or sorted(
        directory.glob("inventory_*.csv")
    )
    return Submission(
        reports=reports,
        # A directory with no workbook still produces a `Submission`, naming the file that should
        # have been there. Intake then rejects it as UNREADABLE_FILE with the name in the message,
        # which is more use to an officer than a KeyError from a glob.
        workbook=workbooks[0] if workbooks else directory / f"claims_{period}.xlsx",
        inventory=inventories[0] if inventories else None,
    )


def build_verdict(state: RunState, context: RunContext) -> Verdict:
    """Assemble the verdict from what the run established. Every stamp, every count, one object.

    `findings` and `definitional_items` are kept apart because the contract requires it and because
    the requirement is the point: a V2 counted as a hotel error is a correct finding that
    discredits the system.
    """
    status = state.status or VerdictStatus.HALTED
    return Verdict(
        run_id=state.run_id,
        status=status,
        rejection_reason=state.rejection,
        hotel_id=state.declared.hotel_id,
        period=str(state.declared.period),
        policy_version=context.policy.version,
        metric_library_version=METRIC_LIBRARY_VERSION,
        model_id=context.policy.model.model_id,
        provider_mode=context.provider_mode,
        prompt_versions=context.trace.prompt_versions(),
        extraction=state.extraction,
        claims_checked=len(state.claims),
        findings=state.findings,
        definitional_items=state.definitional,
        not_verifiable=state.not_verifiable,
        # See the module docstring: the contract's type cannot represent an out-of-scope metric.
        out_of_scope_claims=(),
    )


class RunResult:
    """A verdict and the records that explain it.

    The verdict alone is what a reviewer signs behind. The logs are what answers *"why did it say
    that?"* — and PRD-90 writes both to `artifacts/<run_id>/`. Returning them together means a
    caller holding a verdict can always produce its working.
    """

    def __init__(self, verdict: Verdict, state: RunState, context: RunContext) -> None:
        self.verdict = verdict
        self.state = state
        self.context = context

    @property
    def nodes(self) -> NodeLog:
        return self.context.nodes

    def render(self) -> str:
        lines = [
            f"{self.verdict.run_id}  {self.verdict.status.value}"
            + (
                f"  ({self.verdict.rejection_reason.value})"
                if self.verdict.rejection_reason
                else ""
            ),
            f"  hotel {self.verdict.hotel_id}  period {self.verdict.period}",
            f"  policy {self.verdict.policy_version}  metrics {self.verdict.metric_library_version}"
            f"  model {self.verdict.model_id} ({self.verdict.provider_mode})",
            f"  claims checked {self.verdict.claims_checked}"
            f"  findings {len(self.verdict.findings)}"
            f"  definitional {len(self.verdict.definitional_items)}"
            f"  not verifiable {len(self.verdict.not_verifiable)}",
            "",
            self.context.nodes.render(),
        ]
        if unfinished := self.context.nodes.unfinished():
            lines.append(f"  stopped inside: {', '.join(unfinished)}")
        return "\n".join(lines)


def verify(
    submission: Submission,
    declared: Declaration,
    policy: Policy,
    provider: LLMProvider,
    *,
    run_id: str | None = None,
    context: RunContext | None = None,
) -> RunResult:
    """Verify one submission end to end.

    The whole of `make run`. Builds a context, compiles the graph, invokes it, and turns the final
    state into a verdict.

    `context` may be supplied by a caller that wants to **hold the node log across a failure**.
    This function does not catch, so a run that dies takes its context with it unless the caller
    already had one — and the node records are the whole answer to "where did it stop?". `mizan
    run` builds one for that reason; a caller that does not care can leave it out.

    **It does not catch anything.** A node that fails has already written its exit record and
    re-raised, and letting that propagate is the deferral PRD-89 asks for: a submission that halts
    simply halts, and a caller that wants to keep going is making a decision this function must not
    make for it.
    """
    context = context or RunContext.build(policy, declared.period, provider)
    state = RunState(run_id=run_id or new_run_id(), submission=submission, declared=declared)

    graph = build_graph(context)
    # LangGraph returns the merged state as a mapping over the schema's fields; rebuilding the
    # dataclass from it keeps everything downstream typed rather than dict-shaped.
    final = graph.invoke(state)
    resolved = final if isinstance(final, RunState) else RunState(**dict(final))

    return RunResult(build_verdict(resolved, context), resolved, context)


def verify_directory(
    directory: Path,
    hotel_id: str,
    period: Period,
    policy: Policy,
    provider: LLMProvider,
    *,
    run_id: str | None = None,
    context: RunContext | None = None,
) -> RunResult:
    """`verify`, for a caller that has a directory rather than a file list."""
    return verify(
        discover(directory, period),
        Declaration(hotel_id=hotel_id, period=period),
        policy,
        provider,
        run_id=run_id,
        context=context,
    )
