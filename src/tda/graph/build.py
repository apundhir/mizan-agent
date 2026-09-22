"""The graph itself: five nodes, one edge each, and one decision about when to stop.

```
START → intake → extract → claim_parse → recompute_reconcile → publish → END
           │         │           │                  │
           └─────────┴───────────┴──────────────────┴──────────► publish (finished)
```

Sequential, on purpose and for this sprint only. the orchestrated graph defers `Send` fan-out across the three
PDFs, the bounded retry ladder, and checkpointed interrupt-and-resume, and the reasoning is in
ADR-0005: **a retry ladder that hides a transient extraction failure is worse than a halt**,
because the officer cannot tell which runs were clean.

## The one branch, and why it goes to `publish` rather than to `END`

A rejected or halted run still produces a verdict — a `REJECTED` one with its reason code, or a
`HALTED` one with the blocking findings that stopped it. Routing straight to `END` would give a
reviewer nothing to read, and the officer's question after a failed run ("what happened?") is
answered by a verdict, not by an absent one.

So every path reaches `publish`. What changes is how much of the pipeline ran first, and the node
records say exactly which nodes were entered.

## Why the nodes are bound rather than taking the context as state

LangGraph calls a node with the state and nothing else. The context — policy, provider, the three
recorders — is not state: it is not merged, not returned, and not per-node. Binding it in a closure
is what keeps it out of the state schema, where it would have to be mergeable and where two nodes
returning a `TraceLog` would race to overwrite each other with divergent copies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from tda.graph.nodes import (
    CLAIM_PARSE,
    EXTRACT,
    INTAKE,
    PUBLISH,
    RECOMPUTE_RECONCILE,
    claim_parse_node,
    extract_node,
    intake_node,
    publish_node,
    recompute_reconcile_node,
)
from tda.graph.state import RunState

if TYPE_CHECKING:
    from collections.abc import Callable

    from tda.graph.context import RunContext

# The pipeline, in order. The last entry has no successor other than `publish`, which every path
# reaches; see the module docstring.
SEQUENCE: tuple[tuple[str, Callable[..., Any]], ...] = (
    (INTAKE, intake_node),
    (EXTRACT, extract_node),
    (CLAIM_PARSE, claim_parse_node),
    (RECOMPUTE_RECONCILE, recompute_reconcile_node),
    (PUBLISH, publish_node),
)


def _bind(node: Callable[[RunState, RunContext], dict[str, Any]], context: RunContext) -> Any:
    """Bind a node to its run context, leaving LangGraph a one-argument callable."""

    def run(state: RunState) -> dict[str, Any]:
        return node(state, context)

    return run


def _route_from(name: str) -> Callable[[RunState], str]:
    """After `name`, go to the next node — or straight to `publish` if the run is over.

    One function rather than five, because the rule is one rule: a finished run stops doing work
    and goes to write down what happened. Spelling it per node would be five opportunities for the
    fifth to disagree with the other four.
    """
    order = [node for node, _ in SEQUENCE]
    following = order[order.index(name) + 1]

    def route(state: RunState) -> str:
        return PUBLISH if state.finished else following

    return route


def build_graph(context: RunContext) -> Any:
    """Compile the five-node graph for one run.

    Per run rather than once at import, because the nodes are bound to this run's context — its
    policy, its provider, its recorders. A module-level graph would share one `TraceLog` across
    every verification the process ever performed.
    """
    graph: Any = StateGraph(RunState)

    for name, node in SEQUENCE:
        graph.add_node(name, _bind(node, context))

    graph.add_edge(START, INTAKE)
    for name, _ in SEQUENCE[:-1]:
        graph.add_conditional_edges(name, _route_from(name))
    graph.add_edge(PUBLISH, END)

    return graph.compile()
