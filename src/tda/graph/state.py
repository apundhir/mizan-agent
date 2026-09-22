"""One typed state object, carried through five nodes and mutated by none of them in place.

LangGraph nodes return a mapping of field updates and the framework merges them. That is the whole
contract, and it is why this is a plain dataclass rather than a Pydantic model: the state holds
`MetricResults`, `Reconciliation` and `ExtractionResult`, which are dataclasses from three other
packages, and a Pydantic state would need `arbitrary_types_allowed` — buying validation it cannot
actually perform on any of the interesting fields, at the cost of pretending it had.

## Why one object rather than five signatures

The alternative is each node taking what it needs and returning what it produced, wired by hand.
That reads better in isolation and is worse in aggregate: the thing a reader wants to know about a
verification is *what was established by the time it got here*, and with five bespoke signatures
that answer is spread across five call sites. One object means the run's whole knowledge has one
shape, and adding a field is a decision made once.

## The fields are the pipeline, in order

`submission` and `declared` are inputs. Everything else is something a node established, and every
one of them starts empty — a state that began with a plausible default would let a node be skipped
without the absence being visible.

## Nothing here is a number a model produced

`records`, `claims` and `computed` come from `tda.extract`, `tda.excel` and `tda.metrics`. The only
model outputs that reach this object are `WorkbookMapping` (a set of cell ranges) and the
narratives attached to findings at publish. The deterministic boundary is unmoved: see
docs/adr/0001.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Every name used in a `RunState` annotation is imported at RUNTIME, and that is not an
# oversight. LangGraph calls `typing.get_type_hints()` on the state schema when the graph is built,
# which evaluates these annotations for real — a name reachable only under `TYPE_CHECKING` raises
# `NameError` at `StateGraph(RunState)`. It is the same trap `pyproject.toml` already records for
# Pydantic's runtime-evaluated models, arriving from a different direction, and it is why this
# module carries a `TC` per-file ignore.
from pathlib import Path

from tda.contracts import (
    Claim,
    ExtractionSummary,
    Finding,
    InventoryDay,
    NotVerifiable,
    Period,
    RejectionReason,
    ReservationRecord,
    Severity,
    Verdict,
    VerdictStatus,
)
from tda.excel.run import OutOfScopeClaim
from tda.excel.selfcheck import Inconsistency
from tda.metrics.compute import MetricResults
from tda.reconcile.engine import Reconciliation


@dataclass(frozen=True, slots=True)
class Submission:
    """The files an officer put in front of the system, and nothing derived from them.

    Frozen: the set under verification does not change while it is being verified. A pipeline that
    could add a file mid-run would produce a verdict about a submission nobody submitted.
    """

    reports: tuple[Path, ...]
    workbook: Path
    inventory: Path | None = None

    @property
    def files(self) -> tuple[Path, ...]:
        present = [*self.reports, self.workbook]
        if self.inventory is not None:
            present.append(self.inventory)
        return tuple(present)


@dataclass(frozen=True, slots=True)
class Declaration:
    """What the submission claims to be: this hotel, this period.

    Supplied by the caller rather than read out of the documents, and that is the point of intake.
    A system that derives the hotel and period from the files it was given can never detect the
    case the orchestrated graph asks it to detect — a submission for the wrong property or the wrong quarter, which
    is internally consistent and still wrong. Something outside the files has to assert what they
    are supposed to be, and then the files are checked against it.
    """

    hotel_id: str
    period: Period


@dataclass
class RunState:
    """Everything the run has established, in the order it established it.

    Mutable, because LangGraph merges each node's returned updates into it. The immutable artefact
    is the `Verdict` at the end — which is the only thing a reviewer ever sees, and which is frozen.
    """

    # ── inputs ───────────────────────────────────────────────────────────────
    run_id: str
    submission: Submission
    declared: Declaration

    # ── intake ───────────────────────────────────────────────────────────────
    rejection: RejectionReason | None = None
    rejection_detail: str | None = None

    # ── extract ──────────────────────────────────────────────────────────────
    records: tuple[ReservationRecord, ...] = ()
    extraction: ExtractionSummary | None = None
    inventory_days: tuple[InventoryDay, ...] = ()

    # ── claim_parse ──────────────────────────────────────────────────────────
    claims: tuple[Claim, ...] = ()
    # `OutOfScopeClaim`, not `MetricKey`: an out-of-scope metric name is by definition not a
    # member of the closed `Metric` enum, so it cannot be keyed. See `tda.graph.run` on why
    # `Verdict.out_of_scope_claims` therefore cannot be filled from here.
    out_of_scope: tuple[OutOfScopeClaim, ...] = ()
    inconsistencies: tuple[Inconsistency, ...] = ()

    # ── recompute_reconcile ──────────────────────────────────────────────────
    computed: MetricResults | None = None
    reconciliation: Reconciliation | None = None
    not_verifiable: tuple[NotVerifiable, ...] = ()

    # ── accumulated across nodes ─────────────────────────────────────────────
    # Findings arrive from three places - extraction limits, claim-parse refusals, and
    # reconciliation - and they are kept in one list because a reviewer reads one list. Which node
    # raised a finding is recoverable from its clause and class; which node a reviewer must act on
    # is not a question the finding answers.
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    definitional: tuple[Finding, ...] = field(default_factory=tuple)

    # ── publish ──────────────────────────────────────────────────────────────
    status: VerdictStatus | None = None
    verdict: Verdict | None = None

    @property
    def halted(self) -> bool:
        """Whether anything so far means the run cannot honestly continue.

        Blocking findings only. A run with material findings has done its job and should keep
        going; a run that could not *read* something has not, and everything downstream of it would
        be computed from a partial record.
        """
        return any(f.severity is Severity.BLOCKING for f in self.findings)

    @property
    def rejected(self) -> bool:
        return self.rejection is not None

    @property
    def finished(self) -> bool:
        """Whether the run is over, one way or another. Read by the graph's routing."""
        return self.rejected or self.halted or self.status is not None
