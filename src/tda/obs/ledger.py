"""The run ledger: everything needed to answer *"why did it say that?"* without a debugger.

A verdict says what the system concluded. The ledger says **what it was looking at and under which
rules** — the files by digest, the policy version, the metric library version, the model and the
mode it ran in, the prompt versions, what each node took, and what the whole thing cost.

An agentic system you cannot inspect is an agentic system you cannot defend. A verification officer
who has to take the number on trust will not sign behind it, and neither will the person standing
next to them.

## Why the input digests matter more than they look

`inputs` records a SHA-256 per submitted file. That is the difference between *"the run says
occupancy was 71.2%"* and *"the run says occupancy was 71.2% for **these exact bytes**"*. Without
it, a verdict and a file set can drift apart silently — somebody re-exports the workbook, the
numbers change, and nothing in the record says the two verdicts were about different documents.

It is also what makes a challenge answerable. A hotel disputing a finding can be shown the digest
of the file the finding was computed from.

## What varies between runs, and why that is stated rather than hidden

`run_id`, `started_at`, `finished_at` and every `duration_ms` differ on every run of the same
submission. `make repro` (PRD-94) compares two runs and must exclude them — so they are named here,
in `VOLATILE_FIELDS`, rather than left for that story to rediscover by diffing and being surprised.

Everything else is byte-stable by construction: `UsageLedger.per_agent()` sorts, `prompt_versions`
is a dict built from a sorted trace, and `inputs` is sorted by path.

## Runtime is recorded, not targeted

`duration_ms` exists so a future pilot has a starting point. **There is no target, no threshold and
no assertion anywhere on how long a run takes**, and there will not be one until somebody measures
the manual baseline. The PRD is explicit that no time-saving figure appears in this repository,
because a number used before it is measured gets challenged — and the challenge lands on the whole
result rather than on the number.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from tda.obs.nodes import NodeLog, NodeRecord, Phase
from tda.obs.repro import REPRO_EXCLUDED, volatile_paths
from tda.obs.usage import PRICING_VERSION, AgentUsage, UsageLedger

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path


# Matches `corpus/demo/manifest.json` and `tools/datagen/reproducible.py` — `sha256:` plus the full
# 64 hex characters. A reader meets one convention rather than two, and an unlabelled hex string in
# an artifact is a small mystery for whoever has to verify it later.
DIGEST_PREFIX: Final = "sha256:"


def file_digest(path: Path) -> str:
    """`sha256:<hex>` for one file, read in chunks.

    Chunked because a submitted PDF is a few megabytes and reading it whole to hash it is a
    needless copy — not because anything here is large enough to matter yet, but because the
    obvious next step for this system is bigger files.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"{DIGEST_PREFIX}{digest.hexdigest()}"


class InputFile(BaseModel):
    """One submitted file, by name and by digest.

    `name` rather than an absolute path: where a file sat on the machine that ran the verification
    is not evidence about anything, and a ledger full of `/home/someone/tmp/...` is a ledger that
    leaks the environment into the record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bytes: int = Field(ge=0)

    @classmethod
    def of(cls, path: Path) -> InputFile:
        return cls(name=path.name, sha256=file_digest(path), bytes=path.stat().st_size)


class NodeTiming(BaseModel):
    """What one node took, and what it spent doing it.

    A flattened view of the two `NodeRecord`s that node produced. The records themselves stay in
    `NodeLog` and reach `trace.jsonl`; this is the summary a reader wants in the ledger, where the
    question is "where did the time go?" rather than "what happened in order?".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    duration_ms: int = Field(default=0, ge=0, description=f"Wall clock. {REPRO_EXCLUDED}")
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    entered_only: bool = Field(
        default=False,
        description="True for a node that was entered and never left - the run stopped inside it. "
        "The one field in this summary that a reader of a broken run looks for first.",
    )


class RunLedger(BaseModel):
    """One run, fully described.

    Frozen. A ledger is a statement about a run that has finished, and a mutable one invites a
    later stage to improve it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(
        min_length=1,
        description=f"Unique and meaningless - see `new_run_id`. {REPRO_EXCLUDED}",
    )
    status: str = Field(min_length=1)
    rejection_reason: str | None = None

    hotel_id: str = Field(min_length=1)
    period: str = Field(min_length=1)

    # ── which rules produced this ────────────────────────────────────────────
    policy_version: str = Field(min_length=1)
    metric_library_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    provider_mode: str = Field(min_length=1)
    prompt_versions: dict[str, str] = Field(default_factory=dict)

    # ── which bytes it looked at ─────────────────────────────────────────────
    inputs: tuple[InputFile, ...] = ()

    # ── where the time and the money went ────────────────────────────────────
    nodes: tuple[NodeTiming, ...] = ()
    usage: tuple[AgentUsage, ...] = ()
    pricing_version: str = PRICING_VERSION
    total_cost_usd: str = Field(
        default="0.0000",
        description="A string, not a float. A cost rolled up in binary floating point drifts in "
        "the cents, and a figure that does not reconcile is worse than no figure.",
    )
    redactions: tuple[tuple[str, int], ...] = Field(
        default=(),
        description="What `tda.obs.redact` removed from this run's artifacts, by kind and count - "
        "never the content. A reader opening this file learns that the trace was redacted without "
        "having to read the trace to find out.",
    )
    duration_ms: int = Field(
        default=0,
        ge=0,
        description="Recorded, never targeted. There is no threshold on this anywhere, and there "
        f"will not be one until somebody measures the manual baseline. {REPRO_EXCLUDED}",
    )

    @classmethod
    def volatile_fields(cls) -> frozenset[str]:
        """Every field that legitimately differs between two runs of the same submission.

        Read off the field descriptions rather than kept beside them, so the list cannot drift out
        of step with the model it describes. `make repro` (PRD-94) compares two runs and excludes
        exactly these; anything else differing between two replay runs is a reproducibility defect
        rather than an expected variance.

        **Nested fields are included, as dotted paths.** The first version stopped at the top level
        and was wrong in the way that matters: `nodes.duration_ms` and `usage.duration_ms` are wall
        clock, differ on every run, and would have made the repro diff fail on every single run
        until somebody deleted the check. See `tda.obs.repro`.
        """
        return volatile_paths(cls)

    @property
    def stopped_inside(self) -> tuple[str, ...]:
        """Nodes entered and never left. Empty in a healthy run."""
        return tuple(n.node for n in self.nodes if n.entered_only)

    def render(self) -> str:
        """The ledger as a person reads it, for the end of `make run`."""
        lines = [
            f"  policy {self.policy_version}  metrics {self.metric_library_version}"
            f"  model {self.model_id} ({self.provider_mode})",
            f"  inputs: {len(self.inputs)} file(s)",
            *(f"    {f.name}  {f.sha256[:19]}…  {f.bytes:,} bytes" for f in self.inputs),
        ]
        return "\n".join(lines)


def timings_from(nodes: NodeLog) -> tuple[NodeTiming, ...]:
    """Flatten a node log into one row per node, in the order the run entered them.

    A node entered and never left still produces a row, marked `entered_only`. Dropping it would
    lose the single most useful fact about a broken run — and a ledger whose node list simply stops
    early looks the same as one whose run ended cleanly and early.
    """
    rows: list[NodeTiming | None] = []
    open_rows: dict[str, list[int]] = {}

    for record in nodes:
        if record.phase is Phase.ENTER:
            open_rows.setdefault(record.node, []).append(len(rows))
            rows.append(None)
            continue
        waiting = open_rows.get(record.node)
        if waiting:
            rows[waiting.pop()] = _completed(record)
        else:
            # An exit with no entry. It should be impossible, and dropping it would make the node
            # durations quietly fail to add up to the run's - so it gets a row of its own, and a
            # reader checking that arithmetic finds out from the ledger rather than from a
            # subtraction that does not come out.
            rows.append(_completed(record))

    return tuple(
        row if row is not None else _unfinished(name)
        for row, name in zip(rows, _names_for(rows, open_rows), strict=True)
    )


def _completed(record: NodeRecord) -> NodeTiming:
    return NodeTiming(
        node=record.node,
        outcome=record.outcome.value,
        duration_ms=record.duration_ms,
        model_calls=record.model_calls,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
    )


def _unfinished(node: str) -> NodeTiming:
    return NodeTiming(node=node, outcome="entered", entered_only=True)


def _names_for(rows: list[NodeTiming | None], open_rows: dict[str, list[int]]) -> list[str]:
    """The node name for every slot, including the slots no exit ever filled."""
    names = [row.node if row is not None else "" for row in rows]
    for node, positions in open_rows.items():
        for position in positions:
            names[position] = node
    return names


def cost_summary(usage: UsageLedger) -> str:
    """Per agent and per run, with the rate card that produced it.

    The pricing version is stamped beside the figure because prices are a **configured input, not
    a fact**: a cost without the rate card that generated it cannot be reconciled later.
    """
    per_agent = usage.per_agent()
    if not per_agent:
        return f"  no model calls  ($0.0000, rates {PRICING_VERSION})"
    # The rows and the total are both `AgentUsage.cost_usd`, which rounds once - so the column
    # adds up to the figure printed under it. See that property for why that is not a detail.
    lines = [
        f"  {u.agent:<18} {u.calls:>3} call(s)  "
        f"{u.input_tokens:>7,} in / {u.output_tokens:>6,} out  ${u.cost_usd:.4f}"
        for u in per_agent
    ]
    lines.append(
        f"  {'total':<18} {usage.total_calls():>3} call(s)  "
        f"{'':>7} {'':>6}      ${usage.total_cost_usd():.4f}  (rates {PRICING_VERSION})"
    )
    return "\n".join(lines)


def build_ledger(
    *,
    run_id: str,
    status: str,
    rejection_reason: str | None,
    hotel_id: str,
    period: str,
    policy_version: str,
    metric_library_version: str,
    model_id: str,
    provider_mode: str,
    prompt_versions: dict[str, str],
    inputs: Sequence[Path] | Iterable[Path],
    nodes: NodeLog,
    usage: UsageLedger,
    duration_ms: int,
) -> RunLedger:
    """Assemble the ledger. Every argument is a fact the caller already holds.

    Keyword-only and explicit rather than taking a `RunResult`: `tda.obs` must not import
    `tda.graph`. Observability describes a run; it does not depend on the thing being run, and the
    day it does is the day a change to the pipeline can silently change the record of the pipeline.
    """
    present = [path for path in inputs if path.exists()]
    return RunLedger(
        run_id=run_id,
        status=status,
        rejection_reason=rejection_reason,
        hotel_id=hotel_id,
        period=period,
        policy_version=policy_version,
        metric_library_version=metric_library_version,
        model_id=model_id,
        provider_mode=provider_mode,
        prompt_versions=dict(sorted(prompt_versions.items())),
        # Sorted by name so the ledger is byte-stable across runs and `make repro` compares the
        # pipeline rather than directory iteration order.
        inputs=tuple(InputFile.of(path) for path in sorted(present, key=lambda p: p.name)),
        nodes=timings_from(nodes),
        usage=tuple(usage.per_agent()),
        total_cost_usd=f"{usage.total_cost_usd():.4f}",
        duration_ms=duration_ms,
    )
