"""The demo scenes: three real runs of the actual pipeline, in replay mode, no key required.

This package is neither the generator (`tools/datagen/`) nor the fixture builder
(`tools/fixtures/`), and it is not subject to either one's import-guard rule. It exists to show the
pipeline working, not to produce or score evidence, so it is free to import `tda` directly. That is
the thing the other two tools must not do, because their job is to stay independent of the code
they build ground truth or expectations for.

Two scenes point at what already exists: `corpus/demo/` (unmutated) and `corpus/fixtures/F2/`
(built by `make fixtures`, never committed). The third scene, the refusal, has no committed corpus
at all, by design: it demonstrates D-NAT-12 rather than scoring against it, so there is no
expectation to derive and nothing worth freezing on disk. `run_demo.build_refusal_submission`
re-renders one month's report from a mutated copy of `corpus/demo`'s own ledger, into a temporary
directory, every time `make demo` runs.
"""

from __future__ import annotations
