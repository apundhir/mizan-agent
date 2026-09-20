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
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

import streamlit as st

from tda.contracts import ReviewerDecision, VarianceClass
from tda.obs.artifacts import RUN_LEDGER, latest_run
from tda.obs.ledger import RunLedger
from tda.outputs.verdict import VERDICT_FILE, read_verdict
from tda.policy import load_policy
from tda.review import ui
from tda.review.assist import MAX_QUESTION, Answer, AskResult, put_question
from tda.review.decisions import Recorded, ReviewError, record_decision, superseded
from tda.review.evidence import Evidence, evidence_for
from tda.review.present import answer_citations, decision_summary, unavailable_line

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tda.agents.provider import LLMProvider
    from tda.contracts import Finding
    from tda.obs.ledger import InputFile
    from tda.outputs.verdict import VerdictDocument

REPO_ROOT = Path(__file__).resolve().parents[3]

# Every run this browser session has itself made (started via the console, or opened by an exact
# id). Read by the fallback run picker below and by the console's own replay panel, so neither
# lists a run this session never touched unless the deployment has explicitly said every run on
# disk belongs to one trusted operator.
SESSION_KNOWN_RUNS: Final = "review.known_runs"

SINGLE_OPERATOR_ENV: Final = "MIZAN_SINGLE_OPERATOR"


def single_operator(environ: Mapping[str, str] | None = None) -> bool:
    """Whether every run under `ARTIFACTS` belongs to one trusted person, so any of them may be
    listed and opened by whoever is looking - one officer's own machine, the Docker `review`
    service, `make review` run locally.

    Off unless told otherwise, the same direction `tda.review.live`'s gate defaults in: a hosted
    process is reachable by whoever holds the app's link, often several people at once who do not
    trust each other, and a run picker that shows every viewer's uploads to every other viewer is
    a disclosure this codebase must not make by default. `docker/compose.yaml`'s `review` service
    and the local `make review`/`make review-live` targets set this explicitly to `true`; nothing
    in `.streamlit/secrets.example.toml` does, on purpose - a hosted deployment that forgets to
    set it gets the restricted behaviour, never the open one.
    """
    env = environ if environ is not None else os.environ
    return env.get(SINGLE_OPERATOR_ENV, "").strip().lower() == "true"


UPLOAD_ENABLED_ENV: Final = "MIZAN_UPLOAD_ENABLED"


def uploads_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the console offers to verify a viewer's own files, rather than prepared scenes only.

    Off unless told otherwise, for the same reason `single_operator` above is: a prepared scene is
    this repository's own committed corpus, an input trusted since M2, while an upload is the one
    input this codebase has never authored. Six rounds of security review went into containing what
    a hostile `.xlsx` can cost (`tda.review.sandbox`, ADR-0010 §7), and every one of those rounds
    stays in the codebase and under test. This flag decides whether that path is *reachable*, which
    is a question about who can reach the deployment rather than about how well the path is
    defended.

    A public link is reachable by anyone who has it, and the five scenes are what that audience
    came to see, so the hosted demo runs with this unset and never spawns a child process at all.
    `docker/compose.yaml`'s `review` service and the local `make review`/`make review-live` targets
    set it to `true`; nothing in `.streamlit/secrets.example.toml` does, on purpose, so a hosted
    deployment that forgets it gets the scenes-only behaviour rather than the open one.

    Parsed the lenient way `single_operator` is, not the raising way `tda.review.live` parses its
    own variables: a typo here should fail closed, and closed is the safe state.
    """
    env = environ if environ is not None else os.environ
    return env.get(UPLOAD_ENABLED_ENV, "").strip().lower() == "true"


def env_path(name: str, default: Path) -> Path:
    """An environment variable read as a path, with unset and blank treated the same.

    `make review` used to export `MIZAN_SUBMISSION=` with nothing after the `=` when the caller
    left it unspecified, and a plain lookup with a fallback returns that empty string rather than
    the fallback - the variable exists, it is just empty. `Path("")` is `Path(".")`, which pointed
    the whole screen at the repository root. Treating a blank value as unset is the fix, here
    rather than in the Makefile, so every reader of the variable gets it rather than one call site.
    """
    value = os.environ.get(name, "").strip()
    return Path(value) if value else default


ARTIFACTS = env_path("MIZAN_ARTIFACTS", REPO_ROOT / "artifacts")
DEFAULT_SUBMISSION = REPO_ROOT / "corpus" / "demo" / "submission"

# Session-state keys the Run console writes to hand a reviewer off to this screen: which run to
# open, and where its submission lives, so evidence is cropped from the files that run actually
# read rather than from whatever MIZAN_SUBMISSION happens to point at.
SESSION_RUN = "review.run"
SESSION_SUBMISSION = "review.submission"


def submission_for(run: Path) -> Path:
    """Where this run's evidence lives, in order of how much the caller told us.

    A session value set by the Run console wins, because it names the exact directory that run's
    files were staged into. Failing that, `<run>/submission` - a console-made run stages its own
    files there - if it exists. Failing that, `MIZAN_SUBMISSION` for the officer who launched the
    screen against a directory by hand, and the demo corpus if nobody said anything at all.
    """
    from_session = st.session_state.get(SESSION_SUBMISSION)
    if from_session:
        candidate = Path(str(from_session))
        if candidate.is_dir():
            return candidate
    staged = run / "submission"
    if staged.is_dir():
        return staged
    return env_path("MIZAN_SUBMISSION", DEFAULT_SUBMISSION)


def main(*, configure_page: bool = True) -> None:
    """The Review page. `configure_page=False` when `st.navigation` already called
    `st.set_page_config` for the whole app - Streamlit allows exactly one call per script run."""
    if configure_page:
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


def page() -> None:
    """The `st.Page` entry the Run console's `st.navigation` calls. The `__main__` guard at the
    foot of this file still runs `main()` unwrapped, so `streamlit run src/tda/review/app.py` -
    the Docker image's command, and `make review` before the console existed - keeps working
    unchanged."""
    main(configure_page=False)


def _pick_run() -> Path | None:
    """Which run to review.

    A run the console just made, named in session state, wins - that is the officer clicking
    "Open in Review" and expecting to land on that run rather than the most recent one by mtime,
    which could be a different run made a second earlier by a scene running concurrently. Checked
    against `known` too, redundantly with `console.py` only ever writing a run id it already added
    there - a scoping bug should not need every writer of `SESSION_RUN` to stay correct forever for
    this to hold. Failing that, `MIZAN_RUN` for the officer who launched the screen by hand, and
    the most recent run otherwise - unless `single_operator()` is off, in which case the fallback
    list is scoped to runs this session itself made, because listing every run under `ARTIFACTS` on
    a deployment reachable by more than one untrusting viewer is showing each of them what the
    others uploaded.
    """
    visible = single_operator()
    known: set[str] = st.session_state.get(SESSION_KNOWN_RUNS, set())

    from_console = st.session_state.get(SESSION_RUN)
    if from_console and (visible or str(from_console) in known):
        run = ARTIFACTS / str(from_console)
        if (run / VERDICT_FILE).is_file():
            return run
        # The console's own record of "the last run I made" pointed somewhere that no longer has
        # a verdict - stale state from an earlier session, most likely. Drop it and fall through
        # to the ordinary picker rather than erroring on a run the officer never asked to see.
        del st.session_state[SESSION_RUN]
        st.warning(f"the run this screen was opened on, {from_console!r}, no longer has a verdict.")

    chosen = os.environ.get("MIZAN_RUN")
    if chosen:
        run = ARTIFACTS / chosen
        if not (run / VERDICT_FILE).is_file():
            st.error(f"no {VERDICT_FILE} in {run}.")
            return None
        return run
    runs = (
        sorted(
            (
                d
                for d in ARTIFACTS.iterdir()
                if (d / VERDICT_FILE).is_file() and (visible or d.name in known)
            ),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        if ARTIFACTS.is_dir()
        else []
    )
    if not runs:
        if visible:
            st.error(
                f"No reviewable run under {ARTIFACTS}. `make run` produces one — a run writes "
                f"`{VERDICT_FILE}` beside its other artifacts."
            )
        else:
            st.error(
                "No run from this browser session is reviewable yet. This deployment shows each "
                "viewer only the runs they made themselves - start one from the Run page first."
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
    with st.sidebar:
        ui.downloads(run, key_prefix="review")
    st.sidebar.caption(f"`{run}`")
    st.sidebar.caption("Every decision is written to `verdict.json` and re-issues `memo.docx`.")
    st.sidebar.caption(f"Evidence read from `{submission_for(run)}`")
    if not inputs:
        # Without the ledger there is nothing to check a file against, and the screen would show
        # whatever is in the submission directory. Better to say so than to look confident.
        st.sidebar.warning(
            f"No `{RUN_LEDGER}` beside this verdict, so the evidence below cannot be checked "
            "against the files the run actually read."
        )
    return reviewer


def _header(verdict: VerdictDocument) -> None:
    ui.status_banner(verdict.status.value, hotel_id=verdict.hotel_id, period=verdict.period)
    ui.summary_metrics(verdict.summary, claims_checked=verdict.claims_checked)
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
    from tda.review.live import LiveModeError

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
                try:
                    st.session_state["assist-answer"] = put_question(
                        question, verdict, run, _provider(), load_policy()
                    )
                except LiveModeError as exc:
                    st.error(str(exc))
                    return

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
    reads too, filtered through this deployment's live-mode gate.

    Not `verdict.provider_mode`, which records what *that* run actually used and may have been an
    override on the command line. The two can legitimately differ - a replayed run reviewed on a
    machine with a key - and conflating them would make the screen's behaviour depend on how the
    run happened to be invoked.

    Routed through `tda.review.live` rather than calling `tda.cli.build_provider` directly, so a
    hosted deployment with live mode off refuses a policy that asks for `anthropic` on screen,
    instead of reaching the SDK and surfacing as an opaque failure three layers down.
    """
    from tda.review.live import live_gate

    return live_gate().provider_for(load_policy().model.provider)


def _card(
    finding: Finding,
    verdict: VerdictDocument,
    run: Path,
    reviewer: str,
    inputs: Sequence[InputFile],
) -> None:
    standing = verdict.standing_decisions.get(finding.finding_id)
    ui.finding_card(
        finding,
        _evidence_for(
            str(run), finding.model_dump_json(), _digests(inputs), str(submission_for(run))
        ),
        decide=partial(_decide, finding, run, reviewer, decided=standing is not None),
        standing=(
            None
            if standing is None
            else decision_summary(standing, len(superseded(verdict, finding.finding_id)))
        ),
    )


@st.cache_data(show_spinner=False)
def _evidence_for(run: str, finding_json: str, digests: str, submission: str) -> Evidence:
    """Cached per finding, because Streamlit re-runs this script on every keystroke.

    Keyed on the finding's own JSON rather than on its id: two runs can both have an `F-0001`, and
    a cache that returned the first one's page crop for the second would show a reviewer evidence
    from a different submission. That is the most expensive mistake this screen could make.

    `digests` is in the key for the same reason - the same finding checked against a different file
    set is a different question - and is passed as JSON because a cache key has to be hashable.
    `submission` joins the key too: a console-made run and the demo corpus can both hold a finding
    with the same id, and the cache must not hand one's crop to the other.
    """
    from tda.contracts import Finding
    from tda.obs.ledger import InputFile

    del run  # part of the cache key, not of the lookup
    return evidence_for(
        Finding.model_validate_json(finding_json),
        Path(submission),
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
