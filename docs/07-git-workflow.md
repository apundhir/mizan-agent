# Git workflow, PR discipline and Definition of Done

This repo is a reference implementation, so the *process* is part of the deliverable. A second
engineer should be able to read this page and contribute without asking anyone anything.

## 1. Branches

| Branch | Role | Who writes to it |
|---|---|---|
| `main` | Release trunk. Every commit is a tagged, demonstrable state. | release PRs only |
| `develop` | Integration branch. All feature work lands here first. | feature PRs only |
| `feature/PRD-<n>-<slug>` | One Linear issue, one branch. | the author |
| `fix/PRD-<n>-<slug>` | A defect against work already on `develop`. | the author |
| `chore/<slug>` · `docs/<slug>` · `ci/<slug>` | Housekeeping with no Linear issue. | the author |
| `release/v<x.y.z>` | Cut from `develop`, stabilised, merged to `main` **and** back to `develop`. | the release owner |

Nobody commits directly to `main` or `develop`. Both carry an **active GitHub ruleset**:

| | `main` | `develop` |
|---|---|---|
| Restrict deletions | yes | yes |
| Block force pushes | yes | yes |
| Require status checks (`lint · types · guards · policy · tests`) | yes | yes |
| Require a pull request | **yes** (0 approvals) | no |

`main` therefore only ever moves through a release PR someone merges in the UI, which is the right
shape for the branch that gets tagged.

### How work lands, given those rules

The status-check rule has a consequence worth stating, because it looks like a misconfiguration
the first time it bites: **a direct `git push` to `main` or `develop` is always rejected**, even
when CI is green elsewhere. Required checks are evaluated against the commit *being pushed*, and
CI can only run once that commit exists on the remote. So there is no such thing as a direct push
that satisfies the rule.

That is not a problem to work around — it is the rule working. Work lands one way:

```
feature branch → push → PR → CI green → merge the PR
```

Two ways to take the last step, and the second is preferred:

1. **Merge the PR** once its checks are green.
2. **Enable auto-merge when the PR is opened**, and let GitHub merge it the moment checks pass.
   Nothing waits on a human being awake, and a PR whose CI later fails simply never merges —
   which is the correct outcome rather than an outcome somebody has to notice.

`delete_branch_on_merge` is on, so the head branch cleans itself up. Nothing needs pruning by hand.

**Never reach for `--force`, and never try to bypass a rule to land work.** If a push is rejected,
the rejection is information: either the branch is behind, or CI has not passed, or the change
should have been a PR. All three are answered by opening a PR.

The Linear issue identifier in the branch name is not decoration — Linear picks it up and moves the
issue to **In Progress** on first push and to **Done** when the PR merges. Real examples:

```
feature/PRD-80-definitions-and-policy
feature/PRD-83-synthetic-corpus-generator
feature/PRD-85-pdf-extraction-refusal
fix/PRD-85-month-spanning-row-offset
release/v0.4.0
```

## 2. Commits — Conventional Commits, and they are read

```
<type>(<scope>): <imperative summary, ≤ 72 chars>

<body — why, not what. What is in the diff. Why is not.>

Refs: PRD-<n>
```

**Types:** `feat` · `fix` · `docs` · `test` · `refactor` · `perf` · `build` · `ci` · `chore` ·
`revert`. **Scopes** track the package layout: `contracts` · `policy` · `metrics` · `extract` ·
`excel` · `reconcile` · `agents` · `graph` · `outputs` · `review` · `obs` · `datagen` · `fixtures` ·
`guard` · `eval` · `docs` · `ci`.

A breaking change to a contract or to `policy.yaml` carries a `!` and a `BREAKING CHANGE:` footer,
because both are things other people's numbers depend on:

```
feat(policy)!: make nationality month basis explicit per metric

BREAKING CHANGE: policy.yaml v1 → v2. `month_basis` moves from a single
top-level key to a per-metric mapping. Every verdict produced under v1
is no longer comparable; regenerate the corpus and re-run the eval.

Refs: PRD-2
```

**Commits are small and each one leaves the build green.** A PR of eight readable commits is worth
more to a reviewer than one commit of eight hundred lines — this is a teaching repo, and the commit
log is part of what it teaches.

## 3. The board is the status surface

Anyone should be able to open the Linear board and know what is happening without asking. That
only works if states are moved **as the work happens**, not reconstructed at the end — a ticket
that goes Todo → Done in one jump records that something was delivered and nothing about whether
it is in trouble.

### The five states, and what each one actually asserts

| State | Entry criteria — all of them | What a reader can conclude |
|---|---|---|
| **Backlog** | Has at least one unmet blocker, or is out of the current milestone | Not pickable. Do not start it. |
| **Todo** | **Every blocker closed.** Ready to pick up now | This is the pickable queue. If it is empty, the project is blocked. |
| **In Progress** | Feature branch created and pushed | Someone is actively writing this. |
| **In Review** | PR open **and** `make ci` green locally **and** CI green on the PR | Work is complete and verified; waiting on merge. |
| **Done** | Merged to `develop`, every acceptance criterion ticked | Delivered. |

Two rules keep the states honest:

- **An issue moves to Todo only when its last blocker closes.** Leaving blocked work in Todo makes
  the board claim everything is startable, and then the column stops meaning anything. When a PR
  merges, promote whatever it unblocked in the same pass.
- **In Review means verified, not merely finished.** Moving there before CI is green makes the
  column mean "I think I'm done", which is not a state anyone can plan against.

### There is no Testing state, and where testing lives instead

This team's workflow has no `In Testing` column, and the API cannot create one. Testing is
therefore not a separate state here — it is an **entry criterion for In Review**: `make ci`
green locally, CI green on the PR, and the result recorded as a comment on the issue.

That is a deliberate choice, not a workaround. A separate Testing column is worth having when a
different person or environment does the testing; when the gate is an automated pipeline on every
push, a column for it would be occupied for seconds and would tell a reader nothing. *(If a
Testing state is added in Linear's team settings, use it: PR open → In Testing → CI green → In
Review. The distinction becomes real the moment a human tester is involved.)*

### Comments are the progress trail

State changes say where a ticket is; comments say what happened. Comment on an issue when:

- **Starting** — the plan, and anything the issue's own acceptance criteria got wrong.
- **A decision is taken** that a future reader would otherwise have to infer from the diff.
- **Something goes wrong** — a bug found, a blocker hit, an assumption broken. These are the most
  valuable comments in the system and the ones most often skipped, because writing down that
  something broke feels worse than quietly fixing it.
- **Handing to review** — the CI run, and anything a reviewer should look at first.
- **Closing** — what shipped, and any acceptance criterion renegotiated rather than met.

Never comment to say "working on this". The state already says that.

### Acceptance criteria are ticked, not remembered

Issue descriptions carry `- [ ]` checklists. **Tick them as they are satisfied**, and never close
an issue with unticked criteria — either it is not done, or the criterion was wrong and the issue
should say so explicitly. An issue closed with five unticked boxes is a claim nobody can check.

### Milestone completion

When a milestone's last issue closes: post a **project status update** (health, what shipped,
what moved), then cut the release per §5.

## 4. Pull requests

One PR per Linear issue. The PR body follows `.github/pull_request_template.md` and answers four
questions: what changed, why, how it was verified, and what it deliberately does *not* do.

Rules that are not negotiable:

- **Green before review.** `make ci` passes locally before the PR is opened. CI re-runs it.
- **The architecture guard is never skipped.** If `metrics/` or `reconcile/` needs something from an
  agent, the design is wrong, not the guard.
- **Evidence assertions are never relaxed to make a test pass.** A finding without a PDF page and an
  Excel cell is a bug in the finding, not in the assertion.
- **A PR that changes a number changes the eval.** If `make eval` output shifts, the PR body says
  which fixture moved and why, and `truth_metrics.json` is regenerated by the generator — never
  hand-edited to match.
- **Cassettes are reviewed like code.** A diff in `tests/cassettes/` means a prompt or a schema
  changed; say which, and why the new response is better.
- Squash-merge feature PRs into `develop` when the commit history is noisy; merge-commit when the
  individual commits are worth keeping (usually they are, here).

## 5. Releases

`develop` → `release/v<x.y.z>` → `main`, tagged `v<x.y.z>`, with the artefact bundle
(`verdict.json`, annotated workbook, memo, eval report) attached. Versioning is
`0.<milestone>.<patch>` through the POC — `v0.1.0` at the end of M1, `v0.6.0` at the end of M6 —
because semantic versioning of a POC's public API would be a fiction.

Every release PR body carries the eval scorecard for that tag. A release whose eval has regressed
does not ship.

## 6. Definition of Done

An issue is Done when **all** of these hold. Not "when the code works".

- [ ] Acceptance criteria on the Linear issue are each satisfied, or explicitly renegotiated on the issue
- [ ] `ruff check` and `ruff format --check` clean
- [ ] `mypy --strict` clean on `src/` and `tools/`
- [ ] Unit tests cover the behaviour, and every metric test cites the `definitions.md` clause it tests
- [ ] Architecture guard green — no model client under `metrics/` or `reconcile/`; generator ⊥ metric library
- [ ] Agent schema lint green — no numeric fields in agent output contracts beyond `page` / `row`
- [ ] `make eval` unchanged, or the change is explained in the PR body
- [ ] `make repro` clean — two runs, identical verdict, timestamps excluded
- [ ] An ADR exists for any architectural decision taken
- [ ] `docs/04-runbook.md` updated if a `make` target changed behaviour
- [ ] Every acceptance criterion on the Linear issue is **ticked**, or explicitly renegotiated in a comment
- [ ] The issue carries a comment trail: what was planned, what went wrong, what shipped
- [ ] Issues the merge unblocked have been promoted Backlog → Todo
- [ ] PR reviewed and merged to `develop`; Linear issue moved to Done

## 7. Local loop

```bash
make setup          # venv + dev dependencies, python 3.12
make ci             # lint · types · guards · policy · corpus · tests   ← run before every push
make datagen        # regenerate the synthetic corpus (byte-reproducible)
make corpus         # verify the committed corpus still matches the generator
make run            # one submission end to end → artifacts/
make trace          # the most recent run as a tree (RUN=<run_id> for an older one)
make review         # the verification officer's screen (RUN=<run_id> for an older run)
make eval           # score every fixture against its derived expectation
make repro          # run twice, diff the verdict, timestamps excluded
make demo           # the three scenes: pass, catch, refusal
make record         # refresh model cassettes against the live API (needs ANTHROPIC_API_KEY)
make bundle         # assemble dist/mizan-<version>.tar.gz: a fresh run, the eval report, a manifest
```

`make ci` is the same command CI runs. If it passes locally and fails in CI, that discrepancy is a
bug worth fixing in its own right.
