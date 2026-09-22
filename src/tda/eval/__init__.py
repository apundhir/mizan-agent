"""Pipeline-level evaluation: score a run against a derived expectation, and prove two runs agree.

The **consumer** half of the eval harness. `tools/fixtures/` builds the fixtures and derives what each one
should produce; this package runs them and scores what actually came out. The seam is
`expected.json` on disk, specified by `tools/fixtures/expected.schema.json`, and it is a file rather
than a function call because neither package may import the other: an expectation computed by the
code that produces the verdict would score the system against itself.

## Two layers of eval, and why they do not share a vocabulary

`tests/eval/agents/` scores an *agent answer* — whether prose leaked a figure, whether a citation
was offered, whether an abstention was right. This package scores a *Verdict* — classes, severities,
escalation targets, cells, pages, and above all whether the set of findings is exactly the expected
set rather than a superset.

What they do share is the idea of a result, and it lives here: `Outcome`, `Check` and `verdict_for`
are defined in `tda.eval.scoring` and re-exported by the agent harness. One definition rather than
two that drift, and it has to sit on this side because `src/` cannot import `tests/`.

## Unmeasured is not a pass

Inherited from the agent harness and non-negotiable here too. A fixture whose cassette is missing
reports `NOT_RECORDED` in words and counts as a failure, because a skip reads as a pass in a CI
summary and "we have not measured this" is not a pass.
"""

from __future__ import annotations
