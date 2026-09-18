"""What an agent *is*, structurally, when you are accountable for its output.

An agent here is exactly five things and nothing else:

1. a **versioned prompt**, loaded from a file and stamped into the cassette key;
2. a **typed output contract** deriving from `AgentOutput`, which forbids numeric fields;
3. a **narrow tool allowlist**, enforced at call time by `tda.agents.tools`;
4. an **eval set**, under `tests/eval/agents/`;
5. a **trace record**, written for every call including the ones that fail.

Anything calling itself an agent without all five is a prompt with ambitions. Four of the five are
in `AgentSpec` below — as required constructor arguments, so constructing an agent without them is
a type error under `mypy --strict` rather than a runtime surprise. The fifth, the eval set, cannot
be a constructor argument; it is held by the test suite, and `tests/eval/agents/test_eval.py`
asserts that every agent in the roster has one.

## What the runner does, and the shorter list of what it does not

`AgentRunner.run()` renders the system prompt, builds the `ModelRequest`, calls the provider,
checks that what came back is the declared type, writes the trace record and tallies the usage. It
does **not** decide whether the answer is any good. That is the caller's job, and keeping the two
apart is deliberate: a module that both makes a call and decides what to believe about the answer
is a module where the second half quietly softens under pressure from the first. `tda.excel.run` is
the worked example — it takes the mapping agent's answer unchecked and applies every guarantee
itself.

## Why the trace is written even when the call fails

A failed call is the one anybody will want to read about later. If the trace were written only on
success, a run that ended in a cassette miss or a contract violation would leave behind a trace
that is silent about the thing that went wrong, which is the opposite of what a trace is for. So
the runner catches, records, and re-raises. It never swallows.

## Handoffs carry keys and references, never numbers

`AgentSpec.output_type` is bound to `AgentOutput`, so the schema lint applies to every contract any
agent can return. The mapping agent hands on a cell range, not the value in it; the resolution
agent hands on a country code, not a guest count. This is what makes the deterministic-core claim
survive contact with a six-agent system, and it is enforced by `tools/guard/agent_schema_lint.py`
rather than by reviewer vigilance.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tda.agents.contracts.base import AgentOutput
from tda.agents.prompts.registry import PromptRegistry
from tda.agents.provider.base import Effort, Message, ModelRequest
from tda.agents.roster import entry_for
from tda.agents.tools import ToolRegistry, ToolSession
from tda.obs.trace import TraceCall, TraceLog, TraceRecord, trace_calls

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.agents.provider.base import LLMProvider
    from tda.obs.usage import UsageLedger
    from tda.policy import Policy


# The same shape `PromptRegistry` enforces. Duplicated rather than imported so a spec can be
# validated without loading a prompt from disk — which is exactly the situation where the
# duplication pays: a bad version is caught before anything tries to record it.
_VERSION = re.compile(r"^v\d+$")


class AgentError(Exception):
    """A failure in the runtime, as distinct from a failure in the provider.

    `ProviderError` means the model layer could not produce an answer — a cassette miss, a
    validation failure. `AgentError` means the runtime would not accept one.
    """


@dataclass(frozen=True, slots=True)
class AgentSpec[ContractT: AgentOutput]:
    """The four declarable parts of an agent. No defaults, on purpose.

    The type parameter is bound to `AgentOutput` rather than to `BaseModel`, and the bound is the
    point: it is what makes "an agent returns a contract the schema lint has checked" a statement
    the type checker enforces, rather than a convention that holds until someone is in a hurry.

    Every field is required, so `AgentSpec(name="narrative")` does not type-check and does not run.
    That is the whole reason this is a dataclass with no defaults rather than a builder or a
    dictionary: an agent missing its output contract, or its effort setting, should be impossible
    to construct rather than merely discouraged.

    `tools` is not taken from the caller in the ordinary path — `from_policy` reads it from the
    roster, so widening an allowlist is a diff in `roster.py` and shows up in review as one. The
    field is still explicit here because a spec that hid where its permissions came from would
    defeat the point of having them written down.
    """

    name: str
    prompt_version: str
    output_type: type[ContractT]
    tools: frozenset[str]
    effort: Effort

    def __post_init__(self) -> None:
        """Reject a nonsense name or version here rather than three frames down.

        Not tidiness. `TraceRecord.prompt_version` is pattern-constrained, so a spec carrying a
        malformed version would make the *failure* trace fail to write — and the exception from
        that write would mask the original error, which is the one worth reading. Catching it at
        construction means the trace can always record why a call went wrong.
        """
        if not self.name:
            raise AgentError(
                "an agent needs a name; it keys the prompt, the cassettes and the trace"
            )
        if not _VERSION.match(self.prompt_version):
            raise AgentError(
                f"agent {self.name!r} has prompt_version {self.prompt_version!r}; it must look "
                "like v1. A version that cannot be recorded is a version no verdict can cite."
            )

    @staticmethod
    def from_policy[T: AgentOutput](
        name: str, output_type: type[T], policy: Policy
    ) -> AgentSpec[T]:
        """Build a spec for one agent, with its settings taken from the ruleset.

        `effort` and `prompt_version` come from `policy.model.agents[name]` rather than from
        defaults in code, so the cost profile of a verification is legible in the same file as its
        rules. The allowlist comes from the roster rather than from policy, and the split is
        deliberate: effort is a *tuning* decision a reader may reasonably change, and a tool
        allowlist is a *security* boundary that should not be editable from a YAML file shipped
        alongside the data it governs.
        """
        settings = policy.model.agents.get(name)
        if settings is None:
            raise AgentError(
                f"policy {policy.version} has no model settings for agent {name!r}; "
                f"it configures {sorted(policy.model.agents)}. An agent without a pinned prompt "
                "version and effort has neither a reproducible answer nor a legible cost."
            )
        return AgentSpec(
            name=name,
            prompt_version=settings.prompt_version,
            output_type=output_type,
            tools=entry_for(name).tools,
            effort=Effort(settings.effort),
        )


@dataclass(frozen=True, slots=True)
class AgentResult[ContractT: AgentOutput]:
    """The validated answer, plus the record of how it was obtained.

    The trace record travels with the answer rather than being fetched from a log afterwards,
    because the two are a pair: a caller holding an answer can always say which call produced it.
    """

    output: ContractT
    trace: TraceRecord

    def __bool__(self) -> bool:
        """Falsy when the agent abstained or declined, so `if result:` reads correctly.

        **This is the only place `__bool__` belongs**, and the reason is a bug that cost a full
        recording run to find. An `AgentResult` is this repository's own type and never crosses a
        library boundary; a *contract* does, and the Anthropic SDK reads its own parsed value back
        with `if content.parsed_output:`. A falsy contract is therefore discarded by the SDK, and
        the caller is told the model returned nothing parseable — from a response whose JSON was
        exactly right. So the contracts answer `is_answer` and only this wrapper is falsy.
        """
        return self.output.is_answer


class AgentRunner:
    """Runs one agent's turn: render, request, validate, record.

    Holds the run-scoped collaborators — the provider, the tool registry, the trace log, the usage
    ledger — so a call site supplies only the spec and the message. Constructed per run, because
    the registry's tools are bound to the run's own workbook and lookups.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        policy: Policy,
        registry: ToolRegistry | None = None,
        trace: TraceLog | None = None,
        usage: UsageLedger | None = None,
        prompts: PromptRegistry | None = None,
    ) -> None:
        self.provider = provider
        self.policy = policy
        self.registry = registry if registry is not None else ToolRegistry()
        self.trace = trace if trace is not None else TraceLog()
        self.usage = usage
        self.prompts = prompts or PromptRegistry()

    def session[ContractT: AgentOutput](self, spec: AgentSpec[ContractT]) -> ToolSession:
        """A tool session bound to this agent's allowlist.

        Handed to whatever gathers the agent's view of the world before the request is built. That
        code calls tools *through the session*, so the allowlist is enforced and the calls are
        recorded even where — as with the mapping agent — there is no tool-use loop at all.
        """
        return self.registry.session(spec.name, spec.tools)

    def build_request[ContractT: AgentOutput](
        self,
        spec: AgentSpec[ContractT],
        messages: Sequence[Message],
        *,
        max_tokens: int = 16_000,
    ) -> ModelRequest:
        """Everything that can change the answer, and nothing that cannot.

        The rendered system prompt is the prompt file **plus the tool descriptions**, so an agent
        whose allowlist changed asks a different question and therefore has a different cassette
        key. That is correct and occasionally surprising: granting a tool invalidates recordings
        made before the grant, because an agent that could have asked something new is not the
        agent that was recorded.
        """
        if not messages:
            raise AgentError(
                f"agent {spec.name!r} was given no messages. An agent with an empty conversation "
                "still returns something, and that something looks like an answer."
            )
        prompt = self.prompts.load(spec.name, spec.prompt_version)
        system = f"{prompt.text.rstrip()}\n\n## Tools\n\n{self.registry.describe(spec.tools)}\n"
        return ModelRequest(
            agent=spec.name,
            prompt_version=spec.prompt_version,
            system=system,
            messages=tuple(messages),
            output_schema=spec.output_type.model_json_schema(),
            model_id=self.policy.model.model_id,
            effort=spec.effort,
            max_tokens=max_tokens,
        )

    def run[ContractT: AgentOutput](
        self,
        spec: AgentSpec[ContractT],
        messages: Sequence[Message],
        *,
        session: ToolSession | None = None,
        max_tokens: int = 16_000,
    ) -> AgentResult[ContractT]:
        """One turn. Returns the validated contract, or raises having recorded why.

        `session` is the tool session whose calls produced this message, if any. Passing it in
        rather than creating one here is what lets the trace record tools that were called *before*
        the request was built — which, for the mapping agent, is all of them.
        """
        started = time.perf_counter()
        if session is not None and session.agent != spec.name:
            # A session carries an allowlist and a call log, both scoped to one agent. Accepting
            # another agent's would file its tool calls - and its refusals - under this one, which
            # is the one thing a trace must never do.
            raise AgentError(
                f"agent {spec.name!r} was handed a tool session belonging to {session.agent!r}. "
                "A session is one agent's allowlist and one agent's record; using another's would "
                "attribute its tool calls, refusals included, to this one."
            )
        calls = trace_calls(session.calls) if session is not None else ()

        try:
            request = self.build_request(spec, messages, max_tokens=max_tokens)
        except Exception as exc:
            # A failure before a request exists still gets a record. Without one, a run that died
            # on a missing prompt file would leave a trace that is silent about the only thing
            # that happened.
            self._record_failure(spec, calls, cassette_key="", error=exc, started=started)
            raise

        try:
            response = self.provider.complete(request, spec.output_type)
        except Exception as exc:
            self._record_failure(
                spec, calls, cassette_key=request.cassette_key, error=exc, started=started
            )
            raise

        parsed = response.parsed
        if not isinstance(parsed, spec.output_type):
            # The provider validates, so this is defence against a future adapter that does not.
            # Cheap, and the failure it catches would otherwise surface as a wrong value three
            # layers down with nothing pointing back here.
            error = AgentError(
                f"agent {spec.name!r} returned {type(parsed).__name__}, not "
                f"{spec.output_type.__name__}. The provider is expected to validate; an adapter "
                "that does not is a defect in the model layer, not something to coerce here."
            )
            self._record_failure(
                spec, calls, cassette_key=request.cassette_key, error=error, started=started
            )
            raise error

        record = TraceRecord(
            agent=spec.name,
            prompt_version=spec.prompt_version,
            model_id=request.model_id,
            effort=spec.effort.value,
            provider_mode=response.mode.value,
            cassette_key=response.cassette_key,
            output_contract=spec.output_type.__name__,
            tool_calls=calls,
            output_json=parsed.model_dump_json(),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            duration_ms=response.duration_ms,
        )
        self.trace.append(record)
        if self.usage is not None:
            self.usage.record(
                spec.name,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                duration_ms=response.duration_ms,
            )
        return AgentResult(output=parsed, trace=record)

    def _record_failure[ContractT: AgentOutput](
        self,
        spec: AgentSpec[ContractT],
        calls: tuple[TraceCall, ...],
        *,
        cassette_key: str,
        error: BaseException,
        started: float,
    ) -> None:
        """Write the trace entry for a call that produced no answer, then let the caller re-raise.

        Records and returns — it does not raise and does not swallow. The exception belongs to the
        caller; this method's only job is to make sure the run leaves evidence behind before it
        propagates.

        **The usage ledger is told too, with zero tokens.** A failed call is still a call, and
        counting only the successful ones made `NodeTiming.model_calls` disagree with the trace:
        `mizan trace` attributes records to nodes by consuming that count in order, so one
        uncounted failure shifted every later call onto the wrong node. Zero tokens is the honest
        figure when the request never reached the model — and when it did and the *answer* failed
        validation, the tokens are lost with the response, which is a limit of where this sits
        rather than a decision.
        """
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.trace.append(
            TraceRecord(
                agent=spec.name,
                prompt_version=spec.prompt_version,
                model_id=self.policy.model.model_id,
                effort=spec.effort.value,
                provider_mode=self.provider.mode.value,
                cassette_key=cassette_key,
                output_contract=spec.output_type.__name__,
                tool_calls=calls,
                error=f"{type(error).__name__}: {error}",
                duration_ms=duration_ms,
            )
        )
        if self.usage is not None:
            self.usage.record(spec.name, input_tokens=0, output_tokens=0, duration_ms=duration_ms)
