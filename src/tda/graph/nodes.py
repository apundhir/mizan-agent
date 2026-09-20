"""The five nodes, and what each one does when it cannot do its job.

| Node | Type | On failure |
|---|---|---|
| `intake` | code | reject with a stated reason code |
| `extract` | code | halt with a blocking finding. **Never infer a value** |
| `claim_parse` | model + code | flag unmapped sheets for human mapping |
| `recompute_reconcile` | code | hard fail if reference data is missing |
| `publish` | code + model | an unclassified variance becomes blocking |

Every node is a thin adapter. The work lives in `tda.extract`, `tda.excel`, `tda.metrics` and
`tda.reconcile`, which were built and tested before this file existed and which know nothing about
graphs. That is deliberate and it is the property that makes them testable: a node here is a dozen
lines of "call the thing, record what happened, put it in the state", and if one of these functions
starts making decisions it has taken work from a package that should own it.

## A submission that halts simply halts

No retries, no `Send` fan-out across PDFs, no checkpointed resume. PRD-89 defers all three and says
why: **a retry ladder that hides a transient extraction failure is worse than a halt**, because the
officer cannot tell which runs were clean. For a POC, honest beats resilient. Recording the
deferral is what stops it being mistaken for an oversight — it is in the issue, in ADR-0005, and
here.

So `_guard` re-raises everything it did not expect. The one thing it does before re-raising is
write the node's exit record, because a run that dies must still say where.

## Entry and exit, not a span

Each node writes a record on the way in and another on the way out, including when the way out is
an exception. Two records rather than one because a node that halts mid-way leaves an ENTER with no
EXIT, and that asymmetry names exactly where the run stopped — see `tda.obs.nodes`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from tda.contracts import (
    EscalationTarget,
    Finding,
    FindingIds,
    Metric,
    MetricKey,
    Severity,
    VarianceClass,
    VerdictStatus,
)
from tda.excel.agent import build_registry as mapping_registry
from tda.excel.agent import map_workbook_traced
from tda.excel.run import open_submission, parse_claims
from tda.excel.selfcheck import no_pdf
from tda.extract.inventory import read_inventory
from tda.extract.run import extract as extract_records
from tda.graph.intake import intake as run_intake
from tda.metrics.compute import compute_all
from tda.obs.nodes import NodeOutcome, NodeRecord, Phase
from tda.reconcile.engine import reconcile

if TYPE_CHECKING:
    from collections.abc import Callable

    from tda.excel.selfcheck import IdentityMismatch
    from tda.graph.context import RunContext
    from tda.graph.state import RunState

INTAKE = "intake"
EXTRACT = "extract"
CLAIM_PARSE = "claim_parse"
RECOMPUTE_RECONCILE = "recompute_reconcile"
PUBLISH = "publish"

NODE_ORDER = (INTAKE, EXTRACT, CLAIM_PARSE, RECOMPUTE_RECONCILE, PUBLISH)


class NodeFailureError(Exception):
    """A node could not do its job and the run cannot honestly continue.

    Distinct from the domain errors it wraps, because a caller catching this is catching "the run
    stopped", not "the inventory would not parse". The original is always chained.
    """


def _skip(state: RunState) -> bool:
    """Whether a node should do nothing because the run is already over.

    LangGraph's conditional edges route past the remaining nodes, so in a normal run this never
    fires. It exists because the routing and the nodes must agree about what "finished" means, and
    two implementations of that agreement is one too many: this is the same `state.finished` the
    router reads.
    """
    return state.finished


def _record(
    context: RunContext,
    node: str,
    *,
    outcome: NodeOutcome,
    started: float,
    detail: str | None = None,
) -> None:
    """Write one exit record, with the model figures this node accumulated.

    The token counts are a delta, not a total: `RunContext.checkpoint` remembers what the ledger
    said when the node was entered, so a node's figures are its own rather than the run's to date.
    """
    usage = context.since_checkpoint()
    context.nodes.append(
        NodeRecord(
            node=node,
            phase=Phase.EXIT,
            outcome=outcome,
            duration_ms=int((time.perf_counter() - started) * 1000),
            prompt_versions=context.prompt_versions_since_checkpoint(),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            model_calls=usage.calls,
            detail=detail,
        )
    )


def _guard(node: str, context: RunContext, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one node's body between an entry and an exit record.

    Not a context manager and not a try/except that swallows. An unexpected exception is recorded
    as `FAILED` and **re-raised**: a recorder that caught what it observed would be the last thing
    to report a problem and the first to hide one.
    """
    context.nodes.append(NodeRecord(node=node, phase=Phase.ENTER))
    context.checkpoint()
    started = time.perf_counter()
    try:
        update = work()
    except Exception as exc:
        _record(
            context,
            node,
            outcome=NodeOutcome.FAILED,
            started=started,
            detail=f"{type(exc).__name__}: {exc}",
        )
        raise

    outcome, detail = _outcome_of(update)
    _record(context, node, outcome=outcome, started=started, detail=detail)
    return update


def _outcome_of(update: dict[str, Any]) -> tuple[NodeOutcome, str | None]:
    """Read the node's own answer out of what it returned.

    A node says it rejected by setting `rejection`, and says it halted by returning blocking
    findings. Deriving the outcome from the update rather than having each node assert it keeps the
    two from disagreeing — and a node that reported OK while returning a blocking finding is
    exactly the disagreement that would matter.
    """
    if update.get("rejection") is not None:
        return NodeOutcome.REJECTED, str(update.get("rejection_detail") or "")
    blocking = [f for f in update.get("findings", ()) if f.severity is Severity.BLOCKING]
    if blocking:
        return NodeOutcome.HALTED, f"{len(blocking)} blocking finding(s): {blocking[0].clause}"
    return NodeOutcome.OK, None


# ── the nodes ────────────────────────────────────────────────────────────────


def intake_node(state: RunState, context: RunContext) -> dict[str, Any]:
    """Refuse a submission that is not what it says it is, before any page is read."""

    def work() -> dict[str, Any]:
        rejection = run_intake(state.submission, state.declared)
        if rejection is None:
            return {}
        return {"rejection": rejection.reason, "rejection_detail": rejection.detail}

    return _guard(INTAKE, context, work)


def extract_node(state: RunState, context: RunContext) -> dict[str, Any]:
    """Reservation records from the PMS reports, and the inventory reference beside them.

    Extraction returns its refusals as blocking findings rather than raising, so this node passes
    them into the state and lets the router stop the run. **Records are returned even when
    extraction halted**, and this node does not use them for anything — `state.halted` is what the
    next node consults, not the length of the record list.

    A missing inventory reference is **not** a failure here. It becomes `NotVerifiable` at the
    metrics layer, which is the honest outcome: rooms available is a property attribute and is
    never inferred from the reservations (D-RNA-04). An inventory that exists and will not parse is
    a different matter, and intake has already refused it.
    """

    def work() -> dict[str, Any]:
        if _skip(state):
            return {}
        try:
            result = extract_records(
                list(state.submission.reports), state.declared.period, context.policy
            )
        except Exception as exc:
            # By the time extraction runs, intake has already opened every report - a document
            # that will not open is a rejection, because it has no page to cite and D-EV-01
            # refuses a finding that cites nothing. So anything escaping here is a defect rather
            # than a bad submission, and it stops the run rather than becoming a finding about
            # the hotel.
            raise NodeFailureError(
                f"extraction failed on a report intake had already opened: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        inventory_days: tuple[Any, ...] = ()
        if state.submission.inventory is not None:
            try:
                inventory_days = read_inventory(
                    state.submission.inventory, state.declared.period
                ).days
            except Exception as exc:  # pragma: no cover - intake refuses this first
                raise NodeFailureError(
                    f"the inventory reference would not parse at extraction: {exc}. Intake checks "
                    "this before anything is read, so reaching here means the file changed mid-run."
                ) from exc

        return {
            "records": result.records,
            "extraction": result.summary,
            "inventory_days": inventory_days,
            "findings": (*state.findings, *result.findings),
        }

    return _guard(EXTRACT, context, work)


def claim_parse_node(state: RunState, context: RunContext) -> dict[str, Any]:
    """What the hotel claimed: a model says where the figures are, code reads them.

    The only node that calls a model, and the split is the whole point — `tda.excel.tools` gives
    the mapping agent no code path that could return a cell's value. Everything the mapping is
    checked against lives in `tda.excel.run`, which this node calls and does not second-guess.

    `expected_property` is passed, which makes `check_cover` reachable for the first time: it has
    always been skipped when the caller supplied no expectation, and the graph is the first caller
    that has one. An identity mismatch here is the workbook disagreeing with the declaration that
    intake already checked the inventory against.
    """

    def work() -> dict[str, Any]:
        if _skip(state):
            return {}
        values, formulas = open_submission(state.submission.workbook)
        # The registry is built here rather than in the context because its two tools are bound to
        # *this* workbook - see `tda.agents.tools` on why a registry never outlives the data it
        # reads. The runner shares the run's trace and ledger, so the call still lands in one log.
        mapping = map_workbook_traced(
            formulas,
            context.policy,
            context.runner_for(mapping_registry(formulas), node=CLAIM_PARSE),
        ).output
        claims = parse_claims(
            values,
            mapping,
            state.declared.period,
            context.policy,
            formulas=formulas,
            expected_property=state.declared.hotel_id,
        )

        findings = list(claims.findings)
        for mismatch in claims.identity_mismatches:
            # The workbook says it is for a different property or period than the one declared.
            # Blocking, and it reads as an extraction limit rather than a hotel error: the system
            # cannot tell which of the two is wrong, and guessing would accuse somebody.
            findings.append(_identity_finding(mismatch, context))

        return {
            "claims": claims.claims,
            "out_of_scope": claims.out_of_scope,
            "inconsistencies": claims.inconsistencies,
            "findings": (*state.findings, *findings),
        }

    return _guard(CLAIM_PARSE, context, work)


def recompute_reconcile_node(state: RunState, context: RunContext) -> dict[str, Any]:
    """Recompute every metric from the records, then compare, classify and cite.

    No model is consulted anywhere below this line (D-CLS-10). This is the deterministic core, and
    the node's job is to hand it four things and put the result in the state.

    `compute_all` raises `MetricError` on duplicate reservation ids, which is why extraction merges
    month-spanning stays first. That is a hard fail rather than a finding: a metric computed over a
    record set with duplicates is wrong in a way that has no correct partial answer.
    """

    def work() -> dict[str, Any]:
        if _skip(state):
            return {}
        periods = [state.declared.period, *state.declared.period.months()]
        inventory = list(state.inventory_days) or None
        try:
            computed = compute_all(list(state.records), inventory, periods, context.policy)
            result = reconcile(
                list(state.claims),
                computed,
                list(state.records),
                inventory,
                periods,
                context.policy,
            )
        except Exception as exc:
            raise NodeFailureError(
                f"recomputation could not complete: {type(exc).__name__}: {exc}. A metric computed "
                "from a record set this library will not accept has no correct partial answer, so "
                "the run stops rather than reporting one."
            ) from exc

        # `raised` filters on SEVERITY and `definitional` on VARIANCE CLASS, and policy gives V2
        # `material` - so every definitional finding is in both. Passing both through unfiltered
        # puts the same finding in `findings` and `definitional_items`, which `Verdict` refuses
        # twice over (D-MAT-06, and the duplicate-id check). The filter is not a tidy-up: a hotel
        # error count that included policy disagreements is the exact wrong number D-MAT-06 exists
        # to prevent, and it would have killed every ESCALATED run.
        hotel_side = tuple(
            f for f in result.raised if f.variance_class is not VarianceClass.DEFINITIONAL
        )
        return {
            "computed": computed,
            "reconciliation": result,
            "not_verifiable": result.not_verifiable,
            "findings": (*state.findings, *hotel_side),
            "definitional": result.definitional,
        }

    return _guard(RECOMPUTE_RECONCILE, context, work)


def publish_node(state: RunState, context: RunContext) -> dict[str, Any]:
    """Decide the verdict status. The artefacts themselves are PRD-92's.

    The status rules are `Verdict`'s own invariants read forwards rather than backwards, and the
    contract enforces every one of them on construction — so this function cannot produce a status
    the contract would reject without failing loudly at the end of the run.
    """

    def work() -> dict[str, Any]:
        findings, definitional = renumber(state.findings, state.definitional)
        return {
            "findings": findings,
            "definitional": definitional,
            "status": decide_status(state),
        }

    return _guard(PUBLISH, context, work)


def renumber(
    findings: tuple[Finding, ...], definitional: tuple[Finding, ...]
) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    """Re-issue `F-0001…` across every finding the run produced, in the order they were raised.

    Three libraries raise findings — extraction, the claim parser and reconciliation — and each
    allocates from its own `FindingIds`, starting at `F-0001`. That is correct in isolation and
    collides the moment two of them contribute to one run: two findings with the same id, and
    `Verdict` refuses the duplicate outright.

    Threading one counter through three library signatures was the alternative. Renumbering here is
    smaller and lands in the right place: the graph is the only thing that ever combines the three
    lists, so it is the only thing that can number them consistently. Ids stay sequential in the
    order raised, which is what `FindingIds` promises and what `ReviewRecord.finding_id` needs —
    and nothing has referenced them yet at this point in the run, because the verdict is where they
    first escape.
    """
    ids = FindingIds()
    renumbered = [f.model_copy(update={"finding_id": ids.take()}) for f in findings]
    escalated = [f.model_copy(update={"finding_id": ids.take()}) for f in definitional]
    return tuple(renumbered), tuple(escalated)


def decide_status(state: RunState) -> VerdictStatus:
    """The verdict status, from what the run established.

    Ordered most-serious first, and each branch is a rule `Verdict` also enforces:

    - **rejected** — intake refused; the reason code travels with it.
    - **halted** — something could not be read. `Verdict` refuses `FAIL` or `PASS` alongside a
      blocking finding, because a run that could not see is not a run that found nothing.
    - **escalated** — definitional items, which go to the policy owner and are never hotel errors.
    - **fail** — real findings against the hotel.
    - **pass** — zero findings and zero definitional items. `Verdict` enforces exactly that, so
      `PASS` here cannot quietly come to mean "nothing much".
    """
    if state.rejected:
        return VerdictStatus.REJECTED
    if state.halted:
        return VerdictStatus.HALTED
    if state.definitional:
        return VerdictStatus.ESCALATED
    if state.findings:
        return VerdictStatus.FAIL
    return VerdictStatus.PASS


def _identity_finding(mismatch: IdentityMismatch, context: RunContext) -> Finding:
    """A workbook whose cover sheet disagrees with the declaration.

    V7 rather than a hotel error, and the reasoning is the same one `tda.extract.normalise` makes
    about an unmappable label: the system can see that two statements disagree and cannot see which
    is wrong. Filing that against the property would be an accusation the evidence does not support.
    """
    return Finding(
        finding_id=context.finding_ids.take(),
        key=MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=str(context.period)),
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        source_ref=no_pdf("the workbook's own cover sheet is the evidence here"),
        excel_ref=mismatch.excel_ref,
        clause="D-XLS-05",
        narrative=mismatch.detail,
    )


__all__ = [
    "CLAIM_PARSE",
    "EXTRACT",
    "INTAKE",
    "NODE_ORDER",
    "PUBLISH",
    "RECOMPUTE_RECONCILE",
    "NodeFailureError",
    "claim_parse_node",
    "decide_status",
    "extract_node",
    "intake_node",
    "publish_node",
    "recompute_reconcile_node",
]
