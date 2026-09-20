"""The agent runtime: five things, a code router, and a trace that survives failure.

Three properties carry the weight here, and each corresponds to a claim the POC makes out loud.

**An allowlist is enforced, not described.** The tests below call a tool an agent does not have and
assert it raises — and assert the refusal is *recorded*, because an enforcement nobody can audit
afterwards is only half of one.

**A trace exists even when the call fails.** A trace written only on success is silent about the
one call anybody will want to read about, so the failure paths are tested harder than the happy one.

**The supervisor refuses rather than truncating.** A budget that quietly stops calling agents
produces a verdict indistinguishable from a complete one, which is the failure mode that would
actually reach a reviewer.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from tda.agents import (
    AgentError,
    AgentRunner,
    AgentSpec,
    BudgetExceededError,
    Disposition,
    Supervisor,
    Tool,
    ToolNotAllowedError,
    ToolNotRegisteredError,
    ToolRegistry,
)
from tda.agents.contracts import (
    CitedAnswer,
    CriticVerdict,
    FindingNarrative,
    LabelResolution,
)
from tda.agents.critic import grade, render_case
from tda.agents.narrative import build_registry as narrative_registry
from tda.agents.narrative import narrate, read_finding
from tda.agents.prompts.registry import PromptRegistry
from tda.agents.provider import Effort, Message, ProviderError, StubProvider
from tda.agents.resolution import (
    Candidate,
    fuzzy_candidates,
    iso_lookup,
    render_candidates,
    resolve_label,
)
from tda.agents.resolution import (
    build_registry as resolution_registry,
)
from tda.agents.roster import CRITIC, NARRATIVE, RESOLUTION, ROSTER
from tda.agents.supervisor import Budget
from tda.agents.tools import ToolError
from tda.contracts import (
    EscalationTarget,
    ExcelRef,
    Finding,
    Metric,
    MetricKey,
    PdfRef,
    Severity,
    VarianceClass,
)
from tda.obs import TraceLog, TraceRecord, UsageLedger
from tda.policy import Policy, load_policy

if TYPE_CHECKING:
    from pathlib import Path

PDF = PdfRef(file="2026-01.pdf", page=3, row_start=12, row_end=12)
XL = ExcelRef(sheet="Nationality", cell="D14")


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


def finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "finding_id": "F-0001",
        "key": MetricKey(metric=Metric.OCCUPANCY_PCT, period="2026-01"),
        "variance_class": VarianceClass.TRANSCRIPTION,
        "severity": Severity.MATERIAL,
        "escalates_to": EscalationTarget.HOTEL,
        "claimed": Decimal("71.40"),
        "computed": Decimal("68.20"),
        "difference": Decimal("3.20"),
        "source_ref": PDF,
        "excel_ref": XL,
        "clause": "D-OCC-01",
    }
    return Finding(**(base | overrides))  # type: ignore[arg-type]


def echo_registry() -> ToolRegistry:
    """Two harmless tools, so a session has something to allow and something to refuse."""
    return ToolRegistry(
        (
            Tool(name="read_finding", description="the finding", fn=lambda: "a finding"),
            Tool(name="read_evidence", description="the evidence", fn=lambda: "some evidence"),
            Tool(name="read_values", description="forbidden to everyone", fn=lambda: "1204"),
        )
    )


# ── the tool registry and its allowlists ─────────────────────────────────────


def test_a_tool_outside_the_allowlist_raises_rather_than_returning_nothing() -> None:
    """An empty result would convert a contract violation into a retry loop, and the run would
    succeed with no record that an agent reached outside its surface."""
    session = echo_registry().session(NARRATIVE, {"read_finding", "read_evidence"})

    with pytest.raises(ToolNotAllowedError, match="not in its allowlist"):
        session.call("read_values")


def test_a_refused_call_is_still_recorded() -> None:
    """The refusals are the interesting half of the trace. A log of only the successful calls
    cannot answer what the agent *tried* to do."""
    session = echo_registry().session(NARRATIVE, {"read_finding"})
    session.call("read_finding")
    with pytest.raises(ToolNotAllowedError):
        session.call("read_values")

    assert [(c.name, c.allowed) for c in session.calls] == [
        ("read_finding", True),
        ("read_values", False),
    ]
    assert [c.name for c in session.refusals()] == ["read_values"]


def test_an_allowlist_naming_an_unregistered_tool_fails_at_wiring_time() -> None:
    """Caught at construction, not at the call. A silently-ignored allowlist entry grants nothing
    while looking like it granted something."""
    with pytest.raises(ToolNotRegisteredError, match="no tool named 'invented'"):
        echo_registry().session(NARRATIVE, {"read_finding", "invented"})


def test_the_allowlist_is_checked_before_the_registry() -> None:
    """A tool that exists but is not this agent's gets the allowlist error, not the registration
    one. The two mean different things: a roster defect versus a wiring defect."""
    session = echo_registry().session(CRITIC, set())

    with pytest.raises(ToolNotAllowedError):
        session.call("read_finding")


def test_registering_a_duplicate_name_is_an_error() -> None:
    """Silent replacement would make the tool an agent gets depend on registration order."""
    registry = echo_registry()
    with pytest.raises(ToolError, match="already registered"):
        registry.register(Tool(name="read_finding", description="again", fn=lambda: ""))


def test_tool_descriptions_render_in_a_stable_order() -> None:
    """The description text is hashed into the cassette key. A set iterating differently on
    another interpreter would give the same permissions two different keys."""
    registry = echo_registry()
    first = registry.describe({"read_evidence", "read_finding"})
    second = registry.describe({"read_finding", "read_evidence"})

    assert first == second
    assert first.index("read_evidence") < first.index("read_finding")


def test_an_agent_with_no_tools_is_told_so_rather_than_shown_an_empty_list() -> None:
    """The critic's empty allowlist is deliberate, and a bare heading followed by nothing reads
    as a formatting failure rather than as a design decision."""
    assert "no tools" in echo_registry().describe(set())


def test_the_critic_has_no_tools_and_the_roster_says_so() -> None:
    """An agent that can fetch its own evidence can find something that makes an ungrounded
    sentence look grounded."""
    assert ROSTER[CRITIC].tools == frozenset()


# ── AgentSpec: four of the five things, or it does not construct ─────────────


def test_a_spec_takes_its_effort_and_prompt_version_from_policy(policy: Policy) -> None:
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)

    assert spec.effort is Effort.HIGH
    assert spec.prompt_version == policy.model.agents[NARRATIVE].prompt_version
    assert spec.output_type is FindingNarrative


def test_a_spec_takes_its_allowlist_from_the_roster_not_from_policy(policy: Policy) -> None:
    """Effort is a tuning decision a reader may reasonably change in a YAML file. A tool allowlist
    is a boundary, and it lives in code where widening it is a reviewed diff."""
    spec = AgentSpec.from_policy(RESOLUTION, LabelResolution, policy)

    assert spec.tools == ROSTER[RESOLUTION].tools == frozenset({"iso_lookup", "fuzzy_candidates"})


def test_an_agent_policy_does_not_configure_cannot_be_specified(policy: Policy) -> None:
    with pytest.raises(AgentError, match="no model settings for agent"):
        AgentSpec.from_policy("invented", FindingNarrative, policy)


def test_every_roster_agent_is_configured_in_policy(policy: Policy) -> None:
    """The roster and policy must agree on who exists. An agent in one and not the other has
    either undeclared permissions or no pinned prompt version."""
    assert set(ROSTER) == set(policy.model.agents)


# ── the runner: request, validation, trace ───────────────────────────────────


def runner_for(policy: Policy, answer: object | None = None) -> AgentRunner:
    provider = StubProvider()
    if answer is not None:
        provider.register(answer)
    return AgentRunner(
        provider,
        policy=policy,
        registry=echo_registry(),
        trace=TraceLog(),
        usage=UsageLedger(),
    )


def a_narrative(**overrides: object) -> FindingNarrative:
    base: dict[str, object] = {
        "finding_id": "F-0001",
        "sentence": "The January occupancy figure does not reconcile with the reservation rows.",
        "cites_permutation": None,
        "is_definitional": False,
    }
    return FindingNarrative(**(base | overrides))  # type: ignore[arg-type]


def test_the_rendered_system_prompt_carries_the_tool_descriptions(policy: Policy) -> None:
    """Granting a tool changes the question the agent was asked, so it must change the cassette
    key. That is occasionally surprising and it is correct."""
    runner = runner_for(policy)
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)
    request = runner.build_request(spec, [Message(role="user", content="go")])

    assert "read_finding" in request.system
    assert "read_values" not in request.system


def test_widening_an_allowlist_changes_the_cassette_key(policy: Policy) -> None:
    runner = runner_for(policy)
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)
    wider = AgentSpec(
        name=spec.name,
        prompt_version=spec.prompt_version,
        output_type=spec.output_type,
        tools=spec.tools | {"read_values"},
        effort=spec.effort,
    )
    message = [Message(role="user", content="go")]

    assert (
        runner.build_request(spec, message).cassette_key
        != runner.build_request(wider, message).cassette_key
    )


def test_an_agent_with_no_messages_is_refused(policy: Policy) -> None:
    """An agent with an empty conversation still returns something, and that something looks like
    an answer."""
    runner = runner_for(policy)
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)

    with pytest.raises(AgentError, match="no messages"):
        runner.build_request(spec, [])


def test_a_successful_call_writes_one_trace_record(policy: Policy) -> None:
    runner = runner_for(policy, a_narrative())
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)
    session = runner.session(spec)
    session.call("read_finding")

    result = runner.run(spec, [Message(role="user", content="go")], session=session)

    assert len(runner.trace) == 1
    record = runner.trace[0]
    assert record.agent == NARRATIVE
    assert record.output_contract == "FindingNarrative"
    assert record.output_json is not None
    assert record.error is None
    assert [c.name for c in record.tool_calls] == ["read_finding"]
    assert result.trace is record


def test_a_failed_call_still_writes_a_trace_record(policy: Policy) -> None:
    """The one call anybody will want to read about later. A trace written only on success is
    silent about exactly the thing that went wrong."""
    runner = runner_for(policy)  # nothing registered, so the stub refuses
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)

    with pytest.raises(ProviderError):
        runner.run(spec, [Message(role="user", content="go")])

    assert len(runner.trace) == 1
    record = runner.trace[0]
    assert record.failed
    assert record.output_json is None
    assert "ProviderError" in (record.error or "")


def test_a_spec_with_an_unrecordable_prompt_version_is_refused(policy: Policy) -> None:
    """`TraceRecord.prompt_version` is pattern-constrained, so a spec carrying a malformed version
    would make the *failure* trace fail to write — and that exception would mask the original
    error, which is the one worth reading."""
    with pytest.raises(AgentError, match="must look like v1"):
        AgentSpec(
            name=NARRATIVE,
            prompt_version="latest",
            output_type=FindingNarrative,
            tools=frozenset(),
            effort=Effort.HIGH,
        )


def test_a_failure_before_a_request_exists_is_still_recorded(policy: Policy) -> None:
    """A run that dies on a missing prompt file must not leave a trace that is silent about the
    only thing that happened."""
    runner = runner_for(policy, a_narrative())
    spec = AgentSpec(
        name=NARRATIVE,
        prompt_version="v99",
        output_type=FindingNarrative,
        tools=frozenset(),
        effort=Effort.HIGH,
    )

    with pytest.raises(Exception, match="no prompt at"):
        runner.run(spec, [Message(role="user", content="go")])

    assert runner.trace[0].failed
    assert runner.trace[0].cassette_key == ""


def test_a_session_belonging_to_another_agent_is_refused(policy: Policy) -> None:
    """A session is one agent's allowlist and one agent's record. Accepting another's would file
    its tool calls — and its refusals — under this agent, which is the one thing a trace must
    never do."""
    runner = runner_for(policy, a_narrative())
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)
    borrowed = runner.registry.session(CRITIC, set())

    with pytest.raises(AgentError, match="belonging to 'critic'"):
        runner.run(spec, [Message(role="user", content="go")], session=borrowed)


def test_refused_tool_calls_reach_the_trace(policy: Policy) -> None:
    runner = runner_for(policy, a_narrative())
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)
    session = runner.session(spec)
    with pytest.raises(ToolNotAllowedError):
        session.call("read_values")

    runner.run(spec, [Message(role="user", content="go")], session=session)

    assert runner.trace[0].refused_tools == ("read_values",)


# ── the runner routes through a bound supervisor ─────────────────────────────


def test_a_runner_with_no_supervisor_routes_nothing(policy: Policy) -> None:
    """The default: `AgentRunner(...)` with no `supervisor=` keyword. Reviewer-assist, narrative
    grading and every existing caller construct a runner this way, and none of them should have to
    start carrying a budget just because one now exists."""
    runner = runner_for(policy, a_narrative())
    assert runner.supervisor is None

    result = runner.run(
        AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy),
        [Message(role="user", content="go")],
    )

    assert result.output.is_answer


def test_every_model_call_is_granted_by_a_bound_supervisor_first(policy: Policy) -> None:
    runner = runner_for(policy, a_narrative())
    runner.supervisor = Supervisor()
    runner.node = "publish"
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)

    runner.run(spec, [Message(role="user", content="go")])

    decisions = runner.supervisor.decisions
    assert len(decisions) == 1
    assert decisions[0].node == "publish"
    assert decisions[0].agent == spec.name
    assert decisions[0].granted
    assert decisions[0].reason == "FindingNarrative requested at publish"


def test_a_spent_budget_refuses_the_call_before_the_provider_is_asked(policy: Policy) -> None:
    """The call never reaches the provider - the registered stub answer is never consumed - and
    the refusal is recorded exactly as a provider failure would be: same trace shape, same
    cassette key, so a reader cannot tell a refused call from a failed one without reading why."""
    runner = runner_for(policy, a_narrative())
    runner.supervisor = Supervisor(budget=Budget(max_calls_per_run=60, max_calls_per_agent=0))
    runner.node = "publish"
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)

    with pytest.raises(BudgetExceededError, match="per-agent limit of 0"):
        runner.run(spec, [Message(role="user", content="go")])

    assert len(runner.trace) == 1
    record = runner.trace[0]
    assert record.failed
    assert record.cassette_key != ""
    assert "BudgetExceededError" in (record.error or "")
    assert runner.supervisor.decisions[-1].disposition is Disposition.REFUSED_BUDGET
    # `.calls` is the stub's own record of what it was asked to serve. Empty means the request
    # never reached the provider at all - the refusal happened one step earlier.
    assert runner.provider.calls == []  # type: ignore[attr-defined]


def test_an_agent_the_roster_does_not_know_is_refused_by_the_supervisor(
    policy: Policy, tmp_path: Path
) -> None:
    """`route()` returns a refused decision rather than raising for an unknown agent - the runner
    is what turns that into something the caller sees, the same way it turns a raised budget error
    into one.

    A wiring defect, not a real agent: `AgentSpec.from_policy` and the roster agree by construction
    (`test_every_roster_agent_is_configured_in_policy`), so reaching this branch through `run()`
    needs a name with a real prompt on disk that the roster has never heard of - a private
    `PromptRegistry` root makes that possible without inventing a seventh agent in the repository.
    """
    (tmp_path / "invented").mkdir()
    (tmp_path / "invented" / "v1.md").write_text("a prompt for an agent nobody registered")

    runner = AgentRunner(
        StubProvider(),
        policy=policy,
        registry=echo_registry(),
        trace=TraceLog(),
        usage=UsageLedger(),
        prompts=PromptRegistry(root=tmp_path),
    )
    runner.supervisor = Supervisor()
    runner.node = "publish"
    spec = AgentSpec(
        name="invented",
        prompt_version="v1",
        output_type=FindingNarrative,
        tools=frozenset(),
        effort=Effort.HIGH,
    )

    with pytest.raises(AgentError, match="refused by the supervisor"):
        runner.run(spec, [Message(role="user", content="go")])

    assert runner.supervisor.decisions[-1].disposition is Disposition.REFUSED_UNKNOWN_AGENT
    assert len(runner.trace) == 1
    assert runner.trace[0].failed


def test_usage_is_tallied_per_agent(policy: Policy) -> None:
    runner = runner_for(policy, a_narrative())
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)
    runner.run(spec, [Message(role="user", content="go")])

    assert runner.usage is not None
    assert runner.usage.total_calls() == 1


def test_a_trace_record_with_neither_output_nor_error_is_rejected() -> None:
    """The shape a swallowed exception takes: it reads as a successful call that returned nothing."""
    with pytest.raises(ValueError, match="neither an output nor an error"):
        TraceRecord(
            agent=NARRATIVE,
            prompt_version="v1",
            model_id="claude-sonnet-5",
            effort="high",
            provider_mode="stub",
            output_contract="FindingNarrative",
        )


def test_a_trace_round_trips_through_jsonl(policy: Policy) -> None:
    """JSON Lines rather than an array, so a trace is readable when a run dies halfway — which is
    exactly when someone wants to read it."""
    runner = runner_for(policy, a_narrative())
    spec = AgentSpec.from_policy(NARRATIVE, FindingNarrative, policy)
    runner.run(spec, [Message(role="user", content="go")])

    restored = TraceLog.from_jsonl(runner.trace.to_jsonl())

    assert restored.records == runner.trace.records


def test_a_trace_refuses_to_report_two_prompt_versions_for_one_agent() -> None:
    """A verdict citing one version would be citing instructions that produced some of its
    findings and not others."""

    def record(version: str) -> TraceRecord:
        return TraceRecord(
            agent=NARRATIVE,
            prompt_version=version,
            model_id="claude-sonnet-5",
            effort="high",
            provider_mode="stub",
            output_contract="FindingNarrative",
            output_json="{}",
        )

    assert TraceLog([record("v1")]).prompt_versions() == {NARRATIVE: "v1"}
    with pytest.raises(ValueError, match="ran under both v1 and v2"):
        TraceLog([record("v1"), record("v2")]).prompt_versions()


# ── the supervisor: code, and a budget that refuses ─────────────────────────


def test_the_supervisor_records_a_skip_as_a_decision() -> None:
    """A run with no unmappable labels should not call the resolution agent, and recording that
    shows the agent was considered rather than forgotten."""
    supervisor = Supervisor()
    decision = supervisor.route(
        "extract", RESOLUTION, needed=False, reason="every label resolved from the lookup"
    )

    assert decision.disposition is Disposition.SKIPPED
    assert not decision.granted
    assert supervisor.calls_made == 0
    assert supervisor.decisions == (decision,)


def test_the_supervisor_spends_the_budget_it_grants() -> None:
    supervisor = Supervisor(budget=Budget(max_calls_per_run=5, max_calls_per_agent=5))
    supervisor.route("extract", RESOLUTION, needed=True, reason="one unmappable label")

    assert supervisor.calls_for(RESOLUTION) == 1
    assert supervisor.calls_made == 1


def test_the_per_agent_cap_stops_a_loop_before_it_spends_the_run() -> None:
    """The cap that matters. A total-only budget lets one runaway agent spend every other agent's
    allowance before anything notices."""
    supervisor = Supervisor(budget=Budget(max_calls_per_run=50, max_calls_per_agent=2))
    for _ in range(2):
        supervisor.route("extract", RESOLUTION, needed=True, reason="retry")

    with pytest.raises(BudgetExceededError, match="per-agent limit of 2"):
        supervisor.route("extract", RESOLUTION, needed=True, reason="retry")

    assert supervisor.calls_for(RESOLUTION) == 2
    assert supervisor.decisions[-1].disposition is Disposition.REFUSED_BUDGET


def test_the_per_run_cap_refuses_rather_than_truncating() -> None:
    """Raising rather than continuing without the call: a verdict built from a pipeline that
    quietly stopped calling agents looks exactly like a complete one."""
    supervisor = Supervisor(budget=Budget(max_calls_per_run=2, max_calls_per_agent=10))
    supervisor.route("publish", NARRATIVE, needed=True, reason="one finding")
    supervisor.route("eval", CRITIC, needed=True, reason="one narrative")

    with pytest.raises(BudgetExceededError, match="per-run limit of 2"):
        supervisor.route("publish", NARRATIVE, needed=True, reason="another finding")


def test_an_agent_outside_the_roster_is_refused_without_spending_anything() -> None:
    supervisor = Supervisor()
    decision = supervisor.route("publish", "invented", needed=True, reason="why not")

    assert decision.disposition is Disposition.REFUSED_UNKNOWN_AGENT
    assert supervisor.calls_made == 0


def test_the_budget_comes_from_policy(policy: Policy) -> None:
    supervisor = Supervisor.from_policy(policy)

    assert policy.model.budget is not None
    assert supervisor.budget.max_calls_per_agent == policy.model.budget.max_calls_per_agent


def test_a_policy_with_no_budget_falls_back_to_a_cap_rather_than_to_none(policy: Policy) -> None:
    """A missing budget is not an excuse for an unbounded run."""
    without = policy.model_copy(update={"model": policy.model.model_copy(update={"budget": None})})

    assert Budget.from_policy(without).max_calls_per_run > 0


# ── resolution: code proposes, the model picks, a human confirms ────────────


def test_fuzzy_candidates_offers_both_of_an_ambiguous_pair() -> None:
    """`Austria` and `Australia` are both offered on purpose. The agent is meant to face the
    ambiguity and abstain, not be spared it by a threshold that quietly picked one."""
    names = {candidate.name for candidate in fuzzy_candidates("Austrlia")}

    assert "austria" in names
    assert "australia" in names


def test_fuzzy_candidates_is_bounded_and_stable() -> None:
    """The shortlist is hashed into the cassette key, so a tie broken by dict order would give the
    same label two different keys."""
    first = fuzzy_candidates("Czech Rep")
    second = fuzzy_candidates("Czech Rep")

    assert first == second
    assert len(first) <= 5
    assert len({candidate.iso_alpha2 for candidate in first}) == len(first)


def test_a_label_with_no_near_neighbour_gets_an_empty_shortlist() -> None:
    """A label nobody should be nudged into resolving."""
    assert fuzzy_candidates("Deluxe King Non-Smoking") == ()


def test_an_empty_shortlist_is_rendered_in_words_not_as_an_empty_list() -> None:
    """A list header followed by nothing reads as a formatting failure, and an agent that thinks
    the tool broke behaves differently from one that knows there is nothing close."""
    rendered = render_candidates("Deluxe King", ())

    assert "no country name similar enough" in rendered
    assert "abstain" in rendered


def test_iso_lookup_answers_none_for_a_code_the_table_does_not_know() -> None:
    """ "No such code" is the answer to that question, not an error in asking it."""
    assert iso_lookup("DE") is not None
    assert iso_lookup("QQ") is None


def test_the_resolution_agent_may_abstain_and_the_result_is_falsy(policy: Policy) -> None:
    """Abstention as a typed outcome, so `if resolution:` reads correctly and a caller cannot
    mistake a refusal for an answer."""
    answer = LabelResolution(
        raw_label="Austrlia",
        outcome="abstained",
        reason="Austria and Australia are both plausible and differ by a continent",
    )
    provider = StubProvider()
    provider.register(answer)
    runner = AgentRunner(provider, policy=policy, registry=resolution_registry(), trace=TraceLog())

    result = resolve_label("Austrlia", runner, policy)

    assert not result
    assert result.output.iso_alpha2 is None
    assert "plausible" in result.output.reason


def test_a_candidate_that_was_never_offered_is_rejected_as_a_fabrication(policy: Policy) -> None:
    """Code produces the shortlist precisely so the answer is bounded. An invented suggestion
    reaching a human as a confident one is the failure this check exists for.

    `Grmany` rather than `Austrlia`: the latter is now caught by `too_close_to_choose` before the
    fabrication check is reached, because Austria and Australia score within a hair of each other.
    A test for *this* check needs a label with one obvious winner.
    """
    provider = StubProvider()
    provider.register(
        LabelResolution(
            raw_label="Grmany",
            outcome="resolved",
            iso_alpha2="AT",
            matched_candidate="Ruritania",
            reason="it looks like Ruritania",
        )
    )
    runner = AgentRunner(provider, policy=policy, registry=resolution_registry(), trace=TraceLog())

    with pytest.raises(AgentError, match="which was not offered"):
        resolve_label("Grmany", runner, policy)


def test_a_well_formed_code_that_denotes_nothing_is_rejected(policy: Policy) -> None:
    """The field's pattern accepts any two capitals; only the committed table decides which of
    them denote a country."""
    candidate = fuzzy_candidates("Grmany")[0]
    provider = StubProvider()
    provider.register(
        LabelResolution(
            raw_label="Grmany",
            outcome="resolved",
            iso_alpha2="QQ",
            matched_candidate=candidate.name,
            reason="a transposition",
        )
    )
    runner = AgentRunner(provider, policy=policy, registry=resolution_registry(), trace=TraceLog())

    with pytest.raises(AgentError, match="not in the committed lookup"):
        resolve_label("Grmany", runner, policy)


def test_an_answer_about_a_different_label_is_rejected(policy: Policy) -> None:
    provider = StubProvider()
    provider.register(
        LabelResolution(raw_label="Nigeria", outcome="abstained", reason="not what was asked")
    )
    runner = AgentRunner(provider, policy=policy, registry=resolution_registry(), trace=TraceLog())

    with pytest.raises(AgentError, match="answered about 'Nigeria'"):
        resolve_label("Austrlia", runner, policy)


def test_resolving_uses_only_the_two_tools_it_is_allowed(policy: Policy) -> None:
    candidate = fuzzy_candidates("Grmany")[0]
    provider = StubProvider()
    provider.register(
        LabelResolution(
            raw_label="Grmany",
            outcome="resolved",
            iso_alpha2=candidate.iso_alpha2,
            matched_candidate=candidate.name,
            reason="a dropped vowel",
        )
    )
    runner = AgentRunner(provider, policy=policy, registry=resolution_registry(), trace=TraceLog())

    result = resolve_label("Grmany", runner, policy)

    assert bool(result)
    assert {call.name for call in result.trace.tool_calls} <= {"fuzzy_candidates", "iso_lookup"}
    assert result.trace.refused_tools == ()


# ── narrative: no figures in, no cause it was not given out ─────────────────


def test_the_narrative_agent_is_never_shown_a_figure() -> None:
    """A sentence containing a number is a number a model produced, and the officer cannot tell by
    looking which numbers in a report were computed."""
    rendered = read_finding(finding())

    assert "71.4" not in rendered
    assert "68.2" not in rendered
    assert "3.2" not in rendered
    assert "D-OCC-01" in rendered


def test_a_narrative_citing_an_unassigned_cause_is_discarded(policy: Policy) -> None:
    """The most expensive wrong answer available here: the figures survive review and the
    explanation does not."""
    subject = finding()
    provider = StubProvider()
    provider.register(a_narrative(cites_permutation="P-COMP-EXCLUDED"))
    runner = AgentRunner(
        provider, policy=policy, registry=narrative_registry(subject), trace=TraceLog()
    )

    with pytest.raises(AgentError, match="which the classifier did not assign"):
        narrate(subject, runner, policy)


def test_a_narrative_about_a_different_finding_is_discarded(policy: Policy) -> None:
    subject = finding()
    provider = StubProvider()
    provider.register(a_narrative(finding_id="F-0099"))
    runner = AgentRunner(
        provider, policy=policy, registry=narrative_registry(subject), trace=TraceLog()
    )

    with pytest.raises(AgentError, match="answered about F-0099"):
        narrate(subject, runner, policy)


def test_a_narrative_naming_the_assigned_permutation_is_accepted(policy: Policy) -> None:
    subject = finding(
        variance_class=VarianceClass.DEFINITIONAL,
        escalates_to=EscalationTarget.POLICY_OWNER,
        explaining_permutation="P-COMP-EXCLUDED",
    )
    provider = StubProvider()
    provider.register(a_narrative(cites_permutation="P-COMP-EXCLUDED", is_definitional=True))
    runner = AgentRunner(
        provider, policy=policy, registry=narrative_registry(subject), trace=TraceLog()
    )

    result = narrate(subject, runner, policy)

    assert result.output.cites_permutation == "P-COMP-EXCLUDED"
    assert {c.name for c in result.trace.tool_calls} == {"read_finding", "read_evidence"}


# ── critic: four observations, a computed verdict ───────────────────────────


def a_verdict(**overrides: object) -> CriticVerdict:
    base: dict[str, object] = {
        "finding_id": "F-0001",
        "grounded": True,
        "leaks_a_number": False,
        "names_an_unassigned_cause": False,
        "reads_as_an_accusation": False,
        "reason": "the sentence restates the classification and adds nothing",
    }
    return CriticVerdict(**(base | overrides))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "override",
    [
        {"grounded": False},
        {"leaks_a_number": True},
        {"names_an_unassigned_cause": True},
        {"reads_as_an_accusation": True},
    ],
)
def test_any_one_failure_fails_the_narrative(override: dict[str, object]) -> None:
    """Deliberately unforgiving. A later decision to tolerate one of these has to change a line a
    reviewer sees."""
    assert a_verdict().passed
    assert not a_verdict(**override).passed
    assert a_verdict(**override).failures()


def test_the_critic_sees_exactly_what_the_writer_saw() -> None:
    """Showing the critic more would have it marking a sentence ungrounded for omitting something
    the writer was never told, which grades the wiring instead of the prose."""
    rendered = render_case(finding(), a_narrative())

    assert read_finding(finding()) in rendered
    assert "71.4" not in rendered


def test_the_critic_refuses_to_grade_a_narrative_about_another_finding(policy: Policy) -> None:
    provider = StubProvider()
    provider.register(a_verdict())
    runner = AgentRunner(provider, policy=policy, trace=TraceLog())

    with pytest.raises(AgentError, match="cannot grade narrative for F-0099"):
        grade(finding(), a_narrative(finding_id="F-0099"), runner, policy)


def test_the_critic_runs_with_no_tool_calls_at_all(policy: Policy) -> None:
    """The absence is visible in the trace: every other agent's records carry tool calls and the
    critic's carry none."""
    provider = StubProvider()
    provider.register(a_verdict())
    runner = AgentRunner(provider, policy=policy, trace=TraceLog())

    result = grade(finding(), a_narrative(), runner, policy)

    assert result.trace.tool_calls == ()
    assert result.output.passed


def test_a_misfiled_verdict_is_rejected(policy: Policy) -> None:
    """A misfiled verdict counts towards the wrong narrative's score in both directions."""
    provider = StubProvider()
    provider.register(a_verdict(finding_id="F-0099"))
    runner = AgentRunner(provider, policy=policy, trace=TraceLog())

    with pytest.raises(AgentError, match="filed the verdict against F-0099"):
        grade(finding(), a_narrative(), runner, policy)


# ── the contracts themselves ────────────────────────────────────────────────


def test_agent_contracts_carry_no_numbers() -> None:
    """`tools/guard/agent_schema_lint.py` asserts this across `src/` by reading declarations. This
    asserts it about the contracts PRD-88 added, from the **JSON schema** the provider actually
    sends — so it holds even for a field whose numeric type the lint's textual scan cannot see.

    Agents pass keys and references, never numbers. The whitelist is citations, not quantities:
    nothing downstream does arithmetic on a page number.
    """
    allowed = {"page", "row", "row_start", "row_end"}
    # `CitedAnswer` is here because it is the one contract that nests a model from outside the
    # contracts package (`PdfCitation.ref: PdfRef`). The lint follows that now; this asserts it
    # from the schema the provider actually sends, which is the other direction.

    for contract in (LabelResolution, FindingNarrative, CriticVerdict, CitedAnswer):
        schema = contract.model_json_schema()
        for name, spec in {
            **schema.get("properties", {}),
            **{
                field: definition
                for definition in schema.get("$defs", {}).values()
                for field, definition in definition.get("properties", {}).items()
            },
        }.items():
            if name in allowed:
                continue
            types = {spec.get("type"), *(alt.get("type") for alt in spec.get("anyOf", []))}
            assert not types & {"integer", "number"}, (
                f"{contract.__name__}.{name} is numeric. Return the key or reference that "
                "identifies the value and let the metric library compute it."
            )


def test_a_resolution_carries_its_label_so_the_record_stands_alone() -> None:
    """A resolution saying only `CI` is unreadable six weeks later in a review screen."""
    resolution = LabelResolution(
        raw_label="Cote dIvoir",
        outcome="resolved",
        iso_alpha2="CI",
        matched_candidate="cote divoire",
        reason="the accent and the final e are missing",
    )

    assert resolution.raw_label == "Cote dIvoir"
    assert resolution.iso_alpha2 == "CI"
    assert bool(resolution)


def test_an_abstention_carries_no_code_rather_than_an_empty_one() -> None:
    """An empty string compares equal to another empty string, and two abstentions are not the
    same country."""
    resolution = LabelResolution(raw_label="???", outcome="abstained", reason="not a country")

    assert resolution.iso_alpha2 is None


def test_candidates_render_with_their_codes() -> None:
    assert Candidate(name="Australia", iso_alpha2="AU").render() == "- Australia (AU)"
