"""`mizan trace` — one run as a tree: which agent ran, what it was asked, what it returned.

The ledger answers *what were the rules*. The trace answers *what did each agent say*. Neither is
much use to a person at 5pm on the day a hotel disputes a finding, because both are JSON. This
module is the third thing: the two joined, in the order the run happened, at a width a human reads.

## How a call is attributed to a node, and why that is exact rather than a guess

Nothing in a `TraceRecord` names the node it ran under — and adding one would be the wrong fix,
since a record of a model call has no business knowing about the pipeline that made it. Instead the
join is arithmetic: `NodeTiming.model_calls` says how many calls each node made, `TraceLog`
preserves call order, and the graph is strictly sequential (ADR-0005). So consuming the trace in
order, `model_calls` at a time, assigns every record to exactly one node.

That exactness has a shelf life, and it is worth naming: the day the graph runs two nodes
concurrently, this becomes a guess and a `node` field on the record becomes the right answer.

**Both directions of an imbalance are reported, and the second one is the dangerous one.** Records
left over at the end are obvious. A node claiming more calls than the trace has left is not: it
consumes the records belonging to every node after it, so the tree shows the right number of calls
in the wrong places and nothing on the page looks wrong. That is reachable today — a failed call
writes a trace record without touching the usage ledger — so it is named in the output rather than
left for a reader to notice that a total does not add up.

## Why nothing here imports `tda.agents`

A reader would like a line saying what each agent is *for*, and `ROSTER` has exactly that string.
Taking it would make the viewer depend on the agent package, which already depends on `tda.obs` for
`TraceRecord` — an import cycle, and more to the point the same mistake `build_ledger` refuses:
observability describes a run, and must not depend on the thing being run. What the agent was asked
is therefore read off the record itself — its name, prompt version, the tools it actually reached
for, and the contract it was required to return — which has the advantage of describing *this* run
rather than the roster as it stands today.

## Long free prose never reaches a line

`returned:` prints a contract's short scalar fields and counts everything else. A value longer than
`VALUE_LIMIT` is elided to its length. The rule is not about width: `FindingNarrative.sentence` is
model-written prose about a specific hotel's numbers, and a viewer that pretty-printed it would put
it on a terminal, in a screenshot and eventually in a ticket. Whoever needs the sentence reads the
verdict, which is the artifact that is meant to carry it.

The tree reads the **already-redacted** artifacts — `tda.obs.artifacts` redacts on write and there
is no unredacted copy — so anything the patterns caught arrives here as `[redacted:…]`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from tda.obs.nodes import Phase
from tda.obs.usage import AgentUsage

if TYPE_CHECKING:
    from decimal import Decimal

    from tda.obs.ledger import NodeTiming, RunLedger
    from tda.obs.nodes import NodeLog
    from tda.obs.trace import TraceLog, TraceRecord

# Past this many characters a returned value is counted rather than shown. Wide enough for a metric
# code, an A1 range, a sheet name or an ISO country; far too narrow for a sentence.
VALUE_LIMIT: Final = 48

# How many of a contract's fields to name before summarising the rest. A contract with twenty
# fields is not made legible by printing twenty of them.
FIELD_LIMIT: Final = 6

NODE_WIDTH: Final = 20

# A halt reason or an error is prose and can run to several lines - the cassette miss message is
# four. In a tree, a value that carries its own newlines destroys the structure the tree exists to
# provide, so these are flattened to one line and cut. The full text is in `nodes.jsonl` and
# `trace.jsonl`, which is where somebody who needs the whole message should be reading it.
REASON_LIMIT: Final = 96


def render_tree(ledger: RunLedger, trace: TraceLog, nodes: NodeLog) -> str:
    """The whole run, as a tree. What `mizan trace` prints.

    Takes the three artifacts separately rather than a run directory, so a test can build one by
    hand and a caller that already has them in memory does not have to write them out first.
    """
    lines = _header(ledger)
    attributed = _attribute(ledger.nodes, trace)
    details = _details(nodes)

    for index, (timing, records) in enumerate(attributed):
        last_node = index == len(attributed) - 1
        lines.append(f"{'└─' if last_node else '├─'} {_node_line(timing, details)}")
        stem = "   " if last_node else "│  "
        for position, record in enumerate(records):
            last_call = position == len(records) - 1
            lines.extend(_call_lines(record, stem, last_call=last_call))

    for timing, records in attributed:
        if timing.model_calls > len(records):
            # The quiet direction. Every node after this one is now showing somebody else's calls.
            lines.append(
                f"   {timing.node} reports {timing.model_calls} model call(s) and the trace had "
                f"{len(records)} left to give. Every call shown below it belongs to a later node - "
                "see tda.obs.viewer on why this join is arithmetic."
            )

    placed = sum(len(records) for _, records in attributed)
    after_the_run = trace.records[placed:]

    # Calls from an agent that `runs_in="review"` were made after the graph finished, by an officer
    # on the review screen. They have no node and never will, so counting them as unattributable
    # would fire the warning below on every reviewed run and teach a reader to ignore the one line
    # that exists to catch real mis-attribution. They are shown, in their own section, because a
    # question asked about a verdict is part of how it came to be signed.
    reviewed = tuple(record for record in after_the_run if _runs_in_review(record))
    leftover = len(after_the_run) - len(reviewed)

    if reviewed:
        lines.append("")
        lines.append(f"after the run · {len(reviewed)} question(s) on the review screen")
        for position, record in enumerate(reviewed):
            lines.extend(_call_lines(record, "   ", last_call=position == len(reviewed) - 1))

    if leftover > 0:
        # Not dropped silently. See the module docstring: this is what a concurrent graph would
        # look like from here, and a viewer that hid the calls it could not place would let the
        # attribution go wrong quietly.
        lines.append(
            f"   {leftover} model call(s) could not be attributed to a node. The node timings say "
            "fewer calls were made than the trace records - see tda.obs.viewer on why that join is "
            "arithmetic."
        )
    if stopped := ledger.stopped_inside:
        lines.append(f"   stopped inside: {', '.join(stopped)}")
    return "\n".join(lines)


def _header(ledger: RunLedger) -> list[str]:
    """Four lines: what this was, under which rules, on which bytes, and what it cost."""
    reason = f" ({ledger.rejection_reason})" if ledger.rejection_reason else ""
    redactions = ", ".join(f"{name} x{count}" for name, count in ledger.redactions)
    return [
        f"{ledger.run_id}  {ledger.status}{reason}  {ledger.hotel_id}  {ledger.period}",
        f"  policy {ledger.policy_version} · metrics {ledger.metric_library_version} · "
        f"{ledger.model_id} ({ledger.provider_mode})",
        f"  {len(ledger.inputs)} input file(s) · ${ledger.total_cost_usd} "
        f"(rates {ledger.pricing_version}) · {ledger.duration_ms:,}ms",
        f"  redacted: {redactions or 'nothing'}",
    ]


def _node_line(timing: NodeTiming, details: dict[str, str]) -> str:
    """One node: what it did, how long it took, and why it ended that way if it was not `ok`."""
    if timing.entered_only:
        return f"{timing.node:<{NODE_WIDTH}} entered, never left"
    calls = f"  {timing.model_calls} call(s)" if timing.model_calls else ""
    detail = _one_line(details.get(timing.node, ""))
    return (
        f"{timing.node:<{NODE_WIDTH}} {timing.outcome:<9} {timing.duration_ms:>6,}ms{calls}"
        f"{f'  - {detail}' if detail else ''}"
    )


def _call_lines(record: TraceRecord, stem: str, *, last_call: bool) -> list[str]:
    """One model call: which prompt, which recording, what it cost, and what came back."""
    branch = "└─" if last_call else "├─"
    gutter = f"{stem}{'   ' if last_call else '│  '}"
    cassette = f"{record.cassette_key[:8]}…" if record.cassette_key else "-"
    head = (
        f"{stem}{branch} {record.agent}/{record.prompt_version}"
        f"  [{record.provider_mode} {cassette}]"
        f"  {record.input_tokens:,}/{record.output_tokens:,} tok"
        f"  ${_cost(record):.4f}  {record.duration_ms:,}ms"
    )
    lines = [head, f"{gutter}asked for: {record.output_contract}, with {_tools(record)}"]
    if record.error is not None:
        lines.append(f"{gutter}failed: {_one_line(record.error)}")
    else:
        lines.append(f"{gutter}returned: {summarise_output(record.output_json)}")
    if refused := record.refused_tools:
        # A refusal is never a footnote. It means the agent reached outside its allowlist, which is
        # a roster or prompt defect rather than a runtime event.
        lines.append(f"{gutter}REFUSED: {', '.join(refused)}  (outside this agent's allowlist)")
    return lines


def _one_line(text: str) -> str:
    """Prose, flattened and cut to fit a tree. Empty in, empty out."""
    flat = " ".join(text.split())
    if len(flat) <= REASON_LIMIT:
        return flat
    return f"{flat[:REASON_LIMIT].rstrip()}… (full text in the artifact)"


def _tools(record: TraceRecord) -> str:
    """The tools this call actually reached for, with repeats counted.

    Counted rather than listed one per line because a mapping call peeks at every sheet, and
    `peek_headers, peek_headers, peek_headers, peek_headers` says nothing `peek_headers x4` does
    not. A refused call is marked and also reported on its own line.
    """
    if not record.tool_calls:
        return "no tools"
    order: list[str] = []
    counts: dict[str, int] = {}
    for call in record.tool_calls:
        name = call.name if call.allowed else f"{call.name}!"
        if name not in counts:
            order.append(name)
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(name + (f" x{counts[name]}" if counts[name] > 1 else "") for name in order)


def summarise_output(output_json: str | None) -> str:
    """A contract's shape in one line: short scalars shown, everything else counted.

    The elision is the point rather than the brevity — see the module docstring. A field this
    cannot summarise is named with its length, so a reader knows it exists and where to look.

    Public because the Run console reuses it for a live agent card: the same rule that keeps a
    model's free prose off a terminal (`mizan trace`) should keep it off a browser tab too, rather
    than the console re-deciding independently what is safe to print.
    """
    if output_json is None:
        return "nothing"
    try:
        payload = json.loads(output_json)
    except json.JSONDecodeError:
        return f"unreadable JSON ({len(output_json)} chars)"
    if not isinstance(payload, dict):
        return _value(payload)
    fields = [f"{key} {_value(value)}" for key, value in list(payload.items())[:FIELD_LIMIT]]
    if len(payload) > FIELD_LIMIT:
        fields.append(f"+{len(payload) - FIELD_LIMIT} more field(s)")
    return " · ".join(fields) if fields else "an empty object"


def _value(value: object) -> str:
    """One field, as the tree shows it. Lists and objects become counts; long strings become
    lengths; short scalars are printed as they are."""
    if isinstance(value, list):
        return f"[{len(value)}]"
    if isinstance(value, dict):
        return f"{{{len(value)}}}"
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return f'"{value}"' if len(value) <= VALUE_LIMIT else f"…({len(value)} chars)"
    return str(value)


def _runs_in_review(record: TraceRecord) -> bool:
    """Whether this call was made on the review screen rather than inside the graph.

    Read from the roster rather than from a name test here, so "which agents run after the run" has
    one answer in the repository. An agent the roster does not know is not review-time - it is a
    call this viewer genuinely cannot place, and the warning below is the right outcome.
    """
    from tda.agents.roster import ROSTER

    entry = ROSTER.get(record.agent)
    return entry is not None and entry.runs_in == "review"


def _attribute(
    timings: tuple[NodeTiming, ...], trace: TraceLog
) -> list[tuple[NodeTiming, tuple[TraceRecord, ...]]]:
    """Assign each trace record to the node that made it, by consuming `model_calls` in order.

    Exact for a sequential graph, which is what this one is. `render_tree` reports any record left
    over rather than discarding it.
    """
    records = trace.records
    cursor = 0
    assigned: list[tuple[NodeTiming, tuple[TraceRecord, ...]]] = []
    for timing in timings:
        take = min(timing.model_calls, len(records) - cursor)
        assigned.append((timing, records[cursor : cursor + take]))
        cursor += take
    return assigned


def _details(nodes: NodeLog) -> dict[str, str]:
    """Why each node ended as it did, from its exit record. Empty for an ordinary `ok`."""
    return {
        record.node: record.detail
        for record in nodes
        if record.phase is Phase.EXIT and record.detail
    }


def _cost(record: TraceRecord) -> Decimal:
    """What one call cost, under the same rate card the ledger used.

    Routed through `AgentUsage` rather than multiplied here, so there is exactly one place in this
    repository that turns tokens into money. Two would eventually disagree, and a viewer whose
    per-call figures do not add to the ledger's total is a viewer nobody trusts twice.

    One honest caveat: these are per **call** and rounded to four places like everything else, so
    several sub-cent calls can each show `$0.0000` under an agent row that is not zero. The
    authoritative rollup is the per-agent one in `run.json`, and *that* column adds up to its own
    total exactly — see `AgentUsage.cost_usd`.
    """
    return AgentUsage(
        agent=record.agent,
        calls=1,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_read_tokens=record.cache_read_tokens,
        duration_ms=record.duration_ms,
    ).cost_usd


__all__ = ["FIELD_LIMIT", "VALUE_LIMIT", "render_tree", "summarise_output"]
