# Changelog

Versioning is `0.<milestone>.<patch>` through the POC — `v0.1.0` at the end of M1, `v0.6.0` at the
end of M6. Semantic versioning of a POC's public API would be a fiction.

## v0.6.0 — The Run console, hosted (PRD-115) · 2026-09-20

A viewer, not just a reader of a committed corpus: pick one of five prepared scenes, watch the
five-node pipeline work, read the verdict with the report rows and the workbook cell side by side,
and hand off to the review gate. Runs on Streamlit Community Cloud with no install, replay-only by
default, no key anywhere unless a deployment turns live mode on, and no uploader unless a
deployment asks for one.

### The demo it had to become

The console worked and did not sell. It opened on ninety words of grey disclaimer with no title,
offered its five scenes through a dropdown so that neither the written description nor the expected
outcome was visible until after choosing, rendered the verdict as bold body text, and listed each
finding as one markdown bullet carrying neither the figures nor the evidence. It had no `[theme]`
block at all, so three unrelated hard-coded palettes fought each other and dark mode was unreadable.

Everything needed to fix that was already in the repository. The scenes had descriptions. The
findings had claimed, computed and difference. `tda.review.evidence` had been cropping the exact
PDF rows and highlighting the workbook cell since M5, and only the second screen ever showed it.

- **A theme**, in `.streamlit/config.toml`, light and dark. The palette is the one `README.md`'s
  diagrams already use, because those colours carry the argument: blue is deterministic code, amber
  is where a model was involved. `present.tone()` returns a colour name so the theme supplies the
  value. `chip_colour()` is deliberately untouched, since its hexes mirror the fills
  `tda.outputs.workbook` writes into the annotated spreadsheet and an officer reading both must
  meet one visual language.
- **`tda.review.ui`**, the pieces both screens draw with, so one finding renders one way on each.
- **The Run page rebuilt**: a title, three figures of which the third is `Computed by a model: 0`,
  five scene cards readable before choosing, stages labelled with what they did rather than with
  this repository's node names, a verdict banner with a sentence saying what the verdict means, a
  plain-English account of what was actually read, findings as full cards **with the evidence pair
  on this page for the first time**, and the three artefacts a hotel receives offered for download.
- **The agent calls, supervisor decisions and replay player** move behind one disclosure. They are
  the most interesting material on the page for one audience and noise for the other.
- **A How it works page**, because a link sent to a stranger has to explain itself. It carries the
  stages as prose, where the models are and the rule they are held to, what replay means, an honest
  account of what this demo does not prove, and both responsible-AI disclosures verbatim.

Three defects surfaced while building it, each on a path no test covered: `verdict.extraction` is
absent on a rejected submission, so the new summary sentence would have crashed the missing-report
scene; the working panel read a `run.json` that a killed sandboxed child never writes; and the
replay player opened at its first frame, drawing a second stage rail reading "working, waiting"
beneath a finished verdict.

### The gap this closes

The supervisor (`src/tda/agents/supervisor.py`) has had a budget, a log and a full test suite
since M4, and nothing had ever constructed one on a real call path - `docs/03-architecture.md` §6
said so in plain words. A console built to show "routing decisions" against that code would have
shown decisions nobody made. This release puts the supervisor in front of every model call
`AgentRunner.run` makes, starting with the one call `mizan run` itself invokes, and gives every
decision a fourth artifact of its own: **`routing.jsonl`, byte-reproducible across two runs of the
same submission**, confirmed by `make repro`.

### Added

- **`src/tda/agents/supervisor.py` wired live**: `RunContext.build` constructs a `Supervisor`,
  `runner_for(..., node=...)` binds it to a node, and `AgentRunner.run` calls
  `supervisor.route(...)` between building a request and sending it. A refusal is recorded against
  the request's cassette key and re-raised as the same error a caller already handled - routing
  changes nothing about what happens on a failure, only whether one does.
- **`src/tda/obs/routing.py`**: `RoutingRecord`, `RoutingLog`, `read_routing` (empty on a missing
  file, since every writer before this one still produces none).
- **The Run console** (`src/tda/review/`): `scenes.py` (five demonstration scenes, none naming a
  fixture internally), `staging.py` (an upload, validated by extension, magic bytes, size and
  count, staged by role), `runner.py` (the pipeline, self-contained under its own run id, in
  process on a background thread for a scene or handed to the sandbox for an upload),
  `sandbox.py` (an upload's verification run as a memory- and CPU-limited child process, the
  backstop behind `staging`'s own checks - see the Security section below), `timeline.py` (one
  function builds the live view, the replayed view and the sandboxed upload's all-at-once view
  from the same `nodes.jsonl`/`trace.jsonl`/`routing.jsonl`), `shown.py` (what an agent was asked,
  recovered from the cassette or recomputed and proven equal to it, never stored), `prose.py`
  (narrate-and-grade on demand, one finding's missing cassette does not blank the panel), and
  `console.py` over all of it, with `streamlit_app.py` as the two-page entrypoint.
- **`mizan run --run-id`**: an optional, explicit run id, so a caller that already minted one - the
  console staging an upload's submission before the sandboxed process exists to write into it -
  can hand it to the CLI instead of letting it mint its own.
- **`src/tda/review/live.py`**: the one module that names `ANTHROPIC_API_KEY`. Off unless
  `MIZAN_LIVE_MODE` says otherwise, refused rather than downgraded when misconfigured, bounded by
  a per-session cap and, because a new browser tab resets session state, a per-process cap behind
  it that a tab cannot reset.
- **`requirements.txt`, `.streamlit/config.toml`, `.streamlit/secrets.example.toml`**: what
  Streamlit Community Cloud installs and reads - there is no `pip install -e .` step on that
  platform, so nothing `make setup` would install can be missing from `requirements.txt` (checked
  by `tests/unit/test_packaging.py`).
- **`ADR-0010`**: where the console runs and why, including the run-id-divergence bug caught
  during manual testing before it reached committed code (staging and the job each minting their
  own id, pointed at different directories) and the reasoning for recovering, rather than storing,
  what an agent was shown.

### Changed

- **`make review`** now opens `streamlit_app.py` (both the Run console and the review screen) and
  guards `RUN`/`SUBMISSION` with `$(if ...)` rather than exporting them blank - a fix for the
  `Path("")` bug the previous form could produce, pinned by a regression test. **`make review-live`**
  is new: the same, with `MIZAN_LIVE_MODE=true` and a key read from the shell.
- **The Docker image** now ships `tools/fixtures` and `tools/datagen`, so the console's two
  mutated scenes build their fixtures on first use, and deliberately not `tools/demo`, so the
  refusal scene - the one that needs `reportlab` - is omitted from the catalogue rather than
  offered and failing when run. Its `CMD` now runs `streamlit_app.py`. Verified by building the
  image and driving it through a real browser: a fixture built live, a run reached FAIL with the
  correct findings, and Open in Review handed off cleanly with cropped evidence.
- **`docker/compose.yaml`** and **CI** both set `MIZAN_LIVE_MODE: "false"` explicitly, alongside
  CI dropping the never-read `MIZAN_LLM_PROVIDER` variable it carried since M1.
- **An upload no longer runs on the console's own background thread.** It runs sandboxed (see
  Security below); a scene still runs in process, live, exactly as before. The console shows a
  single "verifying your submission" state for an upload rather than the five-chip live view a
  scene gets, then the same complete timeline either way, read back from the files the run wrote.

### Security

`ANTHROPIC_API_KEY` is read in exactly one place (`tda.review.live.provider_for`, handed to
`tda.cli.build_provider` as a bare provider name, never held), never logged, never rendered, and
`tda.obs.redact` gained an `api_key` pattern so a key pasted into an uploaded cell or the
assistant's question box cannot reach a written artifact or the console's own screen. It cannot
reach a build context (`.dockerignore` denies `**/secrets.toml`) or a commit (`.gitignore` and the
secret guard both refuse a tracked `secrets.toml`). On Streamlit Community Cloud it is a
root-level secret, verified against Streamlit's own current documentation to become an environment
variable only at that level, never inside a `[section]` - the exact property `tda.review.live`
depends on.

Two independent pre-share reviews (G5 security, G6 responsible-AI) ran against the console before
this shipped. G6 returned BLOCK once, on the cross-viewer disclosure below, and passed on
re-review. G5 returned BLOCK six times against the upload path: five rounds against `tda.review.
staging`'s own content checks, each finding a real, working proof of concept the previous fix had
not closed, and a sixth against the resource-limited subprocess built to replace them, which found
real bugs in that mechanism's first form.

- **Cross-viewer disclosure.** The review screen's run picker and the console's own replay panel
  both listed every run under one shared artifacts directory, with no session scoping - on a
  hosted deployment reachable by more than one viewer, each could see what the others had
  uploaded. `tda.review.app.single_operator()` restricts both listings to runs the current session
  itself made, off by default; `docker/compose.yaml` and `make review`/`make review-live` opt the
  trusted single-operator context back in explicitly.
- **An upload could exhaust the shared hosted process, and content inspection could not keep up.**
  Five rounds, each a real proof of concept: a decompressed-size bypass (many short cells cost far
  more resident memory per byte of XML than an ordinary export does); a regex cell count undercounted
  by a namespace-prefixed `<c>` tag; the same count undercounted again by a merge range, which
  needs no `<c>` element at all; a `<dimension>` hint that lied by being wrong rather than absent,
  defeating the check that trusted it; and finally a `<hyperlink ref="A1:XFD1048576">` that drove
  a 1.5-kilobyte upload past a gigabyte of resident memory, the construction that closed the
  pattern. **`tda.review.sandbox`** runs an upload's verification as its own memory- and CPU-limited
  child process, so a file that finds a sixth construction exhausts its own child's ceiling instead
  of the process every viewer shares. `tda.review.staging` now bounds only what a byte cap can
  answer from zip metadata alone - the cell count and merge-range sum four of the five rounds spent
  building are gone, not kept alongside the sandbox: a sixth review found that unsandboxed parse
  was itself costing several seconds of CPU and hundreds of megabytes in this process, on a payload
  built to clear the byte cap by design, so the check meant to protect the process was itself the
  cost worth removing once the process boundary existed to make it redundant. See
  [ADR-0010](docs/adr/0010-the-console-runs-in-process-and-replays-the-record.md) §7 for the full
  account, including two bugs this build's own verification caught before either review pass would
  have reached them: `RLIMIT_AS` is unusable on macOS (an ordinary process reserves hundreds of
  gigabytes of virtual address space there from allocator conventions alone, guarded by a
  Linux-only check, verified against the platform this actually ships to) and a child process does
  not inherit the parent's in-memory `sys.path` patch, which would have made every upload fail
  silently on Streamlit Community Cloud's own `requirements.txt`-only install shape had it shipped
  unfixed.
- **The subprocess boundary itself crashed the page on its own success path, and still let the
  parent do the expensive work it existed to avoid.** A sixth G5 review, of `tda.review.sandbox`
  specifically: a subprocess a resource limit or the wall-clock timeout killed writes nothing, and
  the console called `timeline_from_run` on it anyway, an uncaught `FileNotFoundError` on exactly
  the path this mechanism exists to serve, reproduced against a real killed subprocess and fixed by
  checking the ledger file's presence before reading it. `preexec_fn`, which ran in a
  forked-but-not-yet-exec'd copy of a process the console's background worker makes multithreaded -
  the exact hazard the `subprocess` module's own documentation warns about - is gone; `mizan run`
  now applies its own resource limits to itself, via two new CLI flags, immediately after parsing
  arguments. A kill reported after a real verdict had already been written would have been read
  back as a total loss; checked against disk first now, not against what the subprocess reported
  about itself. `ANTHROPIC_API_KEY` no longer reaches a replay or stub run's child, which never
  needs it. The memory ceiling had never been checked against Streamlit Community Cloud's own
  published range, and nothing bounded how many sandboxed children could run at once; both lowered
  and capped (a new `MIZAN_SANDBOX_MAX_CONCURRENT`), flagged in the runbook as needing
  re-verification against the real deployed container rather than claimed as measured. Smaller
  fixes in the same pass: `--hotel` and its neighbours are passed `=`-joined so a viewer-typed
  value cannot be mistaken for a second flag; a malformed sandbox environment variable now fails
  the run loudly instead of silently substituting the default; the child no longer inherits the
  parent's working directory or holds an unbounded stdout/stderr pipe into it. A third bug turned
  up verifying the fix on real Linux, prompted by neither review: the lower memory ceiling let a
  bomb's `MemoryError` reach `mizan run`'s own failure path cleanly, then let *building the
  failure ledger* - digesting every input file - throw a second, unhandled `MemoryError`, exiting
  with Python's default code for an uncaught exception (`1`) rather than `COULD_NOT_RUN` (`2`) -
  indistinguishable from a real verdict to anything reading the exit code. `tda.cli.main` now
  wraps that write in its own `try`/`except`.

### Known gaps

- **The per-session live-run cap resets on a new browser tab**, by design (`st.session_state` is
  per-session); the per-process cap added this release is what actually bounds total spend on a
  deployment between reboots.
- **Nothing a console run writes survives a reboot.** Streamlit Community Cloud's own documentation
  states it does not guarantee local file storage persists and may delete it at any time; a run
  worth keeping has to be exported before that happens.
- **Viewer invitations are the only access control.** Not an authentication system: an invited
  account can see everything a private app's Sharing list grants, and the account is limited to
  one private app at a time.
- **No bound on replay-mode submission frequency or cross-tab concurrency.** The live-run caps
  bound spend; nothing yet bounds how many free replay runs one session, or several concurrent
  ones, can start. Tracked, not blocking a synthetic-data demo on its own.
- **An upload gives up the live, node-by-node view a scene gets**, in exchange for running in its
  own resource-limited process rather than the shared one. Accepted in ADR-0010 §7 as the cost of
  a real isolation guarantee, after five rounds of content inspection alone did not converge on
  one.

## v0.5.2 — Narrative grading (PRD-95) · 2026-09-16

The numbers have been covered by `make eval` since PRD-94. The prose was not, and the prose is what
the officer reads. The critic agent was already built and already passing its own eval set before
this patch started. This patch wires it into `make eval`, over every narrative across all six
fixtures: **11 of 11 real findings narrated and graded, 6 of 6 fixtures still green.**

### The finding this patch is really about

Grading real fixture findings for the first time surfaced a genuine grounding gap in the narrative
agent, invisible until something graded a real answer rather than a hand-written eval case. A
transcription, completeness or extraction-limit finding carries no permutation to lean on, and
`read_finding()` gave the agent nothing else, just a class name and a clause. Asked for a sentence
anyway, it invented one: a directional claim about which document holds a missing value, a specific
mechanism for an unresolved label, a pair of source systems for a plain mismatch. Every one of those
is a plausible sentence attached to a correct finding, which the critic exists precisely to catch,
and every one of those failed on the first real recording.

Fixed at the source rather than by softening the gate. `read_finding()` now states one small,
non-numeric, code-derived fact for these three classes (`tda.agents.narrative._structural_fact`):
which side is missing a figure, or that nothing could be compared at all, so the agent has real
material instead of a blank slate. `prompts/narrative/v2.md` names the failure mode explicitly:
restate the structural fact and stop; a citation belongs beside the sentence, not inside it. Three
recording rounds converged on 11 of 11 passing on their merits, the same discipline PRD-94's F6
label needed.

### Added

- **`src/tda/eval/narrative.py`**: `grade_narratives` runs `narrate()` then `grade()` over every
  finding and definitional item a fixture's published `Verdict` carries, converting each
  `CriticVerdict` to a plain `NarrativeResult` so `tda.eval.scoring` stays free of every agent
  contract, exactly as it already claimed to be.
- **`NarrativeResult` and `narrative_check`** in `tda.eval.scoring`: a failing narrative becomes an
  ordinary `Check` in the same all-or-nothing tuple `verdict_for` already decides `Outcome` from, so
  there is no second gate to keep in sync with the real one. `Tally` gains a narrative pass rate
  alongside recall; the report gains a "Narrative grading" section; the scorecard gains a
  `narrative` array per fixture.
- **`prompts/narrative/v2.md`** (policy 1.3.1): the structural-fact and no-embedded-citation rules
  above. `v1.md` stays, immutable, as the record of what every pre-existing cassette was recorded
  against.
- **`src/tda/eval/record_narratives.py`**: extends `make record` to run `narrate()`/`grade()` live
  against each of F2/F3/F4/F6's real published findings. No hand-written case file is needed, since
  a fixture's findings are already fully derived. Also records one held-back, deliberately bad
  accusation graded against F3's real definitional finding and never used as its actual narrative.
  `tests/eval/pipeline/test_narrative_grading.py` replays it to prove the eval gate, not just the
  critic, catches it.
- **Twenty-three cassettes**: 11 narrative, 11 critic, 1 held-back critic-only.

### Known gap closed

- **No critic agent wired into a grading run** (PRD-95), closed. `narrate()`/`grade()` run only
  inside `make eval`'s scorer, never inside `mizan run`: `publish_node`'s own doc table has said
  "code + model" since M4, and nothing has called a model there yet. That gap is real, decided with
  the user to stay open, and belongs to its own issue rather than this one.

## v0.5.1 — The first cassettes · 2026-09-15

A patch, and the one that deletes the sentence every tag since v0.2.0 has carried. The API key
arrived, `make record` ran for the first time, and the eval set has numbers: **20 of 20 cases
recorded, 20 of 20 passing, 0 unmeasured**. `make run` completes end to end in replay, offline, with
no key. Three recording rounds, $1.63 in total.

The first round recorded fourteen of the twenty. Everything below came out of the six that did not,
and none of it was visible from any amount of review, because a contract nothing has ever produced
is a contract nobody has tested.

### The claim this patch settles

> **The eval scores are real model answers, and CI replays them offline with no key and no
> network.**

Both halves are load-bearing. The scores are not a mock agreeing with itself: each one is a recorded
response from the live API, keyed by a hash of the prompt version, the system prompt, the rendered
messages, the output schema, the model id, the effort and `max_tokens`, so a response can only
replay against the request that produced it. And nothing in `make ci` touches the network, which is
what makes a cassette a regression test rather than a receipt.

### Added

- **Twenty cassettes** under `tests/cassettes/<agent>/<key>.json`: `critic` 4, `mapping` 1,
  `narrative` 3, `resolution` 4, `reviewer_assist` 8. One per committed eval case, because the
  recording set and the eval set are the same set and `tda.agents.cases.run_case` is shared by both.
  A case cannot be recorded against one question and replayed against another.
- **`too_close_to_choose`** in the resolution driver. Any resolution whose top two candidates score
  within `AMBIGUOUS_MARGIN` (0.05) is refused, and the abstention the code would have produced
  itself is substituted.
- **`Côte d'Ivoire` in the committed country lookup**, which had no entry for it.

### Changed

- **Contracts answer `is_answer`; `__bool__` lives on `AgentResult`.** A contract crosses a library
  boundary and an `AgentResult` never does, which is what makes one of them safe to overload and the
  other one not. The bug this pays for is the first item under Fixed.
- **`Resolved | Abstained` and `Answered | Declined` are each one flat model with an `outcome` field
  and a validator.** The guarantee is unchanged: an answered outcome carrying no citation still
  cannot be constructed. It is enforced by a validator rather than by the type checker, which is a
  real cost and is recorded as one.
- **The prose leak detector treats labels as labels.** Periods (`January 2026`), this system's own
  identifiers (`D-MAT-06`, `V2`) and rendered citation coordinates (`page 11, rows 40-44`) are no
  longer read as leaked figures, on the same argument the contracts' numeric whitelist already makes
  about `page`.
- **The mapping prompt says a quarter roll-up is a period rather than a total**, because the first
  recorded mapping left row 8 and column E unmapped and halted the run on two blocking D-XLS-06
  findings.
- **Nine files stop saying the directory is empty.** `tests/cassettes/README.md`,
  `docs/03-architecture.md`, `docs/04-runbook.md`, ADR-0004, ADR-0009, the eval suite's two
  docstrings, the graph test that explained why it uses a rejected submission, and `mizan run`'s
  provider helper each carried a claim recording has now falsified. Separately, `pyproject.toml`
  still read `0.2.0` three tags after v0.2.0, and now reads `0.5.0`.

### Fixed

**A falsy pydantic model is dropped by the SDK.** The expensive one. Every contract overrode
`__bool__` so that `if resolution:` read nicely. The Anthropic SDK populates `parsed_output` and
reads it back with `if content.parsed_output:`, so a contract that is falsy when the agent declines
was discarded inside the library, and the caller was told the model returned nothing parseable from
a response whose JSON was exactly right. The idiom had been reviewed, typed and unit-tested since
M4, and every test that exercised it built the object by hand.

**A `oneOf` with a discriminator cannot be generated.** Asked to abstain, the model emitted the
union of both branches' fields, discriminator set to `"abstained"` alongside `iso_alpha2: ""`, and
`extra=forbid` refused it. The schema is well formed; constrained decoding flattens it anyway.

**A unit test spent money.** `test_the_recorder_refuses_without_a_key` called the recorder with the
ambient environment. Harmless only while nobody had a key: the first time a developer followed
`.env.example` exactly as the recorder tells them to, the test found the key, took the live path,
and recorded every committed case inside `make ci` before asserting on the exit code it reached. The
no-key condition is now forced three ways, because the failure mode is silent and shows up on a bill.

**The scorer had never scored anything.** Four of the eight failures in the second round were not the
model's fault. The leak detector flagged `January 2026`, the word `two`, `D-MAT-06`, `V2` and
`page 11, rows 40-44` as leaked figures, every one of which the prompts explicitly instruct the agent
to write. A check that punishes the behaviour its own prompt demands is measuring obedience to the
scorer.

**Two case files were wrong about the corpus.** The mapping case expected a `revenue` block; the
workbook's rate sheet carries average length of stay and no revenue row at all, so the model was
right and the hand-written expectation was not. The resolution case asked the agent to resolve
`Cote dIvoire`, and the committed country lookup had no entry for Côte d'Ivoire, so the shortlist
came back empty and the agent correctly abstained on a country that plainly exists.

**Ambiguity needed a check, not a sentence.** The agent resolved `Austrlia` to Australia as "a closer
character match than Austria": one insertion against one deletion, a continent apart. Two rounds of
prompt instruction did not stop it. `too_close_to_choose` does.

### The line this patch is really about

A prompt is a request. A check inside the function that returns the answer is a guarantee. It is the
argument this repository has made since M1 about the deterministic boundary, and it arrived on
schedule in the one place nobody had applied it: the model layer's own contracts, where two rounds
of careful instruction lost to five lines of arithmetic.

### Known gaps at this tag

- **`make eval` and `make repro` remain unimplemented** and still exit 2. **No eval scorecard.**
  PRD-94. The numbers in this entry came out of `pytest` and a commit message, which is not
  something anybody can diff between tags.
- **No critic agent wired into a grading run** (PRD-95), **no delivery pipeline** (PRD-96), **no
  demo scenes** (PRD-97).
- **The review screen's question box is recorded, not answered.** The eight reviewer-assist cassettes
  cover the eight eval-case questions. Anything an officer actually types misses, and the box says
  so. Honest, and not useful. ADR-0009 holds the argument.
- **A bare name is still not redacted.** `redact` matches structure, so a guest's name typed into the
  question box reaches the trace verbatim. A test pins the gap rather than implying it away.
- **No run-time target, and no time-saving figure anywhere.** Durations are recorded, never targeted,
  until somebody measures the manual baseline.

### Closed since v0.5.0

The cassettes, listed as a gap in every tag since v0.2.0 and as the binding constraint at v0.5.0.
Three of the four things v0.5.0 said they were blocking are closed: the eval cases are measured,
a replay run reaches `publish`, and the reviewer-assist path is exercised by recorded answers from
a real model rather than by its three tools called as pure functions. The fourth, an officer's
free-text question, is listed above as the gap it remains.

### PRD-96 — `make bundle`, and the delivery pipeline it completes

The gap this tag's own "Known gaps" section names as open: **no delivery pipeline.** It is closed.

`tools/release/bundle.py` runs `mizan run` against the demo corpus, requires the eval scorecard to
already exist or produces one with `make eval`, and assembles `dist/mizan-<version>.tar.gz`: the
verdict, the memo, the annotated workbook, the run ledger and trace, and the eval report, under one
`MANIFEST.json` naming the package version, the exact git commit and whether the tree was dirty.
Every required file is checked for on disk before the archive is written; a missing one fails the
build rather than shipping a smaller archive.

`.tar.gz` over `.zip`, because the two files this bundle already carries, `memo.docx` and the
annotated `.xlsx`, are themselves zip containers, and nesting a zip inside a zip is the more
confusing thing to hand somebody. `fresh_run` calls `tda.cli.main` in-process rather than shelling
out, because a bare `mizan run` has no build dependency of its own; `ensure_scorecard` does shell
out to `make eval`, because that target's dependency on `fixtures` belongs to the Makefile and
re-deriving it in Python would be a second copy of a rule that already lives in one place.

`docs/04-runbook.md` gained a failure-table row for `make bundle` and a "What a release bundle
contains" section on the same pattern as the run artefacts before it, and picked up two rows for
`make demo` and a correction: `make eval` scores three fixtures, not six, matching
`tools/fixtures/spec.py`'s own "Three fixtures, not six" note, which the target table had not
caught up to. `docs/07-git-workflow.md`'s branch protection table was checked against
`gh api repos/<owner>/<repo>/branches/develop/protection` and already correctly says the ruleset is
active rather than merely documented; nothing there needed a change.

## v0.5.0 — M5 Surfaces & Outputs · 2026-09-15

The surfaces a human actually touches. This is the first tag at which a verification **leaves the
machine**: three artefacts an officer files, a screen they decide on, and an assistant that answers
their questions or refuses to. It carries **PRD-91** (the review screen), **PRD-92** (the outputs)
and **PRD-93** (the reviewer-assist agent), and closes the gaps v0.4.0 recorded as *"no verdict
document and no memo"* and *"no human review screen"*.

### The claim this milestone establishes

> **Nothing material passes without a named human decision, and every artefact renders from the
> written verdict rather than from memory.**

Both halves are enforced rather than intended. A decision without a reviewer's name is refused by
`ReviewRecord`, so the gate cannot record that a button was pressed; and the memo and the annotated
workbook are rendered from `verdict.json` after it is written, so the file an officer forwards and
the file an auditor reads cannot disagree about what the run found.

The third surface is the one that needed the most defending. Everywhere else in this system a
model's output is checked by something — a mapping against the workbook's geometry, a narrative
against figures the officer can compare it to, a resolution against a blocking finding a human must
clear. On the review screen the model's output **is** what the officer is checking with, and there
is nothing behind it. So an uncited answer is not a weaker answer; it is unconstructible.

### Added

- **`artifacts/<run_id>/verdict.json`** — the document, with a `summary` block derived on write and
  re-derived on read. A file whose counts have been edited **does not load**. It is a count
  checksum and says so: an edited citation still loads, and what protects that is the input digest
  in `run.json`.
- **The annotated workbook** — a *copy* of the submission, five colours, one comment per flagged
  cell. `_check_untouched` re-reads the original afterwards and compares digests, because "we only
  write to the copy" is a sentence and a digest is a fact. A finding whose cell cannot be placed
  becomes an `Unplaced` row on its own sheet rather than a silent omission.
- **`memo.docx`** — front-loaded: the decision, the counts and what the property must do, before
  any table. The signature block renders the standing decisions, so a memo **cannot claim a review
  that did not happen**.
- **`make review`** — the officer's screen. Each finding is a self-contained card: the figures, the
  report rows outlined in red, the workbook cell outlined in red, and accept/reject/amend with a
  note. PRD-91's acceptance criterion is a stopwatch, and the layout is an argument about it.
- **Evidence cropped to what was cited.** `tda.extract.pdf.row_bands` is the *same generator* that
  numbered the rows during extraction, so the crop and the caption cannot drift. Every crop is
  checked against the digest `run.json` recorded — pointing the screen at the wrong submission
  produces *"pms_2026-01.pdf … is not the file this run read"* rather than another property's rows
  under this finding's citation.
- **`tda.review.decisions`** — the recorder, headless. The screen calls it and so would a console
  fallback, so the fallback cannot change the schema. Decisions are **appended, never overwritten**:
  an officer who accepts a finding and later rejects it has done something an auditor needs to see.
- **The reviewer-assist agent** — three read-only tools over one verdict (`query_verdict`,
  `get_evidence`, `get_policy_clause`), **no arithmetic and no file access**. `Answered.citations`
  has `min_length=1`, so prose with nothing behind it fails schema validation in the provider; and
  every citation is checked against the run before the answer is shown, with a fabricated one
  withholding the prose rather than annotating it.
- **Eight reviewer-assist eval cases, three of them unanswerable** — a period the run did not cover,
  a figure that would have to be recomputed, and a question about a person's intent. The agent must
  decline all three. An assistant that answers everything is indistinguishable from one that makes
  things up.
- **Questions are appended to the run's trace**, redacted, with their citations and tokens, and
  `mizan trace` shows them under **after the run**. A run reviewed twice has a longer trace than one
  reviewed once; the questions asked about a verdict are part of how it came to be signed.

### Changed

- **The agent schema lint now follows nested models.** `PdfCitation.ref: PdfRef` was the first agent
  contract to point at a bare `BaseModel` outside the contracts package, so the numeric rule was
  being satisfied by coincidence rather than by the guard. No live violation — the ref types carry
  only whitelisted citation integers — and an arch fixture now proves a contract that declares no
  number and reaches two fails, naming the path from contract to number.
- **A contract violation from the live provider is no longer laundered into a transport failure.**
  `client.messages.parse` validates the response, so an `except Exception` around it reported an
  uncited answer as *"the assistant is not configured on this machine"* — a setup problem the
  officer would go and try to fix.
- **`quarter` left the shared prose leak detector.** In this system it is a period, not a fraction,
  and it was failing correct answers for using the domain's own vocabulary.
- **`make run` writes the three artefacts**; `make review` opens the screen on a run.

### The decision that took the most care

**The assistant cites or declines, and computes nothing** (ADR-0009). The tempting build is an
assistant that answers *"by how much?"* — it is the question officers ask most. It is also the one
this agent must refuse, because the answer would be a number a model wrote, sitting on the same
screen as numbers recomputed from reservation records with nothing to tell them apart. The tools
therefore return classifications, clauses, causes and references, and never `claimed`, `computed`,
`difference` — nor any **count**, since `hotel_error_count` is a metric-function output that does
not look like a figure in a listing. The agent names findings by id and answers *which* rather than
*how many*. The officer already has the number on screen; what they lack is which rule made it a
finding.

### Fixed

**A definitional finding was coloured green by redaction, and a material one could be.** Found by
the PRD-92 review: the workbook colouring keyed off text that redaction had already replaced, so
the most expensive class of finding rendered as the reassuring colour.

**The reviewer's own name was redacted out of the accountability record.** `write_verdict` ran the
whole document through `redact`, and the one field whose entire purpose is to name a person was not
exempt. Review records are now lifted out before redaction and restored after.

**Evidence could be cropped from the wrong submission.** `make run SUBMISSION=/data/hotel-x` then
`make review` cropped the demo corpus and captioned it with this run's citations — a wrong picture
under a correct caption, which is the worst thing an evidence screen can do.

**The reviewer-assist agent was never shown the verdict.** Found by the PRD-93 review, and the most
instructive defect of the milestone. `ModelRequest` carries text, not tool calls, so a driver that
built a tool session and never called through it handed the model three tool *descriptions* and no
run — no run id, no finding ids, no page, no cell, no clause. Every honest answer it could give
would cite something it had to invent, the fabrication check would correctly refuse it, and the
officer would read *"the assistant cited evidence this run does not contain"* on every question. A
fabrication check that had inverted into a fabrication generator while looking exactly like a
working one. Nothing caught it: with no cassettes the evals report `NOT_RECORDED`, and every test
exercised the three tools as pure functions rather than through the driver.

Thirty-eight further defects across three adversarial reviews (15 in PRD-92, 11 in PRD-91, 12 in
PRD-93), each with a regression test. A sample of the ones that were hidden by tests that passed:

- **`_verdict_refs` ignored the definitional array**, so a correct citation about a V2 rendered to
  the officer as *"the assistant cited evidence this run does not contain — treat anything else it
  has told you today with suspicion"*.
- **`put_question` raised** when `docs/01-definitions.md` is unreadable, which it is beside an
  installed wheel — from a function whose entire contract is that it never raises.
- **`st.cache_data` on the ask path is process-global**, so two officers asking the same question
  shared one call and the second officer's question reached no trace, while the caption still read
  "Recorded to this run's trace".
- **`mizan trace` counted review-time questions as unattributable**, firing its mis-attribution
  warning on every reviewed run — training readers to ignore the one line that catches the real
  thing — and never rendering the question at all.
- **openpyxl overwrites `dcterms:modified` inside `save()`**, so pinning the timestamps before
  saving did nothing and every workbook differed byte-for-byte between runs.
- **Cropping `image.original` lost the red outline** the crop existed to show.

Each review again found tests that passed against code with the guarantee **deleted** — six in
PRD-92, five in PRD-91, five more in PRD-93. Every one was replaced with a test verified by removing
the behaviour and watching it fail, and the same discipline was applied to the fixes: in PRD-93
alone, 51 mutations were run across the build and the review round, 50 caught. The single miss was a
decorative import-time check, rewritten.

### A suggested fix that was wrong, recorded because the near-miss is instructive

The PRD-93 review proposed making `check_citations`' catch-all raise, on the reasoning that valid
citations are handled by their guards falling through. They are not: a valid `PdfCitation` fails its
guard, falls past three non-matching arms and lands in the catch-all — so the fix as suggested
refused **every honest answer**. The existing tests caught it on the first run. The match now
dispatches on kind with the membership test inside each arm. A review is evidence, not an
instruction.

### Known gaps at this tag

- **Still no cassettes committed**, and this is now the binding constraint. `make record` needs a
  key and makes live calls. Until it runs: the eval cases report `NOT_RECORDED` **in words** rather
  than skipping, `make run` in replay halts at `claim_parse` by design, and the review screen's
  question box honestly says it has no recorded answer for anything an officer types. Honest, and
  not useful.
- **The assistant cannot answer "by how much?"** By design, per ADR-0009 — but it is a real cost and
  an officer meeting three declines in a row may stop asking.
- **`make eval` and `make repro` remain unimplemented** and still say so. **No eval scorecard.**
  PRD-94.
- **A bare name is still not redacted.** The question box is free text a human types, so this limit
  now has a second path to it: `redact` matches structure — an email, a phone number, a booking
  reference — and a guest's name typed into the box reaches the trace verbatim. A test pins the gap
  rather than implying it away.
- **No critic agent wired into a grading run** (PRD-95), **no delivery pipeline** (PRD-96), **no
  demo scenes** (PRD-97).
- **No run-time target, and no time-saving figure anywhere.** Durations are recorded, never
  targeted, until somebody measures the manual baseline.

### Closed since v0.4.0

v0.4.0 listed *"no verdict document and no memo"* and *"no human review screen"*, and noted that
`reviewer_assist` sat in the roster with an allowlist and no implementation. All three are closed.
The cassettes remain uncommitted and are listed above as the gap they still are — unchanged since
v0.3.0, and now blocking four separate things.

### Scale at this tag

763 tests, up from 615 at the end of M4. `make ci` runs lint, format, `mypy --strict` over 129
files, four import guard rules, the agent schema lint (now recursing through nested models), the
secret guard, policy validation against 24 adversarial cases, a byte-reproducibility check of the
corpus against its generator, and the eval scorer's own discrimination check across 20 cases.

## v0.4.0 — M4 Agents & Graph · 2026-09-14

The agents, the graph that runs them, and the record they leave behind. This is the first tag at
which a **single command verifies a submission end to end** and leaves enough behind to defend the
answer afterwards. It carries **PRD-88** (agent runtime), **PRD-89** (the orchestrated graph and
`mizan run`) and **PRD-90** (observability), and closes the gap v0.3.0 recorded as *"no agent has
ever run"*.

### The claim this milestone establishes

> **Every agent has a narrow allowlist, a typed contract and a trace record — and an agent that
> reaches outside any of the three fails loudly rather than quietly.**

An agent here is five things: a versioned prompt, a typed output contract, a narrow tool allowlist,
an eval set, and a trace record. Four are required arguments to `AgentSpec` with no defaults, so an
agent missing one does not type-check. A refused tool call **raises** and is recorded — returning an
empty result instead would convert a contract violation into a retry loop that succeeds with no
record.

The supervisor is code. A model deciding which model to call next is a system whose control flow
cannot be reviewed, and every routing decision — skips included — is logged, because a router that
records only its approvals cannot answer why an agent did not run.

### Added

- **`src/tda/agents/`** — the runtime: `AgentSpec`, `AgentRunner`, `ToolRegistry`, the roster, and a
  code supervisor with a per-agent budget. Exhaustion **raises**; it is never a truncation, because a
  verdict from a pipeline that quietly stopped calling agents looks exactly like a complete one.
- **Four agent contracts** — `WorkbookMapping`, `LabelResolution`, `FindingNarrative`,
  `CriticVerdict`. `LabelResolution.answer` is `Resolved | Abstained`, discriminated: **abstention is
  an outcome, not an error**, it carries the reason a human reads, and it produces the same blocking
  finding the unresolvable label would have produced anyway.
- **`src/tda/graph/`** — five nodes, one typed `RunState`, one conditional routing rule. Every path
  reaches `publish`, a rejected run included, because the officer's question after a failed run is
  answered by a verdict rather than by an absent one.
- **`mizan run`** — the console script `pyproject.toml` has declared since M1 and that had never had
  a module. Prints the verdict, the node log, the cost and where the artifacts went; exits non-zero
  for any submission a human has to read.
- **`artifacts/<run_id>/`** — `run.json` (inputs by SHA-256, the versions, the timings, the cost),
  `trace.jsonl` (one record per model call, refusals included) and `nodes.jsonl` (what the pipeline
  did, in order, and where it stopped). JSON Lines, so a run that dies halfway still leaves a
  readable file — which is exactly when somebody reads it.
- **`mizan trace` / `make trace`** — the three joined into a tree: which agent ran under which node,
  what it was asked for, what it returned and what it cost.
- **Token and cost accounting** — per agent and per run, stamped with the rate card version that
  produced it. Prices are a configured input, not a fact, and a cost without its card cannot be
  reconciled later.
- **`tests/eval/agents/`** — twelve cases across four agents, as **property checks rather than golden
  answers**. Each carries an answer that must pass and one that must fail, so the scorer's own
  discrimination is proved on every push.
- **`.env` support for `make record`** — the one target that needs a key, reading **names, never
  values**, and the real environment always wins.

### The decision that took the most care

**Redact the artifacts rather than withhold them.** PRD-90 asked for a test scanning the trace for
name-shaped content from the corpus. The corpus has no names in it — `tools/datagen/ledger.py` emits
a salted `guest_ref` and says why — so that test would have passed vacuously, which is the worst kind
of green.

The exposure is real but elsewhere, and it is demonstrated by a test rather than asserted:
`tda.excel.tools.digest` copies **every label cell** of the submitted workbook into the mapping
prompt verbatim, because the agent needs the labels to do its job. A property that types contact
details into a header cell has put them in the prompt, hence in the cassette, and they can re-emerge
through a free-prose contract field.

The obvious answer — refuse to write an artifact containing personal data — **destroys the record in
order to protect it**. The trace is the evidence that answers "why did it say that?". So the content
is replaced before anything is written, there is no unredacted copy anywhere, and the *fact* of the
redaction is recorded as kind and count. A ledger field listing the phone numbers it redacted would
be a ledger that leaks them.

### Changed

- **`policy.yaml` moves to 1.3.0** with `model.budget` — `max_calls_per_run` and
  `max_calls_per_agent`. The per-agent cap is the load-bearing one: a total-only budget lets one
  runaway agent spend every other agent's allowance before anything notices.
- **`make record` no longer duplicates the key check.** The Makefile's own `test -n` gate exited
  before Python ran, defeating the `.env` loader through the one documented path that needed it.
- **`make run` runs the pipeline** instead of printing the issue that would implement it.

### Fixed

**A failed model call was not counted as a call.** `AgentRunner` wrote the trace record and left
the usage ledger alone, so `NodeTiming.model_calls` disagreed with the trace — and since the trace
viewer attributes records to nodes by consuming that count in order, one uncounted failure shifted
every later call onto the wrong node with nothing on the page looking wrong.

Six further defects, found by an adversarial review of PRD-89 before it merged, each with a
regression test:

- **Every escalated run died.** `Reconciliation.raised` filters by severity and `.definitional` by
  class, and policy gives V2 `material` — so every definitional finding appeared in both, and
  `Verdict` correctly refused the result. Fixed by filtering on `hotel_side`.
- **Duplicate finding ids.** Three libraries each allocate from `F-0001`; the ids are renumbered at
  publish.
- **Every node after `claim_parse` claimed the mapping prompt's version**, because the prompt
  versions were folded over the whole trace rather than the calls that node made.
- **A non-UTF-8 CSV crashed intake** instead of being rejected as unreadable.
- **Intake opened the PDFs but not the workbook**, so a corrupt one surfaced as a `BadZipFile` from
  `claim_parse` rather than as a stated rejection.
- **The workbook period check was a substring test** — `"2026"` matched `claims_2026-Q1`.

### One acceptance criterion that moved, and why

PRD-89 asked for an unreadable report to produce a **blocking finding**. It cannot: a finding must
cite something (D-EV-01) and a document that will not open has no page. Inventing `page=1` would put
a citation in front of a reviewer that leads nowhere. An unopenable file is an **intake rejection**
(`UNREADABLE_FILE`) instead, and row-level defects inside a readable document are still findings and
still cite their page. Recorded in ADR-0005.

### Eight defects an adversarial review found in PRD-90 before it merged

Listed because the mistakes are more instructive than the design, and three of them were hidden by
tests that passed:

- **The surname survived redaction.** `titled_name` took the honorific and *one* word, so
  `Ms. Jane Doe` was written as `[redacted:titled_name] Doe`. The guarding test asserted
  `"Jane Doe" not in text` and passed — the string had been split, not removed.
- **The console printed the unredacted ledger** while the file beside it said `[redacted:email]`.
  `mizan run` now prints the ledger parsed back from the bytes that reached the disk.
- **The repro exclusions could not see nested fields**, so `nodes.duration_ms` and
  `usage.duration_ms` — wall clock, different on every run — would have failed PRD-94's diff on
  every single run.
- **`run.json` contradicted itself**: it reported `"redactions": []` while its own body carried a
  placeholder, because the counts were stamped before the ledger was redacted.
- **A run that died wrote nothing.** The artifacts existed for every run except the ones somebody
  needed them for.
- **Exception text and the node log reached stderr unredacted** — a pydantic error quotes the value
  it rejected, and `input_value='DOE/JANE'` is precisely what the `slashed_name` pattern is for.
- **A node entered twice reported the last exit twice**, doubling its duration and model calls and
  erasing the failure.
- **The cost column did not add up to its own total**, because the rows were rounded and the total
  was not.

Six more tests in that review passed against code with the guarantee deleted — the node log's
redaction, the merge of counts across files, the per-call cost and the tool counting had no
coverage at all. Each now has a test that was checked by removing the behaviour and watching it
fail.

### Known gaps at this tag

- **Still no cassettes committed.** `make record` needs a key and makes live calls. Until it runs,
  the eval cases report `NOT_RECORDED` **in words** rather than skipping — a skip reads as a pass in
  a CI summary, and this is not a pass. `make run` in replay therefore halts at `claim_parse`, by
  design: a miss is a hard error, never a quiet fall-through to the network.
- **No verdict document and no memo.** PRD-92.
- **No human review screen.** M5, and `reviewer_assist` is in the roster with its allowlist but has
  no implementation yet (PRD-93) — the roster is the audited record, and an agent in the architecture
  diagram but not in the table is an agent whose permissions nobody wrote down.
- **`make eval` and `make repro` remain unimplemented** and still say so. **No eval scorecard in this
  release**, for the same reason as at v0.3.0.
- **No fan-out, no retries, no checkpointing** — deliberately, in ADR-0005. A submission that halts
  simply halts.
- **A bare name is not redacted.** `Jane Doe` alone passes; nothing structural separates it from
  `Deluxe King`. Asserted in a test so the limit cannot be quietly forgotten. What covers it today is
  a corpus with no names in it.
- **No run-time target, and no time-saving figure anywhere.** Durations are recorded, never targeted,
  until somebody measures the manual baseline.

### Closed since v0.3.0

v0.3.0 listed *"No agent has ever run. The mapping agent's request builder and prompt exist; nothing
is recorded, no cassette is committed, and every test drives the checked path with hand-written
mappings."* Half of that is closed: the runtime, the roster, the tool sessions, the supervisor and
the eval harness exist and are exercised on every push, and `make run` drives the mapping agent
through the real call path. The cassettes remain uncommitted, and are listed above as the gap they
still are.

### Scale at this tag

615 tests, up from 410 at the end of M3. `make ci` runs lint, format, `mypy --strict`, four import
guard rules, the agent schema lint, the secret guard, policy validation against 24 adversarial cases,
a byte-reproducibility check of the corpus against its generator, and the eval scorer's own
discrimination check.

## v0.3.0 — M3 Engine · 2026-09-13

Both halves of the comparison, and the engine that decides what a difference **means**. This is the
first tag at which the system reads a real submission end to end: three PDFs and a workbook in, a
classified set of findings out. It carries **PRD-85** (PDF extraction), **PRD-86** (the Excel claim
parser) and **PRD-87** (reconciliation and classification), and closes the gap v0.2.0 recorded as
*"nothing reads the PDFs yet"*.

### The claim this milestone establishes

> **The system is silent about a correct submission, and when it does speak it says who is wrong and why.**

Both halves matter, and the first is the harder one. `test_a_correct_submission_produces_no_findings_at_all`
runs the whole pipeline over the committed corpus — 3 PDFs → 1,200 reservation records → 94 computed
values, workbook → 94 claims, joined on the canonical metric key — and produces **zero findings**. A
verification system that cannot stay quiet about a good submission buries every real finding it later
produces in noise.

The second half is what the POC is actually for. A hotel that apportions a whole stay to the arrival
month reports 1,265 February room-nights where the definitions give 1,299. That is **not** a clerical
error, and the engine says so mechanically: it re-runs the metric library under thirteen committed
alternative rulesets, finds that `P-MONTH-ARRIVAL` reproduces the claim exactly, and files a
**definitional** variance escalated to the policy owner — never counted as a hotel error.

### Added

- **`src/tda/extract/`** — positional PDF parsing against a committed column map, not `line.split()`.
  Printed columns are cross-checks and **never inputs** (D-RNS-01, D-RNS-02): every figure is derived
  from the row and compared to what the report claims about itself, and a disagreement is a blocking
  finding rather than a silent preference for one of the two. Five reconciliation checks run against
  each report's own printed totals before any record is trusted.
- **Three committed lookup tables** — 90 countries, statuses, rate codes. An unmappable label is
  blocking and is **never resolved to its nearest match** (D-NAT-12): a system that guesses will
  eventually accuse a hotel of miscounting guests from a country it never wrote down.
- **`src/tda/excel/`** — the claim parser, in two stages that never touch each other's work. A model
  decides *which range holds which metric*; `openpyxl` then reads the values with no model in the
  call stack. Every claim carries the cell it came from (`Nationality!D14`).
- **`src/tda/excel/tools.py`** — the agent's view of the workbook, with **no code path that can
  return a cell's value**. The restriction is a property of the function, not a sentence in a prompt.
- **`src/tda/reconcile/`** — the join, the permutation runner, the classification ladder and the
  evidence. No model is consulted anywhere in the package, enforced by the import guard and pinned
  from the other side by `classification.consults_model: false`.
- **`tools/guard/secret_guard.py`** — scans every tracked file for credential shapes and fails `make
  ci` on a hit. Added **before** any key existed, which is the only useful order: a guard introduced
  after the first recording session is a guard that was absent for the one commit that mattered.
- **`docs/01-definitions.md` §8** — D-XLS-01..06, the clauses governing the submitted workbook, and
  D-EV-05 for the mirrored typed absence. Written before the code that cites them.

### The distinction that took the most care

**Semantic redaction, not type-based.** The obvious way to keep values away from the mapping agent is
`isinstance(value, str)`. It is wrong in exactly the direction that matters: a percentage stored as
text — `'81.70%'`, which `Claim.raw_text` exists to preserve — *is* a string. A type-based filter
hands the model the one class of value the design exists to withhold, and looks correct in review.

So a cell is redacted on what it *means*: `'Occupancy %'` is a label, `'81.70%'` is a value. A test
runs the type-based version as a mutant and watches it fail.

### Changed

- **The model is pinned to `claude-sonnet-5`** and `policy.yaml` moves to `1.2.0`. The cassette key
  hashes the model id, so this is a decision that has to be taken before the first recording rather
  than discovered after it.
- **A fourth import-guard rule** — `src/tda/` may not import `tools/datagen/`. The existing rule
  stopped the generator co-deriving ground truth from the code under test; this stops the same
  tautology arriving from the other direction, where an extractor reads its column positions out of
  the renderer that drew them.
- **`Finding.pdf_ref` → `Finding.source_ref`**, widened to `PdfRef | InventoryRef | NotReached`.
  `room_nights_available` is in scope and a workbook claims it, but D-RNA-01 makes rooms available a
  *property* attribute — there is no page anywhere in the system to cite. The finding could not be
  constructed at all. Dressing an inventory row as `PdfRef(page=1)` would have put a citation in
  front of a reviewer that leads nowhere.
- **A V5 missing claim may carry no Excel cell.** "NotReached only on V7" was written when V7 was the
  only absence anyone had met. The exemption is narrow on purpose: a V5 *orphan* claim was read from
  a cell and is still refused.
- **`FindingIds` moved to `tda.contracts`** — three layers now issue ids, and the alternative was the
  deterministic core importing the extraction layer for a counter.
- **`policy.yaml` is unchanged**, but the loader now reads five fields `extra="ignore"` was silently
  dropping: `scope`, `consults_model`, `thresholds` and the two definitional-routing pins.

### Fixed

- **A `Finding` could not express its first real refusal.** The contract required at least one of
  `claimed` or `computed`, and an extraction limit raised before the workbook is parsed has neither —
  "I could not read row 14 of page 3" is a finding a reviewer must act on and carries no numbers by
  nature. V7 is now exempt, and only V7.
- **Reading a workbook mutated it.** `iter_rows` past the used range *creates* the cells it walks, so
  peeking a fixed 16 columns grew `worksheet.dimensions` and made one sheet report `A1:B11` in one
  half of the prompt and `A1:P11` in the other.
- **The mapping agent could not see where a block ended.** A fixed row window showed it four of
  twenty-one countries, making every range it returned a guess.
- **An uncached formula read as an empty cell.** In the `data_only=True` view both are `None` and
  they mean opposite things — a blank is the absence of a claim, an uncached formula is a claim we
  decline to compute. The refusal in D-XLS-03 was unreachable until the workbook was opened twice.

### Two places the acceptance criteria were not implemented literally

Both because implementing them as written would have fired on a **correct** workbook, and a check
that fires on correct input is one somebody switches off within a week.

- **"Every month column present for every dimension row."** Iceland has three March guests and blank
  January and February cells, and `truth_metrics.json` has no key at all for those months — the
  blanks are right. A blank is the absence of a claim, not a zero, and whether one conceals an
  omission is settled by arithmetic instead. Reading blanks as zeros would have manufactured two
  claims the workbook never made.
- **"Every total row equals the sum of its components"**, applied to percentages. A quarter's
  occupancy is a ratio, not a sum of its months — `78.05`, not `234.72`.

### The test that was decorative, and how that was found

Four mutants were run against the reconciliation suite. Three were caught. **The fourth was not**:
deleting the V2 rung's precondition left all 27 tests passing, because the test used February, where
no applicable permutation lands anywhere near the baseline.

Rewritten on January — which has no out-of-order rooms, so `P-OOO-INCLUDED` computes *exactly* the
baseline 69.09 and a claim of 69.14 is inside tolerance of both. Without the precondition a rounding
artefact is filed as definitional and routed to the policy owner; since definitional items are never
counted as hotel errors, the system would quietly stop reporting small clerical differences at all.

A test that passes against code with the guarantee removed is worse than a missing one, because it is
counted.

### Known gaps at this tag

- **No agent has ever run.** The mapping agent's request builder and prompt exist; nothing is
  recorded, no cassette is committed, and every test drives the checked path with hand-written
  mappings. The agent runtime, the roster and `make record` are PRD-88.
- **No narrative, no verdict document, no memo.** Findings carry a plain-English `detail` written by
  code, so the output is readable with no model available — but nothing renders it. PRD-92.
- **`make eval` and `make repro` are still unimplemented** and still correctly report themselves so.
  There is therefore **no eval scorecard in this release PR**, which §5 of the git workflow requires
  from the tag where one exists. PRD-94.
- **One corpus, one hotel, one quarter.** The six adversarial fixtures are PRD-94.
- **No human review screen and no annotated workbook.** M5.

### Closed since v0.2.0

v0.2.0 listed *"Nothing reads the PDFs yet (PRD-85). The `PdfRef`s in the Gate 2 fixture are
synthesised."* That gap is closed: extraction reads the three committed reports, reconciles each
against its own printed totals, and the citations on every computed value are real page and row
ranges.

### Scale at this tag

410 tests, up from 295 at the start of M3. `make ci` runs lint, format, `mypy --strict`, four import
guard rules, the agent schema lint, the secret guard, policy validation against 22 adversarial cases,
and a byte-reproducibility check of the corpus against its generator.

## v0.2.0 — M2 Corpus & Truth · 2026-09-13

The synthetic corpus and the metric library, and **Gate 2**: two implementations of the same written
definitions, sharing no code, agreeing on every number. Still no pipeline — nothing at this tag can
verify a submission end to end.

### The claim this milestone establishes

> **Ground truth exists, and it was not produced by the code under test.**

The corpus generator (`tools/datagen/`) and the product metric library (`src/tda/metrics/`) are two
independent readings of `docs/01-definitions.md`. They agree on all **94 metrics** the corpus
establishes, with **zero deviation** — not within a tolerance. *(Corrected at v0.3.0: this entry
originally said 110, a figure taken from a `~110` estimate in a test message rather than counted.
The corpus has had 94 metric keys since it was frozen; the agreement and the zero deviation are
unchanged.)* `tools/guard/import_guard.py` rule 2
forbids the generator from importing any of `tda`, so the agreement cannot be an artefact of shared
code.

That distinction is the whole reason this milestone exists. A system that verified data using the
code that produced it would be demonstrating that the code equals itself.

### Added

- **`tools/datagen/`** — the corpus generator, ledger-first. Reservations are generated from a fixed
  seed; the three monthly PDFs, the claim workbook and `truth_metrics.json` are all derived from that
  one ledger, so nothing is authored twice and a document cannot disagree with the truth.
- **`corpus/demo/`** — committed and frozen. 1,200 reservations over 2026-Q1; occupancy
  69.09 / 81.70 / 83.93% (quarter 78.05%). Split into `submission/` (what a hotel sends) and
  `ground_truth/` (what the generator knows) — two directories rather than a naming convention,
  because "do not read the answers" is the easiest rule here to break by accident.
  The edges are quotas asserted after generation, not properties of the draw: ≥25 month-spanning
  stays, ≥25 COMP and ≥25 HOUSE, ≥30 day-use, 12 stays arriving before the period, 8 open at its end,
  two out-of-order windows, one nationality present in one month only.
- **`src/tda/metrics/`** — the metric library. Pure functions over typed records, `Policy` as a
  parameter on every one, `Decimal` throughout, no I/O and no model client. **Every contestable
  reading is implemented, not only the correct one**: three month bases, three nationality count
  bases, two occupancy denominators — because the reconciliation engine must be able to *reproduce* a
  hotel's figure in order to name a definitional cause rather than report a clerical error.
- **`Period`** (`src/tda/contracts/period.py`) — a period string parsed once into inclusive date
  bounds, validated against its own label. There is no month arithmetic anywhere in `tda/metrics/`.
- **`InventoryRef`** and **`SourceRef`** — a second kind of evidence reference. `room_nights_available`
  is in scope and has no PDF page to cite, because D-RNA-01 makes rooms available a property
  attribute that appears in no reservation export. Inventing `page=1` would put a false citation in
  front of a reviewer.
- **`make datagen`** regenerates the corpus byte-reproducibly; **`make corpus`** rebuilds it into a
  temporary directory and diffs SHA-256 digests. `make corpus` is in `make ci`, so a stale or
  hand-edited corpus fails the build.
- **ADR-0003** — ledger-first corpus, and why the generator may not import the product.
- **154 → 239 tests** (85 new across M2), including the Gate 2 comparison and 35 hand-computed
  metric cases, each citing the `definitions.md` clause it tests.

### Changed

- **`InventoryDay.source` is now required (breaking).** Symmetrical with `ReservationRecord.source`:
  an inventory day whose provenance is unknown cannot be cited, and D-EV-01 requires citation.
- **Import guard rule 2 widened (breaking for the generator).** `tools/datagen/` may no longer import
  **any** of `tda`, not just `tda.metrics`. `ReservationRecord.occupied_nights()` *is* D-RNS-03's
  month apportionment, so importing "just the contracts" would co-derive the exact clause under test.
  A second violation fixture proves the wider rule.
- **`MetricKey` period validation delegates to `Period.is_valid`**, so the accepted forms are defined
  once rather than in two places that agree until somebody adds a third.
- **The import guard skips vendor directories.** `tools/datagen/` existing for the first time made it
  walk `.venv` — 39 seconds to reach the same answer, now 0.8s. A guard slow enough to irritate is a
  guard somebody eventually removes from CI.

### Fixed

- **Three byte-reproducibility defects, each found by generating twice and diffing** rather than by
  reading documentation. reportlab stamps a creation date and document id (`invariant=1`); openpyxl
  writes `time.localtime()` into every zip entry header; and `dcterms:modified` is reassigned from the
  wall clock *during* `save_workbook`, after anything the caller sets. The third is the one that looks
  solved and is not — with the first two fixed the workbook still differed by one timestamp, one
  element deep, in one member.
- **An incoherent corpus row.** `IN_HOUSE` was in the random status draw, so 23 of 31 in-house
  reservations had departed weeks earlier — one on 14 January, in a report covering a completed
  quarter. It now comes only from the seeded boundary set.
- **A totals page that printed a note through the nationality column**, leaving neither half
  extractable.

### Known gaps at this tag

- **`make run`, `review`, `eval`, `repro`, `demo` still exit 2** — declared, not implemented. CI
  asserts they keep saying so, because a no-op exiting 0 would make an unbuilt pipeline look green.
- **Nothing reads the PDFs yet** (PRD-85). The `PdfRef`s in the Gate 2 fixture are synthesised; real
  citations come with the extractor.
- **No reconciliation, tolerance or classification** (PRD-87). No `Finding` is constructed anywhere.
- **`Finding.pdf_ref` still cannot cite an inventory row.** The gap `InventoryRef` closed for
  `ComputedValue` is open on `Finding`; recorded on PRD-87, where a `room_nights_available` variance
  needs it.
- **No eval scorecard**, for the same reason as at `v0.1.0`: the harness is PRD-94, and quoting a
  recall figure before it exists would be inventing one.

### The demo corpus is never scored

A clean pass on the data the system was rendered from is a tautology, not evidence. `corpus/demo/`
exists so the happy path is demonstrable and so parsers have a realistic document to be built
against. The scored fixtures — with planted errors, where catching something means something — are
PRD-94, and they are the only ones `make eval` will report on.

---

## v0.1.0 — M1 Contracts & Foundations · 2026-09-13

The definitions, the typed spine, and the guards that make the architectural claim enforceable
rather than aspirational. No pipeline yet: nothing can verify a submission at this tag.

### The claim this milestone establishes

> **Deterministic core, agentic edges.** Models read layouts, labels and language. Code performs
> every calculation. No path lets a model output reach a metric function.

At `v0.1.0` that is enforced by two CI guards, each **proven by watching it reject** deliberately
broken code — not asserted in a document.

### Added

- **`docs/01-definitions.md`** — occupancy and guests-by-nationality defined precisely enough that
  two engineers reach the same number. ~40 clauses, each with a stable citable id (`D-OCC-02`),
  cited by tests, findings and (later) the reviewer-assist agent.
- **`policy.yaml` 1.1.0 + `policy.schema.json`** — every contestable rule out of code and into
  configuration, with 13 committed permutations. Six fields are pinned `const` because loosening
  them would break a claim the POC makes out loud.
- **`docs/02-assumption-register.md`** — eight assumptions with value, rationale, blast radius and
  cost to change. Six are genuinely arguable; none is ratified by the regulator.
- **`src/tda/contracts/`** — `ReservationRecord`, `InventoryDay`, `MetricKey`, `Claim`,
  `ComputedValue`, `Finding`, `Verdict`, `ReviewRecord`, and the reference types. Seven stated
  promises are constructor validators: a record whose `room_nights` ≠ `nights × rooms` cannot be
  built, and a V2 `Finding` with no named permutation cannot be built.
- **`src/tda/policy/`** — typed loader over the schema gate, plus the permutation runner.
- **`src/tda/agents/provider/`** — `LLMProvider` with four adapters (`replay`, `stub`, `anthropic`,
  `record`), a canonical cassette key, and a versioned prompt registry.
- **`src/tda/obs/usage.py`** — token and cost accounting in `Decimal`, pricing version stamped.
- **`tools/guard/`** — import guard (AST-based, transitive, I/O-aware) and agent schema lint.
- **`tools/policy/validate_policy.py`** — four validation layers including 22 adversarial cases the
  schema must reject.
- **CI** — `make ci` on every push: lint, format, `mypy --strict`, both guards, policy validation,
  **154 tests**. Offline, no API key.
- **ADR-0001** deterministic core · **ADR-0002** model layer.

### Changed

- **`policy.yaml` 1.0.0 → 1.1.0 (breaking).** Four permutations changed one side of an
  `included`/`excluded` partition and therefore did nothing at all. Permutation semantics moved; a
  verdict produced under 1.0.0 is not comparable. Nothing had run under 1.0.0.
- **The agent schema lint was retargeted (breaking).** It now scans all of `src/` for classes
  deriving from an `AgentOutput` marker, rather than one directory for bare `BaseModel` subclasses.
  Telemetry moved to `tda.obs`.

### Known gaps at this tag

- **`make datagen`, `run`, `review`, `eval`, `repro`, `demo` all exit 2** — declared, not
  implemented. A no-op exiting 0 would make an unbuilt pipeline look green.
- **No cassettes are committed.** A cassette is keyed on a prompt version, and no prompts exist
  until PRD-88. Hand-authoring them would fabricate the evidence the replay layer provides.
- **No eval scorecard.** The harness is PRD-94, so this release has no recall or precision figures
  to quote — and quoting any would be inventing them.
- **Branch protection on `main` and `develop` is documented, not applied.** It needs repo admin.

### Deliberately absent

**No time-saving figure appears anywhere in this repository**, because the manual baseline has not
been measured. A number used before it is measured will be challenged, and the challenge lands on
the whole result rather than on the number.
