"""The scored fixtures: declarative mutations of the demo corpus, with expectations derived.

This package is the **producer**. It builds `corpus/fixtures/F1..F6/` from `corpus/demo/` and writes
each fixture's `expected.json` beside it. `src/tda/eval/` is the consumer and scores a run against
that file. The seam between them is JSON on disk, and it is a seam rather than a function call for
the reason the import guard states: a builder that could import `tda.metrics` or `tda.reconcile`
would compute the expected outcome with the code under test, and the eval would then be measuring
whether the code agrees with itself.

That is the same argument `tools/datagen/` already makes about the corpus, one layer up. The
generator may not import `tda`; neither may this.

## Why the demo corpus is not enough on its own

A clean pass on data the system was rendered from demonstrates that nothing crashed. It cannot
demonstrate that the system would *notice*. Two of the six fixtures therefore plant nothing at all
and must still come back empty, because precision is the half a reviewer feels and a system that
finds errors everywhere is not a verification system.
"""

from __future__ import annotations

from typing import Final

# Bumped when a change to the spec, the mutation engine or the derivation would alter a fixture's
# expected outcome. It is written into every `expected.json`, so a stale fixture tree scored against
# a newer builder fails loudly rather than being scored against the wrong expectation.
FIXTURE_SET_VERSION: Final = "1.0.0"
