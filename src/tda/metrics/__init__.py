"""The metric library — pure functions over typed records, policy as a parameter.

Every number in a verdict comes from here, and nothing here has any way to reach a model. That is
enforced rather than intended: `tools/guard/import_guard.py` fails the build if this package imports
the agents package, a model SDK, or performs I/O. ADR-0001 makes the claim; the guard is why the
claim is worth anything.

Four rules shape the whole package:

**Policy is a parameter on every function, never a global.** A global would make a definitional
change invisible at the call site, and the design rests on a reader being able to see which rules a
number was computed under. It also makes the permutation engine possible: recomputing a metric under
an alternative ruleset is passing a different `Policy`, not reconfiguring the world.

**No I/O, no globals, no mutable module state.** A "pure" function whose result depends on a file on
disk is not reproducible, and reproducibility is the entire argument being made to the regulator.

**Every contestable reading is implemented, not just the right one.** Three month bases, three
nationality count bases, two occupancy denominators. The wrong ones are there because the
reconciliation engine has to *reproduce* a hotel's number in order to name the definitional cause
rather than reporting a clerical error. A library that only implemented the correct reading could
only ever say "you are wrong by 4".

**`Decimal` throughout.** The definitions say occupancy is carried at full precision and rounded once
at the presentation boundary; `Decimal` is the stricter reading of that intent, because half-up
rounding of a binary float is a coin toss at the midpoint. A system that reported a variance caused
by its own representation error would have no business reporting variances.

Layout, in dependency order — each module is the single home of one set of rules:

| Module | Owns |
|---|---|
| `qualifying` | the qualifying set (D-QUAL), and the refusal on duplicate ids |
| `apportion` | month apportionment (D-RNS-03), all three bases side by side |
| `occupancy` | sold, available, the percentage, and presentation rounding |
| `nationality` | guests by ISO 3166-1 alpha-2 code (D-NAT) |
| `compute` | keys, citations, and the `NotVerifiable` cases |
"""

from tda.metrics.apportion import guest_periods, room_nights_in
from tda.metrics.compute import MetricResults, compute_all
from tda.metrics.nationality import contribution, guests, guests_by_nationality
from tda.metrics.occupancy import (
    occupancy_pct,
    occupancy_ratio,
    present,
    room_nights_available,
    room_nights_sold,
    rooms_available,
)
from tda.metrics.qualifying import MetricError, qualifies, qualifying, reject_duplicate_ids

# Stamped into every verdict alongside the policy version (D-EV-04,
# `evidence.stamp_metric_library_version`). Bumped whenever a change here could move a number, so a
# figure in a released artefact can always be traced to the code that produced it. A verdict carrying
# a policy version but not this one would be half-defensible: the same ruleset, computed differently.
METRIC_LIBRARY_VERSION = "1.0.0"

__all__ = [
    "METRIC_LIBRARY_VERSION",
    "MetricError",
    "MetricResults",
    "compute_all",
    "contribution",
    "guest_periods",
    "guests",
    "guests_by_nationality",
    "occupancy_pct",
    "occupancy_ratio",
    "present",
    "qualifies",
    "qualifying",
    "reject_duplicate_ids",
    "room_nights_available",
    "room_nights_in",
    "room_nights_sold",
    "rooms_available",
]
