"""`make repro`: what the target must catch, and the two ways a repro diff passes for free.

A reproducibility check is the easiest thing in a codebase to make green and useless, and it fails
in two directions at once.

**Upwards.** Widen the exclusion set and every diff passes. So the tests below feed the diff two
verdicts that differ in a field nobody excluded and require it to name that field, and separately
require a difference confined to an excluded path to be ignored. Both halves are needed: a function
that always returns "identical" satisfies the second on its own.

**Downwards.** Two runs that checked nothing are byte-identical. So is a pair that halted in the
same place. The sanity gate is the floor under the diff, and the test for it asserts both facts
together: that the gate rejects such a run, and that the diff would otherwise have passed it.

The end-to-end double run is marked `eval` because it is three real pipeline runs. Everything else
here works on constructed verdicts and hand-written artifact directories, which is what lets those
cases assert things a real run cannot be made to produce on demand.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tda.cli import DEMO_SUBMISSION, declaration_from_manifest
from tda.contracts import ExtractionSummary, Period, Verdict, VerdictStatus
from tda.eval.repro import (
    LEDGER_LABEL,
    OK,
    RunOutcome,
    artefact_difference,
    differing_paths,
    digest_artefacts,
    excluded_report,
    repro,
    run_once,
    sanity_problems,
    verdict_difference,
)
from tda.graph import new_run_id
from tda.obs import AgentUsage, NodeTiming, RunLedger
from tda.obs.artifacts import RUN_LEDGER
from tda.obs.repro import volatile_paths
from tda.outputs import MEMO_FILE, VERDICT_FILE, VerdictDocument
from tda.policy import load_policy

if TYPE_CHECKING:
    from pathlib import Path

EXCLUDED = volatile_paths(VerdictDocument)
WORKBOOK = "annotated_claims_2026-Q1.xlsx"


def verdict(**overrides: object) -> VerdictDocument:
    """A PASS verdict over 94 claims, the shape the demo corpus produces."""
    base: dict[str, object] = {
        "run_id": "run-000000000001",
        "status": VerdictStatus.PASS,
        "hotel_id": "MZN-DXB-001",
        "period": "2026-Q1",
        "policy_version": "1.3.0",
        "metric_library_version": "1.0.0",
        "model_id": "claude-sonnet-5",
        "provider_mode": "replay",
        "prompt_versions": {"mapping": "v1"},
        "extraction": ExtractionSummary(
            files=("pms_2026-01.pdf",),
            records_extracted=1200,
            pages_read=30,
            printed_total_matched=True,
            duplicate_ids=0,
        ),
        "claims_checked": 94,
    }
    return VerdictDocument.of(Verdict(**(base | overrides)))  # type: ignore[arg-type]


def dump(document: VerdictDocument) -> dict[str, object]:
    return document.model_dump(mode="json")


def outcome(document: VerdictDocument, digests: dict[str, str] | None = None) -> RunOutcome:
    return RunOutcome(
        run_id=document.run_id,
        directory=DEMO_SUBMISSION,
        verdict=document,
        digests=digests or {},
    )


# ── assertion 1: the diff has to bite, and only where it should ──────────────


def test_the_diff_names_the_field_two_verdicts_disagree_on() -> None:
    """A difference outside the exclusion set is reported, with the path in the message.

    "Not reproducible" on its own sends somebody to read two hundred lines of JSON. The path is
    what makes the output a lead.
    """
    difference = verdict_difference(dump(verdict()), dump(verdict(claims_checked=93)), EXCLUDED)

    assert difference is not None
    assert "claims_checked" in difference
    assert "94" in difference and "93" in difference


def test_a_difference_confined_to_an_excluded_path_is_ignored() -> None:
    """The other half. A function that reported every pair as different would pass the test above.

    The two verdicts here really do differ, which is the assertion the comparison rests on.
    """
    first, second = verdict(), verdict(run_id="run-ffffffffffff")

    assert dump(first) != dump(second)
    assert verdict_difference(dump(first), dump(second), EXCLUDED) is None


def test_the_reported_paths_reach_into_the_rows_of_a_list() -> None:
    """One finding out of ninety, named by index.

    `tda.obs.repro` exists because the first version of that walker stopped at the top level. The
    same mistake made here would report "the findings differ" for a defect in a single row, which
    is a re-read of the whole file rather than a lead.
    """
    left = {
        "findings": [{"id": "F-0001", "severity": "material"}, {"id": "F-0002", "sev": "minor"}]
    }
    right = {
        "findings": [{"id": "F-0001", "severity": "material"}, {"id": "F-0002", "sev": "blocking"}]
    }

    assert differing_paths(left, right) == ["findings[1].sev"]
    assert differing_paths({"claims_checked": 94}, {}) == ["claims_checked"]
    assert differing_paths({"rows": [1]}, {"rows": [1, 2]}) == ["rows (length 1 vs 2)"]


def test_the_excluded_paths_are_reported_one_by_one_including_the_nested_one() -> None:
    """What was let through, stated rather than implied.

    `review_records.decided_at` is the nested path, and reporting it needs the same traversal the
    stripping does. A report that only looked at top-level keys would call it absent on a verdict
    that carried two different decision timestamps.
    """
    first, second = verdict(), verdict(run_id="run-ffffffffffff")
    assert dict(excluded_report(dump(first), dump(second), EXCLUDED)) == {
        "run_id": "differed",
        "review_records.decided_at": "absent",
    }

    reviewed = {"run_id": "run-a", "review_records": [{"decided_at": "2026-01-01T00:00:00Z"}]}
    again = {"run_id": "run-a", "review_records": [{"decided_at": "2026-01-02T00:00:00Z"}]}
    assert dict(excluded_report(reviewed, again, EXCLUDED)) == {
        "run_id": "identical (excluded anyway)",
        "review_records.decided_at": "differed",
    }


# ── the floor under the diff ─────────────────────────────────────────────────


def test_a_run_that_checked_no_claims_fails_the_sanity_gate() -> None:
    """The single most important case, and the one a diff cannot see.

    Two runs that checked nothing agree perfectly. The second assertion here is the reason the
    gate is not optional: without it the target reports green for the failure it exists to catch.
    """
    nothing = verdict(claims_checked=0, status=VerdictStatus.HALTED)
    also_nothing = verdict(claims_checked=0, status=VerdictStatus.HALTED, run_id="run-ffffffffffff")
    assert verdict_difference(dump(nothing), dump(also_nothing), EXCLUDED) is None

    problems = sanity_problems(outcome(nothing), VerdictStatus.HALTED)
    assert len(problems) == 1
    assert "0 claims" in problems[0]


def test_a_run_that_reached_the_wrong_status_is_not_compared() -> None:
    """Two runs that crashed the same way are reproducible and worthless."""
    assert sanity_problems(outcome(verdict()), VerdictStatus.PASS) == []

    problems = sanity_problems(outcome(verdict(status=VerdictStatus.HALTED)), VerdictStatus.PASS)
    assert len(problems) == 1
    assert "HALTED" in problems[0] and "PASS" in problems[0]


# ── assertion 2: the artefacts ───────────────────────────────────────────────


def ledger(duration_ms: int, **overrides: object) -> RunLedger:
    """A ledger whose three wall clocks are all set from one argument, so a test can move them."""
    base: dict[str, object] = {
        "run_id": "run-000000000001",
        "status": "PASS",
        "hotel_id": "MZN-DXB-001",
        "period": "2026-Q1",
        "policy_version": "1.3.0",
        "metric_library_version": "1.0.0",
        "model_id": "claude-sonnet-5",
        "provider_mode": "replay",
        "nodes": (NodeTiming(node="intake", outcome="ok", duration_ms=duration_ms // 40),),
        "usage": (
            AgentUsage(
                agent="mapping",
                calls=1,
                input_tokens=5258,
                output_tokens=1001,
                cache_read_tokens=0,
                duration_ms=duration_ms // 3,
            ),
        ),
        "duration_ms": duration_ms,
    }
    return RunLedger(**(base | overrides))  # type: ignore[arg-type]


def written_run(
    directory: Path,
    *,
    duration_ms: int = 3000,
    memo: bytes = b"PK\x03\x04 the memo",
    workbook: bytes = b"PK\x03\x04 the annotated workbook",
    **ledger_overrides: object,
) -> RunOutcome:
    """A run directory carrying the four artefacts, written by hand so a test can perturb one."""
    directory.mkdir(parents=True)
    (directory / VERDICT_FILE).write_bytes(json.dumps(dump(verdict()), indent=2).encode())
    (directory / MEMO_FILE).write_bytes(memo)
    (directory / WORKBOOK).write_bytes(workbook)
    (directory / RUN_LEDGER).write_text(
        ledger(duration_ms, **ledger_overrides).model_dump_json(), encoding="utf-8"
    )
    return RunOutcome(
        run_id="run-000000000001",
        directory=directory,
        verdict=verdict(),
        digests=digest_artefacts(directory),
    )


def test_all_four_artefacts_are_digested(tmp_path: Path) -> None:
    """Named, because an artefact nobody hashed is an artefact nobody checked."""
    assert set(written_run(tmp_path / "run").digests) == {
        VERDICT_FILE,
        MEMO_FILE,
        WORKBOOK,
        LEDGER_LABEL,
    }


def test_a_byte_difference_in_a_rendered_document_is_caught(tmp_path: Path) -> None:
    """One byte in the memo, one in the workbook. Both named.

    The workbook is the case that matters most: `tda.outputs.ooxml.make_reproducible` is the only
    reason a generated `.xlsx` is stable at all, and assertion 2 is the only thing in this target
    that touches it.
    """
    one = written_run(tmp_path / "a")
    assert artefact_difference(one, written_run(tmp_path / "b")) is None

    memo_moved = written_run(tmp_path / "c", memo=b"PK\x03\x04 the memo!")
    difference = artefact_difference(one, memo_moved)
    assert difference is not None
    assert MEMO_FILE in difference and WORKBOOK not in difference

    workbook_moved = written_run(tmp_path / "d", workbook=b"PK\x03\x04 a different workbook")
    difference = artefact_difference(one, workbook_moved)
    assert difference is not None
    assert WORKBOOK in difference


def test_the_ledgers_wall_clock_does_not_defeat_the_digest_but_its_content_does(
    tmp_path: Path,
) -> None:
    """Why `run.json` is digested from its stripped payload rather than its bytes.

    Three of its fields are wall clock on every row, so raw bytes can never match and a target
    that digested them would report a defect on every run. Stripping them is only defensible if
    the digest still catches everything else, which is the second half of this test.
    """
    quick = written_run(tmp_path / "quick", duration_ms=3000)
    slow = written_run(tmp_path / "slow", duration_ms=4100)
    assert quick.digests[LEDGER_LABEL] == slow.digests[LEDGER_LABEL]
    assert artefact_difference(quick, slow) is None

    other_rules = written_run(tmp_path / "other", duration_ms=3000, policy_version="1.4.0")
    difference = artefact_difference(quick, other_rules)
    assert difference is not None
    assert LEDGER_LABEL in difference


# ── the real thing ───────────────────────────────────────────────────────────


@pytest.mark.eval
def test_three_real_runs_of_the_demo_submission_prove_both_assertions(tmp_path: Path) -> None:
    """Both assertions against the pipeline, and the reason the second one is not redundant.

    Runs 2 and 3 share a `run_id` and land in separate roots. The memo and the workbook digests are
    asserted to *differ* between runs 1 and 2, which is the whole argument for assertion 2: those
    two files embed the run id, so assertion 1 can never say anything about the bytes of either.
    """
    declared = declaration_from_manifest(DEMO_SUBMISSION)
    assert declared is not None
    hotel, period_text = declared
    period = Period.parse(period_text)
    policy = load_policy()

    fixed = new_run_id()
    first = run_once(DEMO_SUBMISSION, tmp_path / "a", hotel, period, policy, run_id=new_run_id())
    second = run_once(DEMO_SUBMISSION, tmp_path / "b", hotel, period, policy, run_id=fixed)
    third = run_once(DEMO_SUBMISSION, tmp_path / "c", hotel, period, policy, run_id=fixed)

    for run in (first, second, third):
        assert sanity_problems(run, VerdictStatus.PASS) == []
        assert run.verdict.claims_checked > 0

    assert first.run_id != second.run_id
    assert dump(first.verdict) != dump(second.verdict)
    assert verdict_difference(dump(first.verdict), dump(second.verdict), EXCLUDED) is None

    assert artefact_difference(second, third) is None
    for artefact in (MEMO_FILE, WORKBOOK, VERDICT_FILE):
        assert first.digests[artefact] != second.digests[artefact], artefact


@pytest.mark.eval
def test_the_target_exits_zero_on_the_demo_corpus(tmp_path: Path) -> None:
    """The deliverable: `python -m tda.eval.repro` says the demo submission is reproducible."""
    assert repro(DEMO_SUBMISSION, tmp_path, VerdictStatus.PASS) == OK
