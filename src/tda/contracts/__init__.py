"""Typed contracts — the spine every other package speaks.

Re-exported here so call sites import from `tda.contracts` rather than reaching into
modules, which keeps the internal layout free to change.
"""

from tda.contracts.claim import Claim, ComputedValue, NotVerifiable, index_by_key
from tda.contracts.metric_key import Dimension, Metric, MetricKey
from tda.contracts.period import Period, PeriodKind
from tda.contracts.refs import (
    CellRef,
    ExcelCitation,
    ExcelRef,
    InventoryRef,
    NotReached,
    PdfRef,
    SourceCitation,
    SourceRef,
)
from tda.contracts.reservation import InventoryDay, RateCode, ReservationRecord, Status
from tda.contracts.variance import (
    EscalationTarget,
    Finding,
    FindingIds,
    Severity,
    VarianceClass,
)
from tda.contracts.verdict import (
    ExtractionSummary,
    RejectionReason,
    ReviewerDecision,
    ReviewRecord,
    Verdict,
    VerdictStatus,
)

__all__ = [
    "CellRef",
    "Claim",
    "ComputedValue",
    "Dimension",
    "EscalationTarget",
    "ExcelCitation",
    "ExcelRef",
    "ExtractionSummary",
    "Finding",
    "FindingIds",
    "InventoryDay",
    "InventoryRef",
    "Metric",
    "MetricKey",
    "NotReached",
    "NotVerifiable",
    "PdfRef",
    "Period",
    "PeriodKind",
    "RateCode",
    "RejectionReason",
    "ReservationRecord",
    "ReviewRecord",
    "ReviewerDecision",
    "Severity",
    "SourceCitation",
    "SourceRef",
    "Status",
    "VarianceClass",
    "Verdict",
    "VerdictStatus",
    "index_by_key",
]
