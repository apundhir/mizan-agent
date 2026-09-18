"""The verification officer's screen. `make review`.

**The acceptance criterion is a stopwatch.** PRD-91: *if judging one finding requires opening the
PDF in another window, the screen has failed, regardless of how correct the finding is.* Everything
below follows from that one sentence.

So each finding is a self-contained card carrying, without a click: what was claimed, what the
records support, the difference, the cause, **the rows of the report the computed value came from**,
and **the cell of the workbook the claim was read from**. Then accept, reject or amend, with a note.

## The layout is an argument

Two columns of evidence, side by side, because the judgement is a comparison and a reviewer holding
one half in their head while scrolling for the other is a reviewer who will start trusting the
system instead of checking it. The figures sit above both, so the eye goes number → picture →
picture without leaving the card.

## Definitional items are on their own tab, and it says what they are not

D-MAT-06 keeps a V2 out of the hotel error count in the verdict and out of the findings table in
the memo. Mixing them here would undo both in the one place a human actually reads — a policy
disagreement listed among clerical errors is a correct finding that reads as an accusation.

## Nothing is computed here

The screen reads `verdict.json` and the submitted files and shows them. It does not recompute, rank
or re-classify. `tda.review.decisions` records what the officer decided; the numbers are the
pipeline's, and a screen that quietly recomputed one would be the one place in this system where a
figure has no provenance.

## The evidence is checked against the run before it is shown

`run.json` records a digest per submitted file, and every crop is checked against it. Pointing the
screen at the wrong submission is one environment variable away — `make run SUBMISSION=/data/hotel-x`
then `make review` used to crop the demo corpus and caption it with this run's citations — and a
wrong picture under a correct caption is the worst thing an evidence screen can do.

## The question box answers about the verdict, and cannot answer instead of it

Above the findings sits a collapsed box an officer can type a question into. What comes back is prose **with
citations**, or a refusal — the contract has no third shape, and every citation is checked against
this run before the answer is shown. The agent has three read-only lookups, no arithmetic and no
file access, so it cannot recompute a figure and does not try: ask it "by how much?" and it will
point at the finding that already holds the number.

Nothing it says changes the verdict, and nothing it says is required to decide one. It exists
because an officer with a question currently has two options — open the PDF, or ask a colleague —
and the screen failing that test is the same failure the stopwatch above measures. See
`tda.review.assist` for what happens when it cannot answer, which is most of the interesting cases.

## Why the state lives on disk rather than in the session

Every decision is written to `verdict.json` the moment it is made — and the memo is re-issued with
it. A review interrupted by a closed laptop has lost nothing, and two officers on one run cannot
silently overwrite each other, because the recorder re-reads before every write. Streamlit session
state holds the reviewer's name and nothing else that matters.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

import streamlit as st

from tda.contracts import ReviewerDecision, VarianceClass
from tda.obs.artifacts import RUN_LEDGER, latest_run
from tda.obs.ledger import RunLedger
from tda.outputs.verdict import VERDICT_FILE, read_verdict
from tda.policy import load_policy
from tda.review.assist import MAX_QUESTION, Answer, AskResult, put_question
from tda.review.decisions import Recorded, ReviewError, record_decision, superseded
from tda.review.evidence import Evidence, evidence_for
from tda.review.present import (
    answer_citations,
    cause_line,
    cell_table,
    chip_colour,
    citations,
    decision_summary,
    figures,
    headline,
    unavailable_line,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tda.agents.provider import LLMProvider
    from tda.contracts import Finding
    from tda.obs.ledger import InputFile
    from tda.outputs.verdict import VerdictDocument

REPO_ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = Path(os.environ.get("MIZAN_ARTIFACTS", REPO_ROOT / "artifacts"))
SUBMISSION = Path(os.environ.get("MIZAN_SUBMISSION", REPO_ROOT / "corpus" / "demo" / "submission"))


def main() -> None:
    st.set_page_config(page_title="Mizan — verification review", layout="wide")
    run = _pick_run()
    if run is None:
        return

    verdict = read_verdict(run / VERDICT_FILE)
    inputs = _inputs_of(run)
    reviewer = _sidebar(verdict, run, inputs)
    _header(verdict)

    _ask_box(verdict, run)

    findings, definitional = verdict.findings, verdict.definitional_items
    hotel_tab, policy_tab = st.tabs(
        [f"Findings ({len(findings)})", f"Definitional items ({len(definitional)}) — not errors"]
    )
    with hotel_tab:
        if not findings:
            st.success(
                "No findings. Every figure the workbook states was recomputed from the "
                "reservation records and matched."
            )
        for finding in findings:
            _card(finding, verdict, run, reviewer, inputs)
    with policy_tab:
        st.info(
            "These figures are reproduced **exactly** by an alternative reading of the "
            "definitions. They are a question for the policy owner, not a correction for the "
            "property, and they are excluded from the hotel error count (D-MAT-06)."
        )
        for finding in definitional:
            _card(finding, verdict, run, reviewer, inputs)


def _pick_run() -> Path | None:
    """Which run to review. The most recent by default, because that is what somebody just ran."""
    chosen = os.environ.get("MIZAN_RUN")
    if chosen:
        run = ARTIFACTS / chosen
        if not (run / VERDICT_FILE).is_file():
            st.error(f"no {VERDICT_FILE} in {run}.")
            return None
        return run

    runs = (
        sorted(
            (d for d in ARTIFACTS.iterdir() if (d / VERDICT_FILE).is_file()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if ARTIFACTS.is_dir()
        else []
    )
    if not runs:
        st.error(
            f"No reviewable run under {ARTIFACTS}. `make run` produces one — a run writes "
            f"`{VERDICT_FILE}` beside its other artifacts."
        )
        return None
    latest = latest_run(ARTIFACTS)
    names = [d.name for d in runs]
    default = names.index(latest.name) if latest and latest.name in names else 0
    return ARTIFACTS / str(st.sidebar.selectbox("Run", names, index=default))


def _sidebar(verdict: VerdictDocument, run: Path, inputs: Sequence[InputFile]) -> str:
    st.sidebar.markdown("### Reviewer")
    reviewer = str(
        st.sidebar.text_input(
            "Your name",
            value=str(st.session_state.get("reviewer", "")),
            placeholder="required to record a decision",
        )
    )
    st.session_state["reviewer"] = reviewer
    if not reviewer.strip():
        st.sidebar.warning(
            "A decision must name the person who made it. The point of the gate is that somebody "
            "accountable looked."
        )

    total = len(verdict.findings) + len(verdict.definitional_items)
    decided = total - len(verdict.undecided_findings)
    st.sidebar.markdown("### Progress")
    st.sidebar.progress(decided / total if total else 1.0, text=f"{decided} of {total} decided")
    if verdict.undecided_findings:
        st.sidebar.caption(f"Outstanding: {', '.join(verdict.undecided_findings)}")
    st.sidebar.markdown("### Artifacts")
    st.sidebar.caption(f"`{run}`")
    st.sidebar.caption("Every decision is written to `verdict.json` and re-issues `memo.docx`.")
    st.sidebar.caption(f"Evidence read from `{SUBMISSION}`")
    if not inputs:
        # Without the ledger there is nothing to check a file against, and the screen would show
        # whatever is in the submission directory. Better to say so than to look confident.
        st.sidebar.warning(
            f"No `{RUN_LEDGER}` beside this verdict, so the evidence below cannot be checked "
            "against the files the run actually read."
        )
    return reviewer


def _header(verdict: VerdictDocument) -> None:
    st.markdown(f"## {verdict.status.value} · {verdict.hotel_id} · {verdict.period}")
    columns = st.columns(4)
    figures_ = (
        ("Claims checked", verdict.claims_checked),
        ("Hotel errors", verdict.summary.hotel_errors),
        ("Definitional items", verdict.summary.definitional_items),
        ("Not verifiable", verdict.summary.not_verifiable),
    )
    for column, (label, value) in zip(columns, figures_, strict=True):
        column.metric(label, value)
    st.caption(
        f"Policy {verdict.policy_version} · metric library {verdict.metric_library_version} · "
        f"{verdict.model_id} ({verdict.provider_mode}) · run {verdict.run_id}"
    )


def _ask_box(verdict: VerdictDocument, run: Path) -> None:
    """A question about this verdict, answered with citations or not at all.

    Collapsed by default, and that is the whole of its claim on the screen. It sits above the
    findings because a question is asked about the run rather than about one card, but it occupies
    one line until an officer opens it: the acceptance criterion is that a finding can be judged
    without leaving its card, and an assistant taking space from the cards would be competing with
    the thing it exists to support.

    The last answer is held in `st.session_state` rather than in `st.cache_data`, and the
    difference is not a detail. `cache_data` is **process-global**: two officers asking the same
    question on the same run would have shared one call, and the second officer's question would
    have been appended to no trace at all - which is precisely the record ADR-0009 says the
    questions are part of. Session state also survives the rerun that follows a recorded decision,
    so the answer does not vanish at the moment the officer acts on it.
    """
    with st.expander("Ask about this verdict", expanded=False):
        st.caption(
            "Answered from this verdict, its evidence and the definitions, with citations. It "
            "cannot compute a figure and does not have the submitted files - ask *why* and "
            "*which rule*, not *how much*. Nothing it says changes the verdict."
        )
        question = str(
            st.text_area(
                "Question",
                key="assist-question",
                max_chars=MAX_QUESTION,
                placeholder="Which rule makes F-0002 definitional rather than an error?",
            )
        )
        if st.button("Ask", key="assist-ask"):
            with st.spinner("Asking..."):
                st.session_state["assist-answer"] = put_question(
                    question, verdict, run, _provider(), load_policy()
                )

        result = st.session_state.get("assist-answer")
        if result is None:
            return
        _render_answer(result)


def _render_answer(result: AskResult) -> None:
    """The citations, then the answer, then what it cost.

    In that order, and slightly awkward to read on purpose: the citations are what the officer is
    meant to check, and prose at the top of a block is prose that gets read instead of them.
    """
    if not isinstance(result, Answer):
        st.warning(unavailable_line(result))
        st.caption(result.text)
        return

    for citation in answer_citations(result):
        st.markdown(f"- `{citation}`")
    # An answer the agent declined to give is shown as information rather than as an answer: a
    # refusal styled like a result is a refusal an officer reads as one.
    if result:
        st.write(result.text)
    else:
        st.info(result.text, icon="🚫")
    st.caption(f"Recorded to this run's trace · {result.cost_line}")


def _provider() -> LLMProvider:
    """The model layer the assistant talks to: `policy.model.provider`, the source `mizan run`
    reads too.

    Not `verdict.provider_mode`, which records what *that* run actually used and may have been an
    override on the command line. The two can legitimately differ - a replayed run reviewed on a
    machine with a key - and conflating them would make the screen's behaviour depend on how the
    run happened to be invoked.
    """
    from tda.cli import build_provider

    return build_provider(load_policy().model.provider)


def _card(
    finding: Finding,
    verdict: VerdictDocument,
    run: Path,
    reviewer: str,
    inputs: Sequence[InputFile],
) -> None:
    standing = verdict.standing_decisions.get(finding.finding_id)
    with st.container(border=True):
        left, right = st.columns([3, 1])
        left.markdown(f"**{headline(finding)}**")
        right.markdown(
            f"<div style='background:{chip_colour(finding)};border-radius:4px;padding:2px 8px;"
            f"text-align:center;color:#222;font-weight:600;'>{finding.severity.value}</div>",
            unsafe_allow_html=True,
        )
        st.caption(cause_line(finding))

        for column, (label, value) in zip(st.columns(3), figures(finding), strict=True):
            column.metric(label, value)

        _evidence(finding, run, inputs)
        if finding.narrative:
            st.caption(finding.narrative)

        if standing is not None:
            st.success(
                decision_summary(standing, len(superseded(verdict, finding.finding_id))),
                icon="✅",
            )
        _decide(finding, run, reviewer, decided=standing is not None)


def _evidence(finding: Finding, run: Path, inputs: Sequence[InputFile]) -> None:
    """Both sides, side by side. The whole point of the screen."""
    evidence = _evidence_for(str(run), finding.model_dump_json(), _digests(inputs))
    source_citation, cell_citation = citations(finding)
    # The report pane is wider than the workbook pane, because the evidence it carries is a
    # landscape page of small print and the other is a handful of cells. Equal columns look
    # tidier and make the half that actually needs reading the half nobody can read.
    source, claim = st.columns([3, 2])

    source.markdown("**What the records say**")
    if evidence.source_png is not None:
        source.image(evidence.source_png, caption=source_citation)
    else:
        source.info(evidence.source_caption or source_citation)

    claim.markdown("**What the hotel claimed**")
    if evidence.cell is not None:
        claim.markdown(cell_table(evidence.cell), unsafe_allow_html=True)
        claim.caption(cell_citation)
    else:
        claim.info(cell_citation)

    for problem in evidence.missing:
        st.warning(problem)


@st.cache_data(show_spinner=False)
def _evidence_for(run: str, finding_json: str, digests: str) -> Evidence:
    """Cached per finding, because Streamlit re-runs this script on every keystroke.

    Keyed on the finding's own JSON rather than on its id: two runs can both have an `F-0001`, and
    a cache that returned the first one's page crop for the second would show a reviewer evidence
    from a different submission. That is the most expensive mistake this screen could make.

    `digests` is in the key for the same reason - the same finding checked against a different file
    set is a different question - and is passed as JSON because a cache key has to be hashable.
    """
    from tda.contracts import Finding
    from tda.obs.ledger import InputFile

    del run  # part of the cache key, not of the lookup
    return evidence_for(
        Finding.model_validate_json(finding_json),
        SUBMISSION,
        [InputFile.model_validate(item) for item in json.loads(digests)],
    )


def _digests(inputs: Sequence[InputFile]) -> str:
    return json.dumps([item.model_dump(mode="json") for item in inputs], sort_keys=True)


def _inputs_of(run: Path) -> tuple[InputFile, ...]:
    """What the run recorded reading. Empty when there is no ledger beside the verdict."""
    ledger = run / RUN_LEDGER
    if not ledger.is_file():
        return ()
    try:
        return RunLedger.model_validate_json(ledger.read_text(encoding="utf-8")).inputs
    # Broad on purpose: an unreadable ledger costs the digest check and a warning, not the screen.
    except Exception:
        return ()


def _decide(finding: Finding, run: Path, reviewer: str, *, decided: bool) -> None:
    label = "Record a different decision" if decided else "Decide"
    with st.expander(label, expanded=not decided):
        note = st.text_input("Note (optional)", key=f"note-{finding.finding_id}")
        amended_raw = ""
        if finding.variance_class is VarianceClass.TRANSCRIPTION:
            if finding.proposed_correction is not None:
                # Shown, not pre-filled. A pre-filled box makes "Amend" a one-click way to record
                # the system's own number as a human amendment, which is the review gate agreeing
                # with itself and calling it a decision.
                st.caption(f"The system proposes {finding.proposed_correction}.")
            amended_raw = str(
                st.text_input(
                    "Amended value (required to amend)",
                    key=f"amend-{finding.finding_id}",
                )
            )
        accept, reject, amend = st.columns(3)
        pressed: ReviewerDecision | None = None
        if accept.button("Accept", key=f"a-{finding.finding_id}", use_container_width=True):
            pressed = ReviewerDecision.ACCEPT
        if reject.button("Reject", key=f"r-{finding.finding_id}", use_container_width=True):
            pressed = ReviewerDecision.REJECT
        if amend.button(
            "Amend",
            key=f"m-{finding.finding_id}",
            use_container_width=True,
            # Only a transcription error may carry a correction: proposing one for a definitional
            # variance would tell a hotel to change a number that is not wrong.
            disabled=finding.variance_class is not VarianceClass.TRANSCRIPTION,
        ):
            pressed = ReviewerDecision.AMEND
        if pressed is not None:
            _record(run, finding, pressed, reviewer, note, amended_raw)


def _record(
    run: Path,
    finding: Finding,
    decision: ReviewerDecision,
    reviewer: str,
    note: str,
    amended_raw: str,
) -> None:
    amended: Decimal | None = None
    if decision is ReviewerDecision.AMEND and amended_raw.strip():
        try:
            amended = Decimal(amended_raw.strip())
        except InvalidOperation:
            st.error(f"{amended_raw!r} is not a number.")
            return
    try:
        recorded: Recorded = record_decision(
            run,
            finding_id=finding.finding_id,
            decision=decision,
            reviewer=reviewer,
            note=note,
            amended_value=amended,
        )
    except ReviewError as exc:
        st.error(str(exc))
        return
    # Broad on purpose. Anything else is a failure to write, and the officer needs to know the
    # decision did not stick rather than meet a traceback and have to guess.
    except Exception as exc:
        st.error(f"{finding.finding_id} was not recorded: {type(exc).__name__}: {exc}")
        return
    st.toast(f"{finding.finding_id}: {decision.value} recorded · memo re-issued", icon="📝")
    del recorded
    st.rerun()


if __name__ == "__main__":
    # Streamlit execs this file with `__name__ == "__main__"`, so the screen still runs. An
    # unguarded call meant `import tda.review.app` rendered every finding of the latest run -
    # opening PDFs and rasterising pages - and raised on a verdict that no longer validates,
    # taking the whole test module with it.
    main()
