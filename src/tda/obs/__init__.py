"""Observability: the run ledger, agent traces, token and cost accounting.

An agentic system you cannot inspect is an agentic system you cannot defend.

Token counts and durations live here rather than on an agent contract, and the reason is
structural: they are integers, and `tools/guard/agent_schema_lint.py` forbids an integer on
anything deriving from `AgentOutput`. **A record of what a call cost is not an agent's answer.**
Keeping the two apart is what lets that guard stay strict enough to be worth having.
"""

from tda.obs.artifacts import WrittenRun, latest_run, read_routing, read_run, write_run
from tda.obs.ledger import InputFile, NodeTiming, RunLedger, build_ledger, cost_summary, file_digest
from tda.obs.nodes import NodeLog, NodeOutcome, NodeRecord, Phase
from tda.obs.redact import Redaction, redact, scan
from tda.obs.routing import ROUTING_LOG, RoutingLog, RoutingRecord, routing_records
from tda.obs.trace import TraceCall, TraceLog, TraceRecord, trace_calls
from tda.obs.usage import (
    CACHE_READ_PER_MTOK,
    INPUT_PER_MTOK,
    OUTPUT_PER_MTOK,
    PRICING_VERSION,
    AgentUsage,
    UsageLedger,
)

__all__ = [
    "CACHE_READ_PER_MTOK",
    "INPUT_PER_MTOK",
    "OUTPUT_PER_MTOK",
    "PRICING_VERSION",
    "ROUTING_LOG",
    "AgentUsage",
    "InputFile",
    "NodeLog",
    "NodeOutcome",
    "NodeRecord",
    "NodeTiming",
    "Phase",
    "Redaction",
    "RoutingLog",
    "RoutingRecord",
    "RunLedger",
    "TraceCall",
    "TraceLog",
    "TraceRecord",
    "UsageLedger",
    "WrittenRun",
    "build_ledger",
    "cost_summary",
    "file_digest",
    "latest_run",
    "read_routing",
    "read_run",
    "redact",
    "routing_records",
    "scan",
    "trace_calls",
    "write_run",
]
