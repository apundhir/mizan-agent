"""VIOLATION FIXTURE - the eval scoring the system against itself.

The shortcut is not laziness, it is the reasonable-sounding one: the reconciliation layer already
knows how to classify a variance, so why restate the ladder in the fixture builder? Because the
builder's output is what the reconciliation layer is then scored against. Import it here and
`make eval` reports that `tda.reconcile` agrees with `tda.reconcile`, at whatever precision and
recall that tautology produces, which is 1.0.

The same argument as the generator rule one layer down. `tools/datagen/` may not import `tda`
because ground truth co-derived with the code under test is not ground truth; `tools/fixtures/`
may not import `tda` because an expectation co-derived with the code under test is not an
expectation.
"""

from tda.reconcile.classify import classify


def expected_findings(pairs, index, policy):
    return [classify(pair, index, policy) for pair in pairs]
