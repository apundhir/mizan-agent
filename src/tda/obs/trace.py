"""One record per model call: what was asked, what came back, what it cost, and what it touched.

An agentic system you cannot inspect is an agentic system you cannot defend. A verification officer
who cannot answer *"why did it say that?"* will not sign behind the number, and neither will the
person who has to stand next to them.

## Why this lives in `tda.obs` and not in `tda.agents.contracts`

`TraceRecord` carries token counts and a duration. Those are integers, and an integer on a class
deriving from `AgentOutput` is exactly what `tools/guard/agent_schema_lint.py` forbids. Putting the
trace in the contracts package would force a choice between weakening that guard and mislabelling
the data — so it lives here, next to `UsageLedger`, for the same reason usage does. **A record of
what a call cost is not an agent's answer**, and keeping the two apart is what lets the guard stay
strict enough to be worth having.

## What a record must carry, and why each field earns its place

| Field | The question it answers |
|---|---|
| `agent`, `prompt_version` | which instructions produced this |
| `model_id`, `effort`, `provider_mode` | which model, thinking how hard, live or replayed |
| `cassette_key` | which recording — the one identifier that ties a replayed answer to the live call it came from |
| `tool_calls` | what the agent reached for, **refusals included** |
| `output` | the validated contract, as JSON |
| `input_tokens` / `output_tokens` / `duration_ms` | what it cost |
| `error` | why there is no output, when there is none |

`output` is stored as JSON text rather than as the parsed object. The trace is a record, and a
record that holds a live reference to a model instance changes when that instance does. JSON also
means the file is readable by anything, which matters for the trace viewer in observability.

## Refused tool calls are kept

A trace listing only the calls that succeeded cannot answer the question anyone actually asks after
an incident, which is what the agent *tried* to do. `ToolSession` records refusals and they are
copied here verbatim.

## Ordering

`TraceLog` preserves call order and nothing else — no sorting, no deduplication. Two identical
calls are two entries, because they were two calls, and a log that collapses them is a log that
hides a retry loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from tda.obs.repro import REPRO_EXCLUDED

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from tda.agents.tools import ToolCall


class TraceCall(BaseModel):
    """One tool attempt, as the trace stores it.

    A copy of `tda.agents.tools.ToolCall` rather than that class itself: the trace is serialised
    and outlives the run, and a dataclass carrying a bound callable's name is not the same kind of
    thing as a record. `allowed=False` entries are the interesting ones.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    allowed: bool


class TraceRecord(BaseModel):
    """One model call, start to finish.

    Frozen: a trace entry is a statement about something that already happened, and a mutable one
    invites a later stage to "correct" it, which is how a trace stops being evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str = Field(min_length=1)
    prompt_version: str = Field(pattern=r"^v\d+$")
    model_id: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    provider_mode: str = Field(min_length=1, examples=["replay", "anthropic", "stub"])
    cassette_key: str = Field(
        default="",
        description="Ties a replayed answer to the live call it was recorded from. Empty only "
        "when the call failed before a request was built.",
    )
    output_contract: str = Field(
        min_length=1, description="The output type's name, so a stale cassette is legible later."
    )
    tool_calls: tuple[TraceCall, ...] = ()
    output_json: str | None = Field(
        default=None,
        description="The validated contract, serialised. None when the call failed.",
    )
    error: str | None = Field(
        default=None,
        description="Why there is no output. A record with neither output nor error is a bug, "
        "and the validator below says so.",
    )
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(
        default=0, ge=0, description=f"Wall clock for this call. {REPRO_EXCLUDED}"
    )

    def model_post_init(self, _context: object, /) -> None:
        """A record says either what the agent answered or why it did not. Never neither.

        A trace entry with both fields empty is the shape a swallowed exception takes, and it reads
        as a successful call that happened to return nothing.
        """
        if self.output_json is None and self.error is None:
            raise ValueError(
                f"trace record for {self.agent!r} has neither an output nor an error. "
                "A call either produced a validated contract or failed for a stated reason; "
                "a record of neither is a swallowed exception wearing a trace's clothes."
            )

    @property
    def refused_tools(self) -> tuple[str, ...]:
        """Tools this agent reached for and was denied. Empty in the ordinary case, and a roster
        or prompt defect whenever it is not."""
        return tuple(call.name for call in self.tool_calls if not call.allowed)

    @property
    def failed(self) -> bool:
        return self.error is not None

    def render(self) -> str:
        """One line, for a console run and for the failure message when a test asserts on a trace."""
        outcome = f"error={self.error}" if self.failed else self.output_contract
        tools = ",".join(f"{c.name}{'' if c.allowed else '!'}" for c in self.tool_calls)
        return (
            f"{self.agent}/{self.prompt_version} {outcome} "
            f"[{self.provider_mode} key={self.cassette_key[:8] or '-'}] "
            f"tools=({tools or '-'}) "
            f"tok={self.input_tokens}/{self.output_tokens} {self.duration_ms}ms"
        )


def trace_calls(calls: Iterable[ToolCall]) -> tuple[TraceCall, ...]:
    """Convert the runtime's tool calls into their recorded form, order preserved."""
    return tuple(TraceCall(name=call.name, allowed=call.allowed) for call in calls)


class TraceLog:
    """Every record from one run, in call order.

    A list with a name, and the name is the point: passing `list[TraceRecord]` around invites
    someone to sort it. See the module docstring on why order is the whole content.
    """

    def __init__(self, records: Iterable[TraceRecord] = ()) -> None:
        self._records: list[TraceRecord] = list(records)

    def append(self, record: TraceRecord) -> None:
        self._records.append(record)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[TraceRecord]:
        return iter(self._records)

    def __getitem__(self, index: int) -> TraceRecord:
        return self._records[index]

    @property
    def records(self) -> tuple[TraceRecord, ...]:
        return tuple(self._records)

    def for_agent(self, agent: str) -> tuple[TraceRecord, ...]:
        return tuple(record for record in self._records if record.agent == agent)

    def failures(self) -> tuple[TraceRecord, ...]:
        return tuple(record for record in self._records if record.failed)

    def prompt_versions(self) -> dict[str, str]:
        """`{agent: version}`, for stamping into the verdict (D-EV-04).

        Raises if one agent ran under two prompt versions in a single run. That should be
        impossible — the version comes from `policy.yaml` and policy does not change mid-run — and
        if it ever happens, a verdict claiming one version would be citing instructions that
        produced only some of its findings.
        """
        versions: dict[str, str] = {}
        for record in self._records:
            existing = versions.setdefault(record.agent, record.prompt_version)
            if existing != record.prompt_version:
                raise ValueError(
                    f"agent {record.agent!r} ran under both {existing} and "
                    f"{record.prompt_version} in one run. A verdict can only cite one prompt "
                    "version per agent, and citing either would be citing instructions that "
                    "produced some of its findings and not others."
                )
        return versions

    def to_jsonl(self) -> str:
        """The on-disk form: one JSON object per line, in call order.

        JSON Lines rather than a JSON array so a trace can be written as the run proceeds and is
        still readable if the run dies halfway - which is exactly when someone wants to read it.
        observability puts this at `artifacts/<run_id>/trace.jsonl`.
        """
        return "".join(record.model_dump_json() + "\n" for record in self._records)

    @classmethod
    def from_jsonl(cls, text: str) -> TraceLog:
        lines: Sequence[str] = [line for line in text.splitlines() if line.strip()]
        return cls(TraceRecord.model_validate_json(line) for line in lines)

    def render(self) -> str:
        return "\n".join(record.render() for record in self._records)
