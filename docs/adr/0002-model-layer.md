# ADR-0002 · The model layer: cassettes instead of `temperature`

- **Status:** Accepted
- **Date:** 2026-09-13
- **Related:** [ADR-0001](0001-deterministic-core-agentic-edges.md) · [ADR-0004 agent runtime](0004-agent-runtime.md)

## Context

This POC asks to be trusted on one property: **run it again and get the same answer.** `make repro`
exists to demonstrate exactly that. And the system contains a language model, which is not
deterministic.

The obvious mechanism is `temperature 0`. Two things are wrong with it.

**`temperature` no longer exists.** It is **rejected with HTTP 400** on current models, along with
`top_p` and `top_k`. Code written against it would not run.

**It never did what it was credited with.** `temperature 0` makes sampling greedy; it does not
make inference bit-reproducible. Batching, hardware and server-side changes all move the output.
It was always a *reduction* in variance being treated as an elimination of it, and a fee
calculation cannot rest on that distinction being ignored.

So the reproducibility mechanism has to be built rather than configured.

## Decision

**A four-adapter provider interface. Reproducibility comes from committed cassettes, not from
sampling parameters.**

### 1 · One interface, four adapters

| Mode | Used by | Network |
|---|---|---|
| **`replay`** | `make ci`, `make eval`, `make repro`, CI | no |
| `stub` | unit tests | no |
| `anthropic` | a live run | yes |
| `record` | `make record` | yes |

`replay` is the default. Which provider runs behind the interface is a config change, and keeping
it one is what stops a deployment question leaking into the code. The model id lives in
`policy.yaml`, which is the single place it is pinned.

### 2 · Structured output on every call

Every request names a Pydantic output contract. The response is that contract or an
`OutputValidationError`. **There is no fall-back to free text**, because free text cannot enter the
numeric path: the metric library accepts only typed record objects.

### 3 · A canonical cassette key

Every call is identified by a SHA-256 over canonical JSON of everything that can change the
answer: `key_schema_version`, agent, prompt version, system prompt, rendered messages, **the output
schema**, model id, effort, `max_tokens`. Sorted keys, compact separators, `ensure_ascii=False`.

Deliberately **excluded**: timestamps, run ids, request ids, retry counts. Including any of them
would make every call a fresh key and every replay a miss — which would look like a
reproducibility feature while destroying it.

Including the **output schema** is the subtle one. Same prompt against a wider contract is a
different question; a key that ignored the schema would serve the narrow recording for the wide
request and deserialise into a contract missing a field the caller now relies on.

### 4 · A replay miss is a hard error

Never a fall-through to a live call. A replay mode that quietly reaches the network on a miss is
**worse than having no replay mode**: CI passes, spends money, and stops being reproducible,
without a single line of output saying so.

### 5 · Prompts are versioned files, and a version is immutable

`prompts/<agent>/v<n>.md`. The cassette key hashes the *version*, not the text — a version is what
a human reasons about. That choice creates exactly one dangerous failure: editing `v1.md` leaves
every cassette recorded against `v1` with an unchanged key, serving a response recorded against a
prompt nobody can read any more.

`MANIFEST.txt` holds a digest per version and a test compares it against disk. To change a prompt,
add `v<n+1>.md` and point `policy.yaml` at it.

### 6 · Telemetry is not an agent output

Token counts live in `tda.obs`, not on a contract. They are integers, and an integer on an agent
output is precisely what the schema lint forbids. A count of what a call cost is not an agent's
answer, and keeping them apart is what lets the guard stay strict — see the enforcement note below.

## Enforcement

| Rule | Enforced by | Proven by |
|---|---|---|
| No `temperature` / `top_p` / `top_k` | `build_request_params` never emits them | `test_the_built_request_has_no_temperature`; and `policy.schema.json` rejects a policy reintroducing it |
| Structured output on every call | the `LLMProvider` signature requires an `output_type` | `test_the_built_request_always_declares_the_output_contract` |
| Key stability across processes | canonical JSON, no `hash()`, no process state | `test_key_is_stable_across_processes` — shells out to a fresh interpreter |
| A miss never goes live | `CassetteMiss` raised by `ReplayProvider` | `test_a_miss_raises_rather_than_calling_the_live_api` |
| A stale cassette fails loudly | validated against the contract on read | `test_a_stale_cassette_fails_loudly` |
| Prompt versions immutable | `MANIFEST.txt` digests | `test_committed_prompts_match_the_manifest` |
| Telemetry stays off contracts | schema lint targets the `AgentOutput` marker | `test_schema_lint_does_not_flag_telemetry` |

**A key that is stable only inside one process is not stable.** `hash()` is salted per
interpreter, so a key built from it would look perfectly stable across a test run and change on
every CI invocation, turning every replay into a miss. That is why the stability test spawns a
subprocess rather than comparing two objects.

## Consequences

### Accepted costs

- **Cassettes are an artefact to maintain.** They go stale when a prompt or a schema changes, and
  `make record` needs a key and bills whoever owns it. That is the price of offline
  reproducibility, and it is cheaper than the alternative.
- **Cassette diffs must be reviewed like code.** A changed cassette means a prompt or a schema
  moved; the PR has to say which and why the new response is better. A reviewer who rubber-stamps
  cassette diffs has disabled this whole layer without noticing.
- **The live path is thinly tested here.** There is no API key in this environment, so this
  layer tests the *request built* rather than the round trip. The network path is exercised
  first by `make record`.
- **Recording is not reproducible.** A `record` run is a live run; two of them can disagree. That
  is inherent, and it is why `ProviderMode.RECORD` appears in the run ledger — a verdict produced
  during a recording session is a different kind of evidence from one produced in replay.

### What is bought

- **CI is offline, free and byte-identical.** No API key, no network call to a model, on every push.
- **A cassette is evidence, where a mock is not.** It records that *this prompt, against this
  schema, produced this response from a real model*, and keeps proving it at zero cost. A mock
  proves the code compiles.
- **`make repro` measures the pipeline** rather than the model's sampling variance.
- **The region question stays a config change.** If this work moves to a pinned-region deployment,
  that is a new adapter, not a rewrite.

### What this does not claim

Cassettes make the system reproducible. They do **not** make the prompts good. A cassette faithfully
replays a bad answer forever, and will do so in CI, green, at no cost — which is a real hazard, not
a hypothetical one. The prompt quality question is answered by per-agent eval sets and by the
critic agent grading narratives, not by anything in this layer.

## Alternatives considered

**Pin `temperature` and accept it.** Not available: HTTP 400. And it would have been the weaker
guarantee even if it were.

**A mock provider instead of cassettes.** Cheaper and maintenance-free, and it proves only that
the code compiles. It cannot tell you a prompt still works against the current model, which is the
question that actually matters when a prompt is an input to an invoice.

**Record once and freeze the model id forever.** Tempting — a pinned model plus a cassette is
maximally stable. Rejected because the pin then silently becomes a dependency on a model that will
eventually be retired, and the failure arrives as a 404 at the worst moment. The model id is
pinned *in policy*, visible in every verdict, and changed deliberately.

**Hash the prompt text into the cassette key instead of the version.** Would make an edited prompt
a cassette miss automatically, removing the need for `MANIFEST.txt`. Rejected because the key
becomes something no human can reason about: "why did this miss?" would require diffing two hashes
rather than reading `v1` versus `v2`. The manifest gets the same guarantee while keeping the key
legible — at the cost of one more file, which is the right trade.
