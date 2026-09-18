"""Reconciliation: where the POC earns its keep.

Detecting that a hotel's figure differs from ours is arithmetic. **Classifying** the difference is
what makes a report usable, and it is the whole argument this system makes to the regulator: a flat list of
mismatches tells an officer that something is wrong somewhere; a report that separates a clerical
error from a definitional disagreement tells them what to do about it, and to whom.

Four modules, in the order a run moves through them:

| Module | Owns |
|---|---|
| `join` | claims beside computed values on the canonical key, and the three ways that can fail |
| `permutations` | re-running the metrics under alternative rulesets (D-CLS-07) |
| `classify` | the ladder, walked in the order policy declares (D-CLS-01..06) |
| `engine` | evidence, finding ids, and keeping definitional items out of the hotel-error count |

**No model is consulted anywhere in this package**, and that is enforced rather than intended:
`tools/guard/import_guard.py` fails the build if anything here imports the agents package or a model
SDK, and `classification.consults_model` is pinned `false` in `policy.yaml` so the configuration says
the same thing from the other side. The code finds the cause; the model only writes the sentence
(D-CLS-10).
"""

from tda.reconcile.classify import (
    CLAUSE_BY_CLASS,
    Classification,
    ClassificationError,
    classify,
)
from tda.reconcile.engine import Reconciliation, reconcile
from tda.reconcile.join import Pair, Pairing, join
from tda.reconcile.permutations import Explanation, PermutationIndex

__all__ = [
    "CLAUSE_BY_CLASS",
    "Classification",
    "ClassificationError",
    "Explanation",
    "Pair",
    "Pairing",
    "PermutationIndex",
    "Reconciliation",
    "classify",
    "join",
    "reconcile",
]
