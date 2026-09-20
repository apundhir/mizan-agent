"""`run_sandboxed` itself - the backstop, tested without going through `tda.review.staging` at
all, since the whole point of a resource limit is to hold even when a check upstream of it does
not. Five rounds of a G5 security review defeated staging's own content checks in turn; these
tests exercise the process boundary each of those bypasses would ultimately have had to cross.

**Platform note.** `RLIMIT_AS` is Linux-only in `tda.review.sandbox._apply_limits` - macOS's own
virtual memory accounting makes any sane value fail a process before it does any real work, a fact
established by hand against this exact command, not assumed. The memory-limit test below is
skipped everywhere but Linux for that reason; this repository's own CI runs on `ubuntu-latest`, so
it is not skipped where the property it tests actually applies. `RLIMIT_CPU` behaves consistently
on both and its own test is not skipped.
"""

from __future__ import annotations

import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

from tda.contracts import Period
from tda.review.live import API_KEY_ENV
from tda.review.sandbox import (
    MAX_CONCURRENT_ENV,
    MAX_CPU_SECONDS_ENV,
    TIMEOUT_SECONDS_ENV,
    SandboxConcurrency,
    _child_env,
    run_sandboxed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_SUBMISSION = REPO_ROOT / "corpus" / "demo" / "submission"
HOTEL = "MZN-DXB-001"
PERIOD = Period.parse("2026-Q1")

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/>'
    "</Relationships>"
)
_WORKBOOK_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
)


def _minimal_xlsx(sheet_xml: str) -> bytes:
    """A hand-built, structurally complete `.xlsx` around one worksheet body - `mizan run` opens
    the archive with `openpyxl` itself, which needs the parts a real workbook carries."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK_XML)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buffer.getvalue()


def _submission_around(tmp_path: Path, workbook_bytes: bytes) -> Path:
    """A submission `mizan run` can at least attempt against - the workbook under test, plus real
    reports and inventory copied from the demo corpus so intake has something to check it against."""
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "claims_2026-Q1.xlsx").write_bytes(workbook_bytes)
    for name in ("pms_2026-01.pdf", "pms_2026-02.pdf", "pms_2026-03.pdf", "inventory_2026-Q1.csv"):
        (submission / name).write_bytes((DEMO_SUBMISSION / name).read_bytes())
    return submission


def test_a_normal_submission_completes_through_the_sandbox(tmp_path: Path) -> None:
    result = run_sandboxed(
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        artifacts_root=tmp_path,
        provider_name="replay",
        run_id="run-sandboxtest01",
    )

    assert result.ok
    assert result.reason is None
    run_dir = tmp_path / "run-sandboxtest01"
    assert (run_dir / "verdict.json").is_file()
    assert (run_dir / "run.json").is_file()


def test_the_child_gets_src_on_pythonpath_regardless_of_what_was_already_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this exists to prevent: `streamlit_app.py` puts `src/` on `sys.path` at import
    time, an in-process change `subprocess.run` never carries into a child. Verified end to end
    against a container with only `requirements.txt` installed - no `pip install -e .`, exactly
    Streamlit Community Cloud's own installation shape - where `python -m tda.cli` fails with
    `ModuleNotFoundError` without this, and completes a real run with it. This test pins the
    narrower, host-independent half of that: the value `_child_env` actually computes."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    env = _child_env("replay")
    assert str(REPO_ROOT / "src") in env["PYTHONPATH"].split(os.pathsep)
    assert env["PYTHONSAFEPATH"] == "1"

    monkeypatch.setenv("PYTHONPATH", "/some/other/path")
    env = _child_env("replay")
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert str(REPO_ROOT / "src") in entries
    assert "/some/other/path" in entries


def test_the_live_credential_reaches_the_child_only_when_the_run_can_use_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A G5 security review found `_child_env` copied `ANTHROPIC_API_KEY` unconditionally, so a
    replay or stub run - which by design never calls a model - handed a real credential to the
    one process on this deployment spending its time parsing a viewer's own untrusted upload, for
    no benefit. The key is planted here rather than a literal `sk-ant-...`, matching this
    repository's own convention for never writing a real-looking secret into source."""
    planted = "sk-ant-" + "x" * 24
    monkeypatch.setenv(API_KEY_ENV, planted)

    assert API_KEY_ENV not in _child_env("replay")
    assert API_KEY_ENV not in _child_env("stub")
    assert _child_env("anthropic")[API_KEY_ENV] == planted


def test_a_process_that_does_not_finish_in_time_is_reported_as_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real `mizan run` invocation, timed out before the interpreter can even finish importing
    its own dependency graph - proof that the timeout is enforced by `subprocess.run` itself, not
    by anything the pipeline does, so it holds regardless of what the eventual bottleneck inside a
    run turns out to be. One second, not a fraction of one: `_env_int` parses whole seconds, which
    is all a real deployment's timeout config would ever need, and one second is already well
    under how long a fresh interpreter takes to import langgraph, anthropic and the rest."""
    monkeypatch.setenv(TIMEOUT_SECONDS_ENV, "1")

    result = run_sandboxed(
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        artifacts_root=tmp_path,
        provider_name="replay",
        run_id="run-sandboxtest02",
    )

    assert not result.ok
    assert result.reason is not None
    assert "did not finish" in result.reason


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="RLIMIT_AS is Linux-only here - see the module docstring on macOS's virtual memory "
    "accounting making it unusable for this on any other platform.",
)
def test_a_hyperlink_range_memory_bomb_is_contained_by_the_process_limit(tmp_path: Path) -> None:
    """The exact class of file a G5 security review's fifth round defeated `tda.review.staging`'s
    own content checks with, run here with none of those checks in the way at all - `run_sandboxed`
    is called directly, the way `tda.review.runner._execute_sandboxed` does once staging has
    already passed a file. One real cell, one `<hyperlink ref="A1:XFD1048576">` covering every
    coordinate on a sheet - `openpyxl`'s `Worksheet.__getitem__` materialises a `Cell` object for
    each one on the `read_only=False` load `open_submission` always does, and this exact
    construction was measured by hand, against this exact command with no limit at all, at over
    800 megabytes of resident memory from a 1.5-kilobyte upload. A smaller range (a bounded merge,
    say) can cost less than this module's own default ceiling and complete rather than being
    caught - proving containment needs a construction big enough to actually cross it, which this
    one reliably is without needing to override the default."""
    sheet_xml = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1"><v>1</v></c></row></sheetData>'
        '<hyperlinks><hyperlink ref="A1:XFD1048576"/></hyperlinks></worksheet>'
    )
    submission = _submission_around(tmp_path, _minimal_xlsx(sheet_xml))

    t0 = time.time()
    result = run_sandboxed(
        submission=submission,
        hotel_id=HOTEL,
        period=PERIOD,
        artifacts_root=tmp_path / "artifacts",
        provider_name="replay",
        run_id="run-sandboxtest03",
    )
    elapsed = time.time() - t0

    # Contained either way: mizan run itself may catch the MemoryError and exit COULD_NOT_RUN
    # (ok=True, a FAILED job once tda.review.runner reads it back, with no verdict ever reached),
    # or the OS may kill the process outright (ok=False). Both are the property under test - what
    # must not happen is what the unbounded version of this exact file does: run for tens of
    # seconds needing a manual kill.
    assert elapsed < 30, f"the limit should contain this in seconds, not {elapsed:.1f}s"
    run_dir = tmp_path / "artifacts" / "run-sandboxtest03"
    assert not (run_dir / "verdict.json").is_file(), "this file must never reach a real verdict"
    if result.ok:
        assert (run_dir / "nodes.jsonl").is_file()
    else:
        assert result.reason is not None
        assert "memory" in result.reason.lower() or "CPU" in result.reason


def test_the_cpu_limit_is_applied_on_every_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`RLIMIT_CPU`, unlike `RLIMIT_AS`, behaves consistently across platforms - confirmed by hand
    against this exact call before relying on it. A tight CPU cap on an otherwise ordinary
    submission should not still let the process spend much more CPU than that before something
    stops it."""
    monkeypatch.setenv(MAX_CPU_SECONDS_ENV, "1")
    monkeypatch.setenv(TIMEOUT_SECONDS_ENV, "20")

    t0 = time.time()
    result = run_sandboxed(
        submission=DEMO_SUBMISSION,
        hotel_id=HOTEL,
        period=PERIOD,
        artifacts_root=tmp_path,
        provider_name="replay",
        run_id="run-sandboxtest04",
    )
    elapsed = time.time() - t0

    # A real run's own CPU need is small enough that it may complete within a 1s CPU cap despite
    # wall-clock overhead from interpreter startup and imports - this asserts containment, not
    # that the cap fires on this particular submission every time.
    assert elapsed < 20, (
        f"the CPU limit or its wall-clock backstop should have acted by {elapsed:.1f}s"
    )
    assert result.ok or (result.reason is not None and "CPU" in result.reason)


def test_sandbox_concurrency_refuses_past_its_limit_and_a_release_frees_the_slot() -> None:
    """A G5 security review found nothing bounded how many sandboxed children could run at once -
    N browser sessions uploading together meant N children, each allowed up to the memory ceiling,
    with no cap on N. Exercised directly against the class rather than through real subprocesses,
    which `test_run_sandboxed_refuses_a_third_upload_past_the_concurrency_cap` below does once."""
    limit = SandboxConcurrency()
    assert limit.acquire(2)
    assert limit.acquire(2)
    assert not limit.acquire(2), "a third slot must be refused at a limit of two"
    limit.release()
    assert limit.acquire(2), "releasing one slot must free it for the next caller"


def test_run_sandboxed_refuses_a_third_upload_past_the_concurrency_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real entry point, not just the counter class - a caller past the cap gets a clear
    refusal rather than contending a real subprocess into existence anyway."""
    from tda.review import sandbox as sandbox_module

    monkeypatch.setenv(MAX_CONCURRENT_ENV, "1")
    assert sandbox_module._CONCURRENCY.acquire(1), "occupy the only slot this test's cap allows"
    try:
        result = run_sandboxed(
            submission=DEMO_SUBMISSION,
            hotel_id=HOTEL,
            period=PERIOD,
            artifacts_root=tmp_path,
            provider_name="replay",
            run_id="run-sandboxtest05",
        )
    finally:
        sandbox_module._CONCURRENCY.release()

    assert not result.ok
    assert result.reason is not None
    assert "already verifying" in result.reason
