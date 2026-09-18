"""A deterministic provider for unit tests.

Not a substitute for cassettes. A cassette is evidence that a real model answered a real prompt
this way; a stub is a fixture that lets a test exercise the code *around* a call without caring
what the answer is. Both exist because they answer different questions, and conflating them is
how a test suite comes to prove only that its own mocks are consistent.

Two properties make it useful rather than dangerous:

- **Responses are registered per output type, not per key.** A test says "whenever something asks
  for a `SheetMapping`, hand back this one" without having to compute a cassette key, which would
  couple every test to the hashing scheme.
- **An unregistered type raises.** A stub that invents a plausible default is the single worst
  test double available: it makes a test pass while proving nothing, and the failure surfaces
  later as a wrong number nobody can trace back here.
"""

from __future__ import annotations

from typing import Any

from tda.agents.provider.base import (
    LLMProvider,
    ModelRequest,
    ModelResponse,
    OutputT,
    ProviderError,
    ProviderMode,
    TokenUsage,
)


class StubProvider(LLMProvider):
    """Returns pre-registered answers. Records every request it was asked to serve."""

    mode = ProviderMode.STUB

    def __init__(self) -> None:
        self._answers: dict[str, Any] = {}
        self.calls: list[ModelRequest] = []

    def register(self, answer: Any) -> None:
        """Register one answer, keyed by its own type."""
        self._answers[type(answer).__name__] = answer

    def complete(self, request: ModelRequest, output_type: type[OutputT]) -> ModelResponse:
        self.calls.append(request)
        answer = self._answers.get(output_type.__name__)
        if answer is None:
            raise ProviderError(
                f"StubProvider has no registered {output_type.__name__} for agent "
                f"{request.agent!r}. Register one with `.register(...)` - the stub will not invent "
                "a default, because a test that passes against an invented answer proves nothing."
            )
        return ModelResponse(
            parsed=answer,
            raw_json=answer.model_dump_json(),
            usage=TokenUsage(input_tokens=0, output_tokens=0),
            cassette_key=request.cassette_key,
            mode=self.mode,
            duration_ms=0,
        )
