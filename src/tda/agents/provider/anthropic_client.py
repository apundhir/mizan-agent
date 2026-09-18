"""The live adapter. Anthropic Console API, `claude-opus-5`, structured output on every call.

Four choices here are deliberate and each one has a test or an ADR behind it.

**No `temperature`.** It is rejected with HTTP 400 on current Claude models, and it never
guaranteed determinism even when accepted. The PRD asked for `temperature 0` as its
reproducibility lever; that lever no longer exists, so reproducibility comes from structured
outputs plus committed cassettes instead — which is strictly stronger. A test asserts the
parameter is absent from the built request, because this is exactly the kind of thing someone
re-adds from memory.

**Adaptive thinking, effort per agent.** `thinking={"type": "adaptive"}` with
`output_config={"effort": ...}`. The old `budget_tokens` form is rejected on this model family.

**Structured output via `messages.parse`.** The SDK validates the response against the Pydantic
contract and the model is re-prompted on mismatch, so validation happens at the tool-call layer
rather than in a hand-rolled retry loop here.

**The import is function-local.** `anthropic` is an optional extra (`pip install -e ".[agents]"`),
and CI installs it but never uses it — CI runs in replay. A module-level import would make the
whole provider package unimportable wherever the extra is absent, which would take the replay
path down with it for no reason.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tda.agents.provider.base import (
    LLMProvider,
    ModelRequest,
    ModelResponse,
    OutputT,
    OutputValidationError,
    ProviderError,
    ProviderMode,
    TokenUsage,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


def build_request_params(request: ModelRequest, output_type: type[OutputT]) -> dict[str, Any]:
    """Translate a `ModelRequest` into SDK kwargs.

    Split out from the call so it can be unit-tested without a network, a key, or the SDK
    installed. That matters more than it looks: the assertions worth making about this adapter
    are all about *what it sends* — the pinned model id, adaptive thinking, the effort setting,
    and the absence of `temperature`.
    """
    return {
        "model": request.model_id,
        "max_tokens": request.max_tokens,
        "system": request.system,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": request.effort.value},
        "output_format": output_type,
        # No `temperature`: HTTP 400 on current models, and it never bought determinism.
        # No `top_p`, `top_k` for the same reason. See docs/adr/0002-model-layer.md.
    }


def _usage_from(raw: Mapping[str, Any] | Any) -> TokenUsage:
    """Read usage defensively.

    The adapter must not fail a call because a usage field was renamed: the answer is already in
    hand and correct, and losing it over telemetry would be absurd. Missing counts read as zero
    and show up as zero in the ledger, which is visibly wrong rather than silently wrong.
    """

    def pick(name: str) -> int:
        value = raw.get(name, 0) if isinstance(raw, dict) else getattr(raw, name, 0)
        return int(value) if isinstance(value, int) else 0

    return TokenUsage(
        input_tokens=pick("input_tokens"),
        output_tokens=pick("output_tokens"),
        cache_read_tokens=pick("cache_read_input_tokens"),
    )


class AnthropicProvider(LLMProvider):
    """Live calls against the Anthropic Console API."""

    mode = ProviderMode.ANTHROPIC

    def __init__(self, api_key: str | None = None, *, timeout: float = 120.0) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._timeout = timeout
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError(
                "the anthropic SDK is not installed. Install the extra with:\n"
                '  pip install -e ".[agents]"\n'
                "CI does not need it: `make ci`, `make eval` and `make repro` run in replay mode."
            ) from exc

        # An unset ANTHROPIC_API_KEY does not mean there are no credentials - the SDK also
        # resolves ANTHROPIC_AUTH_TOKEN and an `ant auth login` profile. So construct the client
        # and let it resolve, rather than pre-judging on one env var.
        self._client = (
            anthropic.Anthropic(api_key=self._api_key, timeout=self._timeout)
            if self._api_key
            else anthropic.Anthropic(timeout=self._timeout)
        )
        return self._client

    def complete(self, request: ModelRequest, output_type: type[OutputT]) -> ModelResponse:
        client = self._ensure_client()
        params = build_request_params(request, output_type)

        started = time.perf_counter()
        try:
            response = client.messages.parse(**params)
        except ValidationError as exc:
            # A contract violation, not a transport failure, and the distinction is load-bearing
            # rather than cosmetic. `client.messages.parse` validates the response against the
            # output type, so this is a model that answered with something the contract refuses -
            # for reviewer-assist, prose with no citation. Folding it into `ProviderError` made the
            # one guarantee that feature is built on render to an officer as "the assistant is not
            # configured on this machine", which is a setup problem rather than a refusal.
            raise OutputValidationError(
                f"agent {request.agent!r} returned something that is not a "
                f"{output_type.__name__} (model={request.model_id}, "
                f"prompt={request.prompt_version}): {exc}"
            ) from exc
        except Exception as exc:  # SDK exception hierarchy; re-raised as a provider failure
            raise ProviderError(
                f"live call failed for agent {request.agent!r} "
                f"(model={request.model_id}, prompt={request.prompt_version}): {exc}"
            ) from exc
        duration_ms = int((time.perf_counter() - started) * 1000)

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            # `stop_reason == "refusal"` lands here: the call succeeded, there is no parsed
            # output, and treating that as a soft empty result would put a None where a contract
            # is expected. Better a loud provider error naming the stop reason.
            raise OutputValidationError(
                f"no parsed output for agent {request.agent!r}; "
                f"stop_reason={getattr(response, 'stop_reason', 'unknown')}. "
                "A validation failure is an error, never a fall-back to free text."
            )
        if not isinstance(parsed, output_type):
            raise OutputValidationError(
                f"agent {request.agent!r} returned {type(parsed).__name__}, "
                f"expected {output_type.__name__}"
            )

        return ModelResponse(
            parsed=parsed,
            raw_json=parsed.model_dump_json(),
            usage=_usage_from(getattr(response, "usage", {})),
            cassette_key=request.cassette_key,
            mode=self.mode,
            duration_ms=duration_ms,
        )


class RecordingProvider(LLMProvider):
    """A live provider that writes a cassette for every call. This is `make record`.

    Separated from `AnthropicProvider` so that recording is an explicit, named mode rather than a
    flag on the live client. `ProviderMode.RECORD` appears in the run ledger, so a verdict
    produced during a recording run is distinguishable from a normal one — which matters, because
    a recording run costs money and hits the network.
    """

    mode = ProviderMode.RECORD

    def __init__(self, inner: LLMProvider, cassette_dir: Any) -> None:
        self._inner = inner
        self._cassette_dir = cassette_dir
        self.written: list[str] = []

    def complete(self, request: ModelRequest, output_type: type[OutputT]) -> ModelResponse:
        from tda.agents.provider.replay import write_cassette

        response = self._inner.complete(request, output_type)
        path = write_cassette(
            self._cassette_dir,
            request,
            json.dumps(json.loads(response.raw_json), sort_keys=True),
            response.usage,
        )
        self.written.append(str(path))
        return ModelResponse(
            parsed=response.parsed,
            raw_json=response.raw_json,
            usage=response.usage,
            cassette_key=request.cassette_key,
            mode=self.mode,
            duration_ms=response.duration_ms,
        )
