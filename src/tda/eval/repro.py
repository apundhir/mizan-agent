"""`make repro`: two runs of one submission, and the claim that they agree.

The reproducibility argument this system is sold on is not `temperature=0`. It is recorded
cassettes replayed offline, a pinned model id and a prompt version per call site. That argument is
worth what its demonstration is worth, so this target runs the demo submission twice and shows the
answer did not move.

## Two assertions, because one of them cannot cover the artefacts

**Assertion 1: different run ids, the same verdict.** Two runs, each minting its own `run_id`,
compared after `tda.obs.repro.volatile_paths` is stripped from both. This is the claim a hotel
cares about: the same submission gets the same answer.

**Assertion 2: the same run id, byte-identical artefacts.** Not belt and braces. The memo prints
`run <run_id>` in its subtitle and the annotated workbook writes it into the legend sheet, so those
two files *can never* be byte-identical across assertion 1, whatever else is true. Without a
second run at a fixed id, `tda.outputs.ooxml.make_reproducible` would be entirely unverified by
the target whose name promises it. That repacking pins `dcterms:modified` and every zip entry's DOS
timestamp, and it is the only reason a generated `.docx` is stable at all.

`run.json` is the one artefact digested after stripping rather than raw, and the reason is
structural: `RunLedger.duration_ms`, `nodes.duration_ms` and `usage.duration_ms` are wall clock on
every row, marked `REPRO_EXCLUDED` for that reason, and no fixed run id makes them agree. Digesting
its stripped, re-serialised payload is the strongest claim the file supports. Said out loud in the
output rather than hidden behind a hash that looks like the other three.

## Why the sanity gate comes before the diff

Two runs that crashed in the same place satisfy assertion 1 perfectly, and so do two runs that
checked nothing. A diff harness with no floor under it reports green for the case it exists to
catch. So each run must first reach the expected status and carry a non-zero claim count, and the
excluded paths must be shown to have carried a real difference: an exclusion set that covers
nothing proves nothing about what was let through, which is the failure `tda.obs.repro` records on
its own first version.

The durations are measured rather than pinned for the same reason. Passing `duration_ms=0` to both
ledgers would make the stripping vacuous and the target would pass with the exclusion mechanism
broken.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tda.agents.provider import ProviderError, ReplayProvider
from tda.cli import DEMO_SUBMISSION, declaration_from_manifest
from tda.contracts import Period, VerdictStatus
from tda.graph import RunContext, new_run_id, verify_directory
from tda.obs import RunLedger, build_ledger, write_run
from tda.obs.artifacts import RUN_LEDGER
from tda.obs.repro import strip_volatile, volatile_paths
from tda.outputs import MEMO_FILE, VERDICT_FILE, VerdictDocument, read_verdict, write_outputs
from tda.policy import load_policy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tda.policy import Policy

OK: Final = 0
DIFFERENT: Final = 1
COULD_NOT_RUN: Final = 2

# `run.json` after `RunLedger.volatile_fields()` is removed. Named in the output so nobody reads
# this row as the same kind of claim as the three beside it.
LEDGER_LABEL: Final = f"{RUN_LEDGER} (volatile paths stripped)"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """One run, and everything the two assertions need from it."""

    run_id: str
    directory: Path
    verdict: VerdictDocument
    digests: dict[str, str]


def run_once(
    submission: Path,
    root: Path,
    hotel: str,
    period: Period,
    policy: Policy,
    *,
    run_id: str,
) -> RunOutcome:
    """Verify one submission into `root/<run_id>/` and write every artefact `mizan run` writes.

    Replay is forced rather than read off policy. The target's whole point is an offline
    demonstration, and a machine with a key set would otherwise prove reproducibility by making
    live calls.
    """
    provider = ReplayProvider()
    context = RunContext.build(policy, period, provider)
    started = time.perf_counter()
    result = verify_directory(
        submission, hotel, period, policy, provider, run_id=run_id, context=context
    )
    elapsed = int((time.perf_counter() - started) * 1000)

    verdict = result.verdict
    write_run(
        root,
        build_ledger(
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
            duration_ms=elapsed,
        ),
        context.trace,
        result.nodes,
    )
    directory = root / run_id
    write_outputs(directory, verdict, result.state.claims, result.state.submission.workbook)
    return RunOutcome(
        run_id=run_id,
        directory=directory,
        verdict=read_verdict(directory / VERDICT_FILE),
        digests=digest_artefacts(directory),
    )


# ── the sanity floor under both assertions ───────────────────────────────────


def sanity_problems(outcome: RunOutcome, expected: VerdictStatus) -> list[str]:
    """Why this run cannot be compared, or nothing.

    Both checks exist because their absence is invisible: two runs that halted identically, or
    checked zero claims identically, are byte-identical and mean nothing.
    """
    problems = []
    if outcome.verdict.status is not expected:
        problems.append(
            f"{outcome.run_id} reached {outcome.verdict.status.value}, expected "
            f"{expected.value}. Two runs that failed the same way are identical and prove nothing"
        )
    if outcome.verdict.claims_checked == 0:
        problems.append(
            f"{outcome.run_id} checked 0 claims. There is nothing here to be reproducible about"
        )
    return problems


def excluded_report(
    first: Mapping[str, object], second: Mapping[str, object], excluded: frozenset[str]
) -> list[tuple[str, str]]:
    """What each excluded path actually did across the two dumps, path by path.

    Printed rather than merely counted. A reader of `make repro` should be able to see what was
    let through instead of trusting that the exclusion set is small, and a path recorded as
    `absent` is the honest answer for `review_records.decided_at` on an unreviewed run.
    """
    return sorted((path, _state_of(path, first, second)) for path in excluded)


# ── assertion 1: the verdicts ────────────────────────────────────────────────


def verdict_difference(
    first: Mapping[str, object], second: Mapping[str, object], excluded: frozenset[str]
) -> str | None:
    """`None` when the two verdicts agree, else a unified diff naming every differing path.

    "Not reproducible" without a path is not actionable, so the paths come first and the diff
    underneath them. The stripping is `tda.obs.repro`'s, not a second traversal written here: two
    implementations of "which fields may move" drift, and the drift is silent.
    """
    left = strip_volatile(first, excluded)
    right = strip_volatile(second, excluded)
    if left == right:
        return None
    paths = differing_paths(left, right)
    diff = difflib.unified_diff(
        _pretty(left), _pretty(right), fromfile="run 1", tofile="run 2", lineterm=""
    )
    return "\n".join([f"  differing path(s): {', '.join(paths)}", *(f"  {line}" for line in diff)])


def differing_paths(left: object, right: object, *, path: str = "") -> list[str]:
    """Every dotted path at which two JSON-shaped values disagree, list indices included.

    An index is noise in a description of a *field* and the opposite in a defect report: the
    difference that eventually shows up here will be one finding out of ninety, and `findings[7]`
    is the difference between a lead and a re-read of the whole file.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        found: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                found.append(child)
            else:
                found += differing_paths(left[key], right[key], path=child)
        return found
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path} (length {len(left)} vs {len(right)})"]
        found = []
        for index, (one, other) in enumerate(zip(left, right, strict=True)):
            found += differing_paths(one, other, path=f"{path}[{index}]")
        return found
    return [] if left == right else [path or "(the whole document)"]


# ── assertion 2: the artefacts ───────────────────────────────────────────────


def digest_artefacts(directory: Path) -> dict[str, str]:
    """A digest per artefact in one run directory: the verdict, the ledger, the memo, the workbook.

    Raw bytes for three of them. `run.json` is digested from its stripped payload instead, because
    three of its fields are wall clock by design; see the module docstring.
    """
    digests = {
        VERDICT_FILE: _digest((directory / VERDICT_FILE).read_bytes()),
        MEMO_FILE: _digest((directory / MEMO_FILE).read_bytes()),
        LEDGER_LABEL: _digest(_stable_ledger(directory / RUN_LEDGER)),
    }
    for workbook in sorted(directory.glob("annotated_*.xlsx")):
        digests[workbook.name] = _digest(workbook.read_bytes())
    return digests


def artefact_difference(first: RunOutcome, second: RunOutcome) -> str | None:
    """`None` when every artefact matched, else the ones that did not, named with both digests."""
    names = sorted(set(first.digests) | set(second.digests))
    rows = [
        f"  {name}: {first.digests.get(name, 'not written')} vs "
        f"{second.digests.get(name, 'not written')}"
        for name in names
        if first.digests.get(name) != second.digests.get(name)
    ]
    return None if not rows else "\n".join(["  artefacts that differ:", *rows])


# ── the target ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make repro", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--submission",
        type=Path,
        default=DEMO_SUBMISSION,
        help="The submission directory. Needs a manifest.json beside it, as the corpus has.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Where to keep the four artifact roots. Default: a temporary directory, removed "
        "afterwards, so the target leaves nothing behind.",
    )
    parser.add_argument(
        "--expect",
        default=VerdictStatus.PASS.value,
        choices=[status.value for status in VerdictStatus],
        help="The status both runs must reach. A run that did not get there is not compared.",
    )
    args = parser.parse_args(argv)

    if args.out is not None:
        return repro(args.submission, args.out, VerdictStatus(args.expect))
    with tempfile.TemporaryDirectory(prefix="mizan-repro-") as temporary:
        return repro(args.submission, Path(temporary), VerdictStatus(args.expect))


def repro(submission: Path, out: Path, expected: VerdictStatus) -> int:
    """Both assertions, in order, with the sanity gate between the runs and the diff."""
    declared = declaration_from_manifest(submission)
    if declared is None:
        _say(
            f"no manifest.json beside {submission}, so there is no declaration to verify against. "
            "Intake checks the files against a statement made somewhere other than the files."
        )
        return COULD_NOT_RUN
    hotel, period_text = declared
    period = Period.parse(period_text)
    policy = load_policy()
    excluded = volatile_paths(VerdictDocument)

    _say(f"make repro  submission {submission}")
    _say(f"  excluded from the verdict diff: {', '.join(sorted(excluded))}")
    _say("  every other field of every artefact is expected to be identical")
    _say("")

    try:
        first = run_once(submission, out / "run-1", hotel, period, policy, run_id=new_run_id())
        second = run_once(submission, out / "run-2", hotel, period, policy, run_id=new_run_id())
    except ProviderError as exc:
        # The expected offline failure, and its own message already says what to do about it.
        _say(f"the runs could not complete: {exc}")
        return COULD_NOT_RUN

    _say("1  different run ids, excluded paths stripped, verdicts identical")
    problems = sanity_problems(first, expected) + sanity_problems(second, expected)
    if first.run_id == second.run_id:
        problems.append(
            f"both runs minted {first.run_id}, so stripping the run id compared nothing"
        )
    if problems:
        for problem in problems:
            _say(f"   cannot compare: {problem}")
        return COULD_NOT_RUN

    for outcome in (first, second):
        _say(
            f"   {outcome.run_id}  {outcome.verdict.status.value}  "
            f"{outcome.verdict.claims_checked} claim(s) checked  "
            f"{outcome.verdict.summary.hotel_errors} hotel error(s)"
        )

    left = first.verdict.model_dump(mode="json")
    right = second.verdict.model_dump(mode="json")
    report = excluded_report(left, right, excluded)
    for path, state in report:
        _say(f"   excluded {path}: {state}")
    if not any(state == "differed" for _, state in report):
        _say(
            "   cannot compare: no excluded path carried a difference, so stripping proved nothing"
        )
        return COULD_NOT_RUN

    if (difference := verdict_difference(left, right, excluded)) is not None:
        _say("   NOT REPRODUCIBLE")
        _say(difference)
        return DIFFERENT
    _say("   the verdicts are identical")
    _say("")

    _say("2  same run id, artefacts byte-identical")
    fixed = new_run_id()
    third = run_once(submission, out / "run-3", hotel, period, policy, run_id=fixed)
    fourth = run_once(submission, out / "run-4", hotel, period, policy, run_id=fixed)
    if problems := sanity_problems(third, expected) + sanity_problems(fourth, expected):
        for problem in problems:
            _say(f"   cannot compare: {problem}")
        return COULD_NOT_RUN

    _say(f"   {fixed} written twice, into two roots")
    if (difference := artefact_difference(third, fourth)) is not None:
        _say("   NOT REPRODUCIBLE")
        _say(difference)
        return DIFFERENT
    for name, digest in sorted(third.digests.items()):
        _say(f"   {name}: {digest}")
    _say("")
    _say("  repro green")
    return OK


def _state_of(path: str, first: Mapping[str, object], second: Mapping[str, object]) -> str:
    """Whether one excluded path carried a difference, matched anyway, or was not in the payload."""
    one, other = _at(path, first), _at(path, second)
    if one is _MISSING and other is _MISSING:
        return "absent"
    return "differed" if one != other else "identical (excluded anyway)"


class _Missing:
    """A sentinel for "this path is not in the payload", distinct from a `None` that is."""


_MISSING: Final = _Missing()


def _at(path: str, payload: object) -> object:
    """The value at one dotted path, or `_MISSING`. A list yields the tuple of its rows' values.

    An empty list is `_MISSING` rather than `()`. Without that, `review_records.decided_at` on two
    unreviewed runs reports "identical" and reads as though the exclusion had been examined and
    found unnecessary, when in fact no verdict carried the field at all.
    """
    head, _, rest = path.partition(".")
    if isinstance(payload, list):
        return tuple(_at(path, item) for item in payload) if payload else _MISSING
    if not isinstance(payload, dict) or head not in payload:
        return _MISSING
    return payload[head] if not rest else _at(rest, payload[head])


def _stable_ledger(path: Path) -> bytes:
    """`run.json` re-serialised with its volatile paths removed. See the module docstring."""
    ledger = RunLedger.model_validate_json(path.read_text(encoding="utf-8"))
    stripped = strip_volatile(ledger.model_dump(mode="json"), RunLedger.volatile_fields())
    return json.dumps(stripped, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _pretty(payload: object) -> list[str]:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).splitlines()


def _say(line: str) -> None:
    # `sys.stdout.write` rather than `print`: ruff's T20 bans `print` outside the entry points
    # `pyproject.toml` names one by one, and this module is not one of them.
    sys.stdout.write(f"{line}\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
