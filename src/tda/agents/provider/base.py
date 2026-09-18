"""The provider boundary: one request shape, one response shape, one cassette key.

This is where a non-deterministic model sits inside a system whose entire claim is
reproducibility. Three mechanisms carry that, and none of them is `temperature`:

1. **Structured output on every call.** Every request names a Pydantic output contract, and the
   response is that contract or an error. Free text never enters the numeric path — it cannot,
   because the numeric path accepts only typed record objects.
2. **A canonical cassette key.** Every call is identified by a SHA-256 over canonical JSON of
   everything that could change the answer. Committed cassettes then make CI offline, free and
   byte-identical.
3. **A pinned model id and prompt version**, recorded per call and stamped into the verdict.

`temperature` is **absent on purpose**: it is rejected with HTTP 400 on current Claude models,
and it never guaranteed determinism even when it was accepted. See
`docs/adr/0002-model-layer.md`, and the `policy.schema.json` adversarial case that rejects a
policy trying to reintroduce it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

if TYPE_CHECKING:
    from pydantic import BaseModel

OutputT = TypeVar("OutputT", bound="BaseModel")

# The cassette key format version. Bumping it invalidates every committed cassette, which is
# sometimes correct (the hashed inputs changed shape) and is always a deliberate act: it means
# `make record` has to be re-run and every cassette diff reviewed.
KEY_SCHEMA_VERSION = 1


class Effort(StrEnum):
    """Reasoning depth, per agent.

    `LOW` for classification tasks with a small answer space — mapping a sheet to a metric,
    resolving a label to a country code. `HIGH` for writing and judgement, where quality is
    visible to the officer. Set per agent in `policy.yaml` so the cost profile is legible
    rather than buried in call sites.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ProviderMode(StrEnum):
    """Which adapter is in play. Recorded in every run ledger, because a verdict produced
    against a live model and one produced from cassettes are different kinds of evidence."""

    REPLAY = "replay"
    ANTHROPIC = "anthropic"
    STUB = "stub"
    RECORD = "record"


class ProviderError(Exception):
    """Base for every failure in this layer."""


class CassetteMissError(ProviderError):
    """A replay run needed a cassette that is not committed.

    **This is a hard error, and never a fall-through to a live call.** A replay mode that quietly
    goes to the network when it misses is worse than no replay mode at all: CI would pass, cost
    money, and stop being reproducible, all without a single line of output saying so.
    """

    def __init__(self, key: str, agent: str, available: int) -> None:
        super().__init__(
            f"no cassette for {agent} key={key}\n"
            f"  {available} cassette(s) are committed for this agent, none matching.\n"
            f"  A replay run never calls the live API. Either the prompt, the output schema or\n"
            f"  the rendered messages changed - run `make record` and review the cassette diff."
        )
        self.key = key
        self.agent = agent


class OutputValidationError(ProviderError):
    """The model returned something that is not the declared contract.

    An error, never a silent fall-back to free text. The whole point of declaring a contract is
    that the failure is loud here rather than shaped like a plausible value three layers down.
    """


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation turn. Deliberately minimal — this layer carries text, not tool calls."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Everything that can change an answer, and nothing that cannot.

    What is *in* here is hashed into the cassette key. What is deliberately **out**: timestamps,
    run ids, request ids, retry counts. Including any of those would make every call a fresh key
    and every replay a miss, which would look like a reproducibility feature while destroying it.
    """

    agent: str
    prompt_version: str
    system: str
    messages: tuple[Message, ...]
    output_schema: dict[str, Any]
    model_id: str
    effort: Effort
    max_tokens: int = 16_000

    def canonical(self) -> str:
        """The exact bytes that get hashed.

        Sorted keys, no insignificant whitespace, explicit `ensure_ascii=False` so a non-ASCII
        country name hashes as itself rather than as an escape sequence whose form could vary.
        Stability across processes is asserted by a test that shells out to a fresh interpreter,
        because a key that is only stable within one process is not stable.
        """
        payload = {
            "key_schema_version": KEY_SCHEMA_VERSION,
            "agent": self.agent,
            "prompt_version": self.prompt_version,
            "system": self.system,
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            "output_schema": self.output_schema,
            "model_id": self.model_id,
            "effort": self.effort.value,
            "max_tokens": self.max_tokens,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def cassette_key(self) -> str:
        """A short hex digest. 16 bytes is ample for a per-agent namespace and keeps the
        filenames readable, which matters because a human reviews cassette diffs."""
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What a call cost. Lives here rather than in an `AgentOutput` because it describes the
    call, not the answer — see `tda.agents.contracts.base` for why that distinction is load-bearing."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A validated output, plus what it took to get it."""

    parsed: Any
    raw_json: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    cassette_key: str = ""
    mode: ProviderMode = ProviderMode.STUB
    duration_ms: int = 0


class LLMProvider(Protocol):
    """The only surface the agent runtime talks to.

    Four adapters implement it: `anthropic` (live), `replay` (committed cassettes, the default),
    `stub` (hand-written, for unit tests) and `record` (live + writes cassettes). Swapping them is
    a config change, which is what keeps the pinned-region deployment question out of the code.
    """

    mode: ProviderMode

    def complete(self, request: ModelRequest, output_type: type[OutputT]) -> ModelResponse:
        """Return a `ModelResponse` whose `parsed` is an instance of `output_type`.

        Raises `OutputValidationError` if the model's answer is not that contract, and
        `CassetteMissError` if a replay run has no cassette. Never returns free text, and never
        returns `None`.
        """
        ...
