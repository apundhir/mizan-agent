"""The orchestrated run: five nodes, one typed state object, one verdict.

```
START → intake → extract → claim_parse → recompute_reconcile → publish → END
```

Sequential for this sprint. `Send` fan-out across the PDFs, the bounded retry ladder and
checkpointed interrupt-and-resume are all deferred, and ADR-0005 says why: **a retry ladder that
hides a transient extraction failure is worse than a halt**, because the officer cannot tell which
runs were clean. A submission that halts simply halts.

| Module | Owns |
|---|---|
| `state` | `RunState` — everything the run has established, one object |
| `context` | `RunContext` — what the run was *given*: policy, provider, and the three recorders |
| `intake` | the four rejection reasons, all produced here, none produced anywhere before |
| `nodes` | the five nodes, and what each does when it cannot do its job |
| `build` | the LangGraph assembly and the one routing rule |
| `run` | `verify()` — files in, `Verdict` out |

Every node writes an entry and an exit record (`tda.obs.nodes`). A node that halts leaves an entry
with no exit, which names exactly where the run stopped.
"""

from tda.graph.build import SEQUENCE, build_graph
from tda.graph.context import RunContext, UsageDelta
from tda.graph.intake import (
    Rejection,
    check_files_present,
    check_hotel,
    check_period,
    intake,
)
from tda.graph.nodes import (
    CLAIM_PARSE,
    EXTRACT,
    INTAKE,
    NODE_ORDER,
    PUBLISH,
    RECOMPUTE_RECONCILE,
    NodeFailureError,
    decide_status,
)
from tda.graph.run import (
    RunResult,
    build_verdict,
    discover,
    new_run_id,
    verify,
    verify_directory,
)
from tda.graph.state import Declaration, RunState, Submission

__all__ = [
    "CLAIM_PARSE",
    "EXTRACT",
    "INTAKE",
    "NODE_ORDER",
    "PUBLISH",
    "RECOMPUTE_RECONCILE",
    "SEQUENCE",
    "Declaration",
    "NodeFailureError",
    "Rejection",
    "RunContext",
    "RunResult",
    "RunState",
    "Submission",
    "UsageDelta",
    "build_graph",
    "build_verdict",
    "check_files_present",
    "check_hotel",
    "check_period",
    "decide_status",
    "discover",
    "intake",
    "new_run_id",
    "verify",
    "verify_directory",
]
