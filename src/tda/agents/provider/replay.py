"""Replay: the default, and the reason CI is offline, free and byte-identical.

A cassette records that *this prompt, against this schema, produced this response from a real
model*. Committed, it keeps proving that on every push at zero cost. That is strictly more than a
mock offers — a mock proves the code compiles.

Two properties are non-negotiable and both are tested:

- **A miss is a hard error.** Never a fall-through to a live call. A replay mode that quietly hits
  the network when it misses is worse than no replay mode: CI would pass, spend money, and stop
  being reproducible, without one line of output saying so.
- **A cassette is validated against the contract on read.** A stale cassette recorded before a
  schema change must fail loudly rather than deserialise into something almost right. This is the
  failure that would otherwise show up as a wrong number in a verdict.

Cassettes are reviewed like code. A diff means a prompt or an output schema moved, and the PR has
to say which and why the new response is better.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tda.agents.provider.base import (
    CassetteMissError,
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
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CASSETTE_DIR = REPO_ROOT / "tests" / "cassettes"


def cassette_path(root: Path, agent: str, key: str) -> Path:
    """One file per call, namespaced by agent.

    A single large cassette file would make every diff a merge conflict and every review a
    scroll. Per-call files mean a changed prompt shows up as exactly one added and one removed
    file, which is legible.
    """
    return root / agent / f"{key}.json"


def write_cassette(
    root: Path, request: ModelRequest, response_json: str, usage: TokenUsage
) -> Path:
    """Write one cassette. Used by the recorder and by tests.

    `request_canonical` is stored alongside the response even though it is redundant with the
    key. It costs a few hundred bytes and it means a human reviewing a cassette diff can see
    *what was asked*, not just that a hash changed — which is the difference between a
    reviewable diff and an opaque one.
    """
    path = cassette_path(root, request.agent, request.cassette_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "key": request.cassette_key,
        "agent": request.agent,
        "prompt_version": request.prompt_version,
        "model_id": request.model_id,
        "effort": request.effort.value,
        "request_canonical": json.loads(request.canonical()),
        "response_json": json.loads(response_json),
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
        },
    }
    # Trailing newline and sorted keys so the file is stable under re-record and diffs cleanly.
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


class ReplayProvider(LLMProvider):
    """Serves committed cassettes. The provider `make ci`, `make eval` and `make repro` use."""

    mode = ProviderMode.REPLAY

    def __init__(self, cassette_dir: Path | None = None) -> None:
        self.cassette_dir = cassette_dir or DEFAULT_CASSETTE_DIR

    def complete(self, request: ModelRequest, output_type: type[OutputT]) -> ModelResponse:
        started = time.perf_counter()
        path = cassette_path(self.cassette_dir, request.agent, request.cassette_key)

        if not path.is_file():
            raise CassetteMissError(
                key=request.cassette_key,
                agent=request.agent,
                available=self._count_for(request.agent),
            )

        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"cassette {path} is unreadable: {exc}") from exc

        response_json = json.dumps(payload["response_json"], sort_keys=True)
        try:
            parsed = output_type.model_validate(payload["response_json"])
        except ValidationError as exc:
            raise OutputValidationError(
                f"cassette {path.name} does not satisfy {output_type.__name__}. It was recorded "
                f"against an older version of the contract - re-record it rather than loosening "
                f"the contract to fit a stale recording.\n{exc}"
            ) from exc

        usage_raw = payload.get("usage", {})
        return ModelResponse(
            parsed=parsed,
            raw_json=response_json,
            usage=TokenUsage(
                input_tokens=int(usage_raw.get("input_tokens", 0)),
                output_tokens=int(usage_raw.get("output_tokens", 0)),
                cache_read_tokens=int(usage_raw.get("cache_read_tokens", 0)),
            ),
            cassette_key=request.cassette_key,
            mode=self.mode,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def _count_for(self, agent: str) -> int:
        """How many cassettes exist for this agent, so the miss message can distinguish
        "nothing recorded yet" from "recorded, but the request changed"."""
        directory = self.cassette_dir / agent
        if not directory.is_dir():
            return 0
        return len(list(directory.glob("*.json")))

    def keys(self) -> Iterator[tuple[str, str]]:
        """Every committed (agent, key). Used by a test that asserts no cassette is orphaned."""
        if not self.cassette_dir.is_dir():
            return
        for path in sorted(self.cassette_dir.rglob("*.json")):
            yield path.parent.name, path.stem
