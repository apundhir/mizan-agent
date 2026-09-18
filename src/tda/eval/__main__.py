"""`python -m tda.eval` — score every fixture, write the scorecard, and say what was not measured.

This is what `make eval` runs.

## The exit codes, and why an incomplete scorecard is not a success

    0  every fixture matched its derived expectation
    1  a fixture ran and did not match
    2  the fixtures could not be scored at all

The third case covers an absent fixture tree, an unreadable or foreign expectation, and a cassette
miss. A miss is deliberately not a `1`: the run produced no verdict, so "did it match?" was never
asked, and reporting an unasked question as an answer is the failure this whole package is shaped
around. `2` rather than `0` for the same reason. An eval that exits clean when it measured nothing
is worse than no eval, because a release gate reads the code and not the report.

## Why there is no `repro` subcommand here

`make repro` runs the pipeline twice and diffs the verdict, which is a different question with a
different answer shape. It gets its own entry point rather than a flag on this one, so a CI job
that wants one cannot accidentally satisfy itself with the other.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from tda.eval.expectation import ExpectationError
from tda.eval.run import DEFAULT_FIXTURES_ROOT, DEFAULT_OUT_ROOT, FixtureError, score_fixtures
from tda.eval.scoring import is_complete, render_report, write_scorecard

REPORT_FILE: Final = "report.md"
SCORECARD_FILE: Final = "scorecard.json"

OK: Final = 0
MISMATCH: Final = 1
COULD_NOT_RUN: Final = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tda.eval", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES_ROOT,
        help="The fixture tree to score. Built by `make datagen`; never built here.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_ROOT,
        help=f"Where {REPORT_FILE} and {SCORECARD_FILE} are written.",
    )
    args = parser.parse_args(argv)

    try:
        results = score_fixtures(args.fixtures, out_root=args.out)
    except (FixtureError, ExpectationError) as exc:
        sys.stderr.write(f"the fixtures could not be scored: {exc}\n")
        return COULD_NOT_RUN

    # Written before the exit code is decided, so an incomplete sweep still leaves the report that
    # explains why it was incomplete. The exit code tells a shell; the file tells a person.
    report = render_report(results).rstrip("\n") + "\n"
    sys.stdout.write(report)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / REPORT_FILE).write_text(report, encoding="utf-8")
    write_scorecard(args.out / SCORECARD_FILE, results)
    sys.stdout.write(f"\nwrote {args.out / REPORT_FILE} and {args.out / SCORECARD_FILE}\n")

    if not is_complete(results):
        sys.stderr.write(
            "the scorecard is incomplete: at least one fixture produced no verdict. Unmeasured is "
            "not a pass, so this exits 2 rather than reporting on the fixtures that did run.\n"
        )
        return COULD_NOT_RUN
    return OK if all(result.passed for result in results) else MISMATCH


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
