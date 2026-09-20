"""The pages themselves, driven the way a viewer would drive them: click, wait, read.

Everything the console depends on - scenes, staging, the worker, the timeline, what an agent was
shown, grading - is tested without a browser elsewhere. What can only be proven here is that the
widgets are wired to those modules correctly: pressing Run actually starts a job, the chips actually
turn green, the buttons that should appear once a job is done actually do.

`streamlit.testing.v1.AppTest` runs the real script; nothing here is mocked. A replay run against
the demo corpus takes well under the timeout used throughout, so `default_timeout=60` is generous
rather than tight.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from streamlit.testing.v1 import AppTest

from tda.review import ui
from tda.review.runner import wait

if TYPE_CHECKING:
    import pytest

    from tda.review.runner import RunJob

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "streamlit_app.py"
REVIEW_SCRIPT = REPO_ROOT / "src" / "tda" / "review" / "app.py"


def _run_page(
    artifacts: Path, monkeypatch: pytest.MonkeyPatch, *, uploads: bool = False
) -> AppTest:
    """The Run page as a deployment actually serves it. `uploads` defaults to off because that is
    what a hosted deployment gets: `tda.review.app.uploads_enabled` is unset there, so the console
    offers prepared scenes and nothing else. A test that needs the upload path says so, the same
    way `docker compose` and `make review` say so."""
    monkeypatch.setenv("MIZAN_ARTIFACTS", str(artifacts))
    monkeypatch.delenv("MIZAN_RUN", raising=False)
    monkeypatch.delenv("MIZAN_SUBMISSION", raising=False)
    monkeypatch.delenv("MIZAN_SINGLE_OPERATOR", raising=False)
    if uploads:
        monkeypatch.setenv("MIZAN_UPLOAD_ENABLED", "true")
    else:
        monkeypatch.delenv("MIZAN_UPLOAD_ENABLED", raising=False)
    at = AppTest.from_file(str(ENTRYPOINT), default_timeout=60)
    at.run()
    return at


def _review_page(artifacts: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    """The review screen alone, run directly rather than through `st.navigation` - a fresh,
    independent `st.session_state` standing in for a browser session that never touched the
    console, exactly what a viewer following a bookmarked `/review` link would be.

    `artifacts` must be the directory a console run actually used, read from `RunJob.
    artifacts_root` rather than assumed to be whatever `MIZAN_ARTIFACTS` a test set: `app.py`'s own
    `ARTIFACTS` constant is read once, the first time `tda.review.app` is imported by *any* test in
    this process, and `streamlit_app.py` reaches it through a cached `import` that later
    `monkeypatch.setenv` calls do not reopen - AppTest only re-execs the file it was pointed at
    fresh on every run, which for `_run_page` is `streamlit_app.py`, not the module it imports.
    """
    monkeypatch.setenv("MIZAN_ARTIFACTS", str(artifacts))
    monkeypatch.delenv("MIZAN_RUN", raising=False)
    monkeypatch.delenv("MIZAN_SUBMISSION", raising=False)
    at = AppTest.from_file(str(REVIEW_SCRIPT), default_timeout=60)
    at.run()
    return at


def _run_scene(at: AppTest, key: str) -> AppTest:
    """Click a scene's own run button.

    The scenes used to be titles in a `st.selectbox` with a separate Run button below, so a test
    selected and then clicked. They are cards now, each with its own button, because a viewer
    reading five descriptions should not have to choose before being told what they are choosing
    between. Addressed by scene key rather than by label: every card's button says the same words.
    """
    return next(b for b in at.button if b.key == f"scene-{key}").click().run()


def _stage_states(at: AppTest) -> list[str]:
    """The word under each stage on the rail, read semantically.

    These assertions used to match `"background:#2f9e44"` against raw markdown, which pinned the
    test to one hex in one hand-written `<div>`. That is how the rail ended up unreadable in dark
    mode without a single test noticing: the colour was the thing asserted, so the colour was the
    thing nobody could change. `st.badge` renders as a markdown directive (`:green-badge[done]`),
    so what a viewer actually reads is what this reads.
    """
    states = []
    for md in at.markdown:
        for word in ui.STATE_WORDS.values():
            if f"-badge[{word}]" in md.value:
                states.append(word)
    return states


def _job(at: AppTest) -> RunJob | None:
    # Not state.get("console.job"): AppTest's session-state proxy has no real .get method, and
    # reaching for one through attribute access raises rather than returning the value.
    state = at.session_state
    return state["console.job"] if "console.job" in state else None  # noqa: SIM401


def _wait_for_job(at: AppTest, *, rounds: int = 20) -> RunJob:
    """Wait for the job to appear, then join its real worker thread rather than guess at its
    progress by spin-calling `at.run()`: `at.run()` has no sleep between calls, so a fixed round
    count only bounds wall-clock time when nothing else in the process is competing for the GIL -
    exactly the assumption a full test-file run (several prior jobs' daemon threads still winding
    down) breaks. `runner.wait()` blocks on the thread itself, which is correct regardless of load.
    """
    job = None
    for _ in range(rounds):
        job = _job(at)
        if job is not None:
            break
        at.run()
    assert job is not None, "no job appeared in session state after clicking Run"
    wait(job, timeout=60)
    at.run()  # one more full rerun so the verdict panel, gated on job.done, renders
    assert job.done, f"job did not finish within the join timeout (state={job.state})"
    return job


def test_the_entrypoint_renders_the_run_page_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _run_page(tmp_path, monkeypatch)

    assert not at.exception
    assert next((b for b in at.button if b.key == "scene-clean-quarter"), None) is not None


def test_the_run_page_lists_the_scenes_and_a_replay_only_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _run_page(tmp_path, monkeypatch)

    shown = " ".join(md.value for md in at.markdown)
    for title in (
        "A clean quarter",
        "A mistyped guest count",
        "Occupancy under a different definition",
        "A missing monthly report",
        "A nationality label nobody taught the system",
    ):
        assert title in shown, f"{title!r} should be readable on the page, not hidden in a picker"
    assert len([b for b in at.button if b.key and b.key.startswith("scene-")]) == 5
    provider_box = next(sb for sb in at.selectbox if sb.label == "Provider")
    assert provider_box.options == ["replay"]


def test_a_default_deployment_offers_no_way_to_upload_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a public link serves. `tda.review.app.uploads_enabled` is unset on a hosted
    deployment, so the upload path is not merely refused, it is unreachable: no submission radio
    to choose it with, no uploader to put a file in, and nothing that could start a verification
    subprocess. The scene selector is still there, because the scenes are what that audience came
    for."""
    at = _run_page(tmp_path, monkeypatch)

    assert not at.exception
    assert not [r for r in at.radio if r.label == "Submission"]
    assert not list(at.file_uploader)
    assert [b for b in at.button if b.key and b.key.startswith("scene-")]
    assert not any("Do not upload" in c.value for c in at.caption), (
        "a page with no uploader must not warn about uploading"
    )


def test_a_trusted_deployment_offers_the_upload_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same switch: `docker compose` and `make review` set the flag, and
    there the three uploaders are present. The path itself is unchanged and still carries every
    round of `tda.review.sandbox`'s containment work."""
    at = _run_page(tmp_path, monkeypatch, uploads=True)

    assert not at.exception
    assert next((r for r in at.radio if r.label == "Submission"), None) is not None
    next(r for r in at.radio if r.label == "Submission").set_value("Your own files").run()
    labels = {u.label for u in at.file_uploader}
    assert labels == {"Workbook (.xlsx)", "Monthly reports (.pdf)", "Inventory (.csv, optional)"}


def test_the_clean_quarter_scene_runs_to_a_pass_on_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _run_page(tmp_path, monkeypatch)
    _run_scene(at, "clean-quarter")

    job = _wait_for_job(at)
    assert not at.exception
    assert job.status == "PASS"

    states = _stage_states(at)
    assert states.count("done") >= 5, (
        f"every one of the five stages should read done on a clean pass, got {states}"
    )

    mapping_card = next(ex for ex in at.expander if "mapping" in ex.label)
    assert "🟢" in mapping_card.label

    buttons = {b.label for b in at.button}
    assert "Open in Review" in buttons
    assert "Grade the prose" in buttons

    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Claims checked"] == "94"
    assert metrics["Hotel errors"] == "0"


def test_the_run_button_cannot_start_a_second_job_while_one_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _run_page(tmp_path, monkeypatch)
    _run_scene(at, "clean-quarter")
    first_job = _job(at)
    assert first_job is not None

    # Pressing it again while the first job is still (or already) recorded must not replace it
    # with a second one - every scene button is disabled once a job exists and is not done, and if
    # it finished too fast to observe that, clicking again is still only one job in state.
    again = next(b for b in at.button if b.key == "scene-clean-quarter")
    if not again.disabled:
        again.click().run()
    second_job = _job(at)
    assert second_job is not None
    assert second_job is first_job or second_job.run_id == first_job.run_id


def test_the_missing_report_scene_rejects_with_zero_agent_cards_lit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _run_page(tmp_path, monkeypatch)
    _run_scene(at, "missing-report")
    job = _wait_for_job(at)

    assert job.status == "REJECTED"
    lit = [ex for ex in at.expander if ex.label.startswith("🟢")]
    assert lit == [], "a rejected submission must never reach an agent"


def test_the_replay_panel_animates_a_past_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _run_page(tmp_path, monkeypatch)
    _run_scene(at, "clean-quarter")
    _wait_for_job(at)

    tempo = next(s for s in at.select_slider if s.label == "Tempo")
    tempo.set_value("instant").run()
    run_picker = next(sb for sb in at.selectbox if sb.label == "Run")
    assert run_picker.options  # the just-finished run is available to replay

    play = next(b for b in at.button if b.label == "Play")
    play.click().run()

    assert not at.exception
    states = _stage_states(at)
    assert states.count("done") >= 5, (
        f"instant tempo must show every stage's final state at once, got {states}"
    )


def test_the_review_page_opens_a_console_made_run_and_finds_its_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _run_page(tmp_path, monkeypatch)
    _run_scene(at, "clean-quarter")
    job = _wait_for_job(at)

    open_review = next(b for b in at.button if b.label == "Open in Review")
    open_review.click().run()

    assert not at.exception
    body = " ".join(md.value for md in at.markdown)
    assert "PASS" in body
    assert str(job.submission) in " ".join(c.value for c in at.caption)


def test_a_key_in_the_environment_never_reaches_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = "sk-ant-api03-" + "EXAMPLE" * 4
    monkeypatch.setenv("MIZAN_LIVE_MODE", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", planted)

    at = _run_page(tmp_path, monkeypatch)

    assert not at.exception
    seen = "\n".join(
        [md.value for md in at.markdown]
        + [c.value for c in at.caption]
        + [str(sb.options) for sb in at.selectbox]
    )
    assert planted not in seen


def test_a_second_browser_session_cannot_open_the_first_ones_run_on_the_review_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G6's B1 finding, made concrete: one process's `artifacts/` serves every viewer, and by
    default `tda.review.app.single_operator()` is off, matching `tda.review.live`'s own
    off-unless-told-otherwise direction. A viewer who never used the console - one who opened a
    bookmarked `/review` link, say - must not find another viewer's run in the picker."""
    made = _run_page(tmp_path, monkeypatch)
    _run_scene(made, "clean-quarter")
    job = _wait_for_job(made)

    other_viewer = _review_page(job.artifacts_root, monkeypatch)

    assert not other_viewer.exception
    assert other_viewer.selectbox == []
    body = " ".join(e.value for e in other_viewer.error)
    assert job.run_id not in body
    assert "shows each viewer only the runs they made themselves" in body


def test_single_operator_mode_shows_every_run_to_a_fresh_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trusted-deployment escape hatch `docker/compose.yaml` and `make review` both set: one
    person, one machine, every run on disk is theirs to open."""
    made = _run_page(tmp_path, monkeypatch)
    _run_scene(made, "clean-quarter")
    job = _wait_for_job(made)

    monkeypatch.setenv("MIZAN_SINGLE_OPERATOR", "true")
    operator = _review_page(job.artifacts_root, monkeypatch)

    assert not operator.exception
    run_picker = next(sb for sb in operator.selectbox if sb.label == "Run")
    assert job.run_id in run_picker.options


def test_a_second_browser_sessions_replay_panel_does_not_offer_the_first_ones_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The console's own replay panel is the second half of B1: it lists runs the same way the
    review screen's picker does, and needs the same scoping."""
    made = _run_page(tmp_path, monkeypatch)
    _run_scene(made, "clean-quarter")
    job = _wait_for_job(made)

    other_viewer = _run_page(tmp_path, monkeypatch)

    assert not other_viewer.exception
    run_pickers = [sb for sb in other_viewer.selectbox if sb.label == "Run"]
    assert run_pickers == []
    assert any("No past runs yet" in c.value for c in other_viewer.caption)
    assert job.run_id not in " ".join(c.value for c in other_viewer.caption)


def test_an_uploaded_submission_runs_sandboxed_and_reaches_the_same_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one path every round of the G5 security review was about: an upload runs `mizan run`
    as its own resource-limited subprocess (`tda.review.sandbox`), not on this thread, and this
    page has no live per-node view into it while it runs. Uploading the demo corpus's own real
    files - the same bytes every scene-based test already proves reconcile to PASS - through
    "Your own files" rather than picking a scene should reach the identical verdict, by the
    sandboxed path instead of the in-process one."""
    demo = REPO_ROOT / "corpus" / "demo" / "submission"
    at = _run_page(tmp_path, monkeypatch, uploads=True)

    next(r for r in at.radio if r.label == "Submission").set_value("Your own files").run()

    next(u for u in at.file_uploader if u.label == "Workbook (.xlsx)").upload(
        "claims_2026-Q1.xlsx",
        (demo / "claims_2026-Q1.xlsx").read_bytes(),
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    reports = next(u for u in at.file_uploader if u.label == "Monthly reports (.pdf)")
    for name in ("pms_2026-01.pdf", "pms_2026-02.pdf", "pms_2026-03.pdf"):
        reports.upload(name, (demo / name).read_bytes(), mime_type="application/pdf")
    next(u for u in at.file_uploader if u.label == "Inventory (.csv, optional)").upload(
        "inventory_2026-Q1.csv", (demo / "inventory_2026-Q1.csv").read_bytes(), mime_type="text/csv"
    )
    at.text_input(key="console.hotel").set_value("MZN-DXB-001")
    at.text_input(key="console.period").set_value("2026-Q1")
    at.run()

    next(b for b in at.button if b.label == "Run").click().run()

    assert any("Verifying your submission" in i.value for i in at.info), (
        "an upload should show the sandboxed 'verifying' state, not a live per-node view"
    )

    job = _wait_for_job(at)
    assert job.sandboxed
    assert not at.exception
    assert job.status == "PASS"

    states = _stage_states(at)
    assert states.count("done") >= 5, (
        f"the completed sandboxed run renders every stage at once, all done, got {states}"
    )
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Claims checked"] == "94"
    assert metrics["Hotel errors"] == "0"


def test_a_contained_sandbox_kill_shows_the_reason_instead_of_crashing_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A G5 security review found `_sandboxed_panel` called `timeline_from_run` unconditionally
    once a job was done, including a job the resource limit or the wall-clock timeout killed
    before it wrote a single file - `read_run` raises `FileNotFoundError` on a missing `run.json`,
    which reached the page as an uncaught exception on exactly the path this whole mechanism exists
    to serve. Forced here with a one-second timeout against the real demo corpus, the same
    construction `tests/unit/test_runner.py::test_a_sandbox_failure_itself_is_read_back_as_failed`
    forces below the browser."""
    monkeypatch.setenv("MIZAN_SANDBOX_TIMEOUT_SECONDS", "1")
    demo = REPO_ROOT / "corpus" / "demo" / "submission"
    at = _run_page(tmp_path, monkeypatch, uploads=True)

    next(r for r in at.radio if r.label == "Submission").set_value("Your own files").run()
    next(u for u in at.file_uploader if u.label == "Workbook (.xlsx)").upload(
        "claims_2026-Q1.xlsx",
        (demo / "claims_2026-Q1.xlsx").read_bytes(),
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    reports = next(u for u in at.file_uploader if u.label == "Monthly reports (.pdf)")
    for name in ("pms_2026-01.pdf", "pms_2026-02.pdf", "pms_2026-03.pdf"):
        reports.upload(name, (demo / name).read_bytes(), mime_type="application/pdf")
    at.text_input(key="console.hotel").set_value("MZN-DXB-001")
    at.text_input(key="console.period").set_value("2026-Q1")
    at.run()

    next(b for b in at.button if b.label == "Run").click().run()

    job = _wait_for_job(at)
    assert job.sandboxed
    assert not at.exception, "a contained kill must render an error, never crash the page"
    assert job.status == "FAILED"
    assert any("did not finish" in e.value for e in at.error), (
        "the sandbox's own reason should reach the verdict panel"
    )
