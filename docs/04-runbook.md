# Runbook

Enough for a second engineer to run every target unaided, including what to do when one fails.

## Prerequisites

**Python 3.12.** Not 3.11 — `policy.yaml` loading uses PEP 695 `type` aliases and the contracts
use `Self`. If `python3.12 --version` fails, install it before anything else.

No API key is needed for anything in the local loop. `make ci`, `make eval` and `make repro` run
the model layer in **replay** mode against committed cassettes: offline, free, byte-identical.
A key is needed only for `make record`.

```bash
git clone https://github.com/apundhir/mizan-agent && cd mizan-agent
make setup      # creates .venv, installs everything
make ci         # ~5 seconds. Should be green on a fresh clone.
```

## Targets

| Target | What it does | Story |
|---|---|---|
| `make setup` | venv + all extras, editable install | — |
| **`make ci`** | **lint → format → types → guards → policy → corpus → tests.** The gate. | — |
| `make lint` / `make fmt` / `make fmt-check` | `ruff check` / `ruff format` / `ruff format --check` | — |
| `make types` | `mypy --strict` over `src`, `tools`, `tests` | — |
| `make guard` | Import guard + agent schema lint + secret guard | PRD-81 |
| `make policy` | Validate `policy.yaml`: schema, referential, semantic, 24 adversarial cases | PRD-80 |
| `make test` | `pytest` | — |
| `make datagen` | Regenerate the synthetic corpus, byte-reproducibly | PRD-83 |
| `make corpus` | Rebuild the corpus in a temp dir and diff digests against `corpus/demo/` | PRD-83 |
| `make run` | Verify one submission end to end; prints the verdict, the node log and what it cost, and writes `artifacts/<run_id>/` — the observability files **and** the three artefacts an officer files | PRD-89, PRD-90, PRD-92 |
| `make trace` | Render the most recent run as a tree: which agent ran, what it was asked for, what it returned, what it cost | PRD-90 |
| `make review` | Both pages of `streamlit_app.py`: the Run console (pick a scene or upload files, watch the agents work) and the verification officer's screen, with the assistant's question box. Opens on the latest run; `RUN=<run_id>` for an older one | PRD-91, PRD-93, PRD-115 |
| `make review-live` | Same, with live model calls - export `ANTHROPIC_API_KEY` in the shell first | PRD-115 |
| `make eval` | Score every fixture against its derived expectation (three today; `tools/fixtures/spec.py` explains why not six) | PRD-94 |
| `make repro` | Run twice, diff the verdict, timestamps excluded | PRD-94 |
| `make demo` | The three scenes: pass, catch, refusal | PRD-97 |
| `make record` | Refresh model cassettes against the live API (**needs a key, in the shell or `.env`**) | PRD-82 |
| `make prompts` | Regenerate `prompts/MANIFEST.txt` after adding a prompt version | PRD-82 |
| `make bundle` | Build the versioned release artefact | PRD-96 |
| `make clean` | Remove caches and `artifacts/` | — |

Unimplemented targets print the issue that will implement them and **exit 2**. That is
deliberate: a no-op exiting 0 would make an unbuilt pipeline look green, which is a worse
failure than an error.

## When a target fails

### `make guard` — import guard

```
import guard FAILED: 1 violation(s)
  src/tda/metrics/nationality.py:0  imports anthropic (reached via tda.helpers)
      why it matters: the metric library is pure functions over typed records; ...
```

**Do not silence it.** `metrics/` and `reconcile/` may not reach a model client, directly or
transitively, and may not do I/O. If one of them needs something from an agent, the *call
direction* is wrong: the graph node should call the agent and pass the typed result **in**, not
have the metric function reach **out**. See
[ADR-0001](adr/0001-deterministic-core-agentic-edges.md).

The `(reached via X)` form means the import is indirect. `X` is the first-party module that
actually pulls in the forbidden package — fix it there, not at the reported file.

### `make guard` — agent schema lint

```
agent schema lint FAILED: 1 numeric field(s) on agent contracts
  src/tda/agents/contracts.py:14  SheetMapping.guest_count: int
```

An agent returned a number. Return the **key or reference** that identifies the figure and let
the metric library compute it: a cell range, not the value in it. The whitelist is `page`, `row`,
`row_start`, `row_end` — citations, not quantities. Widening it needs an argument, and
`test_schema_lint_whitelist_is_citations_only` is where you have to make it.

### `make policy` — four layers, and they fail differently

| Failure | Meaning | Fix |
|---|---|---|
| `does not satisfy policy.schema.json` | Structural. A key is missing, mistyped, or one of the pinned `const` values was changed. | Read the path in the message. If you *meant* to change a pinned value, you are trying to loosen a claim the POC makes out loud — argue for it in a PR, don't edit the schema. |
| `override path does not exist` | A permutation's override path has a typo. | Fix the path. **Never** make the setter create the path: a typo would then be a silent no-op permutation that explains nothing and is never named. |
| `sets X but not Y ... silent no-op permutation` | A permutation changed one side of an `included`/`excluded` partition. | Change both. `qualifies()` reads `included`, so a one-sided override changes nothing while looking correct. |
| `the schema accepted N policies it must reject` | The schema got looser. | Find what was relaxed. Each adversarial case carries the reason it exists. |

### Extraction refuses: a blocking V7 and a halted run

Extraction is built to refuse rather than guess, so a refusal is the system working. The finding names
the clause and the exact printed row. Three kinds, and each needs a different response:

| Finding says | Cause | What to do |
|---|---|---|
| `unmappable country` / `unmappable status` / `unmappable rate code` | the label is not in the committed lookup (D-NAT-12, D-QUAL-03) | add a row to `src/tda/extract/reference/*_lookup.yaml`. **Never** widen the matcher |
| `column header does not match the committed column map` | the report's layout changed | a new layout needs a new map — re-measure the bands in `src/tda/extract/layout.py`, do not loosen the parser |
| `the report prints X, we read Y` | the extracted rows disagree with the report's own printed totals | the read is wrong, not the hotel. Work the shape of the disagreement (below) |
| `printed RN Total column says N, but …` | the row contradicts itself (D-RNS-02) | a defect in that row. The printed column is a cross-check and is never preferred |
| `printed on N monthly reports and they disagree` | two reports state different facts about one month-spanning stay | a real inconsistency in the submission. Nothing else in the pipeline can see it |

**Adding a country is a data change, not a code change.** The lookup is deliberately not exhaustive —
90 countries, the major source markets and the documented variant forms. An unmapped label is
blocking *by design*: a refusal costs somebody five minutes and a YAML row, while a silent
mismapping puts a guest under the wrong country and nothing downstream can detect it.

The shape of a totals disagreement narrows it fast:

| Symptom | Usually |
|---|---|
| Row count low, everything else consistent | a page or row was skipped — check the reservation-id pattern in `layout.py` |
| Row count right, `RN Month` sum wrong | a numeric column shifted; a band boundary is too tight |
| Both sums right, exclusion counts wrong | a status or rate code resolved to the wrong side of the qualifying line, or two errors cancelling |
| Two nationality codes wrong, total right | the `Nat` column is reading its neighbour — it is two characters wide between a reservation id and a date |

### `make corpus` — the committed corpus no longer matches the generator

The target rebuilds `corpus/demo/` into a temporary directory and diffs SHA-256 digests. It fails
for one of two reasons, and they need opposite responses:

- **You changed the generator.** Expected. Run `make datagen` and commit the result *in the same
  commit as the generator change*, so the corpus and the code that produces it never diverge in
  history.
- **You changed nothing.** Then either a corpus file was edited by hand, or something in the
  generator is not deterministic. The first is the worse case: `truth_metrics.json` would no longer
  describe the documents beside it, and every number downstream would be checked against a fiction.

The failure names each differing file with both digests. `git diff --stat corpus/` usually settles
which case you are in.

Three byte-reproducibility fixes are load-bearing here and none is obvious, so if a `reportlab` or
`openpyxl` upgrade turns this red, start at `tools/datagen/reproducible.py` — its docstring records
all three and the order they were found in. Their tests are in `tests/unit/test_datagen.py`.

### `make test` — Gate 2 fails: the two implementations disagree

`tests/unit/test_truth_verification.py` compares the product metric library against the corpus
generator's independent aggregation, **exactly**. When it fails, the message lists every key with
both numbers.

**Do not adjust one implementation to match the other.** Decide which one is wrong *against
`docs/01-definitions.md`*. That is the whole reason S1 was written before any code: without a
written definition to appeal to, "make the test pass" is the only available move and whichever file
happens to be open wins the argument. Work it in this order:

1. **Find the clause.** Every metric rule has a citable id. The key that disagrees names the metric
   and the period, which is usually enough to identify the clause in one step.
2. **Work the number by hand** from the clause, for one reservation. `tests/unit/test_metrics.py`
   has the worked examples to copy the shape from.
3. **Fix whichever side the clause says is wrong** — `src/tda/metrics/` or `tools/datagen/aggregate.py`.
   If the clause is genuinely ambiguous, that is an S1 defect: amend `docs/01-definitions.md`, add
   the assumption to `docs/02-assumption-register.md`, and say so on the issue. Both
   implementations then follow the amended clause.
4. **If the corpus changed**, regenerate (`make datagen`) and commit it with the code change.

A disagreement here is good news found cheaply. The same disagreement found after the
reconciliation engine exists arrives dressed as a hotel error.

The shape of the failure narrows it fast:

| Symptom | Usually |
|---|---|
| Every month out, quarter correct | An apportionment boundary — a whole-stay basis somewhere it should be per-night, or the reverse |
| One month out, others correct | That month's edge: February's out-of-order window, or a period boundary |
| Only `guests_by_nationality` keys | The month basis (D-NAT-06 is **arrival month**, not occupied night — deliberately different from occupancy) |
| An *extra* key the generator lacks | A nationality counted in a period it should not be; check D-NAT-06 |
| Occupancy out by 0.01 | Rounding: half-up at the presentation boundary only (D-OCC-04), and in `Decimal`, never `float` |

### `make types`

`mypy --strict` with the Pydantic plugin. Two things trip people up:

- **Never move a Pydantic field's annotation into a `TYPE_CHECKING` block.** Pydantic resolves
  annotations at runtime to build validators, so it breaks validation at import. `ruff` is
  configured with `runtime-evaluated-base-classes` so `TC001`/`TC003` will not suggest it.
- `tests/arch/fixtures/` is excluded. It is deliberately-broken data, several files share module
  names on purpose, and one imports a package that is not installed.

### `make test` — an arch test fails

`tests/arch/test_guards.py` asserts the guards **reject** deliberately-broken fixtures. If one
of those tests fails, the guard has stopped working and every architectural claim in ADR-0001 is
currently unenforced. Fix the guard; do not adjust the fixture to match the guard's new
behaviour.

### `make guard` — secret guard

A credential is in a tracked file. **Rotate first, tidy second**: it was compromised the moment it
was pushed, and removing it from a later commit does not unpublish it. Revoke the key in the
Anthropic Console, then clean the file.

The guard scans what **git tracks**, not the working tree — your own gitignored `.env` is invisible
to it, by design. A guard that fired on correct local setup would be switched off within a week, and
then the real leak would go unnoticed. `.env.example` is committed and names the variable with a
placeholder; that passes, and there is a test asserting it keeps passing.

Only `make record` needs a key. Prefer your shell or your environment's variable config over writing
one to a file at all — a key that never touches the filesystem cannot be committed by accident.

### `make record` — refreshing cassettes

```bash
export ANTHROPIC_API_KEY=sk-ant-...
make record
```

Costs real money and hits the live API — it is the only target that does either. A cassette diff
means a prompt or an output schema changed: the PR body has to say which, and why the new response
is better. **Cassettes are reviewed like code**, and a reviewer who rubber-stamps a cassette diff
has disabled the whole replay layer without noticing.

Without a key it exits 2 and says so, rather than falling back to something that looks like a
recording.

### A cassette miss

```
no cassette for mapping key=09fb3cfe1121fa68f25944eb6a5c64f7
  0 cassette(s) are committed for this agent, none matching.
  A replay run never calls the live API. Either the prompt, the output schema or
  the rendered messages changed - run `make record` and review the cassette diff.
```

**A miss is never a fall-through to a live call.** The key is printed so it can be recorded. The
"none matching" count distinguishes *nothing recorded yet* from *recorded, but the request changed*
— the second is the interesting case, and it means one of the hashed inputs moved: prompt version,
system prompt, rendered messages, output schema, model id, effort, or `max_tokens`.

### `cassette ... does not satisfy <Contract>`

A cassette recorded before the contract gained a field. **Re-record it; do not loosen the contract
to fit the recording.** Loosening is how a stale response becomes a wrong number in a verdict with
nothing pointing back at the cause.

### `make demo`: a scene reports the wrong outcome

Three real runs of the pipeline in replay mode, no key anywhere: a clean quarter, a mistyped
guest count caught at the cell and at the roll-up it feeds, and a nationality label the committed
lookup has never seen. Each scene asserts the `VerdictStatus` it exists to demonstrate, and `make
demo` exits 1 if any run does not match it, stated in the output as a real regression, not a
formatting issue.

| Scene reports | Cause | What to do |
|---|---|---|
| *the second scene's submission is missing* | `corpus/fixtures/F2/` was never built | run `make fixtures` first, or just run `make demo` again; the target already depends on `fixtures` |
| Scene 1 does not report `PASS` | the demo corpus stopped being a clean quarter | work it like any other regression against `corpus/demo/`, starting from `make run`'s node log |
| Scene 2 does not report `FAIL` | fixture F2's planted mutation stopped being caught | work it like an eval regression: `make eval` scores the same fixture and names which check failed |
| Scene 3 does not report `HALTED` | the two-letter placeholder in `tools/demo/run_demo.py`'s `UNRESOLVABLE_NATIONALITY` was added to the committed lookup | pick a different unresolvable code, one the lookup genuinely has no entry for, not a specific string |

### `make bundle`: assembling the release artefact

`make bundle` runs the real pipeline (`mizan run` against the demo corpus) and requires the eval
scorecard to already exist or to be produced by `make eval`. Both steps run for real; neither is
skipped quietly.

| Symptom | Cause | Fix |
|---|---|---|
| `... is missing [...]` after the run | `mizan run` did not reach `publish`, or wrote no artefacts at all | read the node log `make run` printed above the error and work it like any other halted run |
| `... has no annotated workbook` | the run never reached a claim to annotate | not expected against the demo corpus; if it happens, the run id printed above says why |
| `... has no scorecard.json or report.md, even after make eval` | `corpus/fixtures/` does not exist yet, or a cassette needed by a fixture is missing | run `make fixtures` (or `make record` with a key), then run `make bundle` again |
| `mizan-<version>.tar.gz` already sits in `dist/` | a previous build | nothing to clean up first. `make bundle` truncates the archive at that path rather than appending to it |

### `make prompts` and the prompt manifest

A prompt version is **immutable once a cassette exists against it**. The cassette key hashes the
*version*, not the text — so editing `v1.md` keeps every key intact while serving responses
recorded against a prompt nobody can read any more.

To change a prompt: add `v<n+1>.md`, point `policy.yaml` at it, run `make prompts`, and re-record.
If `test_committed_prompts_match_the_manifest` fails, a committed prompt was edited in place.

## Publishing a release

Versioning is `0.<milestone>.<patch>` through the POC: `v0.1.0` at the end of M1, `v0.6.0` at
the end of M6. Semantic versioning of a POC's public API would be a fiction.

**The order below is the procedure, not a summary of it.** A v0.6.0 release was once tagged and
published against a `main` that the release PR had never been merged into, so the tag named a tree
with no console in it while the release notes announced one. The step that was skipped is step 4,
and the reason it is easy to skip is that `main` looks fine from a terminal sitting on `develop`.
Step 5 exists to make that impossible to miss.

**1. Branch, and say what is shipping.**
```bash
git checkout develop && git pull
git checkout -b release/v0.6.0
# bump the version in BOTH pyproject.toml and src/tda/__init__.py (a test pins the pair),
# and date the CHANGELOG heading to the day it actually ships
```

**2. Prove it, on the exact tree that will be tagged.** `make` is the short form; every target
below also runs directly, which is what to use on a host where `make` itself is broken:
```bash
.venv/bin/ruff check src tools tests streamlit_app.py
.venv/bin/ruff format --check src tools tests streamlit_app.py
.venv/bin/python -m mypy
.venv/bin/python tools/guard/import_guard.py
.venv/bin/python tools/guard/agent_schema_lint.py
.venv/bin/python tools/guard/secret_guard.py
.venv/bin/python tools/policy/validate_policy.py
.venv/bin/python -m pytest
.venv/bin/python -m tda.eval --fixtures corpus/fixtures --out artifacts/eval
.venv/bin/python -m tda.eval.repro
```

**3. Commit and push the release branch.**

**4. Open the release PR into `main`, and merge it.** With **the eval scorecard for that tag in
the PR body**: a release whose eval has regressed does not ship. `main` carries a `pull_request`
rule, so there is no direct push and no way to do this by accident from a shell.

**5. Check that `main` actually moved before you tag anything.**
```bash
git checkout main && git pull
git rev-list --count origin/develop..origin/main   # expect 0 or more, never 34
git log --oneline -1                               # this is the commit about to be tagged
```
If `main`'s head is still the previous release's merge commit, step 4 did not happen. Tagging here
is what produces a release that names a tree it does not contain.

**6. Tag the merge commit on `main`, and push the tag.**
```bash
git tag -a v0.6.0 -m "v0.6.0: <what shipped>"
git push origin v0.6.0
```

**7. Build the bundle from the tagged tree**, not from whatever the working copy happens to hold.
The manifest records the commit and whether the tree was dirty, so a clean checkout matters:
```bash
.venv/bin/python tools/release/bundle.py      # → dist/mizan-<version>.tar.gz
```

**8. Publish the GitHub release** against that tag, with the bundle attached.

**9. Merge `main` back into `develop`.** `develop` requires a passing status check, so a direct
push of a fresh merge commit is refused, because the check has never run against it. Open a PR from
`main` into `develop` and merge that instead. Skipping this leaves the two branches permanently
apart by one merge commit.

## Deploying to Streamlit Community Cloud

The hosted demo (PRD-115) is `streamlit_app.py`, deployed from `apundhir/mizan-agent`'s `main`
branch, on Streamlit Community Cloud's own free tier - no Docker in this path, no server to
provision.

**First deploy.**

1. At [share.streamlit.io](https://share.streamlit.io), click **Create app**, then **Yup, I have
   an app**.
2. Repository `apundhir/mizan-agent`, branch `main`, main file path `streamlit_app.py`.
3. **Advanced settings**: Python version `3.12`. Paste this into **Secrets**, verbatim:
   ```toml
   MIZAN_LIVE_MODE = "false"
   ```
   Leave `MIZAN_SINGLE_OPERATOR` out entirely. It tells the review screen and the console's replay
   panel that every run on disk belongs to one trusted person - correct for `docker compose` and
   `make review` on somebody's own machine, never correct for a link more than one viewer can
   reach. Absent is the safe default; do not add it here.
4. Deploy. The first build installs everything in `requirements.txt`; later pushes to `main`
   redeploy in place, code changes landing in seconds and dependency changes taking longer.

**A public deployment runs prepared scenes only, and that is the default.**
`MIZAN_UPLOAD_ENABLED` is what offers a viewer the "Your own files" path, and leaving it out of the
Secrets box, as step 3 above does, means the console never renders an uploader and never spawns a
verification subprocess. The five scenes are what a public audience came to see, and they run this
repository's own committed corpus. Everything under the next heading applies only to a deployment
that turns the flag on, which on this platform means one you have decided to trust.

### If you enable uploads

**An uploaded submission verifies in its own resource-limited subprocess.** A scene's files are
this repository's own corpus and run in process, live, stage by stage; an upload is the one input
this codebase has never authored, so `tda.review.sandbox` runs `mizan run` against it as a child
process with a memory limit (`RLIMIT_AS`, Linux only - see ADR-0010 §7 for why), a CPU limit
(`RLIMIT_CPU`), a wall-clock timeout on top, and a cap on how many such children may run at once.
The defaults (1 GiB, 30 CPU seconds, 90 seconds wall clock, 2 concurrent) leave headroom above what
the demo submission itself measures and rarely need changing; they are configurable, four more
optional root-level secrets, because the right ceiling depends on the hosting tier, which this
codebase does not control:
```toml
MIZAN_SANDBOX_MAX_MEMORY_BYTES = "1073741824"
MIZAN_SANDBOX_MAX_CPU_SECONDS = "30"
MIZAN_SANDBOX_TIMEOUT_SECONDS = "90"
MIZAN_SANDBOX_MAX_CONCURRENT = "2"
```
Leave them out unless a specific hosting tier's own memory ceiling calls for a lower number, or a
real submission's size calls for a higher one. Unlike the console's other numeric settings, a
malformed value here is refused rather than quietly falling back to the default - a typo in a
resource limit is exactly the kind of mistake that should surface, not silently run under a number
nobody chose - and the refusal reaches a viewer as one failed run, not a broken deployment; the
first run after a bad edit will say so.

**These two numbers are a bound chosen before this app has ever run on the real container, not a
measurement of it.** Streamlit Community Cloud publishes only a range for what an app's container
may get ("690MB minimum, 2.7GB maximum" per its own docs, checked 2026-09-19) - a G5 security
review found the original default sized against the demo submission's own cost alone, with nothing
capping how many children could run at once, could let a handful of concurrent uploads push the
whole container, parent included, past whatever it was actually given.

**This calibration is a prerequisite for enabling uploads, not for launching.** A scenes-only
deployment starts no child process, so neither number is ever reached and neither gates the public
URL going out. Before setting `MIZAN_UPLOAD_ENABLED` on any deployment more than one person can
reach, measure both: watch memory during "First checks after any deploy" below and during "An
upload" in "Testing the hosted demo", then lower `MIZAN_SANDBOX_MAX_MEMORY_BYTES` or
`MIZAN_SANDBOX_MAX_CONCURRENT` if the container's own ceiling turns out to be nearer the low end
of that range than the demo submission's own cost leaves room for.

**A root-level secret becomes an environment variable; a secret inside a `[section]` does not.**
This is the whole safety property `tda.review.live` depends on, and it is Streamlit's own
documented behaviour, not this codebase's assumption - so the key must go in at the top level of
the Secrets box, never nested under a heading.

**Privacy.** App settings → **Sharing** → **Only specific people can view this app**, then add
viewers by email; each gets a link and signs in with Google OAuth or a single-use emailed link.
The account is allowed one private app at a time - a second private app (a spike, a second demo)
needs the first made public or deleted first.

**First checks after any deploy.** Open the app's own URL, not `localhost`: the Run page lists
five scenes and **no uploader of any kind**, "A clean quarter" reaches PASS, "Open in Review"
carries the run across, and the sidebar reads *"Live mode: off. Provider: replay (committed
cassettes, no API call)."* Then, in a
**second browser profile** (or an invited viewer's own account) that has never used the console in
this session: confirm the Review page offers no run to pick and the console's replay panel says
"No past runs yet" - the actual property `MIZAN_SINGLE_OPERATOR` being absent is supposed to buy,
checked against the deployed app rather than only against `make ci`. If any of those is wrong, do
not send the link out.

**Before a live demo (viewer-facing, don't skip):**

- Open the app yourself first. Streamlit Community Cloud sleeps an app after twelve hours with no
  traffic; a sleeping app shows a wake-up page to the *first* viewer who opens it, developer or
  not, and that should not be the audience.
- Confirm the sidebar still reads "Live mode: off" before deciding whether this demo needs a live
  run at all - most of what there is to show works in replay, for free, with no key anywhere.

**Enabling live mode for a demo.**

1. Create a demo-only Anthropic API key, in its own workspace, with a monthly spend limit.
2. App settings → **Secrets**, add two root-level lines and **Save**:
   ```toml
   MIZAN_LIVE_MODE = "true"
   ANTHROPIC_API_KEY = "sk-ant-..."
   ```
3. **Reboot the app** (overflow menu, or "Manage app" → Reboot) regardless of whether the platform
   already restarts it on a secrets save - a demo is the wrong moment to find out which.
4. Verify the sidebar reads *"Live mode: on. Credential: present. Live runs per session: 3. Per
   process: 20."* before anyone else opens the link.

**After the demo, in order:** set `MIZAN_LIVE_MODE` back to `"false"`, delete the
`ANTHROPIC_API_KEY` line, Save, Reboot, then revoke the key in the Anthropic console. On any
suspicion the key leaked - a screen share, a pasted log - revoke first and rotate after; do not
wait to finish the checklist.

**Rollback.** A bad `main` redeploys automatically on the next push, so the fix is a revert PR,
not a platform action: `git revert` the offending commit, push to `main`, and the app rebuilds
within minutes. If `main` cannot be fixed inside the hour, deploy a second app from the last known
good tag rather than leave a broken one live. **Kill switch**, fastest first: remove viewers under
Sharing (link stops working immediately for everyone but the owner); failing that, delete the app
from the overflow menu.

**Reading logs without leaking a key.** "Manage app" opens the log pane; logs can be downloaded
from the same overflow menu. Before pasting a log anywhere - an issue, a chat, a message to
Anthropic support - search it for `sk-ant-` first. The redactor and the secret guard both use that
same prefix for exactly this reason; a log is not an artifact this codebase redacts on its own.

## Things that are not bugs

- **`make eval` exits 2 when a fixture went unmeasured, not only when the tree is missing.** 0
  means every fixture matched its derived expectation, 1 means at least one ran and did not
  match, and 2 covers both "no fixture tree at that path" and "a fixture hit a cassette miss" - an
  unmeasured fixture is not a pass, so it takes the exit code a build failure would, not the one a
  clean sweep would.
- **`make repro` exits 2 before it diffs anything, if either run does not reach the expected
  status or checks zero claims.** That sanity gate runs first on purpose: two runs that crashed in
  the same place, or checked nothing, would otherwise satisfy a verdict diff for free.
- **`make run` fails with a cassette miss.** Not expected on the demo corpus any more: the twenty
  committed cassettes carry a replay run from intake to publish. A miss now means the *request*
  moved, so a prompt, an output schema, the model id, the effort or an allowlist changed and the
  recording no longer matches it. The node log printed underneath names exactly where it stopped.
  `make record` with a key re-records, and the cassette diff is reviewed like a code diff;
  `make run SUBMISSION=... --provider stub` is not a workaround, because the stub refuses to invent
  an answer.
- **`make run` exits 1 on a submission that is not clean.** Deliberate. A rejected or halted
  verdict returning success would make an unread verdict look like a clean one. Exit 0 means
  `PASS`, 1 means a human must read it, 2 means the run could not complete.
- **`make datagen` rewrites files that then show as modified in `git status`.** Only if something
  changed. If the diff is not empty and you did not touch the generator, that is worth
  investigating rather than committing: see `make corpus` below.
- **The demo corpus never appears in `make eval`.** By design. It is the data the system was
  rendered from, so a clean pass on it is a tautology (ADR-0003). Only the S11 fixtures are scored.
- **`agent schema lint ok: N AgentOutput contract(s)`** — the count is the thing to read. A lint
  that passes because it found nothing to check is a state a test deliberately watches for, and it
  was the correct output until PRD-88 created `src/tda/agents`.
- **A per-agent eval case reports `not_recorded`.** Correct, and not a skip: it means no cassette
  matches that request. All twenty committed cases have one, so this is what a newly added case
  looks like, or a case whose prompt or schema moved. `make record` needs an API key and makes live
  calls, so until somebody runs it the case is unmeasured, which is stated in words rather than
  reported as a pass: see `tests/eval/agents/test_agent_evals.py` and `tests/cassettes/README.md`.
- **`make record` exits 2 with "ANTHROPIC_API_KEY is not set".** The only target that needs a key.
  Everything else — `make ci`, the whole test suite — runs offline in replay. With a key it walks
  every eval case, calls its agent once, writes one cassette per call and prints what it spent.
  `python -m tda.agents.provider --dry-run` lists the calls without making any. The key can come
  from the shell or from `.env` — which this target reads and no other does, and which the shell
  always overrides. `--no-dotenv` makes the shell the only source.
- **Occupancy reported as `not_verifiable`** when the inventory reference is missing. That is
  D-RNA-04 working: rooms available is a property attribute and is never inferred from the
  reservations. The honest output is "not verifiable, here is what is missing".
- **An upload shows "Verifying your submission…" with no live chips, unlike a scene.** Deliberate,
  not a stalled page: an uploaded submission verifies in a resource-limited subprocess
  (`tda.review.sandbox`, ADR-0010 §7), and that subprocess writes its own `nodes.jsonl` and
  `trace.jsonl` only once it finishes, so there is nothing live to poll mid-run the way there is
  for a scene running in process. The complete timeline and verdict appear together as soon as the
  subprocess exits.

## What a finished run leaves you

```
artifacts/<run_id>/
  verdict.json                  the complete result, with a summary block
  annotated_claims_2026-Q1.xlsx the hotel's own workbook, marked
  memo.docx                     one page: the verdict first, then the detail
  run.json  trace.jsonl  nodes.jsonl        the working (PRD-90)
```

**The annotated workbook is a copy.** The submitted file is never modified — `annotate` hashes it
before and after and fails the run rather than hand you an artefact it cannot prove it left alone.
Open the copy, read the `Mizan verification` sheet first: it carries the verdict, the colour key,
and any finding that has no cell to sit on.

| Colour | What it means |
|---|---|
| green | recomputed from the reservation records and matched |
| amber | a definitional difference. The comment names the rule that reproduces the figure exactly — **not** a hotel error |
| red | a material variance. The comment carries the computed value, the proposed correction and the page |
| blue | could not be checked at all |
| grey | a finding that is neither definitional nor material — a rounding difference |
| no colour | **not checked.** Silence is not approval (D-SCOPE-02) |

**The memo's first block is the verdict**, because a supervisor reads the first block and nothing
else. Its signature block says *"No human review has been recorded"* until somebody actually decides
something (PRD-91) — it will never print a name nobody supplied.

## What a release bundle contains

```
dist/mizan-<version>.tar.gz
  MANIFEST.json           the version, the git commit, the run id, the eval outcome, the file list
  run/verdict.json  memo.docx  annotated_<workbook>.xlsx        a fresh run of the demo corpus
  run/run.json  trace.jsonl  nodes.jsonl                        that run's own working
  eval/scorecard.json  report.md                                what the pipeline was measured against
```

**Every file above must be present, or `make bundle` fails instead of shipping a smaller
archive.** `MANIFEST.json` names its own commit rather than trusting an environment variable, and
lists a `git_dirty` flag: a bundle built from an uncommitted tree is not wrong, but a reader
deciding how much to trust it needs to know the difference. The run inside is always a fresh one,
never whatever was last sitting in `artifacts/`; see `tools/release/bundle.py` on why.

## Reviewing a run

```bash
make run                             # produces artifacts/<run_id>/
make review                          # opens the console and the screen on that run
make review RUN=run-a1b2c3d4e5f6     # or an older one
make review SUBMISSION=/data/hotel-x # if the run was not against the demo corpus
```

A run does not have to start on the command line first. `make review` on its own opens the Run
page: pick one of five prepared scenes or upload a workbook, reports and inventory, press Run, and
watch the same five stages and agent calls this section describes - the stage chips turn green in
order, each agent card fills in as its call returns, and the verdict panel appears the moment the
run finishes. **Open in Review** on that panel carries the run id across to the screen below, so
everything from here on applies unchanged to a run started either way.

**Pass the same `SUBMISSION` you ran with.** The screen reads the submitted files to crop the
evidence, and it checks each one against the digest `run.json` recorded — so pointing it at the
wrong directory produces *"pms_2026-01.pdf … is not the file this run read"* on the card rather than
another property's rows under this finding's citation.

Type your name in the sidebar — **a decision will not record without it**, because the point of
the gate is that somebody accountable looked. Then, per finding: what was claimed, what the records
support, the difference, and the two pieces of evidence side by side — the report rows outlined in
red on the left, the workbook cell outlined in red on the right. Accept, reject or amend, with an
optional note.

**Amend is only available on a transcription error (V1).** Proposing a corrected figure for a
definitional variance would be telling a hotel to change a number that is not wrong.

Every decision is written to `verdict.json` the moment it is made, and `memo.docx` is re-issued
with it, so the two never disagree about whether a human has looked. A closed laptop loses nothing,
and two officers on one run cannot overwrite each other.

**Definitional items are on their own tab**, under a banner saying they are a question for the
policy owner rather than a correction for the property (D-MAT-06). They still need a decision; they
are not hotel errors.

If a finding's evidence cannot be shown, the card says why in the words of the citation — *"no cell
exists: the workbook states no figure for it"* is a correct refusal, and *"could not read
pms_2026-01.pdf"* is a defect. The other findings on the screen are unaffected.

### Asking the assistant

**Ask about this verdict** at the top of the screen takes a question and answers it with citations,
or declines. It reads the verdict, the references its findings carry and `docs/01-definitions.md`,
and nothing else.

What it answers well:

- *"Which rule makes F-0002 definitional rather than an error?"* — a clause, read back to you under
  the policy version this run used.
- *"Where did the March German guest count come from?"* — the report rows and the workbook cell,
  as citations you can check against the card.
- *"Which of these are the hotel's own errors?"* — it names them by id, with the definitional
  items called out separately and D-MAT-06 cited for why. Note the shape of the question: it is
  shown no counts at all, so it answers *which* rather than *how many*.

What it will decline, by design:

- **Anything needing a figure computed.** It has no arithmetic and is shown no values. Ask *by how
  much* and it will point you at the finding, which already has the number. This is deliberate: a
  figure in its prose would sit on the same screen as figures recomputed from reservation records,
  with nothing to tell them apart.
- **Anything outside this run** — another quarter, another property, a previous submission.
- **Anything about intent.** Whether a difference was deliberate is not in the evidence.

Every question and its answer is appended to `artifacts/<run_id>/trace.jsonl` with the citations
returned and the tokens spent, and `mizan trace` shows them under **after the run**. Your questions
are part of the record of how the verdict came to be signed, so a run reviewed twice has a longer
trace than one reviewed once.

The append is redacted the same way the run's own artifacts are, which catches an email address, a
phone number or a booking reference. It does **not** catch a bare name — `tda.obs.redact` is
explicit about that limit and a test pins it. Do not type a guest's name into the box; refer to the
finding.

Four things can stop an answer arriving, and the box says which:

| What you see | What it means |
|---|---|
| *no recorded answer for this question* | replay has no cassette for it — run `make record` with a key. **Nothing is wrong with the verdict.** |
| *not configured on this machine* | no API key, live mode |
| *an answer with nothing behind it* | the assistant produced prose with no citation, so there is no answer |
| *cited evidence this run does not contain* | **it invented a reference.** Distrust everything else it has told you today and check it against the findings themselves |

The fourth is the one to act on. The answer is withheld rather than shown with a warning, because a
warning beside readable prose is a warning nobody acts on.

**Nothing the assistant says changes the verdict, and nothing it says is needed to decide one.**
If it is unavailable, the screen works exactly as it did before it existed.

## Testing the hosted demo

Everything below runs against the deployed URL, in a real browser, not `localhost` - a change that
only ever ran through `make ci` and a local `make review` has not been tested against the
environment it actually ships to. None of it needs a key; do this in replay first, live mode
second, and only if the demo calls for it.

1. **Cold start.** Open the app from a link nobody has opened in the last twelve hours (or reboot
   it first). The Run page should list five scenes within the first render; if the first scene
   selected times out, the fixture build (`corpus/fixtures/F2`, `F3`, built once per server
   process, not shipped in the repo) is the first place to look.
2. **A clean pass, twice.** Run "A clean quarter" to PASS. Reload the page in a second tab and run
   it again. Both should reach the same verdict; two different tabs are two different
   `st.session_state`s but the same server process, and this is what proves the process-wide
   pieces (the fixture cache, the live-run counter) survive that correctly.
3. **A catch.** Run "A mistyped guest count" to FAIL, open the two findings' agent cards, expand
   "What it was shown" on the mapping card, and confirm no cell value from the workbook appears
   verbatim where a `<value>` placeholder is expected - the redaction boundary the console shares
   with the written artifacts.
4. **Handoff.** From that same run, click **Open in Review**, confirm the URL changes to the
   `review` page and the run id carries across, accept one finding, and confirm the verdict panel
   updates without a page reload.
5. **A rejection.** Run "A missing monthly report." REJECTED, zero agent cards lit, zero routing
   lines - a rejected submission must never reach an agent, on the hosted app exactly as
   `tests/unit/test_console_pages.py` proves it locally.
6. **No upload path at all.** On a public deployment this is the check, not the upload itself:
   confirm there is no submission chooser, no file uploader, and no declared-hotel or period box
   anywhere on the Run page, and that the notice above the scenes says this deployment accepts no
   uploads. A hosted app offering an uploader means `MIZAN_UPLOAD_ENABLED` reached the Secrets box
   by accident; remove it and reboot before sending the link out. *On a deployment you have
   deliberately given the flag to*: download the demo workbook and PDFs from a completed run's
   evidence, change one figure, and submit through **Your own files**. The page should show only
   "Verifying your submission…" with no live stage chips while the sandboxed subprocess runs, then
   the complete timeline and verdict at once, and not an error; see ADR-0010 §7 for that trade.
7. **Private sharing.** From a browser session that has never been invited, confirm the app
   refuses; from an invited account, confirm it opens.
8. **Cross-viewer isolation.** From the invited account's session that ran step 2, in a *different*
   browser or a private window signed in as a second invited viewer who has not touched the Run
   page yet: open Review directly. It must offer no run to pick, and the console's own replay panel
   must say "No past runs yet" - not the first viewer's run or its id. This is the one check that
   only means something against the deployed app; `tests/unit/test_console_pages.py` proves the
   same property with two independent test sessions, but a deployment that quietly turned
   `MIZAN_SINGLE_OPERATOR` on would still pass every local test.
9. **Live mode, only if this demo needs it.** Follow "Enabling live mode for a demo" above, run
   one scene with `anthropic` selected as the provider, and open the mapping agent's card: its
   caption line names the provider mode that answered - `mapping/v1 - anthropic (...)`, not
   `replay`. Then follow the after-the-demo steps in order before closing the laptop.

## Where to look when a number looks wrong

1. **`docs/01-definitions.md`** — find the clause. Every metric rule has a citable id.
2. **`policy.yaml`** — find the setting. Every contestable rule is here, not in code.
3. **`docs/02-assumption-register.md`** — check whether the rule is an unratified assumption
   rather than an established regulatory rule. Six of eight are genuinely arguable.
4. **`artifacts/<run_id>/verdict.json`** — the finding itself, with the cell it was read from
   and the page it was checked against. Its `summary` block is derived on write and re-derived
   on read, so the **counts** cannot drift from the arrays they summarise — a file whose
   summary has been edited does not load. It is a count checksum and not an integrity check:
   an edited citation or policy version still loads, and what protects those is the digest of
   the inputs in `run.json`.
5. **`artifacts/<run_id>/run.json`** — the policy version and metric library version the number
   was produced under, and a SHA-256 per input file. If the digest is not the file you are
   holding, the verdict is about a different document and nothing else in this list matters.

A number and its ruleset travel together. If you have one without the other, that is the bug.

## Where to look when a *run* stops

1. **The node log**, printed by `make run` on both the success and the failure path. A node that
   was entered and never left is named under `stopped inside:` — that is where it stopped.
2. **The verdict's status.** `REJECTED` means intake refused the submission and the reason code
   says which of the four reasons fired. `HALTED` means something could not be read. Neither is a
   crash and both are on purpose.
3. **`docs/adr/0005-sequential-graph-and-deferred-resilience.md`** — if the answer you want is
   "why didn't it just retry?", it is there. A retry ladder that hides a transient failure is worse
   than a halt, because nobody can tell which runs were clean.

## Where to look when an *agent* looks wrong

1. **`make trace`** — the whole run as a tree: which agent ran under which node, what it was
   asked for, which tools it reached for, what it returned and what it cost. `mizan trace
   <run_id>` reads an older run; the file behind it is `artifacts/<run_id>/trace.jsonl`, one
   record per model call, and the format is in `docs/03-architecture.md` §5. Refused tool calls
   are in there too, on their own line, because an agent reaching outside its allowlist is a
   roster or prompt defect rather than a runtime event.
2. **`docs/03-architecture.md`** — the roster. What that agent is allowed to reach, and what its
   output contract can and cannot carry.
3. **`src/tda/agents/prompts/<agent>/v<n>.md`** — the instructions it ran under. The version is in
   `policy.yaml` and stamped into the trace, so the file you read is the file that ran.
4. **`tests/eval/agents/cases/<agent>/`** — whether the behaviour you are looking at is one the
   eval set already has an opinion about.

An agent that returned a *number* is not a tuning problem; it is a guard failure. Run
`make guard` and read `tools/guard/agent_schema_lint.py`.

## When an artifact says something was redacted

```
  redacted from the artifacts: email x1, phone x1  (personal data in the submitted workbook, not from this system)
```

The same line can name `api_key` instead. That one is not personal data in a workbook - it is the
shape of an Anthropic key (`sk-ant-...`), and the only way it reaches an artifact is a viewer
pasting one into an uploaded cell or the assistant's question box on the Run console. Same
handling: nothing to fix, nothing recoverable, and the count in `run.json` is deliberately all it
says.

This is not a defect in the pipeline and there is nothing to fix here. The submitted workbook's
label cells reach the mapping agent's prompt verbatim — they have to, or the agent cannot tell
which block is which metric — so a property that typed contact details into a header cell put
them in the prompt, and they would otherwise have reached `trace.jsonl`.

What to do: **ask the property to take contact details out of the sheet.** Contact details do
not belong in a statistical return, and the next submission will be clean.

What not to do: go looking for the original text. There is no unredacted copy anywhere — the
redaction happens before the file is written, and a viewer that could recover it would be a
second place the data lives. The counts in `run.json` say what kind and how many, and that is
deliberately all they say.

`src/tda/obs/redact.py` states what these patterns do **not** catch, and
`docs/adr/0006-observability-redaction-and-recorded-runtime.md` says why they are narrow.

## When somebody asks how long a run takes

`make run` prints a duration and `run.json` records one per node. Both are labelled *recorded,
not targeted*, and the honest answer to "is that good?" is that nobody knows yet: **no
threshold, budget or assertion on run time exists anywhere in this repository**, and none will
until the manual baseline is measured.

This is not modesty. A time-saving figure quoted before it is measured gets challenged, and the
challenge lands on the whole result rather than on the number.
