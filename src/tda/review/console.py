"""The Run console: pick or upload a submission, watch the agents work, read the verdict.

A thin layer over the headless modules that do the real work - `tda.review.scenes` builds a known
submission, `tda.review.staging` builds one from an upload, `tda.review.runner` runs the pipeline on
a background thread, `tda.review.timeline` turns its logs into events, `tda.review.shown` recovers
what an agent was asked, `tda.review.prose` grades the writing on demand, `tda.review.live` decides
what the provider selector may offer. Every one of those is tested without a browser; this module's
own job is arranging widgets around them and is checked by `streamlit.testing.v1.AppTest`.

## Every string a viewer can see is redacted, again, here

Every artifact a run writes is already redacted before it reaches disk (`tda.obs.redact`). This
page renders some of that content live, in memory, before it is ever written - a node's failure
detail, a routing decision's reason, an agent's answer - so the same redaction is applied again at
the point of display. Belt and braces: two callers redacting the same text once each costs nothing
extra and removes the one path where a change to the write order could reintroduce a leak the tests
would not catch until the artifact existed to catch it in.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

import streamlit as st

from tda.agents.roster import ROSTER
from tda.obs.artifacts import RUN_LEDGER
from tda.obs.redact import redact
from tda.obs.viewer import summarise_output
from tda.outputs.verdict import VerdictSummary
from tda.policy import load_policy
from tda.review import ui
from tda.review.app import (
    SESSION_KNOWN_RUNS,
    _digests,
    _evidence_for,
    _inputs_of,
    single_operator,
    uploads_enabled,
)
from tda.review.live import LiveGate, LiveModeError, ProcessLiveRuns, live_gate, take_live_run
from tda.review.prose import grade_verdict
from tda.review.runner import JobState, prepare_job, start
from tda.review.shown import shown_for
from tda.review.staging import Upload, UploadError, stage_upload, write_manifest
from tda.review.timeline import EventKind, TimelineEvent, timeline_from_job, timeline_from_run

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from streamlit.navigation.page import Page
    from streamlit.runtime.uploaded_file_manager import UploadedFile

    from tda.contracts import Finding, Verdict
    from tda.obs import InputFile
    from tda.review.evidence import Evidence
    from tda.review.prose import GradeRow
    from tda.review.runner import RunJob
    from tda.review.scenes import Scene, SceneCatalogue
    from tda.review.timeline import Timeline

# Session-state keys, named once and reused everywhere they are read or written.
KEY_JOB: Final = "console.job"
KEY_SCENE: Final = "console.scene"
KEY_PROVIDER: Final = "console.provider"
KEY_MODE: Final = "console.mode"
KEY_GRADES: Final = "console.grades"
KEY_HOTEL: Final = "console.hotel"
KEY_PERIOD: Final = "console.period"
KEY_PLAYER_RUN: Final = "console.player_run"
KEY_PLAYER_CURSOR: Final = "console.player_cursor"
KEY_PLAYER_TEMPO: Final = "console.player_tempo"
KEY_PLAYER_PLAYING: Final = "console.player_playing"

MODE_SCENE: Final = "Prepared scene"
MODE_UPLOAD: Final = "Your own files"

TEMPO_OPTIONS: Final = ("0.25x", "0.5x", "1x", "2x", "4x", "8x", "instant")


@st.cache_resource
def _process_live_runs() -> ProcessLiveRuns:
    """One counter per server process, shared by every session - see `ProcessLiveRuns`."""
    return ProcessLiveRuns()


def render(
    catalogue: SceneCatalogue,
    *,
    review_page: Page | None,
    artifacts_root: Path,
    env: Mapping[str, str] | None = None,
) -> None:
    """The whole Run page. `review_page` is the `st.Page` `st.switch_page` should open for "Open
    in Review" - `None` when this is rendered standalone (a test, `make review` before the console
    has a companion page), in which case that button becomes a caption instead."""
    environ = env if env is not None else os.environ
    try:
        gate = live_gate(environ)
    except LiveModeError as exc:
        st.error(str(exc))
        st.stop()

    uploads = uploads_enabled(environ)
    _hero()
    # Two variants of the same G6 disclosure, because warning a viewer not to upload real records
    # on a page with no uploader is noise, and noise is how a disclosure stops being read.
    st.caption(
        "Synthetic demonstration data only. Do not upload real guest or property records - "
        "uploaded files are written to this server's disk and stay there until the app next "
        "restarts, where whoever operates this deployment can read them. Other viewers of this "
        "link are shown only the runs they started themselves."
        if uploads
        else "Synthetic demonstration data only. Every scene below runs against this project's "
        "own committed corpus, generated from a ledger this repository authored. No real hotel "
        "or guest data is involved, and this deployment accepts no uploads."
    )
    # The second G6 paragraph is not repeated here. The hero caption above already states the same
    # guarantee against the figure that makes it ("Computed by a model: 0"), and stacking a third
    # grey paragraph under the first two is how the disclosure at the top of this page stopped
    # being read in the first place. Both paragraphs are carried verbatim, under their own heading,
    # on the How it works page.
    st.caption("How it works explains where the models are, and what they may not do.")

    job: RunJob | None = st.session_state.get(KEY_JOB)
    _submission_block(catalogue, artifacts_root, gate, job, uploads=uploads)

    job = st.session_state.get(KEY_JOB)
    if job is not None:
        _live_panel(job)
        if job.done:
            _verdict_panel(job, review_page)
            _working_panel(job)

    st.divider()
    _replay_panel(artifacts_root)


def _hero() -> None:
    """What this is, for somebody who arrived from a link and knows nothing.

    The page used to open with ninety words of grey disclaimer and no title at all, which asked a
    viewer to read the small print before they had been told what they were looking at. The three
    figures are this run's own corpus rather than marketing: `corpus/demo` holds 1,200 reservations
    across three monthly reports, and the workbook states 94 figures derived from them.

    The third figure is the one this project exists to make: agents label and phrase, and the
    arithmetic is ordinary Python. That is not a claim asking to be believed - `tools/guard/
    import_guard.py` fails the build if `tda.metrics` or `tda.reconcile` can so much as import a
    model client, and `tools/guard/agent_schema_lint.py` fails it if an agent contract declares a
    numeric field outside the citation whitelist.
    """
    st.title("Does the hotel's return match its own records?")
    st.write(
        "A hotel files a quarterly return. Its property system exports the reservation reports "
        "those figures were supposed to be built from. Mizan reads both, recomputes every figure "
        "in plain deterministic code, and shows each disagreement with the report rows on one side "
        "and the workbook cell on the other."
    )
    left, middle, right = st.columns(3)
    left.metric("Reservations read", "1,200", border=True)
    middle.metric("Figures recomputed", "94", border=True)
    right.metric("Computed by a model", "0", border=True)
    st.caption(
        "The third figure is the point. Language models label a workbook's layout, resolve an "
        "ambiguous country name and write the sentence beside a finding. None of them may return a "
        "number that reaches a calculation, and a guard in CI fails the build rather than trusting "
        "anyone to remember."
    )


# ── submitting ─────────────────────────────────────────────────────────────────


def _submission_block(
    catalogue: SceneCatalogue,
    artifacts_root: Path,
    gate: LiveGate,
    job: RunJob | None,
    *,
    uploads: bool,
) -> None:
    running = job is not None and not job.done
    provider_choice = _provider_choice(gate, running)

    st.markdown("### Pick a scene")
    # Rendered only where uploads are reachable. A one-option chooser is a control that does
    # nothing, and offering a choice this deployment will not honour is worse than not offering it.
    mode = (
        st.radio(
            "Submission", (MODE_SCENE, MODE_UPLOAD), key=KEY_MODE, horizontal=True, disabled=running
        )
        if uploads
        else MODE_SCENE
    )

    if mode == MODE_SCENE:
        _scene_cards(catalogue, artifacts_root, gate, provider_choice, running=running)
    else:
        _upload_form(artifacts_root, gate, provider_choice, running=running)

    if running:
        st.info("A run is already in progress.")


def _provider_choice(gate: LiveGate, running: bool) -> str:
    """The provider selector, in the sidebar rather than between the scenes and the button.

    A viewer picking a scene is not choosing a model backend, and on a replay-only deployment the
    control has exactly one option. It belongs beside the sentence that says whether anything is
    called at all, which already lives in the sidebar.
    """
    with st.sidebar:
        st.markdown("### Model access")
        choice = st.selectbox(
            "Provider", gate.offered_providers(), key=KEY_PROVIDER, disabled=running
        )
        st.caption(gate.render())
    return str(choice)


def _scene_cards(
    catalogue: SceneCatalogue,
    artifacts_root: Path,
    gate: LiveGate,
    provider_choice: str,
    *,
    running: bool,
) -> None:
    """Five scenes as cards a viewer can read, rather than five titles in a dropdown.

    Each one already carries a written description and the outcome it is expected to reach; both
    were in `tda.review.scenes` from the start and neither was ever visible until a viewer had
    already chosen. Choosing from a list of titles asks somebody to pick before they have been
    told what they are picking between, which for the one scene that reaches ESCALATED rather than
    FAIL is the difference between a demo and a puzzle.

    The card's own button runs it. A separate Run control below would be a second click for a
    choice already made, and the demo's whole job is to get a stranger to a verdict quickly.
    """
    st.caption(
        "Each of these is a real run of the whole pipeline against a prepared submission. Nothing "
        "below is a recording of a screen: the verdict you get is computed while you wait."
    )
    scenes = catalogue.scenes
    for row_start in range(0, len(scenes), 2):
        for column, scene in zip(st.columns(2), scenes[row_start : row_start + 2], strict=False):
            with column, st.container(border=True):
                st.badge(
                    scene.expected_status.value,
                    color=ui.STATUS_TONES.get(scene.expected_status.value, "gray"),
                    width="content",
                )
                st.markdown(f"**{scene.title}**")
                st.caption(scene.description)
                if not scene.makes_model_calls:
                    st.caption("This one reaches its answer without calling a model at all.")
                if st.button(
                    "Run this scene",
                    key=f"scene-{scene.key}",
                    disabled=running,
                    type="primary",
                    width="stretch",
                ):
                    st.session_state[KEY_SCENE] = scene.title
                    _on_run(
                        scene,
                        None,
                        [],
                        None,
                        scene.hotel_id,
                        scene.period,
                        provider_choice,
                        gate,
                        artifacts_root,
                    )


def _upload_form(
    artifacts_root: Path, gate: LiveGate, provider_choice: str, *, running: bool
) -> None:
    """The upload path, unchanged, reachable only where `uploads_enabled` says so."""
    workbook_file = st.file_uploader("Workbook (.xlsx)", type=["xlsx"], disabled=running)
    report_files = st.file_uploader(
        "Monthly reports (.pdf)", type=["pdf"], accept_multiple_files=True, disabled=running
    )
    inventory_file = st.file_uploader("Inventory (.csv, optional)", type=["csv"], disabled=running)
    # A widget's key and its `value=` argument may not both be set once the key already holds
    # a value in session state - Streamlit treats that as the value being set from two places
    # at once. `setdefault` seeds the key only the first time this page ever renders; every
    # widget on every rerun after that reads and writes through `key=` alone.
    st.session_state.setdefault(KEY_HOTEL, "")
    st.session_state.setdefault(KEY_PERIOD, "2026-Q1")
    hotel_id = str(st.text_input("Declared hotel", key=KEY_HOTEL, disabled=running))
    period_text = str(st.text_input("Declared period", key=KEY_PERIOD, disabled=running))
    st.caption(
        "Your own files reconcile inside the replay boundary: an edited value in the demo "
        "workbook, or a missing report, still matches a recorded answer. A renamed header, an "
        "added sheet, or a real PMS export will miss the recording and be refused rather than "
        "guessed at."
    )
    if st.button("Run", disabled=running, type="primary"):
        _on_run(
            None,
            _to_upload(workbook_file),
            [u for f in (report_files or []) if (u := _to_upload(f)) is not None],
            _to_upload(inventory_file),
            hotel_id,
            period_text,
            provider_choice,
            gate,
            artifacts_root,
        )


def _to_upload(file: UploadedFile | None) -> Upload | None:
    if file is None:
        return None
    return Upload(name=file.name, data=file.getvalue())


def _on_run(
    scene: Scene | None,
    workbook: Upload | None,
    reports: list[Upload],
    inventory: Upload | None,
    hotel_id: str,
    period_text: str,
    provider_choice: str,
    gate: LiveGate,
    artifacts_root: Path,
) -> None:
    from tda.contracts import Period
    from tda.graph import new_run_id

    try:
        period = Period.parse(period_text)
    except ValueError as exc:
        st.error(f"{period_text!r} is not a period: {exc}")
        return
    if not hotel_id.strip():
        st.error("A declared hotel is required.")
        return

    # A new run's findings do not share ids with the last one by construction, but a fixture-backed
    # demo corpus does - two different scenes both produce an F-0001. Left in place, the previous
    # run's graded sentence would render as if it belonged to this run's own F-0001.
    st.session_state.pop(KEY_GRADES, None)

    # Minted here, once, and threaded through both the staging directory and the job - the two
    # must name the same run, or the job would write its verdict into a directory that is not the
    # one its own submission was staged into.
    run_id = new_run_id()
    run_dir = artifacts_root / run_id
    # Recorded before anything can fail, so a run this session started is always in scope for its
    # own replay panel and, on a deployment where `single_operator()` is off, the review screen's
    # picker - see `tda.review.app.single_operator`.
    known: set[str] = st.session_state.setdefault(SESSION_KNOWN_RUNS, set())
    known.add(run_id)
    try:
        if scene is not None:
            submission = scene.prepare(run_dir)
        else:
            if workbook is None:
                st.error("A workbook is required.")
                return
            submission = stage_upload(run_dir, period, workbook, reports, inventory)
        write_manifest(run_dir, hotel_id, str(period))
    except UploadError as exc:
        st.error(str(exc))
        return
    except OSError as exc:
        st.error(f"could not stage this submission on the server: {redact(str(exc))[0]}")
        return

    try:
        provider = gate.provider_for(provider_choice)
    except LiveModeError as exc:
        st.error(str(exc))
        return
    if provider_choice != "replay":
        try:
            # st.session_state behaves like a MutableMapping at runtime (get, __setitem__) but
            # is not typed as one.
            take_live_run(st.session_state, gate)  # type: ignore[arg-type]
            _process_live_runs().take(gate)
        except LiveModeError as exc:
            st.error(str(exc))
            return

    policy = load_policy()
    job = prepare_job(
        artifacts_root=artifacts_root,
        submission=submission,
        hotel_id=hotel_id,
        period=period,
        policy=policy,
        provider=provider,
        provider_name=provider_choice,
        scene_key=scene.key if scene is not None else None,
        run_id=run_id,
        # A scene is this repository's own committed corpus; an upload is a viewer's own file,
        # and the one input `tda.review.sandbox` exists for - see its module docstring for why
        # this is the one branch point between the live, node-by-node path and the sandboxed one.
        sandboxed=scene is None,
    )
    start(job)
    st.session_state[KEY_JOB] = job
    st.rerun()


# ── the live view ──────────────────────────────────────────────────────────────


def _live_panel(job: RunJob) -> None:
    if job.sandboxed:
        _sandboxed_panel(job)
        return
    if job.done:
        _render_timeline(timeline_from_job(job), cursor=None)
        return

    @st.fragment(run_every=0.4)
    def _poll() -> None:
        _render_timeline(timeline_from_job(job), cursor=None)
        if job.done:
            st.rerun(scope="app")

    _poll()


def _working_panel(job: RunJob) -> None:
    """Everything a sceptic asks for, one click away from everything a viewer asks for.

    A sandboxed job's calls are only readable from what its child process wrote, and a child the
    resource limit or the timeout killed wrote nothing at all - not even `run.json`. There is then
    no working to show, and trying to read it anyway is how this panel crashed the page the first
    time it existed; `_sandboxed_panel` above guards the same file for the same reason.
    """
    if job.sandboxed:
        if not (job.run_dir / RUN_LEDGER).is_file():
            return
        timeline = timeline_from_run(job.run_dir)
    else:
        timeline = timeline_from_job(job)
    with st.expander("Show the working: every model call, and what it was shown"):
        _render_working(timeline, cursor=None, workbook=_workbook_of(job))


def _sandboxed_panel(job: RunJob) -> None:
    """An upload's run has no live node-by-node view to poll: the pipeline is executing in a
    separate, resource-limited process (`tda.review.sandbox`), and this one has no window into
    its progress until it exits and writes what `mizan run` always writes. Shown at once, from
    those files, the moment it is done - the same `timeline_from_run` the replay panel already
    uses for a run made earlier, not a rendering path invented for this case.

    A subprocess killed by its own resource limit, or by the wall-clock timeout, writes nothing -
    not even `run.json` - so there is nothing here to turn into a timeline. `_verdict_panel`
    already renders `job.error` for exactly that case; this function's job is to not crash trying
    to read a file that a contained kill never got to write. Checked directly, on disk, rather
    than inferred from `job.state` - a `COULD_NOT_RUN` exit still writes `run.json` through
    `mizan run`'s own failure ledger before this ever runs, and that partial timeline is real and
    worth showing, so the file's presence is the question, not why the run stopped."""
    if not job.done:

        @st.fragment(run_every=0.4)
        def _poll() -> None:
            st.info("Verifying your submission…")
            if job.done:
                st.rerun(scope="app")

        _poll()
        return
    if not (job.run_dir / RUN_LEDGER).is_file():
        return
    _render_timeline(timeline_from_run(job.run_dir), cursor=None)


def _workbook_of(job: RunJob) -> Path | None:
    """The workbook a live mapping call's agent card would need to recompute its own request
    against. Read off the live result when there is one; for a sandboxed job there is no
    `RunResult` to hold it, but the submission was staged by role name before the run started, so
    the path is derivable without one - `tda.review.staging.role_names`' own naming convention."""
    if job.result is not None:
        return job.result.state.submission.workbook
    if job.sandboxed:
        candidate = job.submission / f"claims_{job.period}.xlsx"
        return candidate if candidate.is_file() else None
    return None


def _render_timeline(timeline: Timeline, *, cursor: int | None) -> None:
    """The five stages and where this run got to. `cursor` is the replay player's position in
    display time, `None` for the finished state."""
    st.markdown("### What the system did")
    ui.stage_rail(timeline.chip_states(cursor))


def _render_working(
    timeline: Timeline, *, cursor: int | None, workbook: Path | None = None
) -> None:
    """The supervisor's decisions and every agent call, behind a disclosure.

    All of it used to sit in the main scroll between the stage rail and the verdict, so a viewer
    who wanted to know whether the return was accepted read a cassette key and a token count on
    the way. It is the most interesting material on the page for one audience and noise for the
    other, and a disclosure is how a page serves both without choosing.
    """
    decisions = timeline.decisions(cursor)
    if decisions:
        st.markdown("**Supervisor**")
        for event in decisions:
            line = f"{event.node} to {event.agent}: {event.outcome} ({event.detail})"
            st.caption(redact(line)[0])

    st.markdown("**Agents**")
    calls_by_agent: dict[str, list[TimelineEvent]] = {name: [] for name in ROSTER}
    for event in timeline.upto(cursor):
        if event.kind is EventKind.AGENT_CALL and event.agent in calls_by_agent:
            calls_by_agent[event.agent].append(event)

    for agent, entry in ROSTER.items():
        calls = calls_by_agent.get(agent, [])
        if not calls:
            icon = "⚪"
        elif any(call.outcome == "failed" for call in calls):
            icon = "🔴"
        else:
            icon = "🟢"
        with st.expander(f"{icon} {agent}: {entry.job}", expanded=bool(calls)):
            if not calls:
                st.caption(f"runs at {entry.runs_in}; not called in this run")
                continue
            for call_event in calls:
                _render_call(call_event, workbook=workbook)


def _render_call(event: TimelineEvent, *, workbook: Path | None) -> None:
    record = event.call
    if record is None:
        return
    cassette = f"{record.cassette_key[:8]}..." if record.cassette_key else "live"
    st.caption(
        f"{record.agent}/{record.prompt_version} - {record.provider_mode} ({cassette}) - "
        f"{record.input_tokens:,}/{record.output_tokens:,} tok - {record.duration_ms:,}ms"
    )
    if record.failed:
        st.error(redact(record.error or "")[0])
    else:
        st.write(redact(summarise_output(record.output_json))[0])
    if record.tool_calls:
        names = ", ".join(f"{c.name}{'' if c.allowed else ' (refused)'}" for c in record.tool_calls)
        st.caption(redact(f"tools: {names}")[0])

    shown = shown_for(record, workbook=workbook, policy=load_policy())
    with st.expander("What it was shown", expanded=False):
        if shown is None:
            st.caption(
                "Request text is not available for this call (no cassette, and it did not run "
                "live as the mapping agent)."
            )
        else:
            st.caption(shown.label)
            st.code(shown.system, language="text")
            for role, content in shown.messages:
                st.caption(role)
                st.code(content, language="text")


# ── the verdict panel ──────────────────────────────────────────────────────────


def _findings_panel(job: RunJob, verdict: Verdict) -> None:
    """Every finding as a full card, evidence included.

    This page used to render a finding as one markdown bullet - the id, the metric key and the
    clause - while the Review screen rendered the same finding with its three figures and the PDF
    crop beside the workbook cell. The data for the second was available to both the whole time.
    A viewer watching a run reach FAIL and being told only that `F-0001 - guests_by_nationality`
    disagreed has been shown the conclusion and none of the working, which is the opposite of what
    this system exists to demonstrate.

    Definitional items sit in their own tab, labelled so that nobody reads them as errors: D-MAT-06
    keeps them out of the hotel error count, and a screen that filed them beside clerical mistakes
    would undo that where it actually matters.
    """
    if not verdict.findings and not verdict.definitional_items:
        st.success(
            "No findings. Every figure the workbook states was recomputed from the reservation "
            "records and matched.",
            icon=":material/check:",
        )
        return

    inputs = _inputs_of(job.run_dir)
    tabs = st.tabs(
        [
            f"Findings ({len(verdict.findings)})",
            f"Definitional items ({len(verdict.definitional_items)}) - not errors",
        ]
    )
    with tabs[0]:
        if not verdict.findings:
            st.caption("Nothing was filed against the property in this run.")
        for finding in verdict.findings:
            ui.finding_card(finding, _evidence(finding, job, inputs))
    with tabs[1]:
        if not verdict.definitional_items:
            st.caption("No figure here is explained by a different reading of the definitions.")
        for item in verdict.definitional_items:
            ui.finding_card(item, _evidence(item, job, inputs))


def _evidence(finding: Finding, job: RunJob, inputs: Sequence[InputFile]) -> Evidence | None:
    """The two pictures for one finding, or `None` when this run never staged a submission to crop
    them out of. Routed through `tda.review.app`'s own cache rather than a second one, so a finding
    opened on this page and then on the Review page is cropped once."""
    submission = job.submission
    if not submission.is_dir():
        return None
    return _evidence_for(
        str(job.run_dir), finding.model_dump_json(), _digests(inputs), str(submission)
    )


def _verdict_panel(job: RunJob, review_page: Page | None) -> None:
    st.markdown("### Verdict")
    if job.state is JobState.FAILED:
        # Redacted here too, on the same belt-and-braces basis every other string on this page is
        # (see the module docstring) - `job.error` is written redacted for an in-process failure
        # already, but a sandboxed job's is not derived from a write path this module controls,
        # and the invariant should hold regardless of where the string came from.
        detail = redact(job.error)[0] if job.error else ""
        st.error(f"The run did not complete. {detail}")
        return

    verdict = job.verdict
    assert verdict is not None
    summary = VerdictSummary.of(verdict)
    ui.status_banner(verdict.status.value, hotel_id=verdict.hotel_id, period=str(verdict.period))
    # Absent on a submission intake refused: nothing was read, so the sentence that says how much
    # was read would be a claim about a run that never happened. The banner's own line already
    # says why a rejected submission stopped where it did.
    extraction = verdict.extraction
    if extraction is not None:
        st.write(
            ui.what_happened(
                records=extraction.records_extracted,
                pages=extraction.pages_read,
                files=len(extraction.files),
                totals_matched=extraction.printed_total_matched,
                claims=verdict.claims_checked,
            )
        )
    ui.summary_metrics(summary, claims_checked=verdict.claims_checked)
    _findings_panel(job, verdict)
    ui.downloads(job.run_dir, key_prefix=f"console-{job.run_id}")

    left, middle, _ = st.columns(3)
    if review_page is not None and left.button("Open in Review"):
        from tda.review.app import SESSION_RUN, SESSION_SUBMISSION

        st.session_state[SESSION_RUN] = job.run_id
        st.session_state[SESSION_SUBMISSION] = str(job.submission)
        st.switch_page(review_page)
    elif review_page is None:
        left.caption(f"Run id: {job.run_id}")

    if middle.button("Grade the prose"):
        with st.spinner("Narrating and grading..."):
            st.session_state[KEY_GRADES] = grade_verdict(verdict, job.provider, job.policy)

    grades: tuple[GradeRow, ...] | None = st.session_state.get(KEY_GRADES)
    if grades:
        _render_grades(grades)


def _render_grades(grades: tuple[GradeRow, ...]) -> None:
    icons = {"graded_pass": "✅", "graded_fail": "❌", "not_recorded": "⚪", "failed": "⚠️"}
    st.markdown("**Grading**")
    for row in grades:
        key = "graded_pass" if row.status == "graded" and row.passed else "graded_fail"
        icon = icons[key if row.status == "graded" else row.status]
        st.markdown(f"{icon} **{row.finding_id}**")
        if row.sentence:
            st.caption(redact(row.sentence)[0])
        if row.status == "graded" and not row.passed:
            st.caption(f"failed: {', '.join(row.failures)}. {row.reason}")
        elif row.status != "graded":
            st.caption(row.detail)


# ── replaying a past run ────────────────────────────────────────────────────────


def _replay_panel(artifacts_root: Path) -> None:
    st.markdown("### Replay a past run")
    runs = _past_runs(artifacts_root)
    if not runs:
        st.caption("No past runs yet.")
        return

    chosen = st.selectbox("Run", [d.name for d in runs], key=KEY_PLAYER_RUN)
    tempo = st.select_slider("Tempo", TEMPO_OPTIONS, value="1x", key=KEY_PLAYER_TEMPO)
    run_dir = next(d for d in runs if d.name == chosen)
    timeline = timeline_from_run(run_dir)

    columns = st.columns(3)
    if columns[0].button("Play"):
        st.session_state[KEY_PLAYER_PLAYING] = True
        st.session_state[KEY_PLAYER_CURSOR] = 0
    if columns[1].button("Pause"):
        st.session_state[KEY_PLAYER_PLAYING] = False
    if columns[2].button("Reset"):
        st.session_state[KEY_PLAYER_PLAYING] = False
        st.session_state[KEY_PLAYER_CURSOR] = 0

    if tempo == "instant":
        _render_timeline(timeline, cursor=None)
        with st.expander("Show the working: every model call, and what it was shown"):
            _render_working(timeline, cursor=None)
        return

    # A run that finished days ago opens on its *final* state, not on its first frame. Defaulting
    # the cursor to zero drew a second rail reading "working, waiting, waiting" underneath a
    # finished verdict, which looks like a run still going rather than a player waiting to be
    # pressed. Play sets the cursor to zero itself, so animating from the start still works.
    stored = st.session_state.get(KEY_PLAYER_CURSOR)
    cursor = int(stored) if stored is not None else None
    _render_timeline(timeline, cursor=cursor)
    # Behind the same disclosure the live panel uses, for the same reason: a page that ends in a
    # cassette key and a token count has buried its own verdict under its own diagnostics.
    with st.expander("Show the working: every model call, and what it was shown"):
        _render_working(timeline, cursor=cursor)

    if st.session_state.get(KEY_PLAYER_PLAYING):
        multiplier = float(tempo.rstrip("x"))

        @st.fragment(run_every=0.2)
        def _advance() -> None:
            advanced = (cursor or 0) + int(200 * multiplier)
            st.session_state[KEY_PLAYER_CURSOR] = min(advanced, timeline.total_display_ms)
            if advanced >= timeline.total_display_ms:
                st.session_state[KEY_PLAYER_PLAYING] = False
            st.rerun(scope="app")

        _advance()


def _past_runs(artifacts_root: Path) -> list[Path]:
    """Runs this replay panel may offer - every run on disk for a single-operator deployment,
    otherwise only the ones this browser session itself started. See
    `tda.review.app.single_operator` for why the second is the default."""
    if not artifacts_root.is_dir():
        return []
    from tda.obs.artifacts import RUN_LEDGER

    visible = single_operator()
    known: set[str] = st.session_state.get(SESSION_KNOWN_RUNS, set())
    runs = [
        d
        for d in artifacts_root.iterdir()
        if d.is_dir() and (d / RUN_LEDGER).is_file() and (visible or d.name in known)
    ]
    return sorted(runs, key=lambda d: d.stat().st_mtime, reverse=True)
