"""`make record` — refresh cassettes against the live API.

Deliberately a module with a `main()` rather than a shell one-liner in the Makefile, because
recording is the one operation here that costs money and touches the network. It should be
readable, and it should say what it is about to do.

## What it records, and why that set

**Every committed eval case, once.** Not an arbitrary sample, and not a fixture written for the
recorder: the eval cases *are* the questions this system asks its agents, so the cassettes are
recordings of exactly the calls the test suite will replay. `tda.agents.cases.run_case` is shared
between the two, so a case cannot be recorded against one question and replayed against another —
which would surface as a total cassette miss, and be diagnosed as a missing recording rather than
as the bug it is.

## What it does not do

**It does not score.** A recording run says what came back and writes it down; whether the answer
is any good is `tests/eval/agents/`'s question, asked afterwards against the committed file. Mixing
the two would let a bad answer be silently re-recorded until it passed, which is the failure mode
that makes a cassette suite worthless.

**It does not skip a failure.** A case that raises is reported and the exit code is non-zero, even
when other cases recorded cleanly. A partial recording that exits 0 is how a team comes to believe
its cassettes are current when a third of them are missing.

## Reviewing what it writes

A cassette diff is a code diff. It means a prompt, an output schema or a rendered message moved,
and the PR body has to say which and why the new response is better. Each file stores
`request_canonical` beside the response so a reviewer can see *what was asked*, not just that a
hash changed. See `tests/cassettes/README.md`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tda.agents.cases import load_cases, registry_for, run_case
from tda.agents.provider.anthropic_client import AnthropicProvider, RecordingProvider
from tda.agents.provider.dotenv import DEFAULT_ENV_PATH, load_dotenv
from tda.agents.provider.replay import DEFAULT_CASSETTE_DIR
from tda.agents.runtime import AgentRunner
from tda.obs import PRICING_VERSION, TraceLog, UsageLedger
from tda.policy import load_policy

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CASES_ROOT = REPO_ROOT / "tests" / "eval" / "agents" / "cases"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record cassettes against the live API.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_CASSETTE_DIR)
    parser.add_argument(
        "--agent", default=None, help="Record one agent's cases rather than all of them."
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

    # `.env` is read here and nowhere else. Every other target is offline in replay and needs no
    # credential; a loader that fired on import would reach for a secret in processes that have no
    # business holding one. The real environment wins over the file - see `dotenv.load_dotenv`.
    if not args.no_dotenv:
        loaded = load_dotenv(args.env_file)
        if loaded:
            # Names only. The one thing this path must never do is put a credential in a log.
            print(f"loaded {', '.join(loaded)} from {args.env_file}", file=sys.stderr)

    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ANTHROPIC_API_KEY is not set. Recording makes live calls; every other target "
            "runs in replay and needs no key.\n"
            f"  Set it in the shell, or put it in {args.env_file} - which is gitignored, is read "
            "by this target, and\n"
            "  survives the shell going away. `cp .env.example .env` and fill in the one line.",
            file=sys.stderr,
        )
        return 2

    if not args.cases.is_dir():
        print(f"no eval cases at {args.cases}", file=sys.stderr)
        return 2

    cases = load_cases(args.cases, args.agent)
    if not cases:
        print(
            f"nothing to record: no eval cases under {args.cases}.\n"
            "  Cassettes are recordings of the calls the eval suite replays, so there is nothing\n"
            "  to record until those cases exist. Hand-authoring cassettes would fabricate the\n"
            "  evidence this layer exists to provide.",
            file=sys.stderr,
        )
        return 2

    policy = load_policy()
    print(
        f"recording {len(cases)} case(s) against {policy.model.model_id} "
        f"(policy {policy.version}) into {args.out}",
        file=sys.stderr,
    )

    if args.dry_run:
        for case in cases:
            print(f"  would record {case.ref}", file=sys.stderr)
        return 0

    usage = UsageLedger()
    failures: list[str] = []
    written: list[str] = []

    for case in cases:
        provider = RecordingProvider(AnthropicProvider(), args.out)
        runner = AgentRunner(
            provider,
            policy=policy,
            registry=registry_for(case, policy),
            trace=TraceLog(),
            usage=usage,
        )
        try:
            run_case(case, runner, policy)
        except Exception as exc:
            # Reported and counted, never swallowed. A case that fails here has told us something
            # real - a fabrication check fired, or the contract did not validate - and the run
            # must not exit 0 with a third of its cassettes missing.
            failures.append(f"  {case.ref}: {type(exc).__name__}: {exc}")
            print(f"  FAIL {case.ref}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        written.extend(provider.written)
        print(f"  ok   {case.ref}", file=sys.stderr)

    print(
        f"\n{len(written)} cassette(s) written, {len(failures)} case(s) failed.\n"
        f"  cost: ${usage.total_cost_usd():.4f} over {usage.total_calls()} call(s), "
        f"at the rates in tda.obs.usage (version {PRICING_VERSION})",
        file=sys.stderr,
    )
    print(
        "\nReview the diff before committing: a cassette diff is a code diff. It means a prompt,\n"
        "an output schema or a rendered message moved, and the PR has to say which and why the\n"
        "new response is better. See tests/cassettes/README.md.",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
