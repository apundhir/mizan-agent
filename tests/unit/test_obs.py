"""Observability: the ledger, the artifacts, what gets stripped on the way out, and the tree.

Four claims, and the third is the one that matters.

**A ledger identifies the bytes it ran on.** Without a digest per input file, a verdict and a file
set can drift apart silently — somebody re-exports the workbook, the numbers change, and nothing in
the record says the two verdicts were about different documents.

**Artifacts are byte-stable except where they say they are not.** `RunLedger.volatile_fields()` is
read off the field descriptions, so PRD-94's `make repro` cannot be lied to by a list that drifted
out of step with the model.

**Nothing personal reaches an artifact.** PRD-90 asks for a scan of the trace for name-shaped
content from the corpus, and the corpus has no names in it — `tools/datagen/ledger.py` emits a
salted `guest_ref` and says why. A scan for corpus names would therefore pass for the wrong reason.
So the test below *demonstrates the real exposure first* — `tda.excel.tools.digest` copies every
label cell of the submitted workbook into the mapping prompt verbatim, contact details included —
and then asserts it is redacted before it reaches disk, and that the counts are recorded.

**Free model prose never reaches a log line.** The viewer prints a contract's short scalars and
counts the rest. A `FindingNarrative.sentence` is prose about a named property's numbers, and a
viewer that pretty-printed it would put it on a terminal, then in a screenshot, then in a ticket.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from openpyxl import Workbook
from pydantic import BaseModel, Field

from tda.excel.tools import digest
from tda.obs import (
    InputFile,
    NodeLog,
    NodeOutcome,
    NodeRecord,
    Phase,
    RunLedger,
    TraceCall,
    TraceLog,
    TraceRecord,
    UsageLedger,
    build_ledger,
    cost_summary,
    file_digest,
    latest_run,
    read_run,
    redact,
    scan,
    write_run,
)
from tda.obs.artifacts import AGENT_TRACE, NODE_LOG, RUN_LEDGER
from tda.obs.ledger import NodeTiming, timings_from
from tda.obs.repro import REPRO_EXCLUDED, strip_volatile, volatile_paths
from tda.obs.viewer import VALUE_LIMIT, render_tree
from tda.outputs.verdict import VerdictDocument

if TYPE_CHECKING:
    from collections.abc import Iterable

CORPUS = Path(__file__).resolve().parents[2] / "corpus" / "demo"
SUBMISSION = CORPUS / "submission"

# A header cell a hotel might plausibly type, and the thing this module exists for. Assembled from
# parts rather than written out, so `tools/guard/secret_guard.py` and a future PII sweep of the
# repository do not both have to carry an exception for this file.
CONTACT = "Prepared by " + "Ms" + ". Jane Doe, jane.doe" + "@" + "hotel.ae, " + "+971 50 123 4567"


def trace_record(**overrides: object) -> TraceRecord:
    """One valid trace record, with the fields a test does not care about already filled."""
    fields: dict[str, object] = {
        "agent": "mapping",
        "prompt_version": "v1",
        "model_id": "claude-sonnet-5",
        "effort": "low",
        "provider_mode": "stub",
        "cassette_key": "f0a0e6f0abcdef0123456789",
        "output_contract": "WorkbookMapping",
        "output_json": '{"blocks": [], "unmapped": []}',
    }
    fields.update(overrides)
    return TraceRecord.model_validate(fields)


def node_log(*nodes: tuple[str, NodeOutcome | None], duration_ms: int = 7) -> NodeLog:
    """A node log from `(name, outcome)` pairs. `None` means entered and never left."""
    log = NodeLog()
    for name, outcome in nodes:
        log.append(NodeRecord(node=name, phase=Phase.ENTER))
        if outcome is None:
            continue
        detail = None if outcome is NodeOutcome.OK else f"{name} said why"
        log.append(
            NodeRecord(
                node=name,
                phase=Phase.EXIT,
                outcome=outcome,
                duration_ms=duration_ms,
                detail=detail,
            )
        )
    return log


def usage_with(*, duration_ms: int = 0, calls: int = 1) -> UsageLedger:
    """A usage ledger with `calls` recorded against the mapping agent."""
    usage = UsageLedger()
    for _ in range(calls):
        usage.record("mapping", input_tokens=1000, output_tokens=100, duration_ms=duration_ms)
    return usage


def ledger_for(nodes: NodeLog, inputs: Iterable[Path] = (), **overrides: object) -> RunLedger:
    fields: dict[str, object] = {
        "run_id": "run-0123456789ab",
        "status": "PASS",
        "rejection_reason": None,
        "hotel_id": "MZN-DXB-001",
        "period": "2026-Q1",
        "policy_version": "1.3.0",
        "metric_library_version": "1.0.0",
        "model_id": "claude-sonnet-5",
        "provider_mode": "stub",
        "prompt_versions": {"mapping": "v1"},
        "inputs": inputs,
        "nodes": nodes,
        "usage": UsageLedger(),
        "duration_ms": 1234,
    }
    fields.update(overrides)
    return build_ledger(**fields)  # type: ignore[arg-type]


def ledger_with(timings: tuple[NodeTiming, ...]) -> RunLedger:
    """A ledger whose node rows are given directly.

    The viewer tests care about how calls are attributed to nodes, not about how a node log
    flattens into rows - `timings_from` has its own tests above. Building the rows here keeps
    the two questions apart.
    """
    return ledger_for(NodeLog()).model_copy(update={"nodes": timings})


# ── the ledger identifies the bytes ──────────────────────────────────────────


def test_the_digest_convention_matches_the_one_the_corpus_manifest_already_uses() -> None:
    """Two conventions for the same hash is a small mystery for whoever has to verify a file later.

    `tools/datagen/reproducible.py` wrote the manifest; this reads it back with a different
    function. If the two ever disagree, a reader comparing a ledger against a manifest would have
    to work out which one was lying.
    """
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    recorded: dict[str, str] = manifest["files"]
    assert recorded, "the manifest lists no files; this test would pass vacuously"

    for relative, expected in recorded.items():
        assert file_digest(CORPUS / relative) == expected, relative


def test_an_input_file_is_recorded_by_name_and_never_by_path() -> None:
    """Where a file sat on the machine that ran the verification is not evidence about anything,
    and a ledger full of `/home/someone/tmp/...` leaks the environment into the record."""
    workbook = SUBMISSION / "claims_2026-Q1.xlsx"
    recorded = InputFile.of(workbook)

    assert recorded.name == "claims_2026-Q1.xlsx"
    assert str(workbook.parent) not in recorded.model_dump_json()
    assert recorded.bytes == workbook.stat().st_size


def test_the_ledger_records_every_submitted_file_sorted_by_name() -> None:
    """Sorted so two runs of the same submission produce the same bytes, and `make repro` compares
    the pipeline rather than directory iteration order."""
    files = sorted(SUBMISSION.iterdir())
    ledger = ledger_for(node_log(("intake", NodeOutcome.OK)), inputs=reversed(files))

    assert [f.name for f in ledger.inputs] == sorted(f.name for f in files)
    assert all(f.sha256.startswith("sha256:") for f in ledger.inputs)


def test_a_file_that_is_not_there_is_left_out_rather_than_hashed_as_empty() -> None:
    """An input digest is a claim about bytes that existed. A zero-length hash for a missing file
    would be a claim about bytes nobody ever had."""
    present = SUBMISSION / "claims_2026-Q1.xlsx"
    ledger = ledger_for(
        node_log(("intake", NodeOutcome.OK)), inputs=[present, SUBMISSION / "not-here.csv"]
    )

    assert [f.name for f in ledger.inputs] == ["claims_2026-Q1.xlsx"]


# ── what is allowed to differ between two runs, stated on the field ──────────


def test_a_newly_marked_field_is_found_without_anyone_updating_a_list() -> None:
    """The property the whole mechanism exists for, tested on models the implementation cannot know.

    A hardcoded `frozenset({"run_id", "duration_ms"})` satisfies every assertion about `RunLedger`
    and is still wrong the moment somebody marks a new field — which is exactly the drift the
    description-derived approach exists to prevent. So the derivation is exercised against a model
    written here and nowhere else.
    """

    class Row(BaseModel):
        stamp: int = Field(default=0, description=f"Wall clock. {REPRO_EXCLUDED}")
        stable: int = 0

    class Outer(BaseModel):
        rows: tuple[Row, ...] = ()
        only_row: Row | None = None
        identifier: str = Field(default="", description=f"Unique. {REPRO_EXCLUDED}")
        settled: str = ""

    assert volatile_paths(Outer) == frozenset({"identifier", "rows.stamp", "only_row.stamp"})


def test_the_ledger_asks_the_walker_rather_than_answering_from_memory() -> None:
    """`RunLedger.volatile_fields()` must *derive* its answer, not carry one.

    The two are indistinguishable today — a hardcoded set of the four current paths passes every
    other assertion in this file — so the test adds a field the hardcode cannot know about. That is
    the whole failure mode: the list and the model drift apart, and the drift is silent.
    """

    class LedgerWithAnExtraClock(RunLedger):
        started_at: str = Field(default="", description=f"When it began. {REPRO_EXCLUDED}")

    assert "started_at" in LedgerWithAnExtraClock.volatile_fields()
    assert RunLedger.volatile_fields() == volatile_paths(RunLedger)


def test_the_volatile_paths_reach_inside_the_nested_models() -> None:
    """The defect this replaced. The first version walked only `RunLedger.model_fields`, so
    `nodes.duration_ms` and `usage.duration_ms` — wall clock, different on every run — were
    invisible to it, and `make repro` would have reported a reproducibility defect on every single
    run. The fix somebody reaches for under that kind of pressure is deleting the check."""
    assert RunLedger.volatile_fields() == frozenset(
        {"run_id", "duration_ms", "nodes.duration_ms", "usage.duration_ms"}
    )


def test_the_verdicts_volatile_paths_are_exactly_these_two() -> None:
    """Pinned as a literal, because the cheap way to make `make repro` pass is to widen this set.

    `run_id` carries the marker because two runs of one submission mint different ids; without it
    a verdict diff fails on every run and the fix under pressure is to stop diffing. Everything
    else in a verdict is supposed to be identical across two runs, which is the whole claim the
    target exists to make. So a third path appearing here is either a real discovery about what
    varies, or somebody silencing a reproducibility defect, and the two need different responses.
    Asserting the set rather than a subset is what forces that conversation into the diff.
    """
    assert volatile_paths(VerdictDocument) == frozenset({"run_id", "review_records.decided_at"})


def test_two_runs_of_one_submission_differ_only_in_the_volatile_paths() -> None:
    """The claim PRD-94 will rest on, with every wall clock deliberately *different* between the two.

    An earlier version passed the same `NodeLog` and an empty `UsageLedger` to both ledgers, so the
    nested durations were identical by construction and the test could not have failed whatever
    `volatile_fields()` returned. Here they differ, which is what two real runs look like.
    """
    first = ledger_for(
        node_log(("intake", NodeOutcome.OK), ("publish", NodeOutcome.OK), duration_ms=7),
        inputs=sorted(SUBMISSION.iterdir()),
        usage=usage_with(duration_ms=900),
    )
    second = ledger_for(
        node_log(("intake", NodeOutcome.OK), ("publish", NodeOutcome.OK), duration_ms=11),
        inputs=sorted(SUBMISSION.iterdir()),
        usage=usage_with(duration_ms=1080),
        run_id="run-ffffffffffff",
        duration_ms=9999,
    )

    stable = RunLedger.volatile_fields()
    assert strip_volatile(first.model_dump(mode="json"), stable) == strip_volatile(
        second.model_dump(mode="json"), stable
    )
    # And the excluded paths really were carrying a difference, or the comparison above proves
    # nothing about them.
    assert first.model_dump(mode="json") != second.model_dump(mode="json")
    assert first.nodes[0].duration_ms != second.nodes[0].duration_ms
    assert first.usage[0].duration_ms != second.usage[0].duration_ms


def test_the_cost_is_a_string_so_it_still_reconciles_after_a_round_trip() -> None:
    """A cost rolled up in binary floating point drifts in the cents, and a figure that does not
    reconcile is worse than no figure."""
    usage = UsageLedger()
    usage.record("mapping", input_tokens=1_234_567, output_tokens=89_012)
    ledger = ledger_for(node_log(("claim_parse", NodeOutcome.OK)), usage=usage)

    assert ledger.total_cost_usd == "8.3981"
    assert json.loads(ledger.model_dump_json())["total_cost_usd"] == "8.3981"


def test_the_cost_summary_names_the_rate_card_that_produced_it() -> None:
    """Prices are a configured input, not a fact. A cost figure without the rate card that
    generated it cannot be reconciled later."""
    usage = UsageLedger()
    usage.record("mapping", input_tokens=1000, output_tokens=100)

    assert "2026-09-13" in cost_summary(usage)
    assert "2026-09-13" in cost_summary(UsageLedger())
    assert "no model calls" in cost_summary(UsageLedger())


# ── a node entered and never left ────────────────────────────────────────────


def test_a_node_that_was_entered_and_never_left_still_gets_a_row() -> None:
    """The single most useful fact about a broken run. A ledger whose node list simply stops early
    looks exactly like one whose run ended cleanly and early."""
    nodes = node_log(("intake", NodeOutcome.OK), ("extract", None))
    rows = timings_from(nodes)

    assert [(r.node, r.entered_only) for r in rows] == [("intake", False), ("extract", True)]
    assert ledger_for(nodes).stopped_inside == ("extract",)


def test_a_healthy_run_stopped_inside_nothing() -> None:
    nodes = node_log(("intake", NodeOutcome.OK), ("publish", NodeOutcome.OK))
    assert ledger_for(nodes).stopped_inside == ()


# ── the exposure, demonstrated then closed ───────────────────────────────────


def test_a_contact_detail_in_a_header_cell_really_does_reach_the_mapping_prompt() -> None:
    """The demonstration the redaction rests on, and the reason PRD-90's stated test was reframed.

    `tda.excel.tools.digest` copies every label cell into the prompt verbatim — its redaction is
    semantic and suppresses only cells that read as *numbers*, because the agent needs the labels
    to do its job. So a hotel that types contact details into a header has put them in the prompt,
    hence in the cassette, and they can re-emerge through a free-prose contract field.

    If this ever stops being true the redaction below is no longer load-bearing, and somebody
    should find out from this test rather than from a leak.
    """
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Summary"
    sheet["A1"] = "Room Nights"
    sheet["A9"] = CONTACT

    assert CONTACT in digest(workbook)


def test_personal_data_is_redacted_before_it_is_written_and_the_count_is_kept(
    tmp_path: Path,
) -> None:
    """The whole claim, end to end: nothing personal on disk, and the fact of it recorded.

    Redaction rather than refusal. The trace is the evidence that answers *"why did it say that?"*,
    and withholding it because one header cell had a phone number in it destroys the record in
    order to protect it. So the text goes, the count stays, and a reader of `run.json` learns that
    something was removed without having to read the trace to find out.
    """
    leaked = json.dumps({"unmapped": [{"sheet": "Summary", "cells": "A9", "reason": CONTACT}]})
    trace = TraceLog([trace_record(output_json=leaked)])
    nodes = node_log(("claim_parse", NodeOutcome.OK))

    written = write_run(tmp_path, ledger_for(nodes), trace, nodes)

    for path in written.files:
        text = path.read_text(encoding="utf-8")
        assert "jane.doe" not in text
        assert "+971 50 123 4567" not in text
        assert "Jane Doe" not in text
        # The surname on its own, and the assertion that matters most here. An earlier version of
        # `titled_name` consumed the honorific and **one** word, so this wrote
        # `[redacted:titled_name] Doe` and the three lines above still passed - the string had been
        # split, not removed, and the most identifying half was on disk.
        assert "Doe" not in text

    assert dict(written.redaction.counts) == {"email": 1, "phone": 1, "titled_name": 1}
    assert "[redacted:email]" in written.trace.read_text(encoding="utf-8")

    recorded, _, _ = read_run(written.directory)
    assert dict(recorded.redactions) == {"email": 1, "phone": 1, "titled_name": 1}


def test_the_recorded_counts_never_carry_the_content_they_counted() -> None:
    """A ledger field listing the phone numbers it redacted from the trace is a ledger that leaks
    them. The counts are a count and a kind, and nothing else."""
    _, redaction = redact(CONTACT)

    rendered = redaction.render()
    assert "jane.doe" not in rendered
    assert "971" not in rendered
    assert rendered == "email x1, phone x1, titled_name x1"


@pytest.mark.parametrize(
    "innocent",
    [
        "Room Nights",
        "Rate Revenue",
        "Total Occupancy 2026-Q1",
        "RN/Mo",
        "Q1/Q2",
        "1,285 room nights at 71.2%",
        "ADR 412.50 AED",
        "2026-01-31",
    ],
)
def test_the_patterns_do_not_fire_on_ordinary_workbook_text(innocent: str) -> None:
    """A guard that cries wolf on correct data is a guard somebody switches off.

    Every pattern fires on a structural signal — an `@`, a dialling prefix, an honorific, a
    `SURNAME/FORENAME` pair — rather than on "looks like a name". One matching capitalised word
    pairs would fire on half of every workbook in the corpus.
    """
    assert scan(innocent).clean, innocent


def test_a_bare_name_is_not_caught_and_that_limit_is_stated_rather_than_implied() -> None:
    """The cost of narrow patterns, asserted so it cannot be quietly forgotten.

    `A9: Jane Doe` alone passes: nothing structural distinguishes it from `A9: Deluxe King`, and a
    rule that caught the first would flag the second on every run. What closes the gap is the
    corpus design — no names exist to leak — and, for a real pilot, the pseudonymisation boundary
    the build plan defers until a real-file pilot is agreed. A test asserting the opposite would be
    claiming a protection this repository does not have.
    """
    assert scan("Jane Doe").clean
    assert not scan("Ms. Jane Doe").clean


def test_a_clean_run_redacts_nothing_and_says_so(tmp_path: Path) -> None:
    """The ordinary case. A redaction report that is never empty is a report nobody reads."""
    nodes = node_log(("claim_parse", NodeOutcome.OK))
    written = write_run(tmp_path, ledger_for(nodes), TraceLog([trace_record()]), nodes)

    assert written.redaction.clean
    assert "nothing redacted" in written.redaction.render()
    assert "redacted" not in written.render()


# ── the artifacts on disk ────────────────────────────────────────────────────


def test_a_run_writes_three_files_under_its_own_id(tmp_path: Path) -> None:
    nodes = node_log(("intake", NodeOutcome.OK))
    written = write_run(tmp_path, ledger_for(nodes), TraceLog([trace_record()]), nodes)

    assert written.directory == tmp_path / "run-0123456789ab"
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == sorted(
        [RUN_LEDGER, AGENT_TRACE, NODE_LOG]
    )


def test_the_verdict_is_not_written_here(tmp_path: Path) -> None:
    """`verdict.json` is PRD-92's, and the split is deliberate: the verdict is the thing an officer
    signs behind, and these are its working. One writer for both would make the evidence and the
    conclusion move together whenever either changed."""
    nodes = node_log(("publish", NodeOutcome.OK))
    written = write_run(tmp_path, ledger_for(nodes), TraceLog(), nodes)

    assert not (written.directory / "verdict.json").exists()


def test_a_written_run_reads_back_as_what_was_written(tmp_path: Path) -> None:
    trace = TraceLog([trace_record(), trace_record(agent="narrative", output_contract="Finding")])
    nodes = node_log(("claim_parse", NodeOutcome.OK), ("publish", NodeOutcome.OK))
    written = write_run(tmp_path, ledger_for(nodes), trace, nodes)

    ledger, read_trace, read_nodes = read_run(written.directory)

    assert ledger.run_id == "run-0123456789ab"
    assert [r.agent for r in read_trace] == ["mapping", "narrative"]
    assert read_nodes.completed() == ("claim_parse", "publish")


def test_reading_a_directory_that_is_not_a_run_says_which_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"no run\.json"):
        read_run(tmp_path / "nowhere")


def test_the_latest_run_is_the_most_recent_one_rather_than_the_highest_id(tmp_path: Path) -> None:
    """By modification time, because a run id is meaningless by design and sorting them would order
    runs by a hash."""
    assert latest_run(tmp_path / "missing") is None
    assert latest_run(tmp_path) is None

    nodes = node_log(("intake", NodeOutcome.OK))
    older = write_run(tmp_path, ledger_for(nodes, run_id="run-ffffffffffff"), TraceLog(), nodes)
    newer = write_run(tmp_path, ledger_for(nodes, run_id="run-000000000000"), TraceLog(), nodes)
    (older.directory / RUN_LEDGER).touch()
    (newer.directory / RUN_LEDGER).touch()

    assert latest_run(tmp_path) == newer.directory


# ── the tree ─────────────────────────────────────────────────────────────────


def test_every_call_is_shown_under_the_node_that_made_it() -> None:
    """The join is arithmetic, not a guess: node timings say how many calls each node made, the
    trace preserves call order, and the graph is strictly sequential (ADR-0005)."""
    timings = (
        NodeTiming(node="intake", outcome="ok"),
        NodeTiming(node="claim_parse", outcome="ok", model_calls=2),
        NodeTiming(node="publish", outcome="ok", model_calls=1),
    )
    trace = TraceLog(
        [
            trace_record(agent="mapping"),
            trace_record(agent="resolution", output_contract="LabelResolution"),
            trace_record(agent="narrative", output_contract="FindingNarrative"),
        ]
    )
    lines = render_tree(ledger_with(timings), trace, NodeLog()).splitlines()

    def line_of(fragment: str) -> int:
        return next(i for i, line in enumerate(lines) if fragment in line)

    # Two calls under `claim_parse`, then one under `publish` - which is what the model_calls
    # counts say, and the only reading of the trace consistent with them.
    assert (
        line_of("intake")
        < line_of("claim_parse")
        < line_of("mapping/v1")
        < line_of("resolution/v1")
        < line_of("publish")
        < line_of("narrative/v1")
    )


def test_a_call_the_timings_cannot_account_for_is_reported_rather_than_dropped() -> None:
    """What a concurrent graph would look like from here. A viewer that silently dropped the calls
    it could not place would let the attribution go wrong quietly, which is the one failure mode a
    trace viewer must not have."""
    timings = (NodeTiming(node="claim_parse", outcome="ok", model_calls=1),)
    trace = TraceLog([trace_record(), trace_record(agent="narrative")])

    tree = render_tree(ledger_with(timings), trace, NodeLog())

    assert "1 model call(s) could not be attributed" in tree


def test_free_model_prose_never_reaches_a_line_of_the_tree() -> None:
    """PRD-90, in one assertion. A narrative sentence is model-written prose about a named
    property's numbers; a viewer that pretty-printed it would put it on a terminal, then in a
    screenshot, then in a ticket. Whoever needs the sentence reads the verdict, which is the
    artifact meant to carry it."""
    sentence = (
        "Reported room nights for February exceed the PMS total by 1,285, which is 4.1% of the "
        "month and above the materiality threshold for this property."
    )
    assert len(sentence) > VALUE_LIMIT
    trace = TraceLog(
        [
            trace_record(
                agent="narrative",
                output_contract="FindingNarrative",
                output_json=json.dumps({"finding_id": "F-0001", "sentence": sentence}),
            )
        ]
    )
    timings = (NodeTiming(node="publish", outcome="ok", model_calls=1),)

    tree = render_tree(ledger_with(timings), trace, NodeLog())

    assert "1,285" not in tree
    assert "exceed the PMS total" not in tree
    assert f"…({len(sentence)} chars)" in tree
    # The short, structural field is still shown, which is the whole point of eliding by length
    # rather than hiding the contract.
    assert '"F-0001"' in tree


def test_a_refused_tool_call_gets_its_own_line() -> None:
    """A refusal means the agent reached outside its allowlist, which is a roster or prompt defect
    rather than a runtime event. Folding it into the tool list would make it a footnote."""
    trace = TraceLog(
        [
            trace_record(
                tool_calls=[
                    TraceCall(name="peek_headers", allowed=True),
                    TraceCall(name="read_values", allowed=False),
                ]
            )
        ]
    )
    timings = (NodeTiming(node="claim_parse", outcome="ok", model_calls=1),)

    tree = render_tree(ledger_with(timings), trace, NodeLog())

    assert "REFUSED: read_values" in tree
    assert "outside this agent's allowlist" in tree


def test_the_tree_names_the_node_a_broken_run_stopped_inside() -> None:
    """The question a reader of a broken run opens the file to answer."""
    nodes = node_log(("intake", NodeOutcome.OK), ("extract", None))

    tree = render_tree(ledger_for(nodes), TraceLog(), nodes)

    assert "entered, never left" in tree
    assert "stopped inside: extract" in tree


def test_the_tree_says_why_a_node_ended_the_way_it_did() -> None:
    """A halt with no stated reason is the thing the node records exist to prevent, and a viewer
    that dropped the reason would undo that."""
    nodes = node_log(("intake", NodeOutcome.REJECTED))

    tree = render_tree(ledger_for(nodes), TraceLog(), nodes)

    assert "rejected" in tree
    assert "intake said why" in tree


def test_the_header_carries_the_rules_the_run_ran_under() -> None:
    """The ledger's whole job in four lines: what this was, under which rules, on which bytes, and
    what it cost."""
    nodes = node_log(("intake", NodeOutcome.OK))
    ledger = ledger_for(nodes, inputs=sorted(SUBMISSION.iterdir()))

    header = render_tree(ledger, TraceLog(), nodes).splitlines()[:4]

    assert "run-0123456789ab" in header[0]
    assert "PASS" in header[0]
    assert "MZN-DXB-001" in header[0]
    assert "policy 1.3.0" in header[1]
    assert "metrics 1.0.0" in header[1]
    assert "stub" in header[1]
    assert "5 input file(s)" in header[2]
    assert "rates 2026-09-13" in header[2]


def test_a_rejected_run_names_its_reason_in_the_first_line() -> None:
    """`REJECTED` alone sends the reader back to the JSON to find out which of four conditions
    fired."""
    nodes = node_log(("intake", NodeOutcome.REJECTED))
    ledger = ledger_for(nodes, status="REJECTED", rejection_reason="PERIOD_MISMATCH")

    assert "PERIOD_MISMATCH" in render_tree(ledger, TraceLog(), nodes).splitlines()[0]


# ── the channels and the joins an earlier review found untested ──────────────


def test_the_honorific_pattern_takes_the_whole_name_and_not_just_the_first_word() -> None:
    """The defect a passing test hid. `Ms. Jane Doe` used to redact to `[redacted:titled_name] Doe`,
    and every assertion written as `"Jane Doe" not in text` passed while the surname sat in the
    artifact. The bound is three words, so `Dr. Jane Marie Doe` goes and `Dr. Smith Room Nights Q1`
    does not eat the whole label."""
    assert redact("Ms. Jane Doe")[0] == "[redacted:titled_name]"
    assert redact("Dr. Jane Marie Doe")[0] == "[redacted:titled_name]"
    assert redact("Dr. Smith Room Nights Q1 Total")[0] == "[redacted:titled_name] Q1 Total"


def test_scanning_and_redacting_agree_about_the_same_bytes() -> None:
    """They used to disagree. `scan` ran each pattern over the original text and `redact` ran them
    in sequence over the text its predecessors left, so `+971.50.123.4567@hotel.ae` counted as an
    email *and* a phone when scanned and as an email alone when written. A ledger whose counts
    differ from what a guard reports for identical bytes is a ledger that has to be checked by
    hand, which is the opposite of the point."""
    overlapping = "+971.50.123.4567" + "@" + "hotel.ae"

    assert scan(overlapping) == redact(overlapping)[1]
    assert scan(CONTACT) == redact(CONTACT)[1]


def test_the_ledger_counts_what_was_redacted_from_the_ledger_itself(tmp_path: Path) -> None:
    """`run.json` used to contradict itself: the body said `[redacted:email]` and the `redactions`
    field said nothing had been removed, because the counts were stamped before the ledger was
    redacted. The reader most likely to notice is the one the field exists for."""
    submission = tmp_path / "in"
    submission.mkdir()
    leaky = submission / ("claims_prepared_by_jane.doe" + "@" + "hotel.ae.xlsx")
    leaky.write_bytes(b"not really a workbook, but it has a name and a size")

    nodes = node_log(("intake", NodeOutcome.OK))
    written = write_run(tmp_path / "art", ledger_for(nodes, inputs=[leaky]), TraceLog(), nodes)

    body = written.ledger.read_text(encoding="utf-8")
    assert "jane.doe" not in body
    assert "[redacted:email]" in body

    recorded, _, _ = read_run(written.directory)
    assert dict(recorded.redactions) == {"email": 1}
    assert dict(written.redaction.counts) == {"email": 1}


def test_the_node_log_is_redacted_too_and_not_only_the_trace(tmp_path: Path) -> None:
    """`NodeRecord.detail` carries file names and exception strings — a pydantic error quotes the
    value it rejected, and `input_value='DOE/JANE'` is precisely what `slashed_name` is for. Only
    the trace was covered before, so removing the node log's redaction entirely broke nothing."""
    nodes = NodeLog(
        [
            NodeRecord(node="extract", phase=Phase.ENTER),
            NodeRecord(
                node="extract",
                phase=Phase.EXIT,
                outcome=NodeOutcome.HALTED,
                detail="guest_ref rejected: input_value='DOE/JANE'",
            ),
        ]
    )

    written = write_run(tmp_path, ledger_for(nodes), TraceLog(), nodes)

    assert "DOE/JANE" not in written.nodes.read_text(encoding="utf-8")
    assert dict(written.redaction.counts) == {"slashed_name": 1}


def test_the_counts_from_all_three_files_are_added_rather_than_overwritten(tmp_path: Path) -> None:
    """Every earlier test put personal data in exactly one file, so a `_merge` that overwrote
    instead of summing produced identical output and nothing failed."""
    address = "jane.doe" + "@" + "hotel.ae"
    trace = TraceLog([trace_record(output_json=json.dumps({"reason": address}))])
    nodes = NodeLog(
        [
            NodeRecord(node="claim_parse", phase=Phase.ENTER),
            NodeRecord(
                node="claim_parse",
                phase=Phase.EXIT,
                outcome=NodeOutcome.FAILED,
                detail=f"could not reach {address}",
            ),
        ]
    )

    written = write_run(tmp_path, ledger_for(nodes), trace, nodes)

    assert dict(written.redaction.counts) == {"email": 2}


def test_the_ledger_that_reached_disk_is_the_one_handed_back(tmp_path: Path) -> None:
    """`mizan run` prints this rather than the ledger it passed in, because the object in memory is
    the one copy that was never redacted. The file used to say `[redacted:email]` while the
    terminal printed the address."""
    submission = tmp_path / "in"
    submission.mkdir()
    leaky = submission / ("q1_jane.doe" + "@" + "hotel.ae.xlsx")
    leaky.write_bytes(b"bytes")

    nodes = node_log(("intake", NodeOutcome.OK))
    original = ledger_for(nodes, inputs=[leaky])
    written = write_run(tmp_path / "art", original, TraceLog(), nodes)

    assert "jane.doe" in original.render()
    assert "jane.doe" not in written.as_written.render()
    assert "[redacted:email]" in written.as_written.render()


def test_a_run_directory_missing_its_trace_refuses_to_read_as_a_clean_run(tmp_path: Path) -> None:
    """ "The evidence file is gone" and "this run made no model calls" must not render identically.
    `write_run` always writes all three files, so an absent one means the directory is incomplete."""
    nodes = node_log(("intake", NodeOutcome.OK))
    written = write_run(tmp_path, ledger_for(nodes), TraceLog(), nodes)
    written.trace.unlink()

    with pytest.raises(FileNotFoundError, match=r"trace\.jsonl is missing"):
        read_run(written.directory)


# ── the flattening and the join ──────────────────────────────────────────────


def test_a_node_entered_twice_gets_two_rows_and_keeps_both_outcomes() -> None:
    """Keying the exits by node name gave every entry the *last* exit, so a node that failed and
    was then entered again reported the successful figures twice — the failure erased, the duration
    and the model calls doubled. The doubled `model_calls` then feeds the trace attribution, which
    is how one wrong row becomes a whole tree of calls under the wrong nodes."""
    log = NodeLog(
        [
            NodeRecord(node="claim_parse", phase=Phase.ENTER),
            NodeRecord(
                node="claim_parse",
                phase=Phase.EXIT,
                outcome=NodeOutcome.FAILED,
                duration_ms=10,
                model_calls=1,
                detail="the first attempt",
            ),
            NodeRecord(node="claim_parse", phase=Phase.ENTER),
            NodeRecord(
                node="claim_parse",
                phase=Phase.EXIT,
                outcome=NodeOutcome.OK,
                duration_ms=20,
                model_calls=3,
            ),
        ]
    )

    rows = timings_from(log)

    assert [(r.outcome, r.duration_ms, r.model_calls) for r in rows] == [
        ("failed", 10, 1),
        ("ok", 20, 3),
    ]


def test_an_exit_with_no_entry_is_recorded_rather_than_dropped() -> None:
    """It should be impossible. Dropping it would make the node durations quietly fail to add up to
    the run's, and a reader checking that arithmetic is the reader this ledger is written for."""
    log = NodeLog(
        [
            NodeRecord(
                node="publish",
                phase=Phase.EXIT,
                outcome=NodeOutcome.OK,
                duration_ms=5,
                model_calls=2,
            )
        ]
    )

    rows = timings_from(log)

    assert [(r.node, r.duration_ms, r.model_calls) for r in rows] == [("publish", 5, 2)]


def test_a_node_claiming_more_calls_than_the_trace_has_says_so() -> None:
    """The quiet direction of the imbalance, and the dangerous one: the node consumes what is left
    and every node after it shows somebody else's calls, with nothing on the page looking wrong.
    Reachable today — a failed call writes a trace record without touching the usage ledger."""
    timings = (
        NodeTiming(node="claim_parse", outcome="ok", model_calls=3),
        NodeTiming(node="publish", outcome="ok", model_calls=1),
    )
    trace = TraceLog([trace_record()])

    tree = render_tree(ledger_with(timings), trace, NodeLog())

    assert "claim_parse reports 3 model call(s) and the trace had 1 left to give" in tree


# ── what a call cost, and what it reached for ────────────────────────────────


def test_the_tree_prints_what_each_call_cost() -> None:
    """Every trace record in this module used to carry zero tokens, so the tree always printed
    `$0.0000` and replacing the cost arithmetic with anything at all changed nothing."""
    trace = TraceLog([trace_record(input_tokens=1_000_000, output_tokens=200_000)])
    timings = (NodeTiming(node="claim_parse", outcome="ok", model_calls=1),)

    tree = render_tree(ledger_with(timings), trace, NodeLog())

    # 1M input at $5.00/Mtok + 200k output at $25.00/Mtok = $10.00.
    assert "$10.0000" in tree
    assert "1,000,000/200,000 tok" in tree


def test_the_tree_counts_repeated_tool_calls_rather_than_listing_them() -> None:
    """A mapping call peeks at every sheet, and four identical lines say nothing `x4` does not.
    Nothing asserted this, so a `_tools` that returned a constant passed."""
    trace = TraceLog(
        [
            trace_record(
                tool_calls=[
                    TraceCall(name="list_sheets", allowed=True),
                    *[TraceCall(name="peek_headers", allowed=True) for _ in range(4)],
                ]
            )
        ]
    )
    timings = (NodeTiming(node="claim_parse", outcome="ok", model_calls=1),)

    tree = render_tree(ledger_with(timings), trace, NodeLog())

    assert "with list_sheets, peek_headers x4" in tree


def test_the_cost_column_adds_up_to_the_total_printed_under_it() -> None:
    """Seven sub-cent calls used to display as $0.0001 each under a total of $0.0009, because the
    rows were rounded and the total was not. This is the exact figure `tda.obs.usage` says must not
    happen: a reader who adds the column up and gets a different answer has found a reason to
    distrust every number on the page."""
    usage = UsageLedger()
    for agent in ("mapping", "narrative", "resolution"):
        usage.record(agent, input_tokens=27, output_tokens=3)

    summary = cost_summary(usage)
    rows = [line for line in summary.splitlines() if "call(s)" in line and "total" not in line]
    printed = [Decimal(line.rsplit("$", 1)[1]) for line in rows]

    assert sum(printed) == usage.total_cost_usd()
    assert f"${usage.total_cost_usd():.4f}" in summary.splitlines()[-1]


def test_the_latest_run_breaks_a_tie_by_name_rather_than_by_the_filesystem(
    tmp_path: Path,
) -> None:
    """Two runs can land in one filesystem timestamp, and `max` over `iterdir` then answers by
    inode order — so the same two runs give different answers on two machines and `mizan trace`
    stops being reproducible for a reason nobody can see."""
    root = tmp_path / "artifacts"
    nodes = node_log(("intake", NodeOutcome.OK))
    first = write_run(root, ledger_for(nodes, run_id="run-aaaaaaaaaaaa"), TraceLog(), nodes)
    second = write_run(root, ledger_for(nodes, run_id="run-bbbbbbbbbbbb"), TraceLog(), nodes)

    for directory in (first.directory, second.directory):
        os.utime(directory, (1_700_000_000, 1_700_000_000))

    assert latest_run(root) == second.directory


def test_a_question_asked_on_the_review_screen_is_shown_rather_than_counted_as_unplaceable() -> (
    None
):
    """A reviewer-assist call has no node and never will - the agent `runs_in="review"`, after the
    graph has finished.

    Attributing it arithmetically like every other record made the viewer print "1 model call(s)
    could not be attributed to a node" on every reviewed run, which trains a reader to ignore the
    one line that catches real mis-attribution - and left the officer's question, its citations and
    its tokens on disk but invisible to `mizan trace`, the documented way to read a trace.
    """
    from tda.agents.roster import REVIEWER_ASSIST

    timings = (NodeTiming(node="publish", outcome="ok", model_calls=1),)
    trace = TraceLog(
        [
            trace_record(agent="narrative", output_contract="FindingNarrative"),
            trace_record(
                agent=REVIEWER_ASSIST,
                output_contract="CitedAnswer",
                effort="high",
                output_json='{"question": "why?", "answer": {"outcome": "declined"}}',
            ),
        ]
    )

    rendered = render_tree(ledger_with(timings), trace, NodeLog())

    assert "after the run" in rendered
    assert "CitedAnswer" in rendered
    assert "could not be attributed" not in rendered


def test_a_call_from_an_agent_the_roster_does_not_know_is_still_reported_as_unplaceable() -> None:
    """The other half, and the reason the review section reads the roster rather than testing a
    name. A record this viewer genuinely cannot place must still say so - a bucket that swallowed
    everything left over would be the silent failure the warning exists to prevent."""
    trace = TraceLog([trace_record(agent="some_agent_nobody_declared")])

    rendered = render_tree(ledger_with(()), trace, NodeLog())

    assert "could not be attributed" in rendered
    assert "after the run" not in rendered
