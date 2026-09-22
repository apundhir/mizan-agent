"""The orchestrated run: five nodes, one verdict, and a record of where it stopped.

The load-bearing test here is the first one. **The whole pipeline over the committed corpus
produces zero findings** — three PDFs to 1,200 records, a workbook to 94 claims, joined on the
canonical metric key, and silence. `tests/unit/test_reconcile.py` already asserted that about the
reconciliation layer; this asserts it about the *run*, which is a different claim: it covers intake,
extraction, the claim parser and the metric library agreeing with each other, not just the last of
them being right about inputs a test handed it.

Everything else is about failure, because a pipeline is judged on what it does when a stage cannot
do its job. the orchestrated graph names five behaviours and each has a test:

| Node | What it must do | Test |
|---|---|---|
| `intake` | reject with a stated reason code, before extraction | the four `test_intake_*` |
| `extract` | halt with a blocking finding, never infer | `test_an_unreadable_report_is_rejected_rather_than_crashing` |
| `claim_parse` | flag unmapped sheets for human mapping | `test_an_unmapped_sheet_is_flagged_rather_than_skipped` |
| `recompute_reconcile` | hard fail if reference data is missing | `test_a_record_set_the_metric_library_refuses_is_a_hard_fail` |
| `publish` | an unclassified variance becomes blocking | `Verdict`'s own invariants, asserted in `test_a_status_the_contract_would_reject_is_never_produced` |

One row moved while these were being written. A whole document that will not open cannot produce a
blocking *finding*, because a finding must cite something and an unopenable file has no page — so
`UNREADABLE_FILE` at intake is where it belongs, and extraction deals only with files already known
to open. Row-level defects inside a readable document are still findings, and still cite their page.

The model call is stubbed throughout. That is not a shortcut around the mapping agent — the mapping
it returns is the committed known-good one from `test_reconcile`, and what these tests exercise is
every line of code *around* the call. The agent's own answers are the eval suite's subject
(`tests/eval/agents/`), and conflating the two would let a pipeline bug hide behind a model result.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.fixtures.workbook import demo_mapping

from tda.agents import BudgetExceededError
from tda.agents.provider import ProviderError, ProviderMode, ReplayProvider, StubProvider
from tda.cli import COULD_NOT_RUN, FINDINGS, OK, declaration_from_manifest, report_failure
from tda.cli import main as cli_main
from tda.contracts import Finding, Period, RejectionReason, Severity, VerdictStatus
from tda.graph import (
    NODE_ORDER,
    Declaration,
    RunContext,
    RunState,
    Submission,
    build_verdict,
    check_hotel,
    check_period,
    decide_status,
    discover,
    intake,
    new_run_id,
    verify_directory,
)
from tda.obs import NodeOutcome, NodeRecord, Phase
from tda.obs.nodes import NodeLog
from tda.policy import Policy, load_policy
from tda.policy.loader import Budget

if TYPE_CHECKING:
    from tda.graph.run import RunResult

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "demo"
SUBMISSION = CORPUS / "submission"
HOTEL = "MZN-DXB-001"


@pytest.fixture(scope="module")
def policy() -> Policy:
    return load_policy()


@pytest.fixture(scope="module")
def period() -> Period:
    return Period.parse("2026-Q1")


def stub() -> StubProvider:
    """A provider that answers the one model call the pipeline makes, with the committed mapping.

    The mapping is `tests.fixtures.workbook.demo_mapping()` — the same committed answer
    `test_reconcile` uses, shared rather than re-authored so the two cannot drift into testing
    different workbooks.
    """
    provider = StubProvider()
    provider.register(demo_mapping())
    return provider


def run_demo(policy: Policy, period: Period, **kwargs: object) -> RunResult:
    return verify_directory(SUBMISSION, HOTEL, period, policy, stub(), **kwargs)  # type: ignore[arg-type]


# ── the claim this milestone makes ───────────────────────────────────────────


def test_the_whole_pipeline_is_silent_about_a_correct_submission(
    policy: Policy, period: Period
) -> None:
    """The load-bearing test. Three PDFs and a workbook in, zero findings out.

    A verification system that cannot stay quiet about a good submission buries every real finding
    it later produces in noise. This is that property asserted over the *orchestrated run* rather
    than over the reconciliation layer alone — so it covers intake, extraction, the claim parser and
    the metric library agreeing with one another.
    """
    result = run_demo(policy, period)

    assert result.verdict.status is VerdictStatus.PASS
    assert result.verdict.findings == ()
    assert result.verdict.definitional_items == ()
    assert result.verdict.claims_checked == 94
    assert result.verdict.rejection_reason is None


def test_every_node_runs_in_order_on_a_clean_submission(policy: Policy, period: Period) -> None:
    result = run_demo(policy, period)

    assert result.nodes.entered() == NODE_ORDER
    assert result.nodes.completed() == NODE_ORDER
    assert result.nodes.unfinished() == ()


def test_a_clean_run_records_one_granted_routing_decision_per_model_call(
    policy: Policy, period: Period
) -> None:
    """The supervisor built in `RunContext.build` is on the call path, not a bystander: the one
    model call this pipeline makes (mapping, at `claim_parse`) goes through `route()` first, and
    the decision it produces is what `routing.jsonl` will carry."""
    result = run_demo(policy, period)

    decisions = result.context.supervisor.decisions
    assert len(decisions) == len(result.context.trace) == 1
    assert decisions[0].node == "claim_parse"
    assert decisions[0].agent == "mapping"
    assert decisions[0].granted


def test_a_spent_budget_refuses_the_mapping_call_and_fails_the_node(
    policy: Policy, period: Period
) -> None:
    """Exhaustion is a refusal, not a truncation. With the mapping agent's own per-agent
    cap set to zero, `claim_parse` never gets to call it: the supervisor raises before the request
    reaches the provider, the node records `FAILED` rather than `OK`, and the run stops there
    rather than producing a verdict a reviewer would mistake for a complete one."""
    starved = policy.model_copy(
        update={
            "model": policy.model.model_copy(
                update={"budget": Budget(max_calls_per_run=60, max_calls_per_agent=0)}
            )
        }
    )
    context = RunContext.build(starved, period, stub())

    with pytest.raises(BudgetExceededError, match="per-agent limit of 0"):
        verify_directory(SUBMISSION, HOTEL, period, starved, stub(), context=context)

    # `_guard` writes the exit record before re-raising, so this node did not simply vanish - it
    # left, and the reason it left is on the record.
    assert context.nodes.unfinished() == ()
    exit_record = next(
        r for r in context.nodes.records if r.node == "claim_parse" and r.phase is Phase.EXIT
    )
    assert exit_record.outcome is NodeOutcome.FAILED
    assert "BudgetExceededError" in (exit_record.detail or "")

    decisions = context.supervisor.decisions
    assert decisions[-1].node == "claim_parse"
    assert decisions[-1].agent == "mapping"
    assert not decisions[-1].granted
    assert len(context.trace) == 1
    assert context.trace.records[0].failed
    assert "BudgetExceededError" in (context.trace.records[0].error or "")


def test_the_verdict_stamps_every_ruleset_that_produced_it(policy: Policy, period: Period) -> None:
    """A number and its ruleset travel together, or the number is not defensible (D-EV-04).

    `metric_library_version` in particular: the constant has existed since M2 and nothing read it
    until the graph did.
    """
    verdict = run_demo(policy, period).verdict

    assert verdict.policy_version == policy.version
    assert verdict.metric_library_version
    assert verdict.model_id == policy.model.model_id
    assert verdict.provider_mode == ProviderMode.STUB.value
    assert verdict.hotel_id == HOTEL
    assert verdict.period == "2026-Q1"


def test_the_extraction_summary_survives_into_the_verdict(policy: Policy, period: Period) -> None:
    summary = run_demo(policy, period).verdict.extraction

    assert summary is not None
    assert summary.records_extracted == 1200
    assert summary.printed_total_matched
    assert summary.duplicate_ids == 0
    assert len(summary.files) == 3


# ── intake: the four reasons, none of which anything could produce before ────


def declared(period: Period, hotel: str = HOTEL) -> Declaration:
    return Declaration(hotel_id=hotel, period=period)


def test_intake_rejects_a_missing_month(tmp_path: Path, period: Period) -> None:
    """ "You did not send February" and "you sent April" are two different conversations, so they
    are two different reason codes."""
    for name in ("pms_2026-01.pdf", "claims_2026-Q1.xlsx", "inventory_2026-Q1.csv"):
        shutil.copy(SUBMISSION / name, tmp_path / name)

    rejection = intake(discover(tmp_path, period), declared(period))

    assert rejection is not None
    assert rejection.reason is RejectionReason.INCOMPLETE_FILE_SET
    assert "2026-02" in rejection.detail


def test_intake_rejects_a_report_from_outside_the_period(tmp_path: Path, period: Period) -> None:
    for name in ("pms_2026-01.pdf", "pms_2026-02.pdf", "pms_2026-03.pdf"):
        shutil.copy(SUBMISSION / name, tmp_path / name)
    shutil.copy(SUBMISSION / "pms_2026-01.pdf", tmp_path / "pms_2026-04.pdf")
    shutil.copy(SUBMISSION / "claims_2026-Q1.xlsx", tmp_path / "claims_2026-Q1.xlsx")

    rejection = check_period(discover(tmp_path, period), declared(period))

    assert rejection is not None
    assert rejection.reason is RejectionReason.PERIOD_MISMATCH
    assert "2026-04" in rejection.detail


def test_intake_rejects_a_foreign_property(tmp_path: Path, period: Period) -> None:
    """The check that reads a file. The inventory reference has always carried a `hotel_id` column
    and nothing has ever compared it to anything."""
    shutil.copy(SUBMISSION / "inventory_2026-Q1.csv", tmp_path / "inventory_2026-Q1.csv")
    submission = Submission(
        reports=(), workbook=tmp_path / "x.xlsx", inventory=tmp_path / "inventory_2026-Q1.csv"
    )

    rejection = check_hotel(submission, declared(period, hotel="MZN-AUH-999"))

    assert rejection is not None
    assert rejection.reason is RejectionReason.HOTEL_MISMATCH
    assert HOTEL in rejection.detail


def test_intake_rejects_a_file_that_is_not_there(tmp_path: Path, period: Period) -> None:
    submission = Submission(reports=(tmp_path / "pms_2026-01.pdf",), workbook=tmp_path / "c.xlsx")

    rejection = intake(submission, declared(period))

    assert rejection is not None
    assert rejection.reason is RejectionReason.UNREADABLE_FILE


def test_a_rejected_submission_never_reaches_extraction(
    tmp_path: Path, policy: Policy, period: Period
) -> None:
    """The acceptance criterion in full: *before any extraction is attempted*.

    Asserted from the node log rather than by mocking, because the log is the artefact an officer
    would read to establish the same thing.
    """
    shutil.copy(SUBMISSION / "claims_2026-Q1.xlsx", tmp_path / "claims_2026-Q1.xlsx")

    result = verify_directory(tmp_path, HOTEL, period, policy, stub())

    assert result.verdict.status is VerdictStatus.REJECTED
    assert result.verdict.rejection_reason is RejectionReason.INCOMPLETE_FILE_SET
    assert "extract" not in result.nodes.entered()
    assert result.verdict.extraction is None


def test_a_rejected_verdict_still_says_why(tmp_path: Path, policy: Policy, period: Period) -> None:
    """Routing a refusal straight to END would leave a reviewer nothing to read. Every path reaches
    publish, and `Verdict` refuses a REJECTED status with no reason code."""
    result = verify_directory(tmp_path, HOTEL, period, policy, stub())

    assert result.verdict.rejection_reason is not None
    assert "publish" in result.nodes.entered()


# ── halting, and where the run stopped ───────────────────────────────────────


def test_a_replay_miss_halts_and_the_log_names_the_node(
    policy: Policy, period: Period, tmp_path: Path
) -> None:
    """A cassette miss is a hard error, never a live call. The node log is what turns "it failed"
    into "it failed at claim_parse".

    The miss is **forced** by pointing the provider at an empty directory. It used to rely on the
    repository having no committed cassettes, which stopped being true the moment `make record`
    first ran - and this test then failed for the best possible reason, which is still a failing
    test. A test about halting should halt because of what it does, not because of what happens to
    be on disk.
    """
    empty = tmp_path / "no-cassettes"
    context = RunContext.build(policy, period, ReplayProvider(empty))

    with pytest.raises(Exception, match="no cassette"):
        verify_directory(SUBMISSION, HOTEL, period, policy, ReplayProvider(empty), context=context)

    assert context.nodes.unfinished() == ()
    assert context.nodes.completed()[-1] == "claim_parse"
    assert [r.outcome for r in context.nodes if r.phase is Phase.EXIT][-1] is NodeOutcome.FAILED


def test_a_node_that_fails_still_writes_its_exit_record(
    policy: Policy, period: Period, tmp_path: Path
) -> None:
    """A recorder that caught what it observed would be the last thing to report a problem and the
    first to hide one. `_guard` records, then re-raises.

    The miss is forced with an empty cassette directory, for the reason the test above states.
    """
    empty = tmp_path / "no-cassettes"
    context = RunContext.build(policy, period, ReplayProvider(empty))

    with pytest.raises(Exception, match="no cassette"):
        verify_directory(SUBMISSION, HOTEL, period, policy, ReplayProvider(empty), context=context)

    failed = [r for r in context.nodes if r.outcome is NodeOutcome.FAILED]
    assert len(failed) == 1
    assert failed[0].detail is not None
    assert "CassetteMissError" in failed[0].detail


# ── the status rules, which the contract also enforces ───────────────────────


def state_with(period: Period, **overrides: object) -> RunState:
    base = RunState(
        run_id="run-test",
        submission=Submission(reports=(), workbook=Path("x.xlsx")),
        declared=Declaration(hotel_id=HOTEL, period=period),
    )
    for name, value in overrides.items():
        setattr(base, name, value)
    return base


def test_a_clean_run_passes(period: Period) -> None:
    assert decide_status(state_with(period)) is VerdictStatus.PASS


def test_a_rejection_outranks_everything(period: Period) -> None:
    state = state_with(period, rejection=RejectionReason.HOTEL_MISMATCH, rejection_detail="wrong")

    assert decide_status(state) is VerdictStatus.REJECTED


def test_a_blocking_finding_is_halted_never_failed(
    policy: Policy, period: Period, tmp_path: Path
) -> None:
    """`Verdict` refuses `FAIL` or `PASS` beside a blocking finding: a run that could not read
    something has not found nothing, it has not looked."""
    state = state_with(period, findings=(_blocking_finding(period),))

    assert decide_status(state) is VerdictStatus.HALTED


def test_definitional_items_escalate_rather_than_failing(period: Period) -> None:
    """A definitional variance goes to the policy owner and is never a hotel error (D-MAT-06)."""
    state = state_with(period, definitional=(_definitional_finding(period),))

    assert decide_status(state) is VerdictStatus.ESCALATED


def test_a_status_the_contract_would_reject_is_never_produced(
    policy: Policy, period: Period
) -> None:
    """`decide_status` reads `Verdict`'s invariants forwards; the contract enforces them backwards.

    This walks every combination the run can reach and asserts the verdict constructs — so a status
    rule that drifted out of step with the contract fails here rather than at the end of a real run.
    """
    context = RunContext.build(policy, period, stub())
    blocking = _blocking_finding(period)
    definitional = _definitional_finding(period)

    for overrides in (
        {},
        {"rejection": RejectionReason.PERIOD_MISMATCH, "rejection_detail": "x"},
        {"findings": (blocking,)},
        {"definitional": (definitional,)},
        {"findings": (blocking,), "definitional": (definitional,)},
    ):
        state = state_with(period, **overrides)
        state.status = decide_status(state)
        verdict = build_verdict(state, context)

        assert verdict.status is state.status


# ── node records ─────────────────────────────────────────────────────────────


def test_a_node_record_that_ended_badly_must_say_why() -> None:
    """A halt nobody can explain is indistinguishable from a crash in every report that follows."""
    with pytest.raises(ValueError, match="no stated reason"):
        NodeRecord(node="extract", phase=Phase.EXIT, outcome=NodeOutcome.HALTED)


def test_an_entered_node_with_no_exit_names_where_the_run_stopped() -> None:
    log = NodeLog(
        [
            NodeRecord(node="intake", phase=Phase.ENTER),
            NodeRecord(node="intake", phase=Phase.EXIT, outcome=NodeOutcome.OK),
            NodeRecord(node="extract", phase=Phase.ENTER),
        ]
    )

    assert log.unfinished() == ("extract",)
    assert log.completed() == ("intake",)


def test_node_records_round_trip_through_jsonl(policy: Policy, period: Period) -> None:
    """observability writes these to `artifacts/<run_id>/`. They have to survive the trip."""
    log = run_demo(policy, period).nodes

    assert NodeLog.from_jsonl(log.to_jsonl()).records == log.records


def test_only_the_model_node_reports_a_model_call(policy: Policy, period: Period) -> None:
    """Four of the five nodes call no model, and reporting zero for them is the right answer rather
    than a missing one."""
    exits = {r.node: r for r in run_demo(policy, period).nodes if r.phase is Phase.EXIT}

    assert exits["claim_parse"].model_calls == 1
    for node in ("intake", "extract", "recompute_reconcile", "publish"):
        assert exits[node].model_calls == 0


# ── the CLI ──────────────────────────────────────────────────────────────────


def test_the_cli_reads_the_declaration_from_the_case_record() -> None:
    """The manifest sits *above* `submission/`, which is what keeps this from being circular:
    intake checks the files against a declaration made somewhere other than the files."""
    found = declaration_from_manifest(SUBMISSION)

    assert found == (HOTEL, "2026-Q1")


def test_the_cli_refuses_a_submission_it_cannot_identify(tmp_path: Path) -> None:
    """Guessing the hotel and period from the submission would defeat intake entirely."""
    assert cli_main(["run", str(tmp_path)]) == COULD_NOT_RUN


def test_the_cli_exits_non_zero_for_a_submission_a_human_must_read(tmp_path: Path) -> None:
    """`make run` returning success for a rejected submission would make an unread verdict look
    like a clean one."""
    assert cli_main(
        [
            "run",
            str(tmp_path / "nothing"),
            "--hotel",
            HOTEL,
            "--period",
            "2026-Q1",
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    ) in (FINDINGS, COULD_NOT_RUN)


def test_a_run_writes_its_artifacts_and_trace_reads_them_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The observability loop, at the level a person actually meets it: run, then ask what happened.

    Deliberately a *rejected* submission rather than a clean one, and not as a convenience. The
    artifacts of a run that did not finish are the ones worth proving exist, because that is when
    somebody goes looking for them. It is intake that turns this one away: the cassettes are
    committed now, so a replay run gets past `claim_parse`, and a request with no recording would
    still stop the run there rather than falling through to the network.
    """
    artifacts = tmp_path / "artifacts"
    submission = tmp_path / "submission"
    submission.mkdir()

    code = cli_main(
        [
            "run",
            str(submission),
            "--hotel",
            HOTEL,
            "--period",
            "2026-Q1",
            "--artifacts",
            str(artifacts),
        ]
    )
    assert code == FINDINGS

    run_output = capsys.readouterr().out
    assert "artifacts:" in run_output
    # The usage summary is printed at the end of `make run`. This submission is rejected before any
    # agent runs, so the honest summary is that there were no model calls to price. A run that made
    # calls with no rate card exported says "rates not configured" instead, and one made with rates
    # exported names the version: both are covered in tests/unit/test_obs.py.
    assert "no model calls" in run_output
    assert "mizan trace" in run_output

    directories = [d for d in artifacts.iterdir() if d.is_dir()]
    assert len(directories) == 1
    # The four observability artifacts and the officer's two, in one directory. There is no
    # annotated workbook here on purpose: this submission is empty, so there was never a workbook
    # to copy, and an empty annotated file would be a lie about what was checked. `routing.jsonl`
    # is written even though this run rejects at intake and routes nothing - an empty routing log
    # is still a fact worth writing rather than a file quietly skipped.
    assert sorted(p.name for p in directories[0].iterdir()) == [
        "memo.docx",
        "nodes.jsonl",
        "routing.jsonl",
        "run.json",
        "trace.jsonl",
        "verdict.json",
    ]
    assert "no annotated workbook" in run_output

    # A rejected run still produces a verdict, and the file says which of the four reasons fired.
    rejected = json.loads((directories[0] / "verdict.json").read_text(encoding="utf-8"))
    assert rejected["status"] == "REJECTED"
    assert rejected["rejection_reason"] in {r.value for r in RejectionReason}
    assert rejected["summary"]["hotel_errors"] == 0

    assert cli_main(["trace", "--artifacts", str(artifacts)]) == OK
    tree = capsys.readouterr().out
    assert directories[0].name in tree
    assert "REJECTED" in tree
    assert "intake" in tree


def test_a_run_that_dies_still_writes_its_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run whose trace somebody actually goes looking for.

    Writing artifacts only on success would mean the record exists for every run except the ones
    anybody needs it for. The ledger says `FAILED`, which is the one status `VerdictStatus` cannot
    express and should not: a verdict is a statement about a submission, and a run that died made
    no statement.

    The death is a **real cassette miss**, provoked by giving the run a workbook the recorded
    mapping call never saw. This used to rely on the committed corpus missing because no cassette
    existed at all; once `make record` ran, the demo submission started completing in replay - a
    good thing, and it silently turned this test into an assertion about a successful run. An extra
    sheet changes the rendered workbook digest, which changes the request, which changes the
    cassette key. Nothing about the failure path is simulated.
    """
    from openpyxl import load_workbook

    artifacts = tmp_path / "artifacts"
    submission = tmp_path / "submission"
    shutil.copytree(SUBMISSION, submission)
    workbook = load_workbook(submission / "claims_2026-Q1.xlsx")
    workbook.create_sheet("Unrecorded")
    workbook.save(submission / "claims_2026-Q1.xlsx")

    code = cli_main(
        [
            "run",
            str(submission),
            "--hotel",
            HOTEL,
            "--period",
            "2026-Q1",
            "--provider",
            ProviderMode.REPLAY.value,
            "--artifacts",
            str(artifacts),
        ]
    )
    assert code == COULD_NOT_RUN
    capsys.readouterr()

    directories = [d for d in artifacts.iterdir() if d.is_dir()]
    assert len(directories) == 1
    assert sorted(p.name for p in directories[0].iterdir()) == [
        "nodes.jsonl",
        "routing.jsonl",
        "run.json",
        "trace.jsonl",
    ]

    assert cli_main(["trace", directories[0].name, "--artifacts", str(artifacts)]) == OK
    tree = capsys.readouterr().out
    assert "FAILED" in tree
    # The node the run stopped inside is the question a reader of a broken run opens the file to
    # answer, and it survives into the artifact rather than only into the terminal.
    assert "claim_parse" in tree
    assert "failed" in tree
    # `_guard` writes the exit record before re-raising, so the node log says *how* it ended rather
    # than merely stopping - and the reason travels into the artifact, not only to the terminal.
    assert "CassetteMissError" in tree
    # The failed call is attributed to the node that made it. It is counted in the usage ledger
    # precisely so it can be: an uncounted failure used to shift every later call onto the wrong
    # node, silently.
    assert "mapping/v1" in tree
    assert "could not be attributed" not in tree


def test_a_second_exception_while_writing_the_failure_record_still_exits_could_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real bug, found verifying `tda.review.sandbox`'s memory ceiling on Linux, not simulated
    for this test alone: a sandboxed run's own `MemoryError` reached this failure path cleanly,
    then *building the failure ledger* - digesting every input file - threw a second `MemoryError`
    once `RLIMIT_AS` was already essentially spent, unhandled, exiting with Python's own default
    code for an uncaught exception (`1`) - indistinguishable, to anything reading the exit code,
    from `FINDINGS`, a real verdict a human must read, which it was not. `failure_ledger` is
    monkeypatched to raise here because reproducing the real trigger needs an actual memory-limited
    subprocess (`tests/unit/test_sandbox.py` does that); what this test pins is the property that
    must hold regardless of which exception hits this second `try`."""
    monkeypatch.setattr(
        "tda.cli.build_provider",
        lambda _name: ReplayProvider(cassette_dir=tmp_path / "no-cassettes-here"),
    )

    def _raises(**_kwargs: object) -> None:
        raise MemoryError

    monkeypatch.setattr("tda.cli.failure_ledger", _raises)

    code = cli_main(
        [
            "run",
            str(SUBMISSION),
            "--hotel",
            HOTEL,
            "--period",
            "2026-Q1",
            "--provider",
            ProviderMode.REPLAY.value,
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )

    assert code == COULD_NOT_RUN
    assert "could not write this run's own failure record" in capsys.readouterr().err


def test_the_failure_message_is_redacted_like_every_other_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A validation error quotes the value it rejected, and the node log carries halt reasons
    straight from the submission. The artifacts redact both; a terminal that did not would be the
    one channel with a leak in it, and the terminal is what gets screenshotted into a ticket."""
    context = RunContext.build(load_policy(), Period.parse("2026-Q1"), stub())
    context.nodes.append(NodeRecord(node="extract", phase=Phase.ENTER))
    context.nodes.append(
        NodeRecord(
            node="extract",
            phase=Phase.EXIT,
            outcome=NodeOutcome.HALTED,
            detail="row 14 named " + "DOE" + "/" + "JANE",
        )
    )

    report_failure(ValueError("guest_ref rejected: input_value='" + "DOE" + "/" + "JANE'"), context)

    errors = capsys.readouterr().err
    assert "DOE/JANE" not in errors
    assert "[redacted:slashed_name]" in errors
    assert "stopped inside" not in errors  # the node did leave; only the reason was redacted


def test_a_key_in_a_provider_failure_message_is_redacted_before_the_terminal_sees_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The realistic leak route for a credential is not this system writing one - it is an SDK
    error message quoting a header, or a person pasting one into a question that then fails. Either
    way, the terminal must not be the one channel that shows it in the clear."""
    planted = "sk-ant-api03-" + "EXAMPLE" * 4
    context = RunContext.build(load_policy(), Period.parse("2026-Q1"), stub())

    report_failure(ProviderError(f"401 unauthorized for key {planted}"), context)

    errors = capsys.readouterr().err
    assert planted not in errors
    assert "[redacted:api_key]" in errors


def test_the_trace_command_says_so_when_there_is_nothing_to_read(tmp_path: Path) -> None:
    """`mizan trace` reads artifacts back; nothing here re-runs the pipeline, and a viewer that
    quietly re-ran one would answer a different question from the one asked."""
    assert cli_main(["trace", "--artifacts", str(tmp_path / "empty")]) == COULD_NOT_RUN
    assert cli_main(["trace", "no-such-run", "--artifacts", str(tmp_path)]) == COULD_NOT_RUN


def test_a_run_id_carries_no_meaning() -> None:
    """A meaningful id invites somebody to parse it, and then the format is a contract nobody
    wrote down."""
    assert new_run_id() != new_run_id()
    assert new_run_id().startswith("run-")


# ── fixtures ─────────────────────────────────────────────────────────────────


def _blocking_finding(period: Period) -> Finding:
    from tda.contracts import EscalationTarget, Metric, MetricKey, PdfRef, VarianceClass
    from tda.excel.selfcheck import no_pdf

    return Finding(
        finding_id="F-0001",
        key=MetricKey(metric=Metric.ROOM_NIGHTS_SOLD, period=str(period)),
        variance_class=VarianceClass.EXTRACTION_LIMIT,
        severity=Severity.BLOCKING,
        escalates_to=EscalationTarget.HUMAN_REVIEW,
        source_ref=PdfRef(file="pms_2026-01.pdf", page=3, row_start=1, row_end=1),
        excel_ref=no_pdf("nothing was mapped"),
        clause="D-XLS-06",
    )


def _definitional_finding(period: Period) -> Finding:
    from decimal import Decimal

    from tda.contracts import (
        EscalationTarget,
        ExcelRef,
        Metric,
        MetricKey,
        PdfRef,
        VarianceClass,
    )

    return Finding(
        finding_id="F-0002",
        key=MetricKey(metric=Metric.OCCUPANCY_PCT, period=str(period)),
        variance_class=VarianceClass.DEFINITIONAL,
        severity=Severity.MATERIAL,
        escalates_to=EscalationTarget.POLICY_OWNER,
        claimed=Decimal("74.80"),
        computed=Decimal("71.20"),
        difference=Decimal("3.60"),
        explaining_permutation="P-COMP-EXCLUDED",
        source_ref=PdfRef(file="pms_2026-02.pdf", page=3, row_start=1, row_end=1),
        excel_ref=ExcelRef(sheet="Occupancy", cell="D6"),
        clause="D-OCC-01",
    )


# ── the per-node failure behaviours the orchestrated graph names ─────────────────────────────


def test_an_unreadable_report_is_rejected_rather_than_crashing(
    tmp_path: Path, policy: Policy, period: Period
) -> None:
    """A truncated PDF makes pdfplumber raise from deep inside itself, and an officer handed
    `PdfminerException: Unexpected EOF` has been told nothing they can act on.

    It is a **rejection**, not a finding, and the `Finding` contract is what settles that: a
    document that will not open has no page to cite, and D-EV-01 refuses a finding citing nothing
    on either side. Inventing `page=1` to satisfy the type would put a false citation in front of a
    reviewer. `UNREADABLE_FILE` was declared for exactly this and had no producer until now.
    """
    for name in ("pms_2026-01.pdf", "pms_2026-02.pdf", "pms_2026-03.pdf", "claims_2026-Q1.xlsx"):
        shutil.copy(SUBMISSION / name, tmp_path / name)
    damaged = tmp_path / "pms_2026-02.pdf"
    damaged.write_bytes(damaged.read_bytes()[: len(damaged.read_bytes()) * 3 // 5])

    result = verify_directory(tmp_path, HOTEL, period, policy, stub())

    assert result.verdict.status is VerdictStatus.REJECTED
    assert result.verdict.rejection_reason is RejectionReason.UNREADABLE_FILE
    # Rejected before anything was read, which is the whole point of doing it at intake.
    assert "extract" not in result.nodes.entered()
    assert result.verdict.extraction is None


def test_an_unmapped_sheet_is_flagged_rather_than_skipped(policy: Policy, period: Period) -> None:
    """`claim_parse`'s stated failure behaviour: flag unmapped sheets for human mapping.

    A mapping that omits a sheet is not a mapping with fewer claims in it — the workbook still has
    figures nobody looked at, and "never silently skipped" has to be a property of the code rather
    than of the model's diligence. `tda.excel.run` establishes it by checking, and this asserts the
    graph carries the result through to the verdict.
    """
    from tda.excel import WorkbookMapping

    full = demo_mapping()
    partial = WorkbookMapping(
        cover=full.cover,
        blocks=tuple(b for b in full.blocks if b.sheet != "Nationality"),
    )
    provider = StubProvider()
    provider.register(partial)

    result = verify_directory(SUBMISSION, HOTEL, period, policy, provider)

    assert result.verdict.status is VerdictStatus.HALTED
    unmapped = [f for f in result.verdict.findings if f.clause == "D-XLS-06"]
    assert unmapped, "a sheet nobody mapped must reach the verdict as a finding"
    assert not any(f.is_hotel_error for f in unmapped)


def test_a_record_set_the_metric_library_refuses_is_a_hard_fail(
    policy: Policy, period: Period
) -> None:
    """`recompute_reconcile`'s stated failure behaviour: hard fail rather than a partial answer.

    Duplicate reservation ids make `compute_all` raise before it computes anything. There is no
    correct partial answer to "what is occupancy over a record set this library will not accept",
    so the node raises rather than reporting one — and writes its exit record on the way out.
    """
    from tda.graph.nodes import NodeFailureError, recompute_reconcile_node

    context = RunContext.build(policy, period, stub())
    records = run_demo(policy, period).state.records
    state = state_with(period, records=(*records, records[0]), claims=())

    with pytest.raises(NodeFailureError, match="no correct partial answer"):
        recompute_reconcile_node(state, context)

    exits = [r for r in context.nodes if r.phase is Phase.EXIT]
    assert exits and exits[-1].outcome is NodeOutcome.FAILED
    assert context.nodes.unfinished() == ()


# ── regressions from the review of this change ───────────────────────────────
#
# Five bugs, found by reading the code adversarially rather than by a failing test. Each one below
# would have reached a reviewer, and two of them killed a whole verdict status.


def test_a_definitional_finding_is_filed_once_not_twice(policy: Policy, period: Period) -> None:
    """The bug that killed every ESCALATED run.

    `Reconciliation.raised` filters on **severity** and `.definitional` filters on **variance
    class**, and policy gives V2 `material` — so a definitional finding is in both. Passing both
    through put the same finding in `findings` and `definitional_items`, which `Verdict` refuses
    under D-MAT-06 and again under its duplicate-id check.

    A hotel-error count that included policy disagreements is the exact wrong number D-MAT-06
    exists to prevent, so this is not tidy-up: it is the rule.
    """
    definitional = _definitional_finding(period)
    state = state_with(period, findings=(), definitional=(definitional,))
    state.status = decide_status(state)
    context = RunContext.build(policy, period, stub())

    verdict = build_verdict(state, context)

    assert verdict.status is VerdictStatus.ESCALATED
    assert verdict.definitional_items and not verdict.findings
    assert verdict.hotel_error_count == 0


def test_findings_from_three_libraries_do_not_collide(policy: Policy, period: Period) -> None:
    """Extraction, the claim parser and reconciliation each allocate from their own `FindingIds`,
    each starting at `F-0001`. Two of them contributing to one run produced duplicate ids, and
    `Verdict` refuses a duplicate outright.

    The graph is the only thing that combines the three lists, so it is the only thing that can
    number them consistently.
    """
    from tda.graph.nodes import renumber

    collide = (_blocking_finding(period), _blocking_finding(period))
    findings, definitional = renumber(collide, (_definitional_finding(period),))

    ids = [f.finding_id for f in (*findings, *definitional)]
    assert ids == ["F-0001", "F-0002", "F-0003"]
    assert len(set(ids)) == len(ids)


def test_a_node_record_claims_only_the_prompts_that_node_ran(
    policy: Policy, period: Period
) -> None:
    """`TraceLog.prompt_versions()` folds the whole trace, so reading it at each node's exit gave
    every node after `claim_parse` the mapping agent's version — a record saying the reconciliation
    node ran a prompt it has no way of running.

    These are persisted to `artifacts/<run_id>/`, so the misattribution would outlive the run.
    """
    exits = {r.node: r for r in run_demo(policy, period).nodes if r.phase is Phase.EXIT}

    assert exits["claim_parse"].prompt_versions == {"mapping": "v1"}
    for node in ("intake", "extract", "recompute_reconcile", "publish"):
        assert exits[node].prompt_versions == {}, f"{node} claims a prompt it never ran"


def test_an_inventory_that_is_not_utf8_is_rejected_not_raised(
    tmp_path: Path, period: Period
) -> None:
    """`read_inventory` opens as UTF-8 and does not wrap a decode failure, so a binary CSV raised
    `UnicodeDecodeError` — a `ValueError`, not an `InventoryError` — straight out of intake.

    The question intake answers is "can this file be used?", and every way of failing it has the
    same answer.
    """
    broken = tmp_path / "inventory_2026-Q1.csv"
    broken.write_bytes(b"hotel_id,day\n\x93\x94\xff not utf-8 at all\n")
    submission = Submission(reports=(), workbook=tmp_path / "x.xlsx", inventory=broken)

    rejection = check_hotel(submission, declared(period))

    assert rejection is not None
    assert rejection.reason is RejectionReason.UNREADABLE_FILE


def test_an_unopenable_workbook_is_rejected_at_intake(
    tmp_path: Path, policy: Policy, period: Period
) -> None:
    """Intake opened every PDF and not the workbook, so a corrupt `.xlsx` escaped as `BadZipFile`
    from the middle of `claim_parse` — the exact crash the readability check exists to convert into
    a stated refusal."""
    for name in ("pms_2026-01.pdf", "pms_2026-02.pdf", "pms_2026-03.pdf"):
        shutil.copy(SUBMISSION / name, tmp_path / name)
    (tmp_path / "claims_2026-Q1.xlsx").write_bytes(b"this is not a zip file")

    result = verify_directory(tmp_path, HOTEL, period, policy, stub())

    assert result.verdict.status is VerdictStatus.REJECTED
    assert result.verdict.rejection_reason is RejectionReason.UNREADABLE_FILE
    assert "claim_parse" not in result.nodes.entered()


def test_a_quarter_workbook_does_not_satisfy_a_year_declaration(tmp_path: Path) -> None:
    """The name check was a substring test, and `"2026"` is a substring of `"claims_2026-Q1"`.

    A full year is declared and all twelve reports are named, so the missing-month check passes and
    the workbook name is what is actually under test. `check_period` reads names and opens nothing,
    so the reports can be empty files.
    """
    for month in range(1, 13):
        (tmp_path / f"pms_2026-{month:02d}.pdf").touch()
    submission = Submission(
        reports=tuple(sorted(tmp_path.glob("pms_*.pdf"))),
        workbook=tmp_path / "claims_2026-Q1.xlsx",
    )

    rejection = check_period(submission, declared(Period.parse("2026")))

    assert rejection is not None
    assert rejection.reason is RejectionReason.PERIOD_MISMATCH
