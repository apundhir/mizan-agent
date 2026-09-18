"""The agentic edge.

Models read layouts, labels and language here. Code performs every calculation elsewhere. Nothing
in this package may be imported by `tda.metrics` or `tda.reconcile` — the import guard fails the
build on any attempt. See docs/adr/0001-deterministic-core-agentic-edges.md.

## An agent is five things

A **versioned prompt**, a **typed output contract**, a **narrow tool allowlist**, an **eval set**,
and a **trace record**. Anything with fewer is a prompt with ambitions. Four of the five are
required arguments to `AgentSpec`, so an agent missing one does not type-check; the fifth lives in
`tests/eval/agents/`, and a test there asserts every agent in the roster has one.

| Module | Owns |
|---|---|
| `roster` | the six agents, their tool allowlists and their effort defaults — one auditable table |
| `runtime` | `AgentSpec` and `AgentRunner`: render, request, validate, record |
| `tools` | `ToolRegistry` and `ToolSession` — allowlists enforced at the moment of the call |
| `supervisor` | the **code** router, the per-run budget, and a record of every decision |
| `contracts/` | what each agent may return. No numeric fields, enforced by the schema lint |
| `prompts/` | versioned prompt files, immutable once a cassette exists |
| `provider/` | the model layer: `anthropic` · `replay` · `stub` · `record` |
| `resolution` · `narrative` · `critic` | the agents themselves, and the checks applied to their answers |

The mapping agent lives with the Excel parser (`tda.excel.agent`), because its contract is
inseparable from the geometry checks that make it safe. The roster still declares its allowlist —
an agent whose permissions are not in the table is an agent whose permissions nobody wrote down.

Reviewer-assist is declared in the roster and implemented in PRD-93.
"""

from tda.agents.roster import (
    CRITIC,
    MAPPING,
    NARRATIVE,
    RESOLUTION,
    REVIEWER_ASSIST,
    ROSTER,
    RosterEntry,
    UnknownAgentError,
    allowlist_for,
    entry_for,
)
from tda.agents.runtime import AgentError, AgentResult, AgentRunner, AgentSpec
from tda.agents.supervisor import (
    Budget,
    BudgetExceededError,
    Disposition,
    RoutingDecision,
    Supervisor,
)
from tda.agents.tools import (
    Tool,
    ToolCall,
    ToolError,
    ToolNotAllowedError,
    ToolNotRegisteredError,
    ToolRegistry,
    ToolSession,
)

__all__ = [
    "CRITIC",
    "MAPPING",
    "NARRATIVE",
    "RESOLUTION",
    "REVIEWER_ASSIST",
    "ROSTER",
    "AgentError",
    "AgentResult",
    "AgentRunner",
    "AgentSpec",
    "Budget",
    "BudgetExceededError",
    "Disposition",
    "RosterEntry",
    "RoutingDecision",
    "Supervisor",
    "Tool",
    "ToolCall",
    "ToolError",
    "ToolNotAllowedError",
    "ToolNotRegisteredError",
    "ToolRegistry",
    "ToolSession",
    "UnknownAgentError",
    "allowlist_for",
    "entry_for",
]
