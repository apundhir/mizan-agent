# Cassettes

One file per model call: `tests/cassettes/<agent>/<key>.json`.

A cassette records that **this prompt, against this schema, produced this response from a real
model**. Committed, it keeps proving that offline, in CI, on every push, at zero cost — which is
strictly more than a mock offers, because a mock proves only that the code compiles.

`make ci`, `make eval` and `make repro` all run in **replay** mode. No API key, no network.

## The key

SHA-256 over canonical JSON of everything that can change the answer: key schema version, agent,
prompt version, system prompt, rendered messages, **output schema**, model id, effort, `max_tokens`.
Timestamps and run ids are deliberately excluded — including them would make every call a fresh key
and every replay a miss.

## Reviewing a cassette diff

**A cassette diff is a code diff.** It means a prompt or an output schema moved, and the PR body has
to say which and why the new response is better. Each file stores `request_canonical` alongside the
response so a reviewer can see *what was asked*, not just that a hash changed.

A reviewer who rubber-stamps cassette diffs has disabled this layer without noticing.

## What is in here, and what put it there

Two recording sets, not one.

**Twenty-three cassettes across five agents, one per committed eval case**: `critic` 4, `mapping` 4,
`narrative` 3, `resolution` 4, `reviewer_assist` 8.

`make record` walks every committed eval case under `tests/eval/agents/cases/`, calls its agent
once against the live API, and writes one cassette per call. The case set is the recording set on
purpose: the cassettes are then recordings of exactly the calls the eval suite replays, and
`tda.agents.cases.run_case` is shared by both so a case cannot be recorded against one question and
replayed against another.

**Twenty-three more, for `narrate()`/`grade()` against real fixture findings**: 11 real
findings across F2, F3, F4 and F6, one `narrative` cassette and one `critic` cassette each, plus one
held-back, deliberately bad narrative graded against F3's real definitional finding and never used
as that fixture's actual narrative. A fixture's findings are not declared in a case file the way an
agent-eval case is, so `tda.eval.record_narratives` (also reachable through `make record`) records
these directly against each fixture's published `Verdict` instead.
`tests/eval/pipeline/test_narrative_grading.py` is what replays the held-back one.

Recording needs `ANTHROPIC_API_KEY` and live calls, so adding or refreshing a cassette is always a
deliberate act. A case with no matching cassette is reported by the eval suite as `not_recorded`,
**not** as a skip, because a skip reads as a pass in a CI summary and "we have not measured this"
is not a pass. No case is in that state today. Hand-authoring a cassette would fabricate the
evidence this layer exists to provide, which is why the directory sat empty until a key arrived
rather than being filled by hand.

`make ci` was green before any of these existed and is green now: the harness's own proof, that the
scorer rejects a wrong answer, needs no model at all. What the recordings add is the other proof,
that the agents hold up against their cases, and a `make run` that completes end to end offline.

    make record                                          # every case, needs a key
    python -m tda.agents.provider --dry-run              # list the calls, make none
    python -m tda.agents.provider --agent resolution     # one agent's cases
