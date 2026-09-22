"""An uploaded submission is verified in a resource-limited subprocess, not in this one.

## Why a process boundary, after five rounds of trying without one

`tda.review.staging` bounded an upload's compressed size, then its decompressed size, then its
declared cell count three different ways - and a G5 security review defeated each one in turn with
a real, working proof of concept: a namespace-prefixed cell tag, a merge range needing no `<c>`
element at all, a `<dimension>` hint that lied by being merely wrong rather than absent, and
finally a `<hyperlink ref="A1:XFD1048576">` that drove a 1.5-kilobyte upload to over a gigabyte of
resident memory. Each fix closed the exact construct demonstrated and left the next one open,
because the fix was reading the *shape* of an OOXML file and guessing what `openpyxl` would build
from it - and `openpyxl`'s reader has enough range-expanding methods (merges, hyperlinks, whatever
comes next) that enumerating them is chasing the library's internals rather than bounding the cost.

The reviewer's own conclusion, taken as the fix: stop trying to predict how much memory a file will
cost to open, and bound how much memory the process opening it is *allowed* to use, at the
operating system, regardless of what the file contains. `tda.excel.run.open_submission`'s double
`read_only=False` load - the actual expensive step every one of the five rounds was really about -
now runs inside `mizan run`, spawned as a genuine child process with `RLIMIT_AS` and `RLIMIT_CPU`
applied to itself before doing anything else, plus a wall-clock timeout on top for whatever a CPU
limit alone would not catch (a process blocked on I/O rather than spinning). A file that would
have exhausted the shared Streamlit process instead exhausts its own child's ceiling and is
killed - contained, not prevented at the door by static inspection that the next OOXML construct
only has to route around.

A sixth review, of this backstop itself, found the boundary correctly closed against injection,
traversal and TOCTOU but the parent still doing the one thing it was built to stop doing: `staging`
kept a real cell count and a merge-range sum that only `open_submission` could actually answer,
paid for with an unsandboxed parse in this process on every upload, however small the file. Removed
rather than patched a sixth time - `tda.review.staging` now bounds only what a byte cap can answer
without opening the archive, and everything past that is this module's to contain, consistently
rather than as a second, partial copy of the same idea. See ADR-0010 §7.

## What this module is not

Not a replacement for `tda.review.staging`'s own checks (the byte cap on the upload, the
decompressed-size cap on the archive) - those still run first, cheaply, from zip metadata alone,
and refuse a large class of upload before a subprocess is even started. This module is the
backstop for everything past that: whatever a valid-looking, byte-cap-respecting archive expands
into once a real parser opens it, by a mechanism this codebase did not anticipate and no longer
tries to enumerate. Defence in depth, not either-or.

Not used for a prepared scene. A scene's files are this repository's own committed corpus or a
fixture derived from it - nothing this process did not already trust before the hosted console existed - so
running one keeps the existing in-process path and its live, node-by-node progress. Only a
viewer's own upload, the one input this codebase has never authored, pays the subprocess cost and
gives up watching each stage light up as it happens: `tda.review.console` shows a single "verifying
your submission" state while this runs, then the complete timeline at once, read back from the
same files `mizan run` already writes - not a compromise invented for this module, the same
rendering the replay panel already uses for a run made earlier.

## Reading the outcome back

`mizan run` exits `0` (`PASS`), `1` (a verdict a human must read) or `2` (`COULD_NOT_RUN`, already
recorded to that run's own failure ledger by the same code path `mizan run` itself uses on a bare
terminal) - all three are an ordinary pipeline outcome, read back from the artifacts written under
`run_id`, exactly as a job run in-process is. Anything else - a resource limit killing the process,
a wall-clock timeout, a signal - means no verdict was reached and very possibly nothing was
written; `run_sandboxed` reports that itself, in `SandboxResult.reason`, rather than asking a
caller to infer a crash from a missing file.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from tda.contracts import Period

# `streamlit_app.py` puts `src/` on `sys.path` itself at import time, because Streamlit Community
# Cloud installs from `requirements.txt` alone - there is no `pip install -e .` step there, so
# `tda` is never a real, importable package the way it is in this repository's own `.venv` or the
# Docker image. That `sys.path.insert` is an in-process change and does not cross a `subprocess.run`
# boundary: a child process gets a fresh interpreter and a fresh `sys.path`, built from `PYTHONPATH`
# and its own site-packages, neither of which knows anything about the parent's runtime patch. The
# child this module spawns is given the same directory explicitly, in its own environment, so it
# does not depend on how the parent process happened to become able to import `tda` itself.
_REPO_ROOT = Path(__file__).resolve().parents[3]

MAX_MEMORY_BYTES_ENV: Final = "MIZAN_SANDBOX_MAX_MEMORY_BYTES"
MAX_CPU_SECONDS_ENV: Final = "MIZAN_SANDBOX_MAX_CPU_SECONDS"
TIMEOUT_SECONDS_ENV: Final = "MIZAN_SANDBOX_TIMEOUT_SECONDS"
MAX_CONCURRENT_ENV: Final = "MIZAN_SANDBOX_MAX_CONCURRENT"

# A real run against a five-file demo submission measures under a gigabyte of virtual memory and
# under four seconds of CPU on Linux (the actual hosted platform - macOS's `RLIMIT_AS` accounting
# is unusable for this, see the note on `_apply_limits`). A G5 security review found this default
# had never been checked against the platform's own published ceiling for a whole app - Streamlit
# Community Cloud documents "690MB minimum, 2.7GB maximum" per app (docs.streamlit.io, manage-your-
# app, checked 2026-09-19) - and a child alone allowed to approach the old 1.5 GiB default could
# exceed even the high end of that once the parent's own footprint is added. Lowered here to a
# smaller multiple of the measured real-run cost, with `MAX_CONCURRENT_ENV` below bounding how many
# children may hold their share of it at once - **both still need checking against this
# deployment's actual container** during the runbook's own first-checks-after-deploy step; neither
# figure is a claim about what that container really is, only the best bound available before it is
# deployed and measured directly. Configurable because the right number depends on the hosting
# tier's own ceiling, which this codebase does not control.
DEFAULT_MAX_MEMORY_BYTES: Final = 1_073_741_824  # 1 GiB
DEFAULT_MAX_CPU_SECONDS: Final = 30
# Wall-clock, on top of the CPU limit: catches a process blocked on I/O (a slow disk, a hung
# syscall) that a CPU limit alone would never see, since it is not spending CPU while it hangs.
DEFAULT_TIMEOUT_SECONDS: Final = 90
# Concurrent sandboxed subprocesses, not lifetime spend (contrast tda.review.live.ProcessLiveRuns,
# which never releases): each browser session is free to run its own upload, and N sessions
# uploading at once used to mean N children each allowed up to the memory ceiling above, with
# nothing bounding N. Small on purpose - a legitimate demo rarely has more than one or two viewers
# uploading at the same moment, and refusing a third with a clear reason is a far better failure
# mode than letting it contend the container into an OOM kill that takes every session down.
DEFAULT_MAX_CONCURRENT: Final = 2

# mizan run's own exit codes (tda.cli.COULD_NOT_RUN and its two siblings) - any of these means the
# subprocess ran to completion and wrote what it always writes, success or failure alike. Anything
# else is this module's problem to report, not the pipeline's.
_ORDINARY_EXIT_CODES: Final = frozenset({0, 1, 2})


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Whether the subprocess reached one of `mizan run`'s own outcomes. `ok=True` says nothing
    about whether the *run* passed - only that it ran to completion and its artifacts, if any,
    are `mizan run`'s own to read back the ordinary way."""

    ok: bool
    reason: str | None = None


def _env_int(name: str, default: int) -> int:
    """The configured override for `name`, or `default` when it is unset or blank - never when it
    is *set* to something that does not parse. A malformed value (a typo, a stray unit suffix) is
    an operator's mistake worth surfacing, not one this function should paper over by quietly
    reaching for the number the operator was trying to override in the first place; a raise here
    reaches `tda.review.runner.execute`'s own `except BaseException`, so it becomes one clearly
    worded failed job, not a crashed page."""
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    parsed = int(value)  # raises ValueError on anything that is not a plain integer
    if parsed <= 0:
        raise ValueError(f"{name}={value!r} must be a positive integer")
    return parsed


def _limits() -> tuple[int, int, int, int]:
    return (
        _env_int(MAX_MEMORY_BYTES_ENV, DEFAULT_MAX_MEMORY_BYTES),
        _env_int(MAX_CPU_SECONDS_ENV, DEFAULT_MAX_CPU_SECONDS),
        _env_int(TIMEOUT_SECONDS_ENV, DEFAULT_TIMEOUT_SECONDS),
        _env_int(MAX_CONCURRENT_ENV, DEFAULT_MAX_CONCURRENT),
    )


class SandboxConcurrency:
    """How many sandboxed subprocesses this server process will run at once.

    Unlike `tda.review.live.ProcessLiveRuns`, which counts lifetime spend and never releases, this
    counts current occupancy: `acquire()` reserves a slot before the subprocess starts and
    `release()` gives it back once the subprocess has exited, whatever the outcome - a slot that
    never released on a killed or timed-out child would eventually refuse every upload this process
    ever saw again. A plain module-level instance below is this process's own single copy, the same
    per-process sharing a module-level object always has, needing no Streamlit dependency to get it
    (this module may not import `streamlit` at all; see `tests/unit/test_console.py`).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = 0

    def acquire(self, limit: int) -> bool:
        with self._lock:
            if self._running >= limit:
                return False
            self._running += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._running -= 1


_CONCURRENCY: Final = SandboxConcurrency()


def _child_env(provider_name: str) -> dict[str, str]:
    """The environment the child runs in - a copy of this process's own, with `src/` guaranteed
    to be on `PYTHONPATH` regardless of whether `tda` is a real, pip-installed package here, and
    the live-mode credential present only when this run can actually use it.

    `subprocess.run` does not cross into a child what `streamlit_app.py` did to its own, in-memory
    `sys.path` at import time - a fresh interpreter builds its own `sys.path` from `PYTHONPATH` and
    its own site-packages, and on Streamlit Community Cloud, which installs from `requirements.txt`
    alone, `tda` is in neither. Verified against exactly that installation shape: a container with
    only `requirements.txt` installed cannot `import tda` at all, and `python -m tda.cli` fails
    with `ModuleNotFoundError` unless `PYTHONPATH` names `src/` explicitly, which is what this
    builds. Harmless where `tda` already is a real package (this repository's own `.venv`, the
    Docker image) - an extra `PYTHONPATH` entry pointing at the same source tree changes nothing
    there.

    A replay or stub run never calls a model and so never needs the key - handing it over anyway
    would put a real credential in the one process on this deployment that spends its time parsing
    a viewer's own, untrusted upload, for no benefit a G5 security review pointed out plainly. The
    name is read from `tda.review.live`, the one module in this codebase allowed to name it, rather
    than repeated here as a second literal for the two to drift out of step against.
    """
    from tda.review.live import API_KEY_ENV, LIVE

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(_REPO_ROOT / "src"), env.get("PYTHONPATH")])
    )
    # Belt and braces alongside the explicit cwd=_REPO_ROOT this is launched with: PYTHONSAFEPATH
    # (3.11+) stops `python -m` from prepending the working directory to sys.path at all, so the
    # child's import resolution depends only on PYTHONPATH and site-packages, never on whatever
    # directory it happened to be started from.
    env["PYTHONSAFEPATH"] = "1"
    if provider_name != LIVE:
        env.pop(API_KEY_ENV, None)
    return env


def run_sandboxed(
    *,
    submission: Path,
    hotel_id: str,
    period: Period,
    artifacts_root: Path,
    provider_name: str,
    run_id: str,
) -> SandboxResult:
    """Run `mizan run` against `submission` as a child process, memory- and CPU-limited, and
    report whether it reached one of the pipeline's own outcomes.

    `run_id` is minted by the caller, the same run-id-before-the-job reasoning
    `tda.review.runner.prepare_job` already documents: a console run stages its submission under
    `artifacts_root/<run_id>/submission/` before anything downstream exists to hold that id, and
    the subprocess must write into the same directory the caller already named.

    The limits are passed as `mizan run --max-memory-bytes/--max-cpu-seconds` and applied by the
    child to itself, rather than by this process through `subprocess.Popen`'s `preexec_fn` - a
    callback that runs in a forked-but-not-yet-exec'd copy of this process, which may be
    multithreaded (the console's own background worker calls this function), a fork/exec window
    the `subprocess` module's own documentation warns can deadlock on a lock another thread held at
    the moment of fork. A CLI flag the child applies to itself, once fully exec'd and running as a
    fresh, single-threaded interpreter, has no such window at all. `hotel_id` is passed
    `=`-joined (`--hotel=<value>`) rather than as a separate argument, so a viewer-typed value that
    happens to start with `-` is never mistaken for a second flag.
    """
    max_memory_bytes, max_cpu_seconds, timeout_seconds, max_concurrent = _limits()
    if not _CONCURRENCY.acquire(max_concurrent):
        return SandboxResult(
            ok=False,
            reason=(
                f"this server is already verifying {max_concurrent} upload(s), the most it will "
                "run at once. Try again in a moment."
            ),
        )
    try:
        command = [
            sys.executable,
            "-m",
            "tda.cli",
            "run",
            str(submission),
            f"--hotel={hotel_id}",
            f"--period={period}",
            f"--artifacts={artifacts_root}",
            f"--provider={provider_name}",
            f"--run-id={run_id}",
            f"--max-cpu-seconds={max_cpu_seconds}",
        ]
        if sys.platform.startswith("linux"):
            command.append(f"--max-memory-bytes={max_memory_bytes}")
        process = subprocess.Popen(
            command,
            cwd=_REPO_ROOT,
            env=_child_env(provider_name),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            process_group=0,
        )
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            # process_group=0 above made this child the leader of its own new process group, so
            # its pid is also that group's id - killing the group rather than just the child
            # catches whatever it may itself have spawned, not only the process this started.
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return SandboxResult(
                ok=False,
                reason=(
                    f"verification did not finish within {timeout_seconds}s and was stopped. "
                    "Nothing was written for this run."
                ),
            )
    finally:
        _CONCURRENCY.release()

    if returncode in _ORDINARY_EXIT_CODES:
        return SandboxResult(ok=True)
    if returncode < 0:
        name = signal.Signals(-returncode).name
        return SandboxResult(
            ok=False,
            reason=(
                f"verification was stopped by the operating system ({name}), most likely for "
                f"exceeding its {max_memory_bytes:,}-byte memory limit or "
                f"{max_cpu_seconds}s CPU limit. Nothing was written for this run."
            ),
        )
    return SandboxResult(
        ok=False,
        reason=(
            f"verification exited unexpectedly (code {returncode}) rather than reaching a "
            "verdict. Nothing was written for this run."
        ),
    )
