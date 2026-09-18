"""The synthetic corpus generator — ledger first, documents second, truth third.

**Why this is task one.** Every other story in the build needs data with known answers. If the
documents came first and the expected numbers were worked out afterwards by hand, the expected
numbers would be a second opinion rather than ground truth, and the first disagreement between
them and the pipeline would be unresolvable: nobody could say which side was wrong.

So the order is fixed and it is the whole design:

```
spec  →  ledger (reservations + inventory)  →  documents (PDFs, workbook)
                      ↓
                 truth_metrics.json
```

The ledger is generated once from a seed. The documents are *renderings* of it and the truth
metrics are an *aggregation* of it. Nothing is authored twice, so a document and the truth cannot
disagree — and if a rendering is wrong, it is wrong about something the ledger states plainly.

**This package must not import `tda`.** Not the metric library, not even the contracts. The import
guard enforces it (`tools/guard/import_guard.py`, rule 2) and the reason is narrower than
"independence" in general: `ReservationRecord.occupied_nights()` is *exactly* the month
apportionment primitive that D-RNS-03 defines. A generator that called it would produce a
`truth_metrics.json` co-derived with the thing it is supposed to check, and the POC would then
demonstrate only that the code equals itself. `aggregate.py` therefore re-derives apportionment
from the clause text. The two implementations agreeing is evidence; one implementation agreeing
with itself is not.

What is legitimately shared is *configuration*: `aggregate.py` reads the qualifying rules and
month bases straight out of `policy.yaml`, because truth is only meaningful with respect to a
ruleset. A shared input is not a shared implementation.

Run: `PYTHONPATH=tools python -m datagen --out corpus/demo` (or `make datagen`).
"""

from __future__ import annotations

__all__ = ["GENERATOR_VERSION"]

# Bumped whenever a change would move a byte in the corpus. Stamped into truth_metrics.json and
# manifest.json, so a checked-in corpus can always be traced to the code that produced it.
GENERATOR_VERSION = "1.0.0"
