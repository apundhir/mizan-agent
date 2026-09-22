"""`mizan run` — verify one submission and say what happened.

`pyproject.toml` has declared this console script since M1 and the module has never existed, so
`pip install -e .` produced a `mizan` command that failed on import. the orchestrated graph is the story that gives
it something to do.

## What it prints when a run does not finish

The interesting output, and the reason the node records exist. A run that halts prints the verdict
it *did* reach — `HALTED`, with the blocking findings — and the node log underneath, which names
the last node entered. A run that fails outright prints the same log with the failure, because
`_guard` writes the exit record before re-raising.

So the question an officer asks after a bad run ("where did it stop?") is answered by the output of
the command that stopped, rather than by a log file somebody has to find.

## Why there is no `--retry`

A retry ladder that hides a transient extraction failure is worse than a halt: the officer cannot
tell which runs were clean. Deferred deliberately in the orchestrated graph and recorded in ADR-0005.
"""

from __future__ import annotations

import argparse
import json
import re
import resource
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from tda.agents.provider import ProviderError, ProviderMode, ReplayProvider, StubProvider
from tda.contracts import Period, VerdictStatus
from tda.graph import RunContext, new_run_id, verify_directory
from tda.metrics import METRIC_LIBRARY_VERSION
from tda.obs import (
    RateCard,
    build_ledger,
    cost_summary,
    latest_run,
    read_run,
    redact,
    routing_records,
    write_run,
)
from tda.obs.viewer import render_tree
from tda.outputs import write_outputs
from tda.policy import load_policy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.agents.provider.base import LLMProvider
    from tda.contracts import Claim, Verdict
    from tda.obs import NodeLog, RoutingLog, RunLedger, TraceLog, WrittenRun
    from tda.outputs import WrittenOutputs
    from tda.policy import Policy

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SUBMISSION = REPO_ROOT / "corpus" / "demo" / "submission"
ARTIFACTS = REPO_ROOT / "artifacts"

# Exit codes, because a shell reads these and a human reads the text.
OK = 0
FINDINGS = 1
COULD_NOT_RUN = 2

# Not the exact shape `tda.graph.run.new_run_id()` produces (`run-<12 hex>`) - this repository's
# own tests give a sandboxed run a readable id (`run-sandboxtest01`) rather than a real one, and
# that is a legitimate id too, not a shape to reject. What actually matters before `--run-id` is
# trusted as a path component (`args.artifacts / run_id`, and everything `_write` builds under it)
# is the property a traversal attempt would need to break: no path separator, no `.` at all, so a
# `..` segment cannot appear even by accident.
_RUN_ID = re.compile(r"run-[A-Za-z0-9_-]+")


def declaration_from_manifest(submission: Path) -> tuple[str, str] | None:
    """The hotel and period a case record states, if one sits beside the submission.

    `manifest.json` lives one level **above** `submission/`, and that placement is what makes this
    legitimate rather than circular: intake exists to check the files against a declaration made
    somewhere other than the files. The manifest is the case record — what the system was told to
    expect — and the submission directory is what was actually sent. Reading the declaration out of
    a file *inside* `submission/` would be asking the evidence to vouch for itself.

    Returns `None` when there is no manifest, and the caller must then be told the hotel and period
    explicitly. Guessing them would defeat intake entirely.
    """
    manifest = submission.parent / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        return str(payload["hotel_id"]), str(payload["period"])
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def build_provider(name: str) -> LLMProvider:
    """The model layer this run talks to.

    `replay` is the default everywhere in this repo, and the committed cassettes carry a run on the
    demo corpus from end to end without a key. A request nothing was recorded against still misses
    and says so, which is the designed behaviour: a replay run never makes a live call, and a miss
    is a hard error rather than a quiet fall-through to the network.
    """
    if name == ProviderMode.STUB.value:
        return StubProvider()
    if name == ProviderMode.ANTHROPIC.value:
        from tda.agents.provider import anthropic_provider

        return anthropic_provider()
    return ReplayProvider()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mizan", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Verify one submission end to end.")
    run.add_argument("submission", nargs="?", type=Path, default=DEMO_SUBMISSION)
    run.add_argument("--hotel", default=None, help="The property this submission is declared for.")
    run.add_argument("--period", default=None, help="The period it is declared for, e.g. 2026-Q1.")
    run.add_argument(
        "--artifacts", type=Path, default=ARTIFACTS, help="Where to write artifacts/<run_id>/."
    )
    run.add_argument(
        "--provider",
        default=None,
        choices=[m.value for m in (ProviderMode.REPLAY, ProviderMode.STUB, ProviderMode.ANTHROPIC)],
        help="Overrides policy.model.provider. Default: whatever policy says.",
    )
    run.add_argument(
        "--run-id",
        default=None,
        help=(
            "Use this id rather than minting one. For a caller that already named the directory "
            "the submission was staged into (tda.review.sandbox, running this command as a "
            "resource-limited subprocess) - not meant to be typed by hand."
        ),
    )
    run.add_argument(
        "--max-memory-bytes",
        type=int,
        default=None,
        help=(
            "Set RLIMIT_AS to this many bytes before doing anything else (Linux only - see "
            "tda.review.sandbox for why). For tda.review.sandbox, which computes the number; not "
            "meant to be typed by hand."
        ),
    )
    run.add_argument(
        "--max-cpu-seconds",
        type=int,
        default=None,
        help=(
            "Set RLIMIT_CPU to this many seconds before doing anything else. For "
            "tda.review.sandbox; not meant to be typed by hand."
        ),
    )
    trace = sub.add_parser("trace", help="Render a run's trace as a readable tree.")
    trace.add_argument(
        "run",
        nargs="?",
        default=None,
        help="A run id, or a path to a run directory. Default: the most recent run.",
    )
    trace.add_argument("--artifacts", type=Path, default=ARTIFACTS)

    args = parser.parse_args(argv)

    if args.command == "trace":
        return _trace(args.run, args.artifacts)

    # Applied to this process itself, first, before importing or reading anything the submission
    # might influence: `tda.review.sandbox` used to set these via `subprocess.Popen`'s
    # `preexec_fn`, which runs in a forked-but-not-yet-exec'd copy of a caller that may be
    # multithreaded (the console's own worker thread) - a fork/exec window the `subprocess`
    # module's own documentation warns can deadlock on a lock another thread held at the moment of
    # fork. This process, once this line runs, has already exec'd - a single-threaded interpreter
    # holding no lock any other thread could have contended for - so there is no such window here.
    if args.max_memory_bytes is not None and sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (args.max_memory_bytes, args.max_memory_bytes))
    if args.max_cpu_seconds is not None:
        resource.setrlimit(resource.RLIMIT_CPU, (args.max_cpu_seconds, args.max_cpu_seconds))

    policy = load_policy()

    declared = declaration_from_manifest(args.submission)
    hotel = args.hotel or (declared[0] if declared else None)
    period_text = args.period or (declared[1] if declared else None)
    if not hotel or not period_text:
        print(
            f"cannot tell what {args.submission} is supposed to be.\n"
            "  Pass --hotel and --period, or run against a directory with a manifest beside it.\n"
            "  Intake checks the files against a declaration made somewhere other than the files;\n"
            "  guessing it from the submission would defeat the check entirely.",
            file=sys.stderr,
        )
        return COULD_NOT_RUN

    try:
        period = Period.parse(period_text)
    except ValueError as exc:
        print(f"{period_text!r} is not a period: {exc}", file=sys.stderr)
        return COULD_NOT_RUN

    provider = build_provider(args.provider or policy.model.provider)
    # Built here rather than inside `verify` so the node log survives a failure. `verify` does not
    # catch anything, so a run that dies would otherwise take the record of where it died with it.
    context = RunContext.build(policy, period, provider)
    # Minted here rather than inside `verify` so that a run which dies still writes its artifacts
    # under the id the run actually had, and `mizan trace <id>` finds them - unless a caller
    # already named the directory the submission lives under and passed that id back in. Checked
    # against `_RUN_ID` before it is trusted as a path component (`args.artifacts / run_id` and
    # everything `_write` builds under it): the only caller today (`tda.review.sandbox`) always
    # passes one it minted itself, but a shape this function does not enforce here is a traversal
    # primitive resting on that caller's discipline alone, and a CLI flag is a boundary this
    # codebase checks at, not trusts across.
    if args.run_id is not None and not _RUN_ID.fullmatch(args.run_id):
        print(
            f"--run-id {args.run_id!r} is not a run id. Expected 'run-' followed by letters, "
            "digits, '_' or '-', and nothing else: no path separator, no '.'.",
            file=sys.stderr,
        )
        return COULD_NOT_RUN
    run_id = args.run_id or new_run_id()
    started = time.perf_counter()

    try:
        result = verify_directory(
            args.submission, hotel, period, policy, provider, run_id=run_id, context=context
        )
    except Exception as exc:
        report_failure(exc, context)

        # A failed run's artifacts are the ones somebody goes looking for. Writing them only on
        # success would mean the trace exists exactly when nobody needs it. Wrapped in its own
        # try/except: building the ledger digests every input file, real allocation this process
        # may no longer have room for when the exception above was itself a MemoryError under a
        # sandboxed run's own RLIMIT_AS - measured directly, not hypothesised, driving the exact
        # hyperlink-range construction ADR-0010 §7 describes past its ceiling. Left unguarded, a
        # second, unhandled MemoryError here would exit with Python's own default code for an
        # uncaught exception (1), indistinguishable from `FINDINGS` to anything reading the exit
        # code - an honest sentinel matters as much for an exit code as for a written field.
        try:
            written = _write(
                args.artifacts,
                failure_ledger(
                    run_id=run_id,
                    hotel=hotel,
                    period=period,
                    policy=policy,
                    context=context,
                    submission=args.submission,
                    duration_ms=_elapsed(started),
                ),
                context.trace,
                context.nodes,
                routing_records(context.supervisor.decisions),
            )
        except Exception as write_exc:
            print(f"could not write this run's own failure record: {write_exc}", file=sys.stderr)
            return COULD_NOT_RUN
        if written:
            print(written.render(), file=sys.stderr)
            print(f"  mizan trace {run_id}", file=sys.stderr)
        return COULD_NOT_RUN

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
        usage=context.usage,
        duration_ms=_elapsed(started),
        rates=RateCard.from_env(),
    )

    # The verdict is printed before anything is written. A run that reached a conclusion and then
    # could not write a file has still reached a conclusion, and discarding it behind a traceback
    # from `mkdir` would lose the answer to keep the paperwork.
    print(result.render())
    print()
    print(cost_summary(context.usage, RateCard.from_env()))

    written = _write(
        args.artifacts,
        ledger,
        context.trace,
        result.nodes,
        routing_records(context.supervisor.decisions),
    )
    if written is not None:
        # `as_written` rather than `ledger`: the file on disk is redacted and the object in memory
        # is not, and printing the one we handed over would put on the terminal exactly what was
        # kept out of the file. A submitted file name can carry an address.
        print(written.as_written.render())
        print(written.render())

    # The three artefacts an officer files. Written to the same run directory rather than through
    # `written`, so a failure to write the ledger does not also cost the hotel its memo.
    outputs = _outputs(
        args.artifacts / verdict.run_id,
        result.state.claims,
        # The field intake validated, rather than a fresh guess at which file is the workbook.
        result.state.submission.workbook,
        verdict=verdict,
    )
    if outputs is not None:
        print(outputs.render())
    print(f"  run took {ledger.duration_ms:,}ms  (recorded, not targeted - see ADR-0006)")
    print(f"  mizan trace {verdict.run_id}")

    if result.verdict.status is VerdictStatus.PASS:
        return OK
    # Anything else is a submission a human has to look at. Non-zero, because `make run` returning
    # success for a rejected submission would make an unread verdict look like a clean one.
    return FINDINGS


def _trace(target: str | None, artifacts: Path) -> int:
    """Render one run's trace as a tree. `mizan trace` with no argument takes the latest.

    Reads the **written** artifacts rather than re-running anything, which is the point: the
    question "why did it say that?" is asked after the fact, often by somebody who was not there
    when it ran.
    """
    if target is None:
        directory = latest_run(artifacts)
        if directory is None:
            print(
                f"no runs under {artifacts}. `make run` writes one, and `mizan trace` reads it "
                "back - nothing here re-runs the pipeline.",
                file=sys.stderr,
            )
            return COULD_NOT_RUN
    else:
        candidate = Path(target)
        directory = candidate if candidate.is_dir() else artifacts / target

    try:
        ledger, trace, nodes = read_run(directory)
    except (FileNotFoundError, ValueError) as exc:
        print(f"cannot read a run from {directory}: {exc}", file=sys.stderr)
        return COULD_NOT_RUN

    print(render_tree(ledger, trace, nodes, RateCard.from_env()))
    return OK


def report_failure(exc: Exception, context: RunContext) -> None:
    """Say what went wrong and where it stopped — redacted, like every other channel.

    One handler, two messages: a cassette miss is the expected failure today and its own message
    already says to run `make record`, so it is printed rather than paraphrased.

    **The redaction is the part worth keeping.** A pydantic validation error quotes the value it
    rejected — `input_value='DOE/JANE'` is the exact shape `slashed_name` exists to catch — and the
    node log carries file names and halt reasons straight from the submission. The artifacts
    redact both; a terminal that did not would be the one channel with a leak in it, which is the
    channel somebody screenshots.
    """
    raw = str(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
    print(f"the run could not complete: {redact(raw)[0]}", file=sys.stderr)
    # The node log is the point of the node log: it names the last node entered, which is where
    # the run stopped.
    if len(context.nodes):
        print(f"\n{redact(context.nodes.render())[0]}", file=sys.stderr)
        if unfinished := context.nodes.unfinished():
            print(f"  stopped inside: {', '.join(unfinished)}", file=sys.stderr)


def _outputs(
    directory: Path, claims: Sequence[Claim], workbook: Path | None, *, verdict: Verdict
) -> WrittenOutputs | None:
    """Write the verdict, the memo and the annotated workbook, or say why not.

    Guarded like `_write` and for the same reason: a run that reached a conclusion has reached it
    whether or not a Word file could be saved. The failure is reported rather than raised, so the
    verdict already on the screen is not lost behind a traceback from the last step.

    `Exception` rather than `OSError`, which is the catch this had and which was not enough. A
    corrupt `.xlsx` raises `zipfile.BadZipFile` — not an `OSError` — so a submission intake had
    already refused for being unreadable ended the run in a traceback, with exit code 1 colliding
    with `FINDINGS`. `tda.outputs` now handles that case itself; this stays as the backstop, since
    the point of the guard is that the last step cannot take the verdict down with it.
    """
    try:
        return write_outputs(directory, verdict, claims, workbook)
    # Broad on purpose - see the docstring. The verdict is already printed.
    except Exception as exc:
        print(f"the outputs could not be written to {directory}: {exc}", file=sys.stderr)
        return None


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _write(
    root: Path,
    ledger: RunLedger,
    trace: TraceLog,
    nodes: NodeLog,
    routing: RoutingLog | None = None,
) -> WrittenRun | None:
    """Write the artifacts, or say why not — and never take the verdict down with them.

    An unwritable artifacts root is a problem with the machine, not with the verification. Letting
    it raise would turn a completed run into a traceback with the conclusion discarded, which is
    the wrong trade in both directions: the operator loses the answer *and* the reason.
    """
    try:
        return write_run(root, ledger, trace, nodes, routing)
    except OSError as exc:
        print(
            f"the run completed but its artifacts could not be written to {root}: {exc}",
            file=sys.stderr,
        )
        return None


def failure_ledger(
    *,
    run_id: str,
    hotel: str,
    period: Period,
    policy: Policy,
    context: RunContext,
    submission: Path,
    duration_ms: int,
) -> RunLedger:
    """The ledger for a run that never produced a verdict.

    Public rather than private: the Run console builds the same kind of failure ledger from a
    background thread, on the same rules, and calling this rather than re-deriving it is what keeps
    the two writers from quietly disagreeing about what a failed run's ledger should say.

    `status` is `FAILED`, which is the one value `VerdictStatus` cannot express and should not: a
    `Verdict` is a statement about a submission, and a run that died made no statement. The ledger
    is a statement about the *run*, so it can say this.

    The prompt versions are read straight off the trace rather than through
    `TraceLog.prompt_versions()`, which raises when one agent ran two versions. That check is right
    for a verdict and wrong here — raising inside a failure handler would replace the original
    exception with a second one, and the first is the one somebody needs.
    """
    return build_ledger(
        run_id=run_id,
        status="FAILED",
        rejection_reason=None,
        hotel_id=hotel,
        period=str(period),
        policy_version=policy.version,
        metric_library_version=METRIC_LIBRARY_VERSION,
        model_id=policy.model.model_id,
        provider_mode=context.provider_mode,
        prompt_versions={r.agent: r.prompt_version for r in context.trace.records},
        inputs=files_in(submission),
        nodes=context.nodes,
        usage=context.usage,
        duration_ms=duration_ms,
    )


def files_in(submission: Path) -> tuple[Path, ...]:
    """What was in the submission directory, for a run that failed before it agreed on a file set.

    Public alongside `failure_ledger`, which is its only real caller outside this module. Best
    effort on purpose: a directory that cannot be listed is why some runs fail, and the ledger
    recording no inputs is better than the failure handler failing.
    """
    try:
        return tuple(sorted(p for p in submission.iterdir() if p.is_file()))
    except OSError:
        return ()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
