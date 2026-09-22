"""`make record`'s narrative half: record narrate()+grade() cassettes for real fixture findings.

`python -m tda.agents.provider` (`tda.agents.provider.record`) records every hand-written eval case
once, live. It cannot record these: a fixture's findings are not declared in a case file, they come
out of running the pipeline, in replay, against the mapping cassettes the eval harness already recorded, and
reading its published `Verdict` back. This module does exactly that for the four fixtures that
carry a finding (F2, F3, F4, F6), then runs each one through `narrate()`/`grade()` against a live
`RecordingProvider`, the same mechanism `tda.agents.provider.record` uses for its cases.

## The held-back bad narrative

One more cassette, alongside the real ones: a deliberately bad, hand-written sentence, an
accusation on F3's definitional finding, the PRD's own motivating example, graded once and never
used as a fixture's real narrative. `tests/eval/pipeline/test_narrative_grading.py` replays it to
prove the wiring, not just the critic, turns a bad narrative into a failed fixture.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tda.agents.contracts.narrative import FindingNarrative
from tda.agents.critic import grade
from tda.agents.provider.anthropic_client import AnthropicProvider, RecordingProvider
from tda.agents.provider.dotenv import DEFAULT_ENV_PATH, load_dotenv
from tda.agents.provider.replay import DEFAULT_CASSETTE_DIR
from tda.agents.runtime import AgentRunner
from tda.agents.tools import ToolRegistry
from tda.eval.narrative import grade_finding
from tda.eval.run import DEFAULT_FIXTURES_ROOT, FixtureError, run_fixture
from tda.obs import RateCard, TraceLog, UsageLedger, spend_line
from tda.policy import Policy, load_policy

FIXTURES_WITH_FINDINGS = ("F2", "F3", "F4", "F6")

# F3's is the only definitional finding across the six fixtures, and it is the PRD's own
# motivating example: a correct finding, delivered as a false accusation of a policy disagreement.
BAD_NARRATIVE_FIXTURE = "F3"
BAD_NARRATIVE_SENTENCE = (
    "The hotel appears to have under-reported the room count used for the occupancy calculation "
    "and should correct its figures."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record narrative/critic cassettes for real fixture findings."
    )
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_CASSETTE_DIR)
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated fixture ids to record, e.g. F4,F6 (default: all of them). "
        "Recording an unaffected fixture makes a redundant live call for a request whose cassette "
        "already exists, since `RecordingProvider` never checks first.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List what would be recorded, call nothing."
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Do not read .env. The shell environment is then the only source of the key.",
    )
    args = parser.parse_args(argv)

    if not args.no_dotenv:
        loaded = load_dotenv(args.env_file)
        if loaded:
            print(f"loaded {', '.join(loaded)} from {args.env_file}", file=sys.stderr)

    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. Recording makes live calls; every other target runs "
            "in replay and needs no key.\n"
            f"  Set it in the shell, or put it in {args.env_file}.",
            file=sys.stderr,
        )
        return 2

    if not args.fixtures.is_dir():
        print(f"no fixture tree at {args.fixtures}. Run `make fixtures` first.", file=sys.stderr)
        return 2

    targets = tuple(args.only.split(",")) if args.only else FIXTURES_WITH_FINDINGS
    unknown = set(targets) - set(FIXTURES_WITH_FINDINGS)
    if unknown:
        print(
            f"--only names {sorted(unknown)}, which carry no finding to grade. "
            f"Choose from {FIXTURES_WITH_FINDINGS}.",
            file=sys.stderr,
        )
        return 2

    policy = load_policy()
    usage = UsageLedger()
    written: list[str] = []
    failures: list[str] = []

    for fixture_id in targets:
        directory = args.fixtures / fixture_id
        try:
            verdict = run_fixture(directory, policy)
        except FixtureError as exc:
            failures.append(f"{fixture_id}: {exc}")
            print(f"  FAIL {fixture_id}: {exc}", file=sys.stderr)
            continue

        findings = (*verdict.findings, *verdict.definitional_items)
        provider = RecordingProvider(AnthropicProvider(), args.out)
        for finding in findings:
            if args.dry_run:
                print(f"  would record {fixture_id}/{finding.finding_id}", file=sys.stderr)
                continue
            try:
                result = grade_finding(finding, provider, policy, usage=usage)
            except Exception as exc:
                failures.append(f"{fixture_id}/{finding.finding_id}: {type(exc).__name__}: {exc}")
                print(
                    f"  FAIL {fixture_id}/{finding.finding_id}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue
            print(
                f"  ok   {fixture_id}/{finding.finding_id}: passed={result.passed}",
                file=sys.stderr,
            )
        written.extend(provider.written)

    if BAD_NARRATIVE_FIXTURE in targets:
        if not args.dry_run:
            written.extend(_record_held_back_bad_narrative(args.fixtures, args.out, policy, usage))
        else:
            print(
                f"  would record the held-back bad narrative on {BAD_NARRATIVE_FIXTURE}",
                file=sys.stderr,
            )

    print(
        f"\n{len(written)} cassette(s) written, {len(failures)} failure(s).\n"
        f"  {usage.total_calls()} call(s). {spend_line(usage, RateCard.from_env())}",
        file=sys.stderr,
    )
    return 1 if failures else 0


def _record_held_back_bad_narrative(
    fixtures: Path, out: Path, policy: Policy, usage: UsageLedger
) -> list[str]:
    """One critic cassette against a sentence no fixture will ever really produce.

    Grades a hand-written accusation against F3's real definitional finding directly, bypassing
    `narrate()`: the bad sentence is the point, not what a model would actually write.
    """
    directory = fixtures / BAD_NARRATIVE_FIXTURE
    verdict = run_fixture(directory, policy)
    finding = verdict.definitional_items[0]
    bad = FindingNarrative(
        finding_id=finding.finding_id,
        sentence=BAD_NARRATIVE_SENTENCE,
        cites_permutation=None,
        is_definitional=True,
    )
    provider = RecordingProvider(AnthropicProvider(), out)
    runner = AgentRunner(
        provider, policy=policy, registry=ToolRegistry(), trace=TraceLog(), usage=usage
    )
    result = grade(finding, bad, runner, policy)
    print(
        f"  ok   held-back bad narrative on {BAD_NARRATIVE_FIXTURE}/{finding.finding_id}: "
        f"passed={result.output.passed}",
        file=sys.stderr,
    )
    return list(provider.written)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
