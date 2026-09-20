"""The background worker: the same artefacts `mizan run` writes, without a terminal.

**A failed run still writes what it can, on the same rules `mizan run` follows.** The comparison
below runs the identical submission through both `tda.cli.main` and `tda.review.runner.execute`
and diffs the file sets and the stable parts of the ledger, rather than asserting on this module's
own idea of what should be there.

**Only the pipeline's own failure fails the job.** A write failure afterwards is recorded, not
promoted into the job's state - the same distinction `tda.cli._write` and `tda.cli._outputs` make.

**Nothing escapes the worker thread silently.** A crash inside this module's own bookkeeping, not
inside the pipeline it calls, still resolves the job to `FAILED` rather than leaving it stuck in
`RUNNING` forever.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from tests.fixtures.workbook import demo_mapping

from tda.agents.provider import ReplayProvider, StubProvider
from tda.cli import main as cli_main
from tda.contracts import Period, VerdictStatus
from tda.obs import RunLedger
from tda.obs.repro import strip_volatile, volatile_paths
from tda.policy import load_policy
from tda.review.runner import JobState, RunJob, execute, prepare_job, start, wait
from tda.review.sandbox import SandboxResult

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SUBMISSION = REPO_ROOT / "corpus" / "demo" / "submission"
HOTEL = "MZN-DXB-001"
PERIOD = Period.parse("2026-Q1")


def stub() -> StubProvider:
    provider = StubProvider()
    provider.register(demo_mapping())
    return provider


# ── the worker writes what the CLI writes ─────────────────────────────────────


def test_the_worker_writes_the_same_files_the_cli_writes(tmp_path: Path) -> None:
    """Both against the real committed cassettes, so both take the same call and the same answer
    - a stub registered only on the test's side would make the CLI run fail for a reason that has
    nothing to do with the worker, and prove nothing about the two writers agreeing."""
    policy = load_policy()
    cli_artifacts = tmp_path / "cli"
    console_artifacts = tmp_path / "console"

    assert (
        cli_main(
            [
                "run",
                str(DEMO_SUBMISSION),
                "--hotel",
                HOTEL,
                "--period",
                str(PERIOD),
                "--provider",
                "replay",
                "--artifacts",
                str(cli_artifacts),
            ]
        )
        == 0
    )
    cli_run_dir = next(d for d in cli_artifacts.iterdir() if d.is_dir())

    job = prepare_job(
        artifacts_root=console_artifacts,
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=policy,
        provider=ReplayProvider(),
        provider_name="replay",
    )
    execute(job)

    assert job.state is JobState.FINISHED
    assert job.written is not None
    assert job.outputs is not None

    cli_names = sorted(p.name for p in cli_run_dir.iterdir())
    console_names = sorted(p.name for p in job.written.directory.iterdir())
    assert console_names == cli_names

    paths = volatile_paths(RunLedger) | {"run_id"}
    cli_ledger = strip_volatile(
        json.loads((cli_run_dir / "run.json").read_text(encoding="utf-8")), paths
    )
    console_ledger = strip_volatile(
        json.loads((job.written.directory / "run.json").read_text(encoding="utf-8")), paths
    )
    assert cli_ledger == console_ledger


# ── failure: only the pipeline's own exception fails the job ────────────────


def test_a_failed_run_still_writes_its_artifacts_and_records_the_error(tmp_path: Path) -> None:
    """A cassette miss - a real failure from a real (empty) provider, not simulated."""
    job = prepare_job(
        artifacts_root=tmp_path / "artifacts",
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=load_policy(),
        provider=ReplayProvider(cassette_dir=tmp_path / "empty-cassettes"),
        provider_name="replay",
    )

    execute(job)

    assert job.state is JobState.FAILED
    assert job.status == "FAILED"
    assert job.error is not None
    assert "no cassette" in job.error or "CassetteMissError" in job.error
    assert job.result is None
    assert job.written is not None
    written_names = sorted(p.name for p in job.written.directory.iterdir())
    assert written_names == ["nodes.jsonl", "routing.jsonl", "run.json", "trace.jsonl"]

    routing = json.loads(
        (job.written.directory / "routing.jsonl").read_text(encoding="utf-8").strip()
    )
    assert routing["disposition"] == "granted"
    assert routing["agent"] == "mapping"


def test_the_worker_never_raises_out_of_its_thread(tmp_path: Path) -> None:
    class ExplodingProvider(ReplayProvider):
        def complete(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
            raise SystemExit("not a pipeline exception")

    job = prepare_job(
        artifacts_root=tmp_path / "artifacts",
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=load_policy(),
        provider=ExplodingProvider(),
        provider_name="replay",
    )

    execute(job)

    assert job.state is JobState.FAILED
    assert job.error is not None
    assert job.finished is not None


def test_a_stub_run_completes_without_a_cassette(tmp_path: Path) -> None:
    job = prepare_job(
        artifacts_root=tmp_path / "artifacts",
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=load_policy(),
        provider=stub(),
        provider_name="stub",
    )

    execute(job)

    assert job.state is JobState.FINISHED
    assert job.result is not None
    assert job.result.verdict.status is VerdictStatus.PASS


# ── sandboxed: an upload runs mizan run as its own process, not on this thread ──


def _sandboxed_job(artifacts_root: Path, submission: Path) -> RunJob:
    """A job prepared the way `tda.review.console._on_run` prepares one for an upload - the same
    `provider_name` the subprocess will pass to `--provider`, `provider` itself unused for the run
    (only `execute`'s in-process path ever calls it; a sandboxed job's actual verification is a
    fresh provider `tda.cli.build_provider` constructs inside the child)."""
    return prepare_job(
        artifacts_root=artifacts_root,
        submission=submission,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=load_policy(),
        provider=ReplayProvider(),
        provider_name="replay",
        sandboxed=True,
    )


def test_a_sandboxed_job_completes_and_the_verdict_is_read_back(tmp_path: Path) -> None:
    job = _sandboxed_job(tmp_path / "artifacts", DEMO_SUBMISSION)

    execute(job)

    assert job.state is JobState.FINISHED
    assert job.result is None
    verdict = job.verdict
    assert verdict is not None
    assert verdict.status is VerdictStatus.PASS
    assert job.status == "PASS"


def test_a_sandboxed_jobs_context_never_fills_in(tmp_path: Path) -> None:
    """The point of the process boundary: the pipeline this job's verdict came from ran in a
    different process, so the `RunContext` `prepare_job` built for this job never saw a single
    node or trace record - unlike an in-process job, whose `context` is the pipeline's own."""
    job = _sandboxed_job(tmp_path / "artifacts", DEMO_SUBMISSION)

    execute(job)

    assert job.state is JobState.FINISHED
    assert len(job.context.nodes) == 0
    assert len(job.context.trace) == 0


def test_a_sandboxed_runs_rejection_is_read_back_correctly(tmp_path: Path) -> None:
    """Not every non-PASS outcome is `job.state=FAILED` - a workbook `openpyxl` cannot open at all
    is caught by intake itself and reaches a real `REJECTED` verdict (`unreadable_file`), the same
    outcome the in-process path would reach on the identical submission. `FAILED` is reserved for
    `verify_directory` raising - see `test_a_failed_run_still_writes_its_artifacts_and_records_the_
    error` for that case, forced with an empty cassette directory the in-process path can pass
    directly but a subprocess launched from a bare provider name has no way to override."""
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "claims_2026-Q1.xlsx").write_bytes(b"not a real workbook")
    for name in ("pms_2026-01.pdf", "pms_2026-02.pdf", "pms_2026-03.pdf", "inventory_2026-Q1.csv"):
        (submission / name).write_bytes((DEMO_SUBMISSION / name).read_bytes())

    job = _sandboxed_job(tmp_path / "artifacts", submission)

    execute(job)

    assert job.state is JobState.FINISHED
    verdict = job.verdict
    assert verdict is not None
    assert verdict.status is VerdictStatus.REJECTED
    assert verdict.rejection_reason is not None
    assert verdict.rejection_reason.value == "unreadable_file"
    assert job.status == "REJECTED"


def test_a_sandbox_failure_itself_is_read_back_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other way `_execute_sandboxed` resolves to `FAILED`: `tda.review.sandbox.run_sandboxed`
    itself reporting `ok=False` - a timeout here, a resource-limit kill on a hosted Linux deploy -
    with nothing on disk to read a verdict or a node log back from. `job.error` is `outcome.reason`
    verbatim, a static message with no submission-derived content to redact."""
    monkeypatch.setenv("MIZAN_SANDBOX_TIMEOUT_SECONDS", "1")
    job = _sandboxed_job(tmp_path / "artifacts", DEMO_SUBMISSION)

    execute(job)

    assert job.state is JobState.FAILED
    assert job.status == "FAILED"
    assert job.error is not None
    assert "did not finish" in job.error
    assert job.verdict is None
    assert not job.run_dir.exists()


def test_a_verdict_written_just_before_a_reported_kill_is_still_read_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resource-limit kill or the wall-clock timeout can be reported by `run_sandboxed` after
    `mizan run` has already written `verdict.json`, in the tail of its own exit - a G5 security
    review found `_execute_sandboxed` trusted `outcome.ok` before ever checking disk, so a real
    verdict already on disk would have been reported as if nothing had been written at all.
    Simulated by letting a real subprocess finish normally and then reporting the outcome as a
    kill anyway - the exact race, without needing to land a kill inside the real timing window by
    luck."""
    from tda.review.sandbox import run_sandboxed as real_run_sandboxed

    def fake_run_sandboxed(**kwargs: object) -> SandboxResult:
        outcome = real_run_sandboxed(**kwargs)  # type: ignore[arg-type]
        assert outcome.ok, "the real subprocess must actually finish for this test to mean anything"
        return SandboxResult(ok=False, reason="simulated: reported as killed after finishing")

    # Patched by dotted string rather than `runner_module.run_sandboxed = ...`: mypy's strict,
    # no-implicit-reexport mode does not consider a name `runner.py` merely imports (rather than
    # re-declaring in an `__all__`) part of that module's own public attribute surface, even though
    # it is the exact name `_execute_sandboxed` calls at runtime.
    monkeypatch.setattr("tda.review.runner.run_sandboxed", fake_run_sandboxed)
    job = _sandboxed_job(tmp_path / "artifacts", DEMO_SUBMISSION)

    execute(job)

    assert job.state is JobState.FINISHED
    verdict = job.verdict
    assert verdict is not None
    assert verdict.status is VerdictStatus.PASS


def test_a_sandboxed_run_writes_the_same_files_the_cli_writes(tmp_path: Path) -> None:
    cli_artifacts = tmp_path / "cli"

    assert (
        cli_main(
            [
                "run",
                str(DEMO_SUBMISSION),
                "--hotel",
                HOTEL,
                "--period",
                str(PERIOD),
                "--provider",
                "replay",
                "--artifacts",
                str(cli_artifacts),
            ]
        )
        == 0
    )
    cli_run_dir = next(d for d in cli_artifacts.iterdir() if d.is_dir())

    job = _sandboxed_job(tmp_path / "console", DEMO_SUBMISSION)
    execute(job)

    assert job.state is JobState.FINISHED
    assert sorted(p.name for p in job.run_dir.iterdir()) == sorted(
        p.name for p in cli_run_dir.iterdir()
    )

    paths = volatile_paths(RunLedger) | {"run_id"}
    cli_ledger = strip_volatile(
        json.loads((cli_run_dir / "run.json").read_text(encoding="utf-8")), paths
    )
    sandboxed_ledger = strip_volatile(
        json.loads((job.run_dir / "run.json").read_text(encoding="utf-8")), paths
    )
    assert cli_ledger == sandboxed_ledger


# ── job bookkeeping ────────────────────────────────────────────────────────────


def test_prepare_job_mints_a_fresh_run_id_and_a_matching_run_dir(tmp_path: Path) -> None:
    policy = load_policy()
    first = prepare_job(
        artifacts_root=tmp_path,
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=policy,
        provider=stub(),
        provider_name="stub",
    )
    second = prepare_job(
        artifacts_root=tmp_path,
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=policy,
        provider=stub(),
        provider_name="stub",
    )

    assert first.run_id != second.run_id
    assert first.run_dir == tmp_path / first.run_id
    assert first.state is JobState.QUEUED
    assert first.status == "QUEUED"


def test_start_and_wait_run_on_a_background_thread(tmp_path: Path) -> None:
    job = prepare_job(
        artifacts_root=tmp_path,
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=load_policy(),
        provider=stub(),
        provider_name="stub",
    )

    start(job)
    assert job.thread is not None
    assert job.thread.is_alive() or job.done  # replay is fast; either observation is legitimate

    finished = wait(job, timeout=30)

    assert finished
    assert job.done
    assert job.state is JobState.FINISHED
    assert not job.thread.is_alive()


def test_waiting_on_a_job_that_never_started_does_not_hang(tmp_path: Path) -> None:
    job = prepare_job(
        artifacts_root=tmp_path,
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        policy=load_policy(),
        provider=stub(),
        provider_name="stub",
    )

    assert wait(job, timeout=1) is False
    assert job.state is JobState.QUEUED
