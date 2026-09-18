"""The tool surface: named functions, per-agent allowlists, checked at the moment of the call.

An allowlist described in a prompt is a request. An allowlist checked in the function that
dispatches the call is a guarantee, and only the second survives a model having an off day. This
module is the second kind.

## What a tool is here

A named callable, registered once, with a one-line description that is rendered into the prompt so
the agent knows what it may ask for. Nothing more. Tools are ordinary Python functions bound to
whatever they read — a workbook, a lookup table — and the registry holds the binding, so the
runtime never has to know what an individual tool touches.

## Why the check is at call time rather than at construction

Both, in fact, and the distinction matters. `ToolRegistry.session()` refuses an allowlist naming a
tool that was never registered, which catches the typo at wiring time — a silently-ignored
allowlist entry would grant nothing while looking like it granted something. But the *denial* has
to happen at the call, because that is the only place the decision is real: an agent whose
allowlist was assembled correctly and who then asks for `read_values` must be stopped there, with
the attempt recorded, rather than trusted not to ask.

So a `ToolSession` is the only way to reach a tool, it is bound to exactly one agent's allowlist,
and every call through it is recorded whether it was allowed or refused. The refusals are the
interesting half of the trace.

## Why a refused call is an error rather than an empty result

Returning "no such tool" to the model and letting it try something else is the obvious design and
it is wrong for this system. It converts a contract violation into a retry loop, and the run ends
up succeeding with no record that an agent reached outside its surface. Here it raises, the
supervisor sees it, and the trace carries the attempt. An agent that asks for a tool it does not
have is a defect in the roster or in the prompt, and defects should be loud.

## On the mapping agent, which appears to have no channel at all

`tda.excel.tools` explains why the mapping agent's tools are called by *code* before the request
is built, with their output rendered into the message: with no tool-use loop the model holds no
channel it could keep asking down. That is a stronger guarantee, not a weaker one, and it is
unaffected by anything here — the calls still go through a session, so the allowlist is still
enforced and the trace still records which tools produced the view the agent was given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping


class ToolError(Exception):
    """Base for every failure in this module."""


class ToolNotRegisteredError(ToolError):
    """A name nobody registered.

    Raised at session construction as well as at call time. Catching it at construction is what
    stops a mistyped allowlist entry from reading as a granted permission that never fires.
    """

    def __init__(self, name: str, available: Iterable[str]) -> None:
        known = ", ".join(sorted(available)) or "none"
        super().__init__(f"no tool named {name!r} is registered; registered: {known}")
        self.name = name


class ToolNotAllowedError(ToolError):
    """An agent asked for a tool outside its allowlist.

    An error rather than an empty result, and the reason is in the module docstring: returning
    "not available" turns a contract violation into a retry loop and leaves no record that an
    agent reached outside its surface.
    """

    def __init__(self, agent: str, name: str, allowed: Iterable[str]) -> None:
        permitted = ", ".join(sorted(allowed)) or "none"
        super().__init__(
            f"agent {agent!r} called {name!r}, which is not in its allowlist ({permitted}). "
            "An allowlist is enforced here rather than described in the prompt, so this is a "
            "defect in the roster or in the prompt - not something to widen at the call site."
        )
        self.agent = agent
        self.name = name


@dataclass(frozen=True, slots=True)
class Tool:
    """One named callable and the sentence the agent is shown about it.

    `description` is not documentation. It is rendered into the prompt, so it is an input to the
    answer and therefore to the cassette key: editing it changes what was asked and invalidates
    the recording, which is correct.
    """

    name: str
    description: str
    fn: Callable[..., Any]

    def render(self) -> str:
        return f"- `{self.name}` — {self.description}"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One attempt, allowed or not.

    `allowed=False` entries are kept rather than dropped. A trace that records only the calls that
    succeeded cannot answer the question anyone actually asks after an incident, which is what the
    agent *tried* to do.
    """

    agent: str
    name: str
    allowed: bool

    def render(self) -> str:
        return f"{self.agent}:{self.name}{'' if self.allowed else ' (refused)'}"


class ToolRegistry:
    """Every tool available in a run, by name.

    Constructed per run rather than imported as a module global, because tools are bound to the
    run's own data — this workbook, these lookups. A global registry would either hold unbound
    functions that each call site has to feed correctly, or hold state across runs, and both are
    how a second submission ends up answered from the first one's workbook.
    """

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Add a tool. A duplicate name is an error, not a replacement.

        Silently replacing would mean the tool an agent gets depends on registration order, which
        is exactly the kind of dependency nobody reads for.
        """
        if tool.name in self._tools:
            raise ToolError(
                f"tool {tool.name!r} is already registered. Replacing it silently would make the "
                "tool an agent gets depend on registration order."
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotRegisteredError(name, self._tools)
        return tool

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def session(self, agent: str, allowed: Iterable[str]) -> ToolSession:
        """A session bound to one agent's allowlist.

        Every name in the allowlist must be registered. See the module docstring on why the
        unregistered case is caught here and the disallowed case is caught at the call.
        """
        allowlist = frozenset(allowed)
        for name in sorted(allowlist):
            if name not in self._tools:
                raise ToolNotRegisteredError(name, self._tools)
        return ToolSession(agent=agent, registry=self, allowed=allowlist)

    def describe(self, allowed: Iterable[str]) -> str:
        """The allowed tools as the agent is shown them, in a stable order.

        Sorted, because this text is hashed into the cassette key: a set iterating in a different
        order on a different interpreter would produce a different key for the same permissions.
        """
        tools = [self.get(name) for name in sorted(frozenset(allowed))]
        if not tools:
            return "You have no tools. Everything you need is in the message."
        return "\n".join(tool.render() for tool in tools)


@dataclass(slots=True)
class ToolSession:
    """One agent's window onto the registry, and the record of what it did with it.

    Mutable, because it is a tally of calls made during one agent's turn. The immutable artefact is
    the `ToolCall` tuple it hands to the trace when the turn ends.
    """

    agent: str
    registry: ToolRegistry
    allowed: frozenset[str]
    _calls: list[ToolCall] = field(default_factory=list)

    def call(self, name: str, /, *args: Any, **kwargs: Any) -> Any:
        """Invoke a tool, or raise.

        The allowlist is checked *before* the lookup, so an agent reaching for a tool that exists
        but is not its own gets `ToolNotAllowedError` rather than a message about registration.
        The two mean different things to whoever reads the failure: one is a roster defect, the
        other is a wiring defect.
        """
        if name not in self.allowed:
            self._calls.append(ToolCall(agent=self.agent, name=name, allowed=False))
            raise ToolNotAllowedError(self.agent, name, self.allowed)
        tool = self.registry.get(name)
        self._calls.append(ToolCall(agent=self.agent, name=name, allowed=True))
        return tool.fn(*args, **kwargs)

    @property
    def calls(self) -> tuple[ToolCall, ...]:
        """Every attempt this turn, in order, refusals included."""
        return tuple(self._calls)

    def refusals(self) -> tuple[ToolCall, ...]:
        return tuple(call for call in self._calls if not call.allowed)

    def __iter__(self) -> Iterator[ToolCall]:
        return iter(self._calls)


def registry_from(mapping: Mapping[str, tuple[str, Callable[..., Any]]]) -> ToolRegistry:
    """Build a registry from `{name: (description, fn)}`.

    A convenience for the run assembly code, which has the bound callables to hand and would
    otherwise write the same three-line `Tool(...)` construction for each.
    """
    return ToolRegistry(
        Tool(name=name, description=description, fn=fn)
        for name, (description, fn) in mapping.items()
    )
