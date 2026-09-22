"""Writing a run down: `artifacts/<run_id>/`, redacted on the way out.

Four files, and each answers a different question:

| File | The question |
|---|---|
| `run.json` | what was this run looking at, under which rules, and what did it cost? |
| `trace.jsonl` | what did each agent get asked and what did it answer? |
| `nodes.jsonl` | what did the pipeline do, in order, and where did it stop? |
| `routing.jsonl` | which agent was asked to run, which was skipped or refused, and why? |

The fourth is younger than the other three (v0.6.0, when the supervisor was wired into the
pipeline) and optional on read for exactly that reason: a run written before it existed has nothing
wrong with it, only nothing to say here. See `tda.obs.routing`.

`verdict.json` is **not** here. That is the outputs's, and the split is deliberate: the verdict is the
thing an officer signs behind, and the artifacts are its working. Putting them in one writer would
make the evidence and the conclusion move together whenever either changed.

## Everything is redacted before it is written

Not after, and not on read. `tda.obs.redact` explains what the exposure actually is — the submitted
workbook's label cells reach the mapping agent's prompt verbatim, and can come back out through a
free-prose contract field — and why the answer is redaction rather than refusal. The counts go in
`run.json`; the content goes nowhere.

## Byte-stability, and the fields that are allowed to move

`make repro` runs the pipeline twice and diffs. Everything written here is byte-stable
between two replay runs **except** the fields `RunLedger.volatile_fields()` names — which is read
off the field descriptions, so it cannot drift out of step with the model.

One consequence worth stating for whoever builds that target: the run id is in the *path*, so two
runs of the same submission write to two different directories. A diff harness compares by
basename, not by path.

## JSON Lines, again

`trace.jsonl` and `nodes.jsonl` are written line by line rather than as arrays, for the reason
`tda.obs.trace` gives: a run that dies halfway leaves a readable file, and that is exactly when
somebody reads it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tda.obs.redact import Redaction, redact, redact_lines, scan

if TYPE_CHECKING:
    from pathlib import Path

    from tda.obs.ledger import RunLedger
    from tda.obs.nodes import NodeLog
    from tda.obs.trace import TraceLog

from tda.obs.routing import ROUTING_LOG, RoutingLog

RUN_LEDGER = "run.json"
AGENT_TRACE = "trace.jsonl"
NODE_LOG = "nodes.jsonl"


@dataclass(frozen=True, slots=True)
class WrittenRun:
    """Where a run's artifacts landed, and what was removed on the way.

    `redaction` is returned rather than only recorded, because the caller — `mizan run` — is the
    one place that can tell a human *now* that something was stripped. A count buried in a JSON
    file nobody opens is not a disclosure.

    `as_written` is the ledger **as it reached the disk**, redactions applied. It exists because
    `mizan run` used to print the in-memory ledger it had just written: the file said
    `[redacted:email]` and the terminal said the address. A caller that prints what it handed to
    this function is printing the one copy that was never redacted, so this hands back the other
    one and there is nothing else to print.
    """

    directory: Path
    ledger: Path
    trace: Path
    nodes: Path
    redaction: Redaction
    as_written: RunLedger
    routing: Path | None = None

    @property
    def files(self) -> tuple[Path, ...]:
        base = (self.ledger, self.trace, self.nodes)
        return (*base, self.routing) if self.routing is not None else base

    def render(self) -> str:
        lines = [f"  artifacts: {self.directory}"]
        if not self.redaction.clean:
            lines.append(
                f"  redacted from the artifacts: {self.redaction.render()}"
                "  (personal data in the submitted workbook, not from this system)"
            )
        return "\n".join(lines)


def write_run(
    root: Path,
    ledger: RunLedger,
    trace: TraceLog,
    nodes: NodeLog,
    routing: RoutingLog | None = None,
) -> WrittenRun:
    """Write one run's artifacts under `root/<run_id>/`, redacting as it goes.

    A reader opening `run.json` should learn that the trace was redacted without having to read
    the trace to find out, so `redactions` carries the combined count across every file written.

    The ledger's own redactions are counted **before** it is stamped, which is not a detail. An
    earlier version stamped the trace and node counts and then redacted the serialised ledger, so a
    `run.json` containing `[redacted:email]` reported `"redactions": []`. It was telling the one
    reader the field exists for that nothing had been removed, while they were looking at the
    placeholder. The count of what leaves must include the file doing the counting.

    `routing` is optional and keyword-compatible rather than required, so a caller that has not
    wired a supervisor (there is none yet outside `tda.graph`) still writes the three files this
    function always wrote.
    """
    directory = root / ledger.run_id
    directory.mkdir(parents=True, exist_ok=True)

    trace_lines, trace_redaction = redact_lines(trace.to_jsonl().splitlines())
    node_lines, node_redaction = redact_lines(nodes.to_jsonl().splitlines())
    routing_lines, routing_redaction = (
        redact_lines(routing.to_jsonl().splitlines()) if routing is not None else ((), Redaction())
    )

    from_files = _merge(_merge(trace_redaction, node_redaction), routing_redaction)
    total = _merge(from_files, scan(_as_json(ledger)))
    stamped = ledger.model_copy(update={"redactions": total.counts})
    ledger_text, after_stamping = redact(_as_json(stamped))
    _check_stamp_is_inert(total, from_files, after_stamping)

    ledger_path = directory / RUN_LEDGER
    trace_path = directory / AGENT_TRACE
    nodes_path = directory / NODE_LOG

    ledger_path.write_text(ledger_text, encoding="utf-8")
    trace_path.write_text(_as_jsonl(trace_lines), encoding="utf-8")
    nodes_path.write_text(_as_jsonl(node_lines), encoding="utf-8")

    routing_path = None
    if routing is not None:
        routing_path = directory / ROUTING_LOG
        routing_path.write_text(_as_jsonl(list(routing_lines)), encoding="utf-8")

    from tda.obs.ledger import RunLedger as _RunLedger

    return WrittenRun(
        directory=directory,
        ledger=ledger_path,
        trace=trace_path,
        nodes=nodes_path,
        redaction=total,
        as_written=_RunLedger.model_validate_json(ledger_text),
        routing=routing_path,
    )


def _as_json(ledger: RunLedger) -> str:
    """The ledger's on-disk form. Sorted keys, so two runs of one submission are byte-comparable."""
    return (
        json.dumps(ledger.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n"
    )


def _check_stamp_is_inert(
    total: Redaction, from_files: Redaction, after_stamping: Redaction
) -> None:
    """The stamped counts must not themselves contain anything redactable.

    They are kind names and integers, so this cannot fire. That is exactly why it is worth
    asserting rather than assuming: if it ever does, `run.json` is under-reporting what was removed
    from it, and a silent under-report is the failure this module exists to prevent.

    `from_files` is every redaction found across the sibling files (trace, nodes and, when present,
    routing) merged into one. Callers pass whatever set of files they actually wrote.
    """
    declared = dict(total.counts)
    for name, count in from_files.counts:
        declared[name] = declared.get(name, 0) - count
    ledger_only = {name: count for name, count in declared.items() if count}
    if ledger_only != dict(after_stamping.counts):
        raise ValueError(
            "the run ledger's redaction count changed when the counts were stamped into it. "
            f"Before: {ledger_only}, after: {dict(after_stamping.counts)}. `run.json` would "
            "under-report what was removed from it, which is the one thing its `redactions` field "
            "exists to prevent."
        )


def read_run(directory: Path) -> tuple[RunLedger, TraceLog, NodeLog]:
    """Read a run's artifacts back. What `mizan trace` uses.

    Reads what was written, redactions included — there is no unredacted copy anywhere, which is
    the point. A viewer that could recover the original would be a second place the data lives.

    **A missing `trace.jsonl` raises rather than reading as an empty one.** `write_run` always
    writes all three files, so an absent one means the directory was truncated, partially copied or
    tampered with — and "the evidence file is gone" must not render identically to "this run made
    no model calls". In a system whose case rests on being inspectable, those two are opposites.
    """
    from tda.obs.ledger import RunLedger
    from tda.obs.nodes import NodeLog
    from tda.obs.trace import TraceLog

    ledger_path = directory / RUN_LEDGER
    if not ledger_path.is_file():
        raise FileNotFoundError(
            f"no {RUN_LEDGER} in {directory}. A run directory is named for its run id - "
            "`artifacts/<run_id>/` - so check the id rather than the path shape."
        )
    ledger = RunLedger.model_validate_json(ledger_path.read_text(encoding="utf-8"))
    trace = TraceLog.from_jsonl(_read_required(directory / AGENT_TRACE))
    nodes = NodeLog.from_jsonl(_read_required(directory / NODE_LOG))
    return ledger, trace, nodes


def read_routing(directory: Path) -> RoutingLog:
    """A run's routing decisions, or an empty log for a run written before v0.6.0.

    Kept separate from `read_run` rather than folded into its tuple, so every existing caller of
    `read_run` keeps working unchanged and a fourth return value does not silently need unpacking
    everywhere it is called. Unlike `trace.jsonl` and `nodes.jsonl`, an absent `routing.jsonl` is not
    a sign of a truncated directory: it means the run predates the file, and that is a fact worth
    reading as "nothing recorded" rather than as an error.
    """
    path = directory / ROUTING_LOG
    if not path.is_file():
        return RoutingLog()
    return RoutingLog.from_jsonl(path.read_text(encoding="utf-8"))


def latest_run(root: Path) -> Path | None:
    """The most recently written run directory, or `None`.

    By modification time rather than by name, because a run id is meaningless by design and sorting
    them would order runs by a hash. `mizan trace` with no argument uses this, which is what
    somebody wants immediately after `make run`.
    """
    if not root.is_dir():
        return None
    directories = [d for d in root.iterdir() if d.is_dir() and (d / RUN_LEDGER).is_file()]
    # The name breaks a tie. Two runs can land in the same filesystem timestamp, and `max` over
    # `iterdir` would then resolve it by inode order - so the same two runs answer this question
    # differently on two machines, and `mizan trace` stops being reproducible for a reason nobody
    # can see.
    return max(directories, key=lambda d: (d.stat().st_mtime, d.name), default=None)


def _as_jsonl(lines: list[str]) -> str:
    return "".join(f"{line}\n" for line in lines if line.strip())


def _read_required(path: Path) -> str:
    """Read one of a run's JSON Lines files, or say which one is missing.

    An empty file is fine and means what it says — a run with no model calls writes an empty
    `trace.jsonl`. An *absent* file is not the same statement, and returning `""` for both would
    make a truncated run directory render as a clean one.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"{path.name} is missing from {path.parent}. A run writes all three artifacts "
            "together, so an absent one means the directory is incomplete - and an incomplete run "
            "must not read as a run that simply made no model calls."
        )
    return path.read_text(encoding="utf-8")


def _merge(first: Redaction, second: Redaction) -> Redaction:
    totals = dict(first.counts)
    for name, count in second.counts:
        totals[name] = totals.get(name, 0) + count
    return Redaction(counts=tuple(sorted(totals.items())))
