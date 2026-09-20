"""The reviewer-assist agent: what it may cite, what it may not say, and what it costs.

Every test below removes something and watches a guarantee fail. The guarantees, in the order they
matter:

**An uncited answer cannot be constructed.** Not "is discouraged" — `CitedAnswer`'s validator
refuses an `answered` outcome carrying no citation, so the failure happens at construction inside
the provider and the prose never becomes an answer at all. It used to be a `min_length=1` on a
union branch; recording against the live API proved the API cannot generate that union, and the
guarantee moved to the validator rather than being weakened.

**A citation the run does not contain is refused.** A page and row range is trivial for a model to
write and leads somewhere real when followed, which is exactly what makes an invented one
expensive. `check_citations` is the only thing standing between that and an officer's screen.

**The tools cannot compute and cannot read a file.** Proven by what the registry contains rather
than by what the prompt asks for: three named tools, and the allowlist refuses the fourth at the
call.

**A question and its cost reach the trace.** The officer who signs a verdict and the auditor who
reads it six weeks later are asking the same question about the assistant, and the trace is where
it is answered.

The Streamlit module itself gets an import check and nothing more, as in `test_review.py`: it is a
shell over `tda.review.assist`, and asserting on its widgets would test Streamlit.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from tests.unit.test_outputs import definitional_finding, material_finding, verdict_with

from tda.agents.contracts.reviewer_assist import (
    CitedAnswer,
    ClauseCitation,
    ExcelCitation,
    InventoryCitation,
    PdfCitation,
)
from tda.agents.provider import (
    ModelRequest,
    ModelResponse,
    OutputValidationError,
    ProviderMode,
    StubProvider,
    TokenUsage,
)
from tda.agents.reviewer_assist import (
    MAX_LISTED,
    AssistError,
    ask,
    build_registry,
    check_citations,
    get_evidence,
    get_policy_clause,
    load_clauses,
    query_verdict,
)
from tda.agents.roster import REVIEWER_ASSIST, ROSTER
from tda.agents.runtime import AgentRunner, AgentSpec
from tda.agents.tools import ToolNotAllowedError
from tda.contracts import (
    EscalationTarget,
    ExcelRef,
    Finding,
    InventoryRef,
    Metric,
    MetricKey,
    PdfRef,
    Severity,
    VarianceClass,
)
from tda.obs import TraceLog, UsageLedger
from tda.obs.trace import TraceRecord
from tda.policy import load_policy
from tda.review.assist import (
    MAX_QUESTION,
    Answer,
    NoAnswer,
    Unavailable,
    check_question,
    put_question,
)
from tda.review.present import answer_citations, unavailable_line

if TYPE_CHECKING:
    from tda.agents.contracts import AgentOutput
    from tda.agents.provider.base import OutputT
    from tda.contracts import Verdict
    from tda.policy import Policy

QUESTION = "Why is the January room nights sold figure flagged?"


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


@pytest.fixture
def verdict() -> Verdict:
    return verdict_with(material_finding(), definitional_finding())


def inventory_finding() -> Finding:
    """A `room_nights_available` finding, whose source is a CSV and has no page anywhere.

    D-RNA-01: rooms available is a property attribute, not a reservation one. This finding is the
    reason `InventoryCitation` exists.
    """
    return Finding(
        finding_id="F-0003",
        key=MetricKey(metric=Metric.ROOM_NIGHTS_AVAILABLE, period="2026-01"),
        variance_class=VarianceClass.TRANSCRIPTION,
        severity=Severity.MATERIAL,
        escalates_to=EscalationTarget.HOTEL,
        claimed=Decimal("1860"),
        computed=Decimal("1845"),
        difference=Decimal("15"),
        proposed_correction=Decimal("1845"),
        source_ref=InventoryRef(file="inventory_2026-Q1.csv", row_start=1, row_end=31),
        excel_ref=ExcelRef(sheet="Occupancy", cell="D5"),
        clause="D-RNA-01",
    )


def answered(*citations: object, prose: str = "F-0001 does not reconcile.") -> CitedAnswer:
    return CitedAnswer(
        question=QUESTION,
        outcome="answered",
        prose=prose,
        citations=tuple(citations),  # type: ignore[arg-type]
    )


def declined(reason: str = "this verdict covers 2026-Q1 only") -> CitedAnswer:
    return CitedAnswer(question=QUESTION, outcome="declined", reason=reason)


# Figures a test can assert on. The stub reports zeroes, which makes "the tokens reached the
# trace" a comparison of two constants that agree by accident.
_COST = TokenUsage(input_tokens=1_812, output_tokens=147)


class _CountingProvider:
    """A stub that reports what a call cost.

    Small enough to read, and the only thing it adds over `StubProvider` is the one thing the
    recording requirement is about.
    """

    mode = ProviderMode.STUB

    def __init__(self, answer: AgentOutput, seen: list[str] | None = None) -> None:
        self._answer = answer
        self._seen = seen

    def complete(self, request: ModelRequest, output_type: type[OutputT]) -> ModelResponse:
        del output_type
        if self._seen is not None:
            self._seen.extend(message.content for message in request.messages)
        return ModelResponse(
            parsed=self._answer,
            raw_json=self._answer.model_dump_json(),
            usage=_COST,
            cassette_key=request.cassette_key,
            mode=self.mode,
            duration_ms=412,
        )


class _RefusingProvider:
    """A provider that rejects the answer as not being the contract.

    What a live call does when the model returns prose with an empty `citations` array: the
    response is validated against `CitedAnswer` inside `client.messages.parse`, and an uncited
    an `answered` outcome with no citation cannot be built.
    """

    mode = ProviderMode.ANTHROPIC

    def complete(self, request: ModelRequest, output_type: type[OutputT]) -> ModelResponse:
        raise OutputValidationError(
            f"agent {request.agent!r} returned something that is not a {output_type.__name__}: "
            "an answered outcome must carry at least one citation"
        )


def runner_for(policy: Policy, verdict: Verdict, answer: object | None = None) -> AgentRunner:
    provider = StubProvider()
    if answer is not None:
        provider.register(answer)
    return AgentRunner(
        provider,
        policy=policy,
        registry=build_registry(verdict),
        trace=TraceLog(),
        usage=UsageLedger(),
    )


# ── the contract: an answer that cannot exist without a citation ─────────────


def test_an_answer_with_no_citation_cannot_be_constructed() -> None:
    """The headline guarantee, and the reason it is a schema constraint rather than prompt advice:
    a model that returns confident prose with nothing behind it must fail *before* anything can
    render it, not be caught by a check somebody may skip."""
    with pytest.raises(ValueError, match="at least one citation"):
        CitedAnswer(
            question=QUESTION,
            outcome="answered",
            prose="Because the January figure is wrong.",
            citations=(),
        )


def test_a_decline_carries_no_citations_and_reads_as_a_refusal() -> None:
    """A refusal rests on nothing, and attaching a citation to one would put a reference under a
    sentence that makes no claim."""
    declined = CitedAnswer(
        question=QUESTION, outcome="declined", reason="this verdict covers 2026-Q1 only"
    )

    assert not declined.is_answer
    assert declined.citations == ()
    assert declined.text == "this verdict covers 2026-Q1 only"


def test_a_decline_with_no_stated_reason_cannot_be_constructed() -> None:
    """Constrained like the prose is, and for a reason an officer meets directly: an empty refusal
    renders as an empty box, which reads as the tool having broken rather than as it having
    declined - and those call for different actions."""
    with pytest.raises(ValueError, match="must say why"):
        CitedAnswer(question=QUESTION, outcome="declined", reason="   ")


def test_an_answer_and_a_decline_are_told_apart_by_truthiness() -> None:
    """`is_answer` is the predicate every call site uses. It is a property rather than `__bool__`
    because a falsy pydantic model is dropped by the SDK's own `if content.parsed_output:` check -
    see `test_a_contract_is_never_falsy_however_the_agent_answered`."""
    assert answered(PdfCitation(ref=material_finding().source_ref)).is_answer  # type: ignore[arg-type]
    assert not CitedAnswer(question=QUESTION, outcome="declined", reason="no").is_answer


# ── the fabrication check ────────────────────────────────────────────────────


def test_a_page_no_finding_cites_is_refused(verdict: Verdict) -> None:
    """The expensive fabrication: a well-formed reference into a file that really exists, leading
    to rows that have nothing to do with the finding. An officer following it finds *a* page."""
    invented = PdfCitation(ref=PdfRef(file="pms_2026-01.pdf", page=9, row_start=1, row_end=4))

    with pytest.raises(AssistError, match="which no finding in run"):
        check_citations(answered(invented), verdict)


def test_a_cell_no_finding_cites_is_refused(verdict: Verdict) -> None:
    """A workbook has thousands of cells and every one of them looks like a citation."""
    with pytest.raises(AssistError, match="thousands of cells"):
        check_citations(
            answered(ExcelCitation(ref=ExcelRef(sheet="Occupancy", cell="Z99"))), verdict
        )


def test_an_inventory_reference_no_finding_cites_is_refused() -> None:
    """The one source with no page is checked like the other two, rather than trusted because it
    is unusual."""
    verdict = verdict_with(inventory_finding())
    invented = InventoryCitation(
        ref=InventoryRef(file="inventory_2026-Q1.csv", row_start=40, row_end=44)
    )

    with pytest.raises(AssistError, match="which no finding in run"):
        check_citations(answered(invented), verdict)


def test_a_well_formed_clause_the_definitions_do_not_define_is_refused(verdict: Verdict) -> None:
    """`CLAUSE_PATTERN` accepts any well-formed id. Only the document decides which of them denote
    a rule, which is the same argument `resolve_label` makes about a two-letter country code."""
    invented = ClauseCitation(clause="D-ZZZ-99", policy_version=verdict.policy_version)

    with pytest.raises(AssistError, match="definitions do not define"):
        check_citations(answered(invented), verdict)


def test_a_clause_cited_under_another_policy_version_is_refused(verdict: Verdict) -> None:
    """A clause without the version it was read from is a citation of a moving target - the exact
    failure `Verdict.policy_version` exists to prevent."""
    stale = ClauseCitation(clause="D-MAT-06", policy_version="0.9.0")

    with pytest.raises(AssistError, match="citation of a moving target"):
        check_citations(answered(stale), verdict)


def test_the_citations_a_finding_actually_carries_are_accepted(verdict: Verdict) -> None:
    """The check must not be strict by being useless: the references `get_evidence` shows the agent
    are exactly the ones an answer may cite back.

    **Both arrays**, and the definitional half is the consequential one. An officer asking "which
    rows show the February occupancy difference?" gets a correct citation, and a check that looked
    only in `findings` would answer them with *"the assistant cited evidence this run does not
    contain - treat anything else it has told you today with suspicion"*.
    """
    clerical, definitional = material_finding(), definitional_finding()
    check_citations(
        answered(
            PdfCitation(ref=clerical.source_ref),  # type: ignore[arg-type]
            ExcelCitation(ref=clerical.excel_ref),  # type: ignore[arg-type]
            ClauseCitation(clause=clerical.clause, policy_version=verdict.policy_version),
            PdfCitation(ref=definitional.source_ref),  # type: ignore[arg-type]
            ExcelCitation(ref=definitional.excel_ref),  # type: ignore[arg-type]
        ),
        verdict,
    )


def test_a_decline_is_not_put_through_the_citation_check(verdict: Verdict) -> None:
    """A refusal has no citations by construction, and a check that treated its emptiness as a
    fabrication would make declining impossible - which is the outcome three of the eval cases
    exist to require."""
    check_citations(
        CitedAnswer(question=QUESTION, outcome="declined", reason="nothing here"), verdict
    )


# ── the tools: three lookups, no arithmetic, no file read ────────────────────


def test_the_registry_holds_exactly_the_three_tools_the_roster_declares(verdict: Verdict) -> None:
    """The allowlist and the registry are two statements of the same permission, and a fourth tool
    registered but not declared - or declared but not registered - is how one of them stops being
    true without anybody noticing."""
    assert build_registry(verdict).names() == ROSTER[REVIEWER_ASSIST].tools
    assert build_registry(verdict).names() == {
        "query_verdict",
        "get_evidence",
        "get_policy_clause",
    }


def test_a_tool_outside_the_allowlist_is_refused_at_the_call(
    policy: Policy, verdict: Verdict
) -> None:
    """There is no arithmetic tool and no file read, and the enforcement is at the call rather than
    in the prompt. An agent reaching for one must be stopped there, with the attempt recorded."""
    spec = AgentSpec.from_policy(REVIEWER_ASSIST, CitedAnswer, policy)
    session = runner_for(policy, verdict).session(spec)

    with pytest.raises(ToolNotAllowedError):
        session.call("read_values")


def test_no_tool_returns_a_figure_from_the_verdict(verdict: Verdict) -> None:
    """The narrative agent's rule, applied on the surface where it matters most: the officer reads
    the answer on the same screen as figures that were computed from reservation records, and a
    number in prose is indistinguishable from one of them.

    Asserted against the verdict's own figures rather than against a digit pattern - page numbers
    and row ranges are positions and are allowed."""
    finding = material_finding()
    figures = {str(finding.claimed), str(finding.computed), str(finding.difference)}
    rendered = query_verdict(verdict) + get_evidence(verdict, finding.finding_id)

    assert not [figure for figure in figures if figure in rendered]


def test_the_verdict_view_separates_definitional_items_from_hotel_errors(
    verdict: Verdict,
) -> None:
    """D-MAT-06 in the one place the agent forms its idea of the run. A view that listed them
    together would make "how many hotel errors?" answerable only by getting it wrong."""
    rendered = query_verdict(verdict)
    hotel_half, definitional_half = rendered.split("definitional items")

    assert "F-0001" in hotel_half and "F-0001" not in definitional_half
    assert "F-0002" in definitional_half and "F-0002" not in hotel_half
    assert "never counted as hotel errors" in rendered


def test_asking_about_a_finding_the_verdict_does_not_have_says_so_rather_than_raising(
    verdict: Verdict,
) -> None:
    """The agent discovering a question is unanswerable should lead to a decline, not a crash - so
    the tool answers the question it was asked, which is "is there an F-9999?"."""
    assert "no finding 'F-9999'" in query_verdict(verdict, "F-9999")
    assert "no finding 'F-9999'" in get_evidence(verdict, "F-9999")


def test_the_evidence_view_shows_the_reference_fields_an_answer_must_cite_back(
    verdict: Verdict,
) -> None:
    """Showing `p.4 rows 12-18` and then rejecting an answer that did not know `page=4` would be a
    trap of the wiring's making."""
    rendered = get_evidence(verdict, "F-0001")

    assert "page=4" in rendered and "row_start=12" in rendered and "row_end=18" in rendered
    assert "sheet='Occupancy'" in rendered and "cell='B5'" in rendered


def test_a_finding_with_no_cell_says_there_is_nothing_to_cite() -> None:
    """A typed absence is a statement that there is nothing to point at, and an answer citing one
    would be citing the absence of evidence as evidence."""
    from tda.contracts import NotReached

    halted = Finding(
        finding_id="F-0004",
        key=MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period="2026-01"),
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        source_ref=PdfRef(file="pms_2026-01.pdf", page=4, row_start=12, row_end=18),
        excel_ref=NotReached(reason="extraction halted before claim parsing"),
        clause="D-EV-02",
    )

    assert "nothing here to cite" in get_evidence(verdict_with(halted), "F-0004")


def test_every_clause_a_finding_can_cite_is_one_the_lookup_can_read_back() -> None:
    """The clause tool reads `docs/01-definitions.md`, and a clause a finding cites but the tool
    cannot find would make "which rule?" unanswerable for exactly that finding."""
    clauses = load_clauses()

    assert "D-MAT-06" in clauses
    assert "D-RNA-01" in clauses
    assert "**" not in clauses["D-MAT-06"], "markdown emphasis reached the agent's view"


def test_an_unknown_clause_is_told_not_to_be_cited(verdict: Verdict) -> None:
    del verdict
    assert "Do not cite it" in get_policy_clause("D-ZZZ-99")


# ── the driver, end to end through the stub ──────────────────────────────────


def test_an_answer_about_a_different_question_is_rejected(policy: Policy, verdict: Verdict) -> None:
    """The question is echoed back so a misrouted answer is detectable rather than merely
    unlikely."""
    misrouted = CitedAnswer(
        question="Something else entirely", outcome="declined", reason="not what was asked"
    )

    with pytest.raises(AssistError, match="this one is misrouted"):
        ask(QUESTION, verdict, runner_for(policy, verdict, misrouted), policy)


def test_a_fabricated_citation_raises_rather_than_being_downgraded_to_a_decline(
    policy: Policy, verdict: Verdict
) -> None:
    """An abstention is a considered refusal. Relabelling a fabrication as one would put the two in
    the same bucket in every eval and every trace that follows."""
    invented = answered(
        PdfCitation(ref=PdfRef(file="pms_2026-01.pdf", page=9, row_start=1, row_end=2))
    )

    with pytest.raises(AssistError):
        ask(QUESTION, verdict, runner_for(policy, verdict, invented), policy)


def test_a_good_answer_reaches_the_trace_with_its_question_citations_and_tokens(
    policy: Policy, verdict: Verdict
) -> None:
    """PRD-93's recording requirement, asserted on the record rather than on a log line. The trace
    is where an auditor six weeks later reconstructs what the officer was told.

    The provider reports real figures rather than the stub's zeroes, so "the tokens spent" is an
    assertion about the wiring rather than about two constants agreeing."""
    finding = material_finding()
    runner = AgentRunner(
        _CountingProvider(answered(PdfCitation(ref=finding.source_ref))),  # type: ignore[arg-type]
        policy=policy,
        registry=build_registry(verdict),
        trace=TraceLog(),
        usage=UsageLedger(),
    )

    result = ask(QUESTION, verdict, runner, policy)
    record = runner.trace[0]
    written = json.loads(record.output_json or "{}")

    assert record.agent == REVIEWER_ASSIST
    assert written["question"] == QUESTION
    assert written["citations"][0]["ref"]["page"] == finding.source_ref.page  # type: ignore[union-attr]
    assert (record.input_tokens, record.output_tokens) == (_COST.input_tokens, _COST.output_tokens)
    assert result.output.text == "F-0001 does not reconcile."


# ── the agent is actually shown the verdict ──────────────────────────────────


def test_the_agent_is_shown_the_verdict_rather_than_three_tool_descriptions(
    policy: Policy, verdict: Verdict
) -> None:
    """The defect this test exists for was the whole feature being decorative.

    `ModelRequest` carries text, not tool calls, so a driver that builds a session and never calls
    through it hands the model three tool *descriptions* and no run. Every honest answer it could
    give would then cite something it had to invent, `check_citations` would correctly refuse it,
    and the officer would read "the assistant cited evidence this run does not contain" on every
    question - an inversion that looks like a working fabrication check.
    """
    seen: list[str] = []
    runner = AgentRunner(
        _CountingProvider(
            CitedAnswer(question=QUESTION, outcome="declined", reason="nothing"), seen=seen
        ),
        policy=policy,
        registry=build_registry(verdict),
        trace=TraceLog(),
    )

    ask(QUESTION, verdict, runner, policy)
    shown = seen[0]

    assert verdict.run_id in shown, "the agent was not told which run it is answering about"
    assert verdict.policy_version in shown, "it cannot cite a clause without the policy version"
    for finding in (*verdict.findings, *verdict.definitional_items):
        assert finding.finding_id in shown
        assert f"page={finding.source_ref.page}" in shown  # type: ignore[union-attr]
        assert f"cell={finding.excel_ref.cell!r}" in shown  # type: ignore[union-attr]
    assert QUESTION in shown


def test_every_tool_the_agent_is_given_is_actually_called(policy: Policy, verdict: Verdict) -> None:
    """A tool described in the prompt and never called is a tool whose output the agent must
    invent. The trace records which of them ran, so this is checkable after the fact rather than
    only in the roster."""
    runner = runner_for(
        policy, verdict, CitedAnswer(question=QUESTION, outcome="declined", reason="nothing")
    )

    ask(QUESTION, verdict, runner, policy)
    called = {call.name for call in runner.trace[0].tool_calls}

    assert called == ROSTER[REVIEWER_ASSIST].tools
    assert not runner.trace[0].refused_tools


def test_no_count_and_no_figure_reaches_the_agent(verdict: Verdict) -> None:
    """The narrative agent's rule, and the trap is that `hotel_error_count` and `claims_checked`
    are metric-function outputs that do not look like figures in a listing.

    A model shown `hotel_errors: 1` will restate it, and a count in prose is indistinguishable on
    an officer's screen from the one the memo computed. The agent names findings by id instead;
    the counts are on the screen already.
    """
    rendered = query_verdict(verdict)
    clerical = material_finding()

    # The figures the finding carries, which are the obvious half.
    assert not [
        figure
        for figure in (clerical.claimed, clerical.computed, clerical.difference)
        if str(figure) in rendered
    ]
    # And the counts, which are the half that does not look like a figure in a listing. Asserted
    # on the labels rather than on the digits: `1` also appears in a page number, and a test that
    # banned every `1` would ban the citations the agent has to cite back.
    assert "claims_checked" not in rendered
    assert "hotel_errors" not in rendered
    # The listing headers carry no count either - `findings (2):` is the same leak wearing a
    # different hat, and it is what the agent would reach for to answer "how many?".
    for header in ("findings against the hotel:", "never counted as hotel errors (D-MAT-06):"):
        assert header in rendered
    assert not re.search(r"\((\d+)\):", rendered), "a listing header states a count"


def test_a_truncated_listing_says_what_it_left_out_and_how_to_reach_it(policy: Policy) -> None:
    """A verdict with more findings than fit is legible or it is a lie by omission. Both halves
    elide the same way, because a definitional list that truncated silently would put findings
    beyond the agent's reach with nothing saying so."""
    del policy
    many = verdict_with(
        *(
            material_finding().model_copy(update={"finding_id": f"F-{index:04d}"})
            for index in range(1, MAX_LISTED + 4)
        ),
        definitional_finding().model_copy(update={"finding_id": "F-9999"}),
    )

    rendered = query_verdict(many)

    assert "and 3 more" in rendered
    assert "ask about any of them by id" in rendered
    assert f"F-{MAX_LISTED + 3:04d}" not in rendered
    # The definitional half is short here, so nothing of it is hidden - the point being that the
    # two halves elide through one function rather than one of them eliding silently.
    assert "F-9999" in rendered


def test_what_cannot_be_cited_is_named_as_uncitable(verdict: Verdict) -> None:
    """`not_verifiable` and `out_of_scope_claims` carry no references, so an answered outcome about one
    is unconstructible. The agent must be told that rather than left to discover it by having its
    answer refused."""
    from tda.contracts import MetricKey, NotVerifiable

    key = MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-03")
    widened = verdict.model_copy(
        update={
            "not_verifiable": (
                NotVerifiable(
                    key=key,
                    reason="unreadable_source_pages",
                    detail="pages 7-9 of pms_2026-03.pdf could not be parsed",
                ),
            ),
            "out_of_scope_claims": (
                MetricKey(metric=Metric.ROOM_NIGHTS_AVAILABLE, period="2025-12"),
            ),
        }
    )

    rendered = query_verdict(widened)

    assert "nothing to cite about them" in rendered
    assert "citable either" in rendered
    assert "unreadable_source_pages" in rendered


def test_a_findings_clause_is_always_in_the_agents_view(verdict: Verdict) -> None:
    """*Which rule?* is the question this agent is asked most, and the clause is the answer."""
    rendered = query_verdict(verdict)

    for finding in (*verdict.findings, *verdict.definitional_items):
        assert f"clause={finding.clause}" in rendered


def test_a_citation_kind_nobody_wrote_a_case_for_is_refused_rather_than_accepted(
    verdict: Verdict,
) -> None:
    """The default for the check this module exists for cannot be "accept".

    A fifth member added to the `Citation` union with no case in `check_citations` must fail
    loudly. Simulated here with an object the match cannot place, because adding a real fifth
    member to the union to test this would be testing the union.
    """
    # `model_construct` skips validation, which is the only way to get a shape the union forbids
    # in front of the check. The alternative - adding a fifth member to the union - would be
    # testing the union rather than the check that has to keep up with it.
    unknown: Any = object()
    forged = CitedAnswer.model_construct(
        question=QUESTION, outcome="answered", prose="see here", citations=(unknown,)
    )

    with pytest.raises(AssistError, match="unhandled citation kind"):
        check_citations(forged, verdict)


def test_an_unreadable_definitions_file_is_a_value_rather_than_a_traceback(
    policy: Policy, verdict: Verdict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`put_question` never raises, and `docs/01-definitions.md` does not exist beside an installed
    wheel. An officer meeting a Streamlit traceback learns nothing about whether to trust the
    verdict in front of them."""
    import tda.agents.reviewer_assist as agent_module

    monkeypatch.setattr(agent_module, "DEFINITIONS_PATH", tmp_path / "gone" / "definitions.md")

    result = put_question(QUESTION, verdict, tmp_path, StubProvider(), policy, record=False)

    assert isinstance(result, NoAnswer)
    assert "definitions" in result.text


def test_an_uncited_answer_reads_as_a_refusal_rather_than_as_a_setup_problem(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """The headline guarantee's *rendering* path, which is a different thing from the constraint.

    The validator rejects prose with nothing behind it inside the provider, and the officer
    must read "it cannot tell you anything it cannot show you" - not "the assistant is not
    configured on this machine", which is a setup problem they would go and try to fix.
    """
    result = put_question(QUESTION, verdict, tmp_path, _RefusingProvider(), policy, record=False)

    assert isinstance(result, NoAnswer)
    assert result.reason is Unavailable.UNCITED
    assert "cannot show you" in unavailable_line(result)


def test_a_live_contract_violation_is_not_laundered_into_a_transport_failure() -> None:
    """`anthropic` mode is the only mode where a model can actually emit prose with no citation, so
    an `except Exception` there that wrapped schema validation into a bare `ProviderError` made the
    branch above unreachable in exactly the mode that needs it."""
    import inspect

    from tda.agents.provider import anthropic_client

    source = inspect.getsource(anthropic_client.AnthropicProvider.complete)
    validation = source.index("except ValidationError")
    catch_all = source.index("except Exception")

    assert validation < catch_all, "the catch-all shadows the contract violation"


# ── the review-screen layer ──────────────────────────────────────────────────


def test_a_question_is_recorded_into_the_run_s_own_trace(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """Appended beside the calls that produced the verdict, rather than held in a session that a
    closed laptop discards."""
    (tmp_path / "trace.jsonl").write_text("", encoding="utf-8")
    provider = StubProvider()
    provider.register(answered(PdfCitation(ref=material_finding().source_ref)))  # type: ignore[arg-type]

    result = put_question(QUESTION, verdict, tmp_path, provider, policy)
    lines = [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert isinstance(result, Answer)
    assert len(lines) == 1
    assert lines[0]["agent"] == REVIEWER_ASSIST
    assert QUESTION in lines[0]["output_json"]


def test_an_email_address_typed_into_the_question_box_does_not_reach_the_trace(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """The question box is the one path by which free text a human typed enters an artifact *after*
    the run has finished, so it goes through the same `redact` the run's own artifacts do.

    What that buys is the structural patterns `redact` catches - an email, a phone number, a
    booking reference. It is **not** a guarantee about a bare name: `tda.obs.redact` says so at
    length in its own docstring, and the test below records the gap rather than implying it away.
    """
    (tmp_path / "trace.jsonl").write_text("", encoding="utf-8")
    question = "Did the booking for sarah.okonkwo@example.com land in the January rows?"
    provider = StubProvider()
    provider.register(
        CitedAnswer(
            question=question, outcome="declined", reason="guest identity is not in evidence"
        )
    )

    put_question(question, verdict, tmp_path, provider, policy)
    written = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")

    assert "sarah.okonkwo@example.com" not in written
    assert "redacted" in written


def test_a_bare_name_in_a_question_is_not_caught_and_this_is_recorded_rather_than_claimed(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """The limit of the paragraph above, asserted so nobody has to take its word for it.

    `redact` matches structure - an `@`, a digit run, a reference format - and a guest's name has
    none. It reaches the trace verbatim. This test exists so the claim in `tda.review.assist` and
    ADR-0009 §5 stays the narrow one it can support, and so that a future redactor that *does*
    catch names fails here and gets its docstrings updated.
    """
    (tmp_path / "trace.jsonl").write_text("", encoding="utf-8")
    question = "Did Sarah Okonkwo's booking land in the January rows?"
    provider = StubProvider()
    provider.register(
        CitedAnswer(
            question=question, outcome="declined", reason="guest identity is not in evidence"
        )
    )

    put_question(question, verdict, tmp_path, provider, policy)

    assert "Sarah Okonkwo" in (tmp_path / "trace.jsonl").read_text(encoding="utf-8")


def test_an_answer_that_could_not_be_given_is_still_recorded(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """A trace carrying only the questions that were answered cannot show that the assistant was
    asked something it could not do, which is the half an auditor most wants."""
    (tmp_path / "trace.jsonl").write_text("", encoding="utf-8")
    invented = answered(
        PdfCitation(ref=PdfRef(file="pms_2026-01.pdf", page=9, row_start=1, row_end=2))
    )
    provider = StubProvider()
    provider.register(invented)

    result = put_question(QUESTION, verdict, tmp_path, provider, policy)
    written = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")

    assert isinstance(result, NoAnswer)
    assert result.reason is Unavailable.FABRICATED_CITATION
    assert written.strip(), "a refused answer left no record that the question was asked"


def test_a_fabricated_citation_withholds_the_prose_rather_than_annotating_it(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """A citation that does not exist is not a caveat. An officer shown the answer with a warning
    beside it has read the answer."""
    prose = "The January figure is wrong because page nine shows a different total."
    provider = StubProvider()
    provider.register(
        answered(
            PdfCitation(ref=PdfRef(file="pms_2026-01.pdf", page=9, row_start=1, row_end=2)),
            prose=prose,
        )
    )

    result = put_question(QUESTION, verdict, tmp_path, provider, policy, record=False)

    assert isinstance(result, NoAnswer)
    assert prose not in result.text
    assert prose not in unavailable_line(result)


def test_each_reason_an_answer_is_missing_reads_differently(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """ "We have not recorded this" and "it invented a reference" call for opposite actions from the
    officer, and a single "something went wrong" would collapse them."""
    del policy, verdict, tmp_path
    lines = {
        unavailable_line(NoAnswer(question=QUESTION, reason=reason, detail=""))
        for reason in Unavailable
    }

    assert len(lines) == len(Unavailable)
    fabricated = unavailable_line(
        NoAnswer(question=QUESTION, reason=Unavailable.FABRICATED_CITATION, detail="")
    )
    assert "suspicion" in fabricated


def test_a_replay_miss_is_reported_as_unmeasured_rather_than_as_a_broken_screen(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """No cassettes are committed, so this is the path an officer will actually meet today. It must
    not read as a defect in the verdict."""
    from tda.agents.provider import ReplayProvider

    result = put_question(
        QUESTION, verdict, tmp_path, ReplayProvider(tmp_path / "none"), policy, record=False
    )

    assert isinstance(result, NoAnswer)
    assert result.reason is Unavailable.NOT_RECORDED
    assert "Nothing is wrong with the verdict" in unavailable_line(result)


def test_an_empty_question_is_never_put_to_the_agent(
    policy: Policy, verdict: Verdict, tmp_path: Path
) -> None:
    """A model call on an empty box costs money and produces an answer to nothing."""
    provider = StubProvider()

    result = put_question("   ", verdict, tmp_path, provider, policy)

    assert isinstance(result, NoAnswer)
    assert result.reason is Unavailable.NOT_ASKED
    assert not (tmp_path / "trace.jsonl").exists()


def test_a_document_pasted_into_the_question_box_is_refused() -> None:
    """A question longer than a paragraph is usually a document, and a document in this box is how
    a guest list enters a run's trace."""
    with pytest.raises(ValueError, match=str(MAX_QUESTION)):
        check_question("x" * (MAX_QUESTION + 1))


def test_the_question_reaches_the_agent_exactly_as_it_was_typed() -> None:
    """A question rewritten on the way in is one the officer cannot recognise when they read the
    trace back."""
    typed = "  Which rule makes F-0002 definitional rather than an error?  "

    assert check_question(typed) == typed.strip()


def test_a_citation_renders_the_same_way_under_an_answer_as_under_its_finding() -> None:
    """Two renderings of one reference is how an officer ends up comparing `p.4 rows 12-18` with
    `page 4, rows 12 to 18` and deciding they are different places."""
    finding = material_finding()
    result = Answer(
        question=QUESTION,
        answer=answered(PdfCitation(ref=finding.source_ref)),  # type: ignore[arg-type]
        trace=_a_trace_record(),
    )

    assert answer_citations(result) == (finding.source_ref.citation,)


def _a_trace_record() -> TraceRecord:
    return TraceRecord(
        agent=REVIEWER_ASSIST,
        prompt_version="v1",
        model_id="stub",
        effort="high",
        provider_mode="stub",
        output_contract="CitedAnswer",
        output_json="{}",
    )


def test_the_screen_asks_only_through_the_headless_layer() -> None:
    """The screen is a shell, and the point of `tda.review.assist` is that everything which can be
    wrong - the fabrication check, the trace append, the replay miss - lives somewhere a test can
    reach without a browser.

    An earlier version of this test asserted that importing the module renders nothing, which
    `test_review.py` already proves properly by importing it in a subprocess against a broken
    verdict. This asserts the thing that is new: a screen calling `ask` directly would bypass the
    recording and the failure handling, and every test in this file would still pass.
    """
    import inspect

    import tda.review.app as app

    source = (Path(__file__).resolve().parents[2] / "src/tda/review/app.py").read_text("utf-8")

    assert "put_question" in source
    assert "reviewer_assist import" not in source, "the screen reaches past tda.review.assist"
    # PRD-93 asks for the box on the findings screen. A `_ask_box` nobody calls is a question box
    # that exists in the module and not on the screen, and every other test here would still pass.
    assert "_ask_box(" in inspect.getsource(app.main)


def test_a_policy_asking_for_anthropic_is_refused_when_live_mode_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`app._provider()` used to call `tda.cli.build_provider` directly, so a deployment whose
    `policy.yaml` said `anthropic` would reach the SDK regardless of where it was hosted. Routed
    through the live-mode gate, the same policy is refused on screen instead - the failure an
    officer sees names why, rather than an opaque error three layers down in the client."""
    import tda.review.app as app
    from tda.review.live import LiveModeError

    monkeypatch.delenv("MIZAN_LIVE_MODE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    policy = load_policy()
    live_policy = policy.model_copy(
        update={"model": policy.model.model_copy(update={"provider": "anthropic"})}
    )
    monkeypatch.setattr(app, "load_policy", lambda: live_policy)

    with pytest.raises(LiveModeError, match="live mode is off"):
        app._provider()
