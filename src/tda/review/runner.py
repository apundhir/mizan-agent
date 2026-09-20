"""The background worker: `mizan run`'s own sequence, off the terminal, for the console to poll.

`execute()` is `tda.cli.main`'s run path with the printing removed and the state held on a
`RunJob` instead: build a context, mint a run id, call `verify_directory`, build the ledger, write
the four artifacts, write the verdict and its documents. A failed run still writes what it can,
through the same public `tda.cli.failure_ledger` the CLI itself calls, so the two writers cannot
quietly drift apart about what a failed run's ledger should say.

## Why a job, not a return value

`verify_directory` can take real time - a live call, a large PDF - and the console has to keep
rendering while it runs. `start()` puts `execute()` on a daemon thread and hands back the same
`RunJob` the caller already holds; the console polls `job.state`, `job.context.nodes.records` and
`job.context.trace.records` from the main thread while the worker appends to them. Both logs are
plain lists behind a thin wrapper - see `tda.obs.nodes.NodeLog` and `tda.obs.trace.TraceLog` - and
under CPython an append and a `tuple(list)` snapshot are each atomic, so a reader never sees a torn
record, only a shorter prefix than the writer has reached since.

## What "failed" means here, and what does not fail the job

Only `verify_directory` raising fails the job - the same distinction `mizan run` makes. A written
run's artifacts or its documents failing to save afterwards is recorded in `write_error` without
moving `state` to `FAILED`: a verdict the pipeline reached is a verdict it reached, whether or not
the disk cooperated afterwards, and `mizan run`'s own `_write` and `_outputs` make exactly this
distinction for exactly this reason.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from tda.agents.provider import ProviderError
from tda.cli import failure_ledger
from tda.graph import RunContext, new_run_id, verify_directory
from tda.obs import NodeOutcome, build_ledger, read_run, redact, routing_records, write_run
from tda.outputs import VERDICT_FILE, read_verdict, write_outputs
from tda.review.sandbox import run_sandboxed

if TYPE_CHECKING:
    from pathlib import Path

    from tda.agents.provider.base import LLMProvider
    from tda.contracts import Period, Verdict
    from tda.graph import RunResult
    from tda.obs import WrittenRun
    from tda.outputs import WrittenOutputs
    from tda.policy import Policy


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


@dataclass(slots=True)
class RunJob:
    """One run's identity, inputs and - as the worker fills them in - its outcome.

    `context` is the same `RunContext` `verify_directory` is given, so its `nodes`, `trace` and
    `supervisor` are exactly what the console reads live; there is no second copy for the console
    to fall out of step with the one the pipeline is actually writing to.
    """

    run_id: str
    run_dir: Path
    artifacts_root: Path
    submission: Path
    hotel_id: str
    period: Period
    provider_name: str
    policy: Policy
    provider: LLMProvider
    context: RunContext
    scene_key: str | None = None
    # An upload runs `mizan run` as a resource-limited subprocess (`tda.review.sandbox`) rather
    # than in this thread - see the module docstring. A scene's own files are this repository's,
    # so a scene stays in-process for the live, node-by-node progress that only that path gives.
    sandboxed: bool = False
    state: JobState = JobState.QUEUED
    started: float = 0.0
    finished: float | None = None
    result: RunResult | None = None
    error: str | None = None
    written: WrittenRun | None = None
    outputs: WrittenOutputs | None = None
    write_error: str | None = None
    thread: threading.Thread | None = field(default=None, repr=False, compare=False)

    @property
    def done(self) -> bool:
        return self.state in (JobState.FINISHED, JobState.FAILED)

    @property
    def verdict(self) -> Verdict | None:
        """The verdict this job reached, however it ran. A job run in-process holds it on
        `result`; a sandboxed job has no live `RunResult` to hold one - `mizan run`, in its own
        subprocess, already wrote `verdict.json` before exiting, and this reads that back the same
        way `tda.review.app`'s own picker does for a run made some other way entirely."""
        if self.result is not None:
            return self.result.verdict
        if self.sandboxed and self.state is JobState.FINISHED:
            return read_verdict(self.run_dir / VERDICT_FILE)
        return None

    @property
    def status(self) -> str:
        """One word for the console to colour: the verdict's status once there is one, `FAILED`
        for a run that died, or the job's own state while neither has happened yet."""
        if self.state is JobState.FAILED:
            return "FAILED"
        verdict = self.verdict
        if verdict is not None:
            return verdict.status.value
        return self.state.value.upper()

    @property
    def duration_ms(self) -> int:
        if self.finished is None:
            return 0
        return int((self.finished - self.started) * 1000)


def prepare_job(
    *,
    artifacts_root: Path,
    submission: Path,
    hotel_id: str,
    period: Period,
    policy: Policy,
    provider: LLMProvider,
    provider_name: str,
    scene_key: str | None = None,
    run_id: str | None = None,
    sandboxed: bool = False,
) -> RunJob:
    """Everything a job needs to start, built but not yet run.

    `run_id` is minted here, not inside `execute()`, matching `mizan run`'s own reasoning
    (`tda.cli.main`): a run that dies keeps its context and its id, so a console showing
    "run-<id> failed" is showing the id that run actually had.

    A caller may pass `run_id` explicitly rather than let one be minted - the console does, because
    it stages a submission into `artifacts_root/<run_id>/submission/` *before* a job exists to hold
    that id, and the two must name the same directory. `submission` is still taken as given rather
    than derived from `run_id` here, so a caller with an unusual layout is not forced into one.

    `context` is built and handed to `RunJob` either way, sandboxed or not - `execute()` never
    reads it for a sandboxed job (there is nothing on it worth reading; the subprocess builds its
    own), but `_sidebar`/the console's routing display would otherwise have to special-case a job
    with none, and an unused, cheap object is simpler than that branch.
    """
    run_id = run_id or new_run_id()
    return RunJob(
        run_id=run_id,
        run_dir=artifacts_root / run_id,
        artifacts_root=artifacts_root,
        submission=submission,
        hotel_id=hotel_id,
        period=period,
        provider_name=provider_name,
        policy=policy,
        provider=provider,
        context=RunContext.build(policy, period, provider),
        scene_key=scene_key,
        sandboxed=sandboxed,
    )


def _redacted(exc: BaseException) -> str:
    raw = str(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
    return redact(raw)[0]


def _write_failure_artifacts(job: RunJob) -> None:
    ledger = failure_ledger(
        run_id=job.run_id,
        hotel=job.hotel_id,
        period=job.period,
        policy=job.policy,
        context=job.context,
        submission=job.submission,
        duration_ms=job.duration_ms,
    )
    try:
        job.written = write_run(
            job.artifacts_root,
            ledger,
            job.context.trace,
            job.context.nodes,
            routing_records(job.context.supervisor.decisions),
        )
    except OSError as exc:
        job.write_error = f"the run failed and its artifacts could not be written: {exc}"


def _execute(job: RunJob) -> None:
    try:
        result = verify_directory(
            job.submission,
            job.hotel_id,
            job.period,
            job.policy,
            job.provider,
            run_id=job.run_id,
            context=job.context,
        )
    except Exception as exc:
        job.error = _redacted(exc)
        _write_failure_artifacts(job)
        job.state = JobState.FAILED
        return

    job.result = result
    verdict = result.verdict
    ledger = build_ledger(
        run_id=verdict.run_id,
        status=verdict.status.value,
        rejection_reason=verdict.rejection_reason.value if verdict.rejection_reason else None,
        hotel_id=verdict.hotel_id,
        period=verdict.period,
        policy_version=verdict.policy_version,
        metric_library_version=verdict.metric_library_version,
        model_id=verdict.model_id,
        provider_mode=verdict.provider_mode,
        prompt_versions=verdict.prompt_versions,
        inputs=result.state.submission.files,
        nodes=result.nodes,
        usage=job.context.usage,
        duration_ms=job.duration_ms,
    )
    try:
        job.written = write_run(
            job.artifacts_root,
            ledger,
            job.context.trace,
            result.nodes,
            routing_records(job.context.supervisor.decisions),
        )
    except OSError as exc:
        job.write_error = f"the run completed but its artifacts could not be written: {exc}"

    try:
        job.outputs = write_outputs(
            job.artifacts_root / verdict.run_id,
            verdict,
            result.state.claims,
            result.state.submission.workbook,
        )
    # Broad on purpose, matching `tda.cli._outputs`: a corrupt `.xlsx` raises `zipfile.BadZipFile`,
    # not an `OSError`, and the verdict this run reached must not be lost behind that traceback.
    except Exception as exc:
        note = f"the outputs could not be written: {exc}"
        job.write_error = f"{job.write_error} {note}" if job.write_error else note

    job.state = JobState.FINISHED


def _execute_sandboxed(job: RunJob) -> None:
    """`_execute`'s sandboxed counterpart. `mizan run` does the actual work, in its own memory-
    and CPU-limited subprocess (`tda.review.sandbox.run_sandboxed`); this reads back what it
    wrote rather than holding a live `RunResult` - see this module's and `tda.review.sandbox`'s
    own docstrings for why an upload gets this path and a scene does not.
    """
    outcome = run_sandboxed(
        submission=job.submission,
        hotel_id=job.hotel_id,
        period=job.period,
        artifacts_root=job.artifacts_root,
        provider_name=job.provider_name,
        run_id=job.run_id,
    )
    # Checked before trusting outcome.ok: a resource-limit kill or the wall-clock timeout can land
    # after mizan run has already written verdict.json, in the tail of its own exit - subprocess.run
    # reporting a non-ordinary outcome does not mean nothing was written, only that the parent did
    # not see an ordinary exit. A real verdict on disk is a real verdict, however the child's own
    # exit was reported.
    if (job.run_dir / VERDICT_FILE).is_file():
        # mizan run exited 0 or 1 - a verdict was reached, whatever it says. job.result stays
        # None; RunJob.verdict reads verdict.json back for anything that needs it.
        job.state = JobState.FINISHED
        return

    if not outcome.ok:
        job.error = outcome.reason
        job.state = JobState.FAILED
        return

    # mizan run exited 2 (COULD_NOT_RUN): verify_directory raised inside the subprocess, and its
    # own failure ledger is already on disk, written by the same tda.cli.failure_ledger path a
    # bare-terminal run uses. The failed node's own stated reason (NodeRecord.detail is required
    # for a FAILED outcome - see its model_post_init) is the clearest single line to surface.
    try:
        _, _, nodes = read_run(job.run_dir)
        failed = next((r for r in nodes.records if r.outcome is NodeOutcome.FAILED), None)
        job.error = failed.detail if failed is not None else None
    except (OSError, ValueError):
        job.error = None
    job.state = JobState.FAILED


def execute(job: RunJob) -> None:
    """Run the job to completion. Called on the worker thread by `start()`, or directly by a
    caller (a test, a scene builder) that wants to run synchronously."""
    job.state = JobState.RUNNING
    job.started = time.perf_counter()
    try:
        _execute_sandboxed(job) if job.sandboxed else _execute(job)
    except BaseException as exc:
        # `_execute` already catches the pipeline's own exceptions around `verify_directory`. This
        # is the net under a defect in this module's own bookkeeping - the ledger build, the write
        # calls - so a bug here fails the job visibly rather than leaving it stuck in RUNNING,
        # which a console polling `job.state` would have no way to tell apart from a slow one.
        job.error = _redacted(exc)
        job.state = JobState.FAILED
    finally:
        job.finished = time.perf_counter()


def start(job: RunJob) -> RunJob:
    """Run `job` on a daemon thread and return it immediately, already recorded on `job.thread`.

    Daemon, so a job left running does not keep the process alive on its own - the console's own
    lifecycle decides that, not an orphaned verification nobody is watching anymore.
    """
    thread = threading.Thread(target=execute, args=(job,), name=job.run_id, daemon=True)
    job.thread = thread
    thread.start()
    return job


def wait(job: RunJob, timeout: float | None = None) -> bool:
    """Block up to `timeout` seconds for `job` to finish. Returns whether it had, which is not the
    same question as whether `join` timed out - a job never started has no thread to join."""
    if job.thread is not None:
        job.thread.join(timeout)
    return job.done
