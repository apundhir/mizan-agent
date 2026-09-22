"""The mapping call: build the request through the runtime, and let `run.py` check the answer.

Thin on purpose. Everything that makes the mapping *safe* lives elsewhere — the value redaction in
`tools.py`, the vocabulary and geometry checks in `mapping.py`, the exhaustiveness check in
`run.py` — and this module only assembles the request and hands the answer on. A file that both
calls a model and decides what to believe about the answer is a file where the second half quietly
softens under pressure from the first.

Three things are worth noticing about what goes into the request.

**The digest is the whole of the model's view of the workbook**, and it is hashed into the cassette
key along with the prompt version, the output schema and the model id. So a change to the redaction
rules invalidates the cassettes, which is correct: a mapping made against a different view of the
workbook is a different mapping, and replaying the old answer would be replaying it against a
question that was not asked.

**The vocabulary is rendered into the message from policy**, not written into the prompt file. A
metric added to `policy.yaml` then reaches the agent without a prompt edit — and a prompt edit would
mean a new prompt version and a re-record of every cassette, which is a heavy price for a
configuration change that the checking code already handles correctly.

**The two tools are called through a `ToolSession`**, even though there is no tool-use loop
here and never will be — `tda.excel.tools` explains why an agent with no channel is a stronger
guarantee than an agent with a filtered one. The session is not the enforcement mechanism for
*values*; redaction is. What it adds is the allowlist and the record: the trace now says which tools
produced the view this mapping was made from, and an attempt to reach a third one is refused rather
than merely unimplemented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tda.agents.provider.base import Message
from tda.agents.roster import MAPPING
from tda.agents.runtime import AgentRunner, AgentSpec
from tda.agents.tools import Tool, ToolRegistry
from tda.excel.mapping import WorkbookMapping
from tda.excel.tools import digest, list_sheets, peek_headers

if TYPE_CHECKING:
    from openpyxl.workbook.workbook import Workbook

    from tda.agents.provider.base import LLMProvider, ModelRequest
    from tda.agents.runtime import AgentResult
    from tda.agents.tools import ToolSession
    from tda.policy import Policy

AGENT = MAPPING


def build_registry(workbook: Workbook) -> ToolRegistry:
    """The mapping agent's two tools, bound to one workbook.

    Neither can emit a cell's value — that is `tools.py`'s guarantee and it holds regardless of the
    allowlist. Bound per workbook, so a second submission cannot be answered from the first one's
    sheets.
    """
    return ToolRegistry(
        (
            Tool(
                name="list_sheets",
                description="Every sheet name and its used range. Geometry, never contents.",
                fn=lambda: list_sheets(workbook),
            ),
            Tool(
                name="peek_headers",
                description=(
                    "Every label in one sheet, with quantities and formulas shown as redaction "
                    "tokens. There is no code path here that returns a value."
                ),
                fn=lambda sheet: peek_headers(workbook, sheet),
            ),
        )
    )


def spec_for(policy: Policy) -> AgentSpec[WorkbookMapping]:
    """The mapping agent, with effort and prompt version taken from the ruleset and its tool
    allowlist taken from the roster."""
    return AgentSpec.from_policy(AGENT, WorkbookMapping, policy)


def render_message(workbook: Workbook, policy: Policy) -> str:
    """The vocabulary and the digest: everything the agent is told about this submission."""
    in_scope = ", ".join(sorted(metric.value for metric in policy.scope.metrics_in_scope))
    out_of_scope = ", ".join(sorted(policy.scope.metrics_out_of_scope))
    return (
        f"Metric names you may use.\n"
        f"  verified by this system: {in_scope}\n"
        f"  recorded but not verified: {out_of_scope}\n\n"
        f"Dimension names you may use: nationality_iso2\n\n"
        f"{digest(workbook)}"
    )


def build_request(workbook: Workbook, policy: Policy, runner: AgentRunner) -> ModelRequest:
    """The request for one workbook.

    Goes through `AgentRunner.build_request`, so the rendered system prompt carries the tool
    descriptions and the cassette key covers the agent's permissions as well as its instructions.
    """
    spec = spec_for(policy)
    return runner.build_request(
        spec, [Message(role="user", content=render_message(workbook, policy))]
    )


def map_workbook_traced(
    workbook: Workbook, policy: Policy, runner: AgentRunner
) -> AgentResult[WorkbookMapping]:
    """Ask the mapping agent where the figures are, with a trace record and a tool session.

    Returns the agent's answer **unchecked**, and the trace record beside it. Every guarantee about
    the mapping is applied by `run.parse_claims`, which is where a reader looking for "what stops a
    bad mapping" should be sent — rather than here, where it would be easy to mistake the absence of
    a check for the absence of a risk.
    """
    spec = spec_for(policy)
    session: ToolSession = runner.session(spec)
    # Called through the session so the trace records them, and so a future change that reaches for
    # a third tool is refused here rather than discovered in review.
    session.call("list_sheets")
    for summary in list_sheets(workbook):
        session.call("peek_headers", summary.name)

    message = Message(role="user", content=render_message(workbook, policy))
    return runner.run(spec, [message], session=session)


def map_workbook(workbook: Workbook, policy: Policy, provider: LLMProvider) -> WorkbookMapping:
    """The mapping, for callers that have a provider rather than a runner.

    Kept because `tda.excel.run` and its tests are written against a provider, and because a claim
    parser that cannot be exercised without assembling a trace log and a usage ledger is a claim
    parser nobody will write a test for. It builds a runner internally, so the allowlist and the
    request shape are identical — what is lost is only the caller's access to the trace.
    """
    runner = AgentRunner(provider, policy=policy, registry=build_registry(workbook))
    return map_workbook_traced(workbook, policy, runner).output
