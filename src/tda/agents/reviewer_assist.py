"""The reviewer-assist agent: three lookups over one verdict, and an answer that must cite them.

This is the only agent that runs while a human is watching, and the only one whose output the
human reads *as an answer* rather than as a field in a document. That changes what it has to be
defended against. The narrative agent's sentence sits beside figures the officer can check; this
agent's answer is the thing the officer is checking *with*, so a confident wrong answer here is
much more expensive than a confident wrong sentence anywhere else in the system.

Three things make that failure hard to reach, and all three are wiring rather than instruction:

**The contract cannot hold an uncited answer.** `Answered.citations` is `min_length=1`. A model
that returns fluent prose with nothing behind it fails schema validation and the officer sees the
refusal instead. See `tda.agents.contracts.reviewer_assist`.

**The tools are the agent's whole world, and none of them computes.** `query_verdict` renders the
verdict's shape, `get_evidence` the citations attached to one finding, `get_policy_clause` the text
of one rule. There is no arithmetic tool and no file read: the agent cannot open the submission,
cannot re-derive a figure, and cannot reach a document the run did not already read. A question
needing any of those is a question it declines.

**Every citation is checked against the verdict before the answer is returned.** `ask` rejects a
`PdfRef` no finding carries, an `ExcelRef` no finding carries, a clause the definitions do not
define. This is the same check `resolve_label` applies to a matched candidate and it closes the
same hole: a well-formed reference is trivial for a model to write, and a citation nobody follows
is indistinguishable from a citation that leads nowhere.

## Why the tools withhold the figures

`query_verdict` and `get_evidence` return classifications, periods, clauses and references. They do
not return `claimed`, `computed` or `difference`, for the reason `tda.agents.narrative` gives: a
number in an answer is a number a model wrote, and an officer reading a screen cannot tell which of
the numbers in front of them came from `tda.metrics` and which came from prose. The figures are
rendered beside the answer by the screen, from the verdict, where code put them.

The cost of this is real and worth stating: the agent genuinely cannot answer *"by how much?"*, and
it is expected to say so and point at the finding that holds the figure. That is the correct trade.
The officer already has the number on screen; what they lack, and what this agent supplies, is
which rule made it a finding and where each side came from.

## Why it is not in the graph

`runs_in="review"` in the roster. This agent is never part of producing a verdict — it reads one
that already exists, after the run has finished, on a screen. It has no node, contributes nothing
to the result, and a verification run that never opens the review screen never calls it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from tda.agents.contracts.reviewer_assist import (
    CitedAnswer,
    ClauseCitation,
    ExcelCitation,
    InventoryCitation,
    PdfCitation,
)
from tda.agents.provider.base import Message
from tda.agents.roster import REVIEWER_ASSIST
from tda.agents.runtime import AgentError, AgentSpec
from tda.agents.tools import Tool, ToolRegistry
from tda.contracts import ExcelRef, InventoryRef, NotReached, PdfRef

if TYPE_CHECKING:
    from tda.agents.runtime import AgentResult, AgentRunner
    from tda.agents.tools import ToolSession
    from tda.contracts import Finding, Verdict
    from tda.policy import Policy

REPO_ROOT = Path(__file__).resolve().parents[3]

# The clause text lives in the definitions document, not in `policy.yaml`. `policy.yaml` records
# which clause each *rule* rests on; the sentence a clause actually says is written once, here, and
# that is what an officer asking "which rule?" wants read back to them.
DEFINITIONS_PATH = REPO_ROOT / "docs" / "01-definitions.md"

# How the definitions table writes a clause: `| **D-MAT-06** | Definitional variances ... |`.
_CLAUSE_ROW = re.compile(r"^\|\s*\*\*(D-[A-Z]+-[0-9]{2})\*\*\s*\|(.*?)\|\s*$", re.MULTILINE)

# How many findings `query_verdict` lists before it stops and says so. A verdict with two hundred
# findings would otherwise render a wall of text that pushes the officer's actual question out of
# the model's attention — and the agent can ask about any one of them by id regardless.
MAX_LISTED = 40


class AssistError(AgentError):
    """A reviewer-assist answer that cannot be shown to an officer.

    A subclass so the screen can distinguish "the agent fabricated a citation" from any other
    runtime failure, and say the honest thing in each case.
    """


def load_clauses(path: Path | None = None) -> dict[str, str]:
    """Every clause the definitions define, id to text.

    Read from the document rather than from a table in code, so a clause added in review is
    answerable the moment it is written down — and so this module cannot drift into being a second,
    staler copy of the definitions.
    """
    source = path or DEFINITIONS_PATH
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssistError(
            f"cannot read the definitions at {source}: {exc}. Without them the agent cannot cite a "
            "clause, and a clause citation nobody can check is worse than no answer."
        ) from exc
    # The `**` around the emphasised half of a clause is markdown for the document's reader, not
    # part of what the rule says. Left in, it comes back out in an answer's prose as stray
    # punctuation on a screen that renders no markdown.
    return {
        match.group(1): " ".join(match.group(2).replace("**", "").split())
        for match in _CLAUSE_ROW.finditer(text)
    }


def _describe(finding: Finding) -> str:
    """One finding as the agent sees it: everything but the figures.

    See the module docstring on the omission. `clause` is here because it is the answer to the
    question this agent is asked most.
    """
    parts = [
        f"{finding.finding_id}: {finding.key.metric.value} {finding.key.period}",
        f"class={finding.variance_class.value}",
        f"severity={finding.severity.value}",
        f"escalates_to={finding.escalates_to.value}",
        f"clause={finding.clause}",
    ]
    if finding.key.dimension is not None:
        parts.append(f"{finding.key.dimension.value}={finding.key.value}")
    if finding.explaining_permutation is not None:
        parts.append(f"explained_by={finding.explaining_permutation}")
    if finding.also_explained_by:
        parts.append(f"also_explained_by={','.join(finding.also_explained_by)}")
    return " | ".join(parts)


def _elided(count: int) -> str:
    """What a truncated listing says. Shared by both halves, because a definitional list that
    elided silently would put findings beyond the agent's reach with nothing saying so."""
    return f"  ... and {count} more, not listed here; ask about any of them by id."


def query_verdict(verdict: Verdict, finding_id: str | None = None) -> str:
    """The verdict's shape, or one finding within it. No figures either way.

    Called with no argument this is the agent's map: status, period, counts, and the findings by
    id. Called with an id it is one finding in the same form. Both deliberately include the things
    an officer asks about — which class, which clause, what explained it — and exclude the things
    the screen already shows them.

    An unknown id returns a sentence saying so rather than raising. The agent asking about a
    finding that does not exist is how it discovers a question is unanswerable, and that discovery
    should lead to a decline rather than to a crash.
    """
    if finding_id is not None:
        for finding in (*verdict.findings, *verdict.definitional_items):
            if finding.finding_id == finding_id:
                return _describe(finding)
        return (
            f"This verdict has no finding {finding_id!r}. Its findings are: "
            f"{', '.join(f.finding_id for f in (*verdict.findings, *verdict.definitional_items)) or 'none'}."
        )

    lines = [
        f"run_id: {verdict.run_id}",
        f"status: {verdict.status.value}",
        f"hotel: {verdict.hotel_id}  period: {verdict.period}",
        f"policy_version: {verdict.policy_version}",
    ]
    if verdict.rejection_reason is not None:
        lines.append(f"rejection_reason: {verdict.rejection_reason.value}")

    findings = list(verdict.findings)
    lines.append("\nfindings against the hotel:")
    lines.extend(f"  {_describe(f)}" for f in findings[:MAX_LISTED])
    if len(findings) > MAX_LISTED:
        lines.append(_elided(len(findings) - MAX_LISTED))
    if not findings:
        lines.append("  none")

    definitional = list(verdict.definitional_items)
    lines.append("\ndefinitional items, never counted as hotel errors (D-MAT-06):")
    lines.extend(f"  {_describe(f)}" for f in definitional[:MAX_LISTED])
    if len(definitional) > MAX_LISTED:
        lines.append(_elided(len(definitional) - MAX_LISTED))
    if not definitional:
        lines.append("  none")

    if verdict.not_verifiable:
        lines.append(
            "\nnot verifiable - no finding carries these, so there is nothing to cite about them. "
            "Say so rather than citing something adjacent:"
        )
        lines.extend(
            f"  {nv.key.metric.value} {nv.key.period}: {nv.reason}" for nv in verdict.not_verifiable
        )
    if verdict.out_of_scope_claims:
        lines.append(
            "\nout of scope, recorded so silence is not read as approval (D-SCOPE-02). Nothing "
            "here is citable either:"
        )
        lines.extend(f"  {k.metric.value} {k.period}" for k in verdict.out_of_scope_claims)

    lines.append(
        "\nNo figures and no counts appear above, deliberately. Both are on the officer's screen "
        "already, rendered from the verdict by code; a number you wrote would sit beside them with "
        "nothing to tell it apart. Name the findings you mean by id rather than counting them."
    )
    return "\n".join(lines)


def get_evidence(verdict: Verdict, finding_id: str) -> str:
    """The citations one finding carries, in the exact form an answer must cite them back.

    Rendered as the fields rather than only as the human string, because an answer is checked field
    by field against the verdict: showing the agent `page=4 row_start=12` and then rejecting
    `p.4 rows 12-18` because it wrote the prose form would be a trap of the wiring's making.
    """
    for finding in (*verdict.findings, *verdict.definitional_items):
        if finding.finding_id != finding_id:
            continue
        lines = [f"evidence for {finding_id}:"]
        source = finding.source_ref
        if isinstance(source, PdfRef):
            lines.append(
                f"  source: pdf file={source.file!r} page={source.page} "
                f"row_start={source.row_start} row_end={source.row_end}  ({source.citation})"
            )
        elif isinstance(source, InventoryRef):
            lines.append(
                f"  source: inventory file={source.file!r} row_start={source.row_start} "
                f"row_end={source.row_end}  ({source.citation})"
            )
        else:
            lines.append(f"  source: none - {source.reason}. There is nothing here to cite.")
        excel = finding.excel_ref
        if isinstance(excel, ExcelRef):
            lines.append(f"  excel: sheet={excel.sheet!r} cell={excel.cell!r}  ({excel.citation})")
        else:
            lines.append(f"  excel: none - {excel.reason}. There is nothing here to cite.")
        lines.append(f"  clause: {finding.clause}")
        return "\n".join(lines)
    return (
        f"This verdict has no finding {finding_id!r}, so there is no evidence to show. If the "
        "officer's question depends on it, decline and say the verdict does not record it."
    )


def get_policy_clause(clause_id: str, clauses: dict[str, str] | None = None) -> str:
    """What one clause says, or a sentence explaining that no such clause exists.

    Not a raise, for the reason `iso_lookup` is not one: the agent may reasonably ask about a
    clause it half-remembers, and "there is no such clause" is the answer to that question rather
    than an error in asking it. What it must not do is cite it anyway, and `ask` makes sure of that.
    """
    table = clauses if clauses is not None else load_clauses()
    wanted = clause_id.strip().upper()
    text = table.get(wanted)
    if text is None:
        return (
            f"There is no clause {wanted!r} in the definitions. Do not cite it. If your answer "
            "needs a rule you cannot name, decline."
        )
    return f"{wanted}: {text}"


def build_registry(verdict: Verdict, clauses: dict[str, str] | None = None) -> ToolRegistry:
    """The agent's three tools, bound to one verdict.

    Bound to *this* verdict rather than to a directory of them, which is what makes "it cannot
    answer from another run" a property of the wiring. There is deliberately no fourth tool: no
    arithmetic, no file read, no search. The absences are the design — see the module docstring.
    """
    table = clauses if clauses is not None else load_clauses()
    return ToolRegistry(
        (
            Tool(
                name="query_verdict",
                description=(
                    "This verdict's status, counts and findings; or one finding by id. "
                    "Classifications, clauses and causes. No figures."
                ),
                fn=lambda finding_id=None: query_verdict(verdict, finding_id),
            ),
            Tool(
                name="get_evidence",
                description=(
                    "The references one finding cites: a report page or inventory rows, and a "
                    "workbook cell. Cite these back exactly."
                ),
                fn=lambda finding_id: get_evidence(verdict, finding_id),
            ),
            Tool(
                name="get_policy_clause",
                description="What one clause of the definitions says, by id, e.g. D-MAT-06.",
                fn=lambda clause_id: get_policy_clause(clause_id, table),
            ),
        )
    )


def _verdict_refs(verdict: Verdict) -> tuple[set[PdfRef], set[ExcelRef], set[InventoryRef]]:
    """Every reference the verdict actually carries, by kind.

    `NotReached` is skipped rather than collected: a typed absence is a statement that there is
    nothing to point at, and an answer citing one would be citing the absence of evidence as
    evidence.
    """
    pdfs: set[PdfRef] = set()
    excels: set[ExcelRef] = set()
    inventories: set[InventoryRef] = set()
    for finding in (*verdict.findings, *verdict.definitional_items):
        source = finding.source_ref
        if isinstance(source, PdfRef):
            pdfs.add(source)
        elif isinstance(source, InventoryRef):
            inventories.add(source)
        elif not isinstance(source, NotReached):  # pragma: no cover - the union is closed
            raise AssistError(f"unhandled source citation kind: {type(source).__name__}")
        excel = finding.excel_ref
        if isinstance(excel, ExcelRef):
            excels.add(excel)
    return pdfs, excels, inventories


def check_citations(
    answer: CitedAnswer, verdict: Verdict, clauses: dict[str, str] | None = None
) -> None:
    """Refuse an answer that points at something this run does not contain.

    Every branch here is a fabrication check rather than a quality judgement, and each names a
    thing a model can write effortlessly and an officer cannot check at a glance:

    - a page and row range no finding cites — a plausible reference into a real file;
    - a sheet and cell no finding cites — likewise, and a workbook has thousands of them;
    - a clause id matching the pattern that the definitions do not define;
    - a clause cited under a policy version this verdict was not produced under, which is a
      citation of a moving target and the exact failure `policy_version` exists to prevent.

    Raises rather than downgrading to a decline. A decline is a considered refusal, and quietly
    relabelling a fabrication as one would put the two in the same bucket in every eval and every
    trace that follows — the same argument `resolve_label` makes about abstention.
    """
    if not answer.is_answer:
        # A decline carries no citations by construction - the contract's own validator refuses
        # one that does - so there is nothing here to check. A check that treated that emptiness
        # as a fabrication would make declining impossible, which is the outcome four of the eval
        # cases exist to require.
        return

    pdfs, excels, inventories = _verdict_refs(verdict)
    table = clauses if clauses is not None else load_clauses()

    for citation in answer.citations:
        # Matched on the kind, with the membership test *inside* each arm rather than as a guard on
        # it. A guard that fails falls through to the next case, so `case PdfCitation(...) if ref
        # not in pdfs` sent every **valid** pdf citation past three non-matching arms and into the
        # catch-all - which, once the catch-all raised as it should, refused every honest answer.
        match citation:
            case PdfCitation(ref=pdf_ref):
                if pdf_ref not in pdfs:
                    raise AssistError(
                        f"answer cites {pdf_ref.citation}, which no finding in run "
                        f"{verdict.run_id} cites. A page and row range is trivial to write and "
                        "leads somewhere real when followed, which is what makes an invented one "
                        "expensive."
                    )
            case ExcelCitation(ref=cell_ref):
                if cell_ref not in excels:
                    raise AssistError(
                        f"answer cites {cell_ref.citation}, which no finding in run "
                        f"{verdict.run_id} cites. A workbook has thousands of cells and every one "
                        "of them looks like a citation."
                    )
            case InventoryCitation(ref=csv_ref):
                if csv_ref not in inventories:
                    raise AssistError(
                        f"answer cites {csv_ref.citation}, which no finding in run "
                        f"{verdict.run_id} cites."
                    )
            case ClauseCitation(clause=clause, policy_version=version):
                if clause not in table:
                    raise AssistError(
                        f"answer cites clause {clause}, which the definitions do not define. The "
                        "field's pattern accepts any well-formed id; only the document decides "
                        "which ids denote a rule."
                    )
                if version != verdict.policy_version:
                    raise AssistError(
                        f"answer cites clause {clause} under policy {version}, and this verdict "
                        f"was produced under {verdict.policy_version}. A clause cited under the "
                        "wrong version is a citation of a moving target."
                    )
            case _:
                # The repo's convention, and `_verdict_refs` does the same a few lines up. A fifth
                # member added to the `Citation` union must fail loudly here rather than sail
                # through unchecked: the default for the check this module exists for cannot be
                # "accept".
                raise AssistError(
                    f"unhandled citation kind {type(citation).__name__!r}. Every member of the "
                    "Citation union needs a case here: a citation nobody checks is exactly what "
                    "this function exists to refuse."
                )


def gather(verdict: Verdict, turn: ToolSession) -> str:
    """Everything the agent will ever see about this run, assembled by **code** before the request.

    `ModelRequest` carries text, not tool calls - see `tda.agents.provider.base` - so the tools are
    called here and their output is rendered into the message, exactly as `tda.excel.tools` does
    for the mapping agent and `tda.agents.narrative` does for the narrative agent. That module's
    argument applies here unchanged and is worth repeating: with a tool-use loop the model holds a
    channel it can keep asking down; here it has no channel at all, what this function renders is
    the whole of its world, and those are the same bytes the cassette key hashes.

    Every finding's evidence is gathered rather than only the ones a question appears to name. The
    obvious alternative - parse finding ids out of the question and fetch those - would mean an
    officer who asks *"why is March flagged?"* gets an agent with no reference to cite, because
    nothing in the question spelled `F-0003`. The message is larger for it, and a citation the
    agent cannot make is the thing this contract cannot survive.

    Every call goes through the session, so the allowlist is enforced and the trace records which
    tools produced the view - which is what makes "three tools and no fourth" checkable after the
    fact rather than only in the roster.
    """
    parts = [str(turn.call("query_verdict"))]

    findings = (*verdict.findings, *verdict.definitional_items)[: MAX_LISTED * 2]
    parts.extend(str(turn.call("get_evidence", finding.finding_id)) for finding in findings)

    # Sorted, because this text is hashed into the cassette key: a set iterating in a different
    # order on a different interpreter would give the same verdict two different keys.
    for clause in sorted({finding.clause for finding in findings}):
        parts.append(str(turn.call("get_policy_clause", clause)))
    return "\n\n".join(parts)


def render_question(question: str) -> str:
    """The officer's question, and nothing added to it.

    Deliberately not summarised, rephrased or "clarified" before the agent sees it. A question
    rewritten on the way in is a question the trace records differently from the one that was
    asked, and the officer reading the trace six weeks later is the person least able to spot it.
    """
    return (
        f"An officer reviewing this verdict asks:\n\n{question}\n\n"
        "Answer from what you were shown above, citing what you used. Cite references exactly as "
        "they were given to you. If the answer is not above, decline and say what is missing."
    )


def ask(
    question: str,
    verdict: Verdict,
    runner: AgentRunner,
    policy: Policy,
    *,
    clauses: dict[str, str] | None = None,
    session: ToolSession | None = None,
) -> AgentResult[CitedAnswer]:
    """Put an officer's question to the agent, and refuse an answer that cannot be followed.

    `gather` assembles the agent's entire view first, by calling the three tools through the
    session. Without that the agent would be shown three tool *descriptions* and no verdict, and
    every honest answer it could give would cite something it had to invent - which this function's
    own check would then correctly refuse. The tools have to actually run.

    Two checks before the answer is returned. The question must be echoed back unchanged, so an
    answer that arrived against the wrong question is detectable rather than merely unlikely; and
    every citation must be one this verdict carries. Both raise `AssistError`, which the screen
    renders as a refusal to show the answer rather than as a broken page — an officer who is told
    "the assistant produced a citation that does not exist" has learned something useful about the
    tool, and one shown the answer with a warning beside it has not.
    """
    spec = AgentSpec.from_policy(REVIEWER_ASSIST, CitedAnswer, policy)
    turn = session if session is not None else runner.session(spec)

    body = "\n\n".join([gather(verdict, turn), render_question(question)])
    result = runner.run(spec, [Message(role="user", content=body)], session=turn)
    answer = result.output

    if answer.question != question:
        raise AssistError(
            f"reviewer-assist was asked {question!r} and answered {answer.question!r}. The "
            "question is echoed back so a misrouted answer is detectable; this one is misrouted."
        )
    check_citations(answer, verdict, clauses)
    return result
