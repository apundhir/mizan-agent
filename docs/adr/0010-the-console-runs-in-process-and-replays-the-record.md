# ADR-0010 · The Run console runs in process, and replays the same record a finished run would

- **Status:** Accepted
- **Date:** 2026-09-18
- **Related:** [ADR-0004](0004-agent-runtime.md) · [ADR-0006](0006-observability-redaction-and-recorded-runtime.md) · [ADR-0008](0008-the-review-gate-is-headless-and-the-evidence-is-cropped.md)
- **Issues:** the hosted console

## Context

the hosted console asks for a viewer-facing demo: pick a scene or upload a submission, watch the agents work,
read the verdict, hand off to the review gate ADR-0008 already built. Three facts turned up while
reading the code for that story, and each one narrowed what "watch the agents work" could honestly
mean.

**The supervisor existed and was not on the call path.** `src/tda/agents/supervisor.py` had a
budget, a log and a full test suite, and no runner constructed one. A console showing "routing
decisions" against that code would have been showing decisions nobody made.

**No trace record carries request text.** `TraceRecord` holds a cassette key and the output; the
prompt itself was never a field to begin with, deliberately, so "what was this agent shown" is a
question this codebase had never had to answer live.

**A decision is append-only.** `record_decision` (ADR-0008 §7) keeps a rejected and then accepted
decision as two records, not one overwritten in place. A console re-running the same demo scene
twice was going to produce two runs with two run ids, never one run recorded twice.

Given those, three questions needed a decision before anything could be built: where the pipeline
actually executes while a viewer watches, how the live view stays truthful about a run still in
progress, and how a page that can now reach a real model API is kept from becoming an unbounded
way to spend somebody's key.

## Decision

### 1 · A prepared scene runs in process, on a background thread

`tda.review.runner.start` runs `verify_directory` on a daemon thread and returns the `RunJob`
immediately; the page polls it through a `st.fragment`. No subprocess, no job queue, no separate
worker service, for a scene's own files - this repository's committed corpus, already trusted
before the console existed. Streamlit already owns the process the viewer is talking to, and the
alternative, shipping run state to a queue and back, buys isolation a known, committed input does
not need. §7 below covers the one input this reasoning does not extend to: a viewer's own upload.

### 2 · A console run is self-contained, and stages under the id it will finish under

`prepare_job` accepts a pre-minted `run_id` so the console can build
`artifacts_root/<run_id>/submission/` before the job that will read it exists. The two must name
the same directory, or the review gate opens on a run whose evidence lives somewhere else. This
was caught as a real bug during manual testing, before it reached committed code: staging and the
job each minted their own id.

### 3 · The live view and a replayed view are one function apart

`tda.review.timeline.build_timeline` turns `nodes.jsonl`, `trace.jsonl` and `routing.jsonl` (§9 of
`03-architecture.md`) into the events the page renders, and `timeline_from_job` /
`timeline_from_run` are the same construction over a job still running and a run already on disk.
A chip turns green because a node exit was logged, not because a second, live-only code path
decided the pipeline looked finished. The replay panel's tempo control animates the same events at
a chosen speed; it does not re-run anything.

### 4 · What an agent was shown is recovered, never stored

`tda.review.shown` answers "what was this agent asked" without a request-text field to read.
Replay: the cassette's own `request_canonical` (ADR-0002's record/replay boundary already carries
this). Live, for the one agent that can run live today: rebuilt from the workbook by the exact
function that built the original request, `render_message`, and proven equal to what a cassette
recorded by a test, not asserted by comment. Narrative, critic and reviewer-assist never need
reconstruction, because the console only ever calls them through a provider that is either replay
or, when live mode allows it, the same adapter every other live call uses.

### 5 · Live model access is off by default and bounded twice over

`tda.review.live` is the only module in this codebase that names `ANTHROPIC_API_KEY`. Refused
unless `MIZAN_LIVE_MODE` says otherwise, and refused rather than silently downgraded to replay, so
a misconfigured deployment shows an error instead of quietly working the safe way. Two independent
caps sit in front of a live run: one per browser session, and one per server process, because a
new browser tab resets `st.session_state` and, without the second cap, would reset the first cap
with it. Neither substitutes for `policy.yaml`'s own per-run budget, which still bounds what one
granted run may do once it starts.

### 6 · Grading the prose, on demand, degrades one finding at a time

`tda.review.prose.grade_verdict` catches a missing cassette per finding and reports it as
`not_recorded`, rather than letting the exception propagate the way `tda.eval.narrative`'s own
grading function correctly does for a batch scoring run. `make eval` should stop and say a case is
unmeasured; a console viewer who presses "Grade the prose" on a run with one ungraded finding
should still see the other findings graded, because nothing about the button implied every
finding would have a cassette.

### 7 · An uploaded submission runs in a resource-limited subprocess, not on that background thread

Decision 1 holds for a scene. It does not extend to an upload, and getting there took five rounds
of a G5 security review rather than one design conversation.

`tda.review.staging` bounded an upload's compressed size, then its decompressed size, then its
declared cell count three different ways, and a real, working proof of concept defeated each
bound in turn: a namespace-prefixed cell tag past a naive `<c ` count, a merge range that needs no
`<c>` element at all, a `<dimension>` hint that lied by being merely wrong rather than absent, and
finally a `<hyperlink ref="A1:XFD1048576">` that drove a 1.5-kilobyte upload past a gigabyte of
resident memory. Each fix closed the exact construct demonstrated and left the next one open,
because every one of them was reading the *shape* of an OOXML file and guessing what `openpyxl`
would build from it - and `openpyxl`'s reader has enough range-expanding methods (merges,
hyperlinks, whatever the next one turns out to be) that enumerating them is chasing the library's
internals rather than bounding the cost. The reviewer's own conclusion on the fifth round, put to
the user as the choice it was rather than made silently: keep patching individual constructs, ship
without uploads for now, or stop predicting what a file will cost to open and bound what the
process opening it is *allowed* to use. The user chose the resource-limit backstop.

`tda.review.sandbox.run_sandboxed` runs `mizan run` as a genuine child process for an upload's
verification, with `RLIMIT_AS` and `RLIMIT_CPU` applied to itself before doing anything else, plus
a wall-clock timeout on top for whatever a CPU limit alone would not catch - a process blocked on
I/O rather than spinning. A file that would have exhausted the shared Streamlit process instead
exhausts its own child's ceiling and is killed; `staging`'s checks still run first and still
refuse the large, cheap-to-detect class of upload before a subprocess is even started, so this is
a backstop layered under them, not a replacement for them.

Two problems specific to a subprocess showed up during this build, neither prompted by the
review, both caught before they reached the deployed app:

- **`RLIMIT_AS` is unusable on macOS.** Measured directly, not assumed: an ordinary Python process
  reserves on the order of hundreds of gigabytes of virtual address space from allocator and
  dynamic-linker conventions alone before doing any real work, which makes any sane `RLIMIT_AS`
  value fail the child immediately rather than bound it usefully. Applied only under
  `sys.platform.startswith("linux")` - the platform both Streamlit Community Cloud and this
  repository's own Docker image actually run on, and the one the same measurement puts an ordinary
  run under a gigabyte for.
- **A child process does not inherit `sys.path`.** `streamlit_app.py` puts `src/` on `sys.path` at
  import time, an in-process patch `subprocess.run` never carries into a fresh interpreter. Traced
  through rather than assumed: a container built the way Streamlit Community Cloud actually
  installs a deployment, `requirements.txt` alone, no `pip install -e .`, cannot `import tda` at
  all, and `python -m tda.cli` fails with `ModuleNotFoundError` without `PYTHONPATH` naming `src/`
  explicitly. `tda.review.sandbox._child_env` builds that `PYTHONPATH` for the child regardless of
  what the parent process happened to have. Left unfixed, this would have been a silent, complete
  production failure of the upload feature - every upload would have failed with an opaque
  subprocess error, on the one platform this feature ships to, invisible to `make ci` because the
  test suite runs where `tda` already is a real package.

### A sixth review, of this mechanism itself

A BLOCK, on the mechanism this section describes rather than on a fresh construct `staging` had
not yet bounded - the first review of `sandbox.py` since it existed. Command injection, path
traversal and TOCTOU on the submission were all checked and held; what did not hold:

- **The console crashed on the sandbox's own success path.** A subprocess a resource limit or the
  wall-clock timeout killed writes nothing, not even `run.json` - `_sandboxed_panel` called
  `timeline_from_run` unconditionally once the job was done, which raised `FileNotFoundError`
  straight through the page, reproduced against a real killed subprocess. Fixed by checking the
  ledger file's presence directly before reading it, rather than inferring from `job.state` alone
  (`run.json` can exist for a `COULD_NOT_RUN` outcome, which is also `FAILED`).
- **The parent still ran the expensive, unsandboxed parse `staging` was supposed to have stopped
  doing.** `_checked_xlsx_content`'s cell count and merge-range sum each opened the archive with
  `openpyxl` to answer a question only the sandboxed `open_submission` could actually answer, at a
  measured cost of several seconds of CPU and hundreds of megabytes in this process, on a payload
  sized to clear the byte cap by design. Removed rather than patched a sixth time: `staging` now
  bounds only what `zipfile.infolist()`'s metadata can answer without opening the archive, and
  everything past that is the sandbox's alone to contain.
- **The memory ceiling had never been checked against the platform's own published range**, and
  nothing bounded how many sandboxed children could run at once - both closed in `tda.review.
  sandbox` (a lower default, a concurrency cap) and flagged in the runbook as needing re-
  verification against the real deployed container, not a number this repository can claim without
  deploying it.
- **`preexec_fn` ran in a forked-but-not-yet-exec'd copy of a process the console's own background
  worker thread makes multithreaded** - the exact hazard the `subprocess` module's own
  documentation warns about. Moved into the child itself: `mizan run` now takes `--max-memory-
  bytes`/`--max-cpu-seconds` and applies them to itself immediately after parsing arguments, once
  fully exec'd, where no fork/exec window exists to worry about.
- **A kill reported after `mizan run` had already written a real verdict** would have been read
  back as nothing having run at all - `_execute_sandboxed` trusted `outcome.ok` before checking
  disk. Fixed by checking for `verdict.json` first, regardless of what the subprocess's own exit
  was reported as.
- Smaller findings closed in the same pass: `ANTHROPIC_API_KEY` no longer reaches a replay or stub
  run's child, which never needs it; `--hotel` and friends are passed `=`-joined so a viewer-typed
  value cannot be mistaken for a second flag; a malformed sandbox environment variable now fails
  the run loudly rather than silently substituting the default; the child inherits neither the
  parent's working directory nor an unbounded stdout/stderr pipe.
- **A third bug, self-found verifying the fix on real Linux, not prompted by either review**: a
  lower memory ceiling made the hyperlink-range bomb's own `MemoryError` reach `mizan run`'s
  failure path cleanly, then made *building the failure ledger itself* - digesting every input
  file, real allocation - throw a second `MemoryError` once RLIMIT_AS was already essentially
  spent, unhandled, exiting with Python's own default code for an uncaught exception (`1`) rather
  than `COULD_NOT_RUN` (`2`). Indistinguishable, to anything reading the exit code, from
  `FINDINGS` - a genuine verdict a human must read - which it was not. `tda.cli.main`'s failure
  path now wraps building and writing that ledger in its own `try`/`except`, so a second exception
  there is reported and answered with `COULD_NOT_RUN`, not left to crash past it.

## Enforcement

| Decision | What holds it in place |
|---|---|
| Staging and the job agree on one run id | `tests/unit/test_console_pages.py::test_the_review_page_opens_a_console_made_run_and_finds_its_submission`, driven through `streamlit.testing.v1.AppTest` against the real script, not a mock of it |
| The worker writes what the CLI writes, on the same submission | `tests/unit/test_runner.py`, comparing a console run's artefacts against `mizan run`'s own, both against committed cassettes |
| A `BaseException` from the provider never escapes the background thread | `tests/unit/test_runner.py`, a fake provider that raises inside `execute` |
| Live and replayed timelines agree | `tests/unit/test_timeline.py`, building both from one recorded run and diffing the events |
| The recomputed mapping request equals the cassette's own | `tests/unit/test_shown.py`, the "identical to what was sent" claim asserted rather than stated |
| A contact detail in a header cell is redacted on the agent card, not only in the written artefact | `tests/unit/test_shown.py` and `tests/unit/test_console.py` |
| `anthropic` is refused before any SDK import when live mode is off | `tests/unit/test_live_gate.py`, a subprocess probe |
| A planted key never reaches a rendered element | `tests/unit/test_console_pages.py::test_a_key_in_the_environment_never_reaches_the_page` |
| A fourth live run in one session, and a live run past the process cap, are both refused | `tests/unit/test_live_gate.py` |
| A missing cassette yields one `not_recorded` row, not a blank grading panel | `tests/unit/test_prose.py` |
| A rejected submission reaches zero agents | `tests/unit/test_console_pages.py::test_the_missing_report_scene_rejects_with_zero_agent_cards_lit` |
| The Run button cannot start a second job while one runs | `tests/unit/test_console_pages.py::test_the_run_button_cannot_start_a_second_job_while_one_runs` |
| A resource-limit bomb that defeated `staging`'s own checks is contained by the process ceiling | `tests/unit/test_sandbox.py::test_a_hyperlink_range_memory_bomb_is_contained_by_the_process_limit`, Linux-only (the platform the limit actually holds on), against the exact `<hyperlink ref="A1:XFD1048576">` construction the fifth review round demonstrated |
| The sandboxed child can import `tda` with only `requirements.txt` installed, matching Streamlit Community Cloud exactly | `tests/unit/test_sandbox.py::test_the_child_gets_src_on_pythonpath_regardless_of_what_was_already_there`, plus a manual Docker verification against a container with no `pip install -e .` step |
| A sandboxed job's context never fills in - genuine process isolation, not a shared-memory illusion | `tests/unit/test_runner.py::test_a_sandboxed_jobs_context_never_fills_in` |
| A sandboxed run's verdict, and a sandboxed rejection, read back the same way the CLI's own artefacts would | `tests/unit/test_runner.py::test_a_sandboxed_job_completes_and_the_verdict_is_read_back`, `test_a_sandboxed_runs_rejection_is_read_back_correctly` |
| `sandbox.py` can copy `os.environ` for the child without being able to render it | `tests/unit/test_console.py::test_the_file_allowed_to_copy_the_environment_cannot_render_it` - it never imports Streamlit, checked structurally, not asserted by comment |
| A contained kill renders an error, never crashes the page | `tests/unit/test_console_pages.py::test_a_contained_sandbox_kill_shows_the_reason_instead_of_crashing_the_page`, a real subprocess timed out through a real browser-shaped page render |
| A verdict written just before a reported kill is read back as the finished run it is, not a failure | `tests/unit/test_runner.py::test_a_verdict_written_just_before_a_reported_kill_is_still_read_back` |
| The live credential reaches the child only on an `anthropic` run | `tests/unit/test_sandbox.py::test_the_live_credential_reaches_the_child_only_when_the_run_can_use_it` |
| A third concurrent sandboxed upload is refused, and a released slot is usable again | `tests/unit/test_sandbox.py::test_sandbox_concurrency_refuses_past_its_limit_and_a_release_frees_the_slot`, `test_run_sandboxed_refuses_a_third_upload_past_the_concurrency_cap` |
| A namespace-prefixed cell, a merge needing no `<c>` element, and an understated `<dimension>` hint are no longer refused by `staging` - the sandbox is what contains them now | `tests/unit/test_staging.py::test_a_cell_dense_or_range_expanding_workbook_under_the_byte_cap_is_accepted_here`, a deliberate behaviour change pinned so nobody re-adds the removed check believing it fixes a regression |

## Consequences

### Accepted costs

- **Nothing survives a reboot.** Streamlit's own documentation states that Community Cloud "does
  not guarantee the persistence of local file storage" and that the platform "may delete data
  stored using this technique at any time" (docs.streamlit.io, deploy/streamlit-community-cloud,
  file-organization page, checked 2026-09-19) - a stronger and less predictable claim than "gone at
  the next restart," so treating `artifacts/` as gone at any moment, not only at a reboot, is the
  reading this codebase and the runbook both take.
- **A console run duplicates files a scene already has committed.** Staging copies a scene's PDFs
  and workbook into the run's own directory rather than pointing the pipeline at the shared demo
  copy, so five people running the same scene write five copies. Simplicity over disk: the
  alternative is a submission directory that is sometimes owned by this run and sometimes borrowed
  from another, and `run.json`'s per-file digest (§9) exists precisely so a submission is never
  ambiguous about which bytes it verified.
- **The session cap is a per-tab speed bump, not a hard ceiling on its own.** It resets the moment
  a viewer opens a new tab; the process cap is what actually bounds a deployment's total live
  spend between reboots, and it is the one of the two that matters for cost.
- **Grading the prose makes two more model calls per finding**, on a page that otherwise runs for
  free in replay. It is opt-in for exactly that reason: nothing before the button is pressed
  spends anything.
- **An uploaded submission gives up the live, node-by-node view a scene gets.** The console shows
  one "verifying your submission" state while the sandboxed child runs, then the complete timeline
  at once, read back from the same files `mizan run` always writes - not a compromise invented for
  this feature, the same rendering the replay panel already uses for a run made earlier. A viewer
  who uploads a file watches less than a viewer who picks a scene; the trade is for a real
  isolation guarantee no amount of content inspection converged on.
- **A third viewer uploading at the same moment is refused, not queued.** `MIZAN_SANDBOX_MAX_CONCURRENT`
  (default 2) bounds how many sandboxed children this process will run together, closing the gap a
  sixth G5 review found: nothing previously stopped N concurrent uploads from becoming N children
  each allowed up to the memory ceiling. A clear refusal ("try again in a moment") on a rare third
  concurrent upload is preferred over letting that contention risk the container the way the
  unbounded version could - correct for a synthetic-data demo's own traffic, not a claim this
  would still be the right number under real multi-tenant load.

### What is bought

A viewer watches the real pipeline reach a real verdict, not a recording of one: the same
supervisor, the same budget, the same redaction, the same written artefacts the review gate reads.
Replay and live share one code path throughout, so a bug fixed for one is fixed for both, and nothing
about what the console shows was built to be shown rather than built to be true.

### What this does not claim

Not a job queue: two jobs cannot run in one session, but nothing coordinates jobs across sessions
in the same process. Not authentication beyond Streamlit Community Cloud's own viewer invitations.
Not a persistence layer: a run worth keeping has to be exported before the app next restarts. Not
a claim that live mode is free to leave on; it is bounded, not eliminated as a cost.

## Alternatives considered

| Alternative | Why it lost |
|---|---|
| Read existing logs instead of wiring the supervisor in | Considered and rejected before implementation: a routing panel built over decisions nobody made is a prop, not a demo of the thing the hosted console asks to show |
| A subprocess or external worker for a scene's own run | Buys isolation a known, committed input does not need, at the cost of shipping run state across a process boundary this codebase would then have to keep in sync - the isolation only an upload's untrusted content later turned out to require, in §7 |
| Store request text on `TraceRecord` | Changes a contract every existing writer and reader depends on, to serve one new caller; recovery from the cassette or from the same builder that made the request costs nothing upstream |
| A single process-wide cap only, no per-session cap | A busy demo would let one heavy session exhaust the whole deployment's live budget before a second viewer got a turn |
| Let `grade_verdict` propagate on a missing cassette, matching `make eval` | Correct for a batch scorer that must not report a partial pass; wrong for an on-demand button where one ungraded finding should not blank a panel showing several graded ones |
| Keep enumerating and closing each OOXML construct `staging` did not yet bound | Tried for five rounds; every fix closed the exact construction a review demonstrated and left the next `openpyxl` range-expanding method open, which is chasing a library's internals rather than bounding a cost |
| Ship the console without an upload path, scenes only | Considered and offered to the user as an option after the fifth round; rejected because "upload your own files" is a named part of the hosted console, not an extra |
| Run every console job, scene or upload, in the same resource-limited subprocess | Would pay the lost live-progress cost for a scene too, over an input this codebase already trusted before the console existed; the isolation buys nothing there that decision 1's own reasoning did not already cover |
