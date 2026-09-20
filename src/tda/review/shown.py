"""What an agent was shown, recovered for display - never held on the trace itself.

`TraceRecord` carries no request text (see `tda.obs.trace`'s own docstring on why): only the
agent's name, its prompt version, a cassette key and the parsed answer. That is the right shape for
an artifact and the wrong one for an agent card the console wants to open, so this module recovers
the request from wherever it can honestly come from:

- **A replayed call** (`provider_mode == "replay"`) was answered from a committed cassette, and the
  cassette stores `request_canonical` beside the response precisely so a reviewer can see what was
  asked - `from_cassette()` reads it back. This covers every agent uniformly, because every agent's
  cassette is written the same way (`tda.agents.provider.replay.write_cassette`).
- **A live mapping call** has no cassette to read. `mapping_recomputed()` rebuilds the exact request
  `tda.excel.agent.build_request` would have sent - the same function the pipeline itself calls - so
  what the console shows is provably what was sent, not a guess at it. Nothing else runs live in
  this story (narrative, critic and reviewer-assist are called through replay-backed cassettes when
  the console asks for them), so no other agent needs a recompute path.

Everything returned here has already been through `tda.obs.redact` - a request recovered for
display is exactly as sensitive as one written to an artifact, and gets the same treatment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from tda.agents.provider.replay import DEFAULT_CASSETTE_DIR, cassette_path
from tda.obs.redact import redact

if TYPE_CHECKING:
    from pathlib import Path

    from tda.obs.trace import TraceRecord
    from tda.policy import Policy

Source = Literal["cassette", "recomputed"]

MAPPING: Final = "mapping"


@dataclass(frozen=True, slots=True)
class ShownRequest:
    """What one call was asked, recovered for a viewer rather than kept on the trace.

    `messages` mirrors `ModelRequest.canonical()`'s own shape - `(role, content)` pairs - so a
    renderer does not need to know two message shapes depending on where the request came from.
    """

    agent: str
    source: Source
    label: str
    system: str
    messages: tuple[tuple[str, str], ...]
    model_id: str
    effort: str


def from_cassette(
    agent: str, cassette_key: str, *, cassette_dir: Path | None = None
) -> ShownRequest | None:
    """The recorded request for a replayed call, or `None` when there is nothing to read - an
    empty key (the call never reached the provider), or a cassette that is not there."""
    if not cassette_key:
        return None
    path = cassette_path(cassette_dir or DEFAULT_CASSETTE_DIR, agent, cassette_key)
    if not path.is_file():
        return None

    canonical = json.loads(path.read_text(encoding="utf-8"))["request_canonical"]
    return ShownRequest(
        agent=agent,
        source="cassette",
        label=f"replayed from the committed cassette ({cassette_key[:8]}…)",
        system=redact(str(canonical["system"]))[0],
        messages=tuple(
            (str(m["role"]), redact(str(m["content"]))[0]) for m in canonical["messages"]
        ),
        model_id=str(canonical["model_id"]),
        effort=str(canonical["effort"]),
    )


def mapping_recomputed(workbook_path: Path, policy: Policy) -> ShownRequest:
    """What the mapping agent would be shown for the workbook at `workbook_path`, rebuilt with the
    pipeline's own request builder against a provider that is never asked to answer.

    Opens the workbook exactly the way `tda.graph.nodes.claim_parse_node` does
    (`tda.excel.run.open_submission`) and builds the request from the **formulas** view, the one
    the agent is actually shown - values already replaced with `<value>` by `tda.excel.tools.digest`
    before this function is ever reached.
    """
    from tda.agents.provider import StubProvider
    from tda.agents.runtime import AgentRunner
    from tda.excel.agent import build_registry as mapping_registry
    from tda.excel.agent import build_request as mapping_request
    from tda.excel.run import open_submission

    _, formulas = open_submission(workbook_path)
    runner = AgentRunner(StubProvider(), policy=policy, registry=mapping_registry(formulas))
    request = mapping_request(formulas, policy, runner)

    return ShownRequest(
        agent=MAPPING,
        source="recomputed",
        label="recomputed from the workbook, identical to what was sent",
        system=redact(request.system)[0],
        messages=tuple((m.role, redact(m.content)[0]) for m in request.messages),
        model_id=request.model_id,
        effort=request.effort.value,
    )


def shown_for(
    record: TraceRecord,
    *,
    workbook: Path | None = None,
    policy: Policy | None = None,
) -> ShownRequest | None:
    """What `record`'s call was shown, by whichever route applies - or `None`, meaning the console
    should say plainly that the request text is not available, rather than guess at one.

    A live mapping call is the one case `workbook` and `policy` are needed for; every other agent
    resolves through its cassette alone, so a caller grading past findings (no submitted workbook in
    hand) can still pass neither.
    """
    if record.provider_mode == "replay":
        return from_cassette(record.agent, record.cassette_key)
    if record.provider_mode == "anthropic" and record.agent == MAPPING:
        if workbook is None or policy is None:
            return None
        return mapping_recomputed(workbook, policy)
    return None
