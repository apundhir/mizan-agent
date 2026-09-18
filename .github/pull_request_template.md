## What changed

<!-- One paragraph. The diff shows what; this says what it means. -->

Refs: PRD-

## Why

<!-- The reason this change exists. If it implements a Linear issue, the "why" is
     usually the acceptance criterion it satisfies — name it. -->

## How it was verified

<!-- Not "tests pass". Which test, exercising which behaviour, and what would
     have failed before. For a bug fix: the failing case, now passing. -->

- [ ] `make ci` green locally
- [ ] Unit tests added or extended for the behaviour changed
- [ ] `make eval` unchanged — or the movement is explained below
- [ ] `make repro` clean

## What this deliberately does NOT do

<!-- Scope boundaries. Deferred work belongs here, not in a TODO comment. -->

## Checks that must not be bypassed

- [ ] Architecture guard green — no model client under `metrics/` or `reconcile/`
- [ ] Agent schema lint green — no numeric fields in agent output contracts beyond `page`/`row`
- [ ] Every new `Finding` path carries both a PDF page/row range and an Excel cell
- [ ] `policy.yaml` version bumped if a contestable rule changed
- [ ] Cassette diffs (if any) explained: which prompt or schema changed, and why the new response is better

## Docs

- [ ] ADR added for any architectural decision taken
- [ ] `docs/04-runbook.md` updated if a `make` target changed behaviour
