"""Mizan - an agentic reconciliation and verification system (POC).

Deterministic core, agentic edges. See docs/adr/0001-deterministic-core-agentic-edges.md.
"""

__version__ = "0.6.1"

# METRIC_LIBRARY_VERSION deliberately does NOT live here. It belongs to the metric library and is
# defined in `tda.metrics`, which is the only place that knows when a metric's behaviour moved. A
# copy sat here until M6 carrying "0.1.0" while `tda.metrics` had moved to "1.0.0", and nothing
# imported either from this module, so the disagreement was invisible. The hazard was not the dead
# constant: it was that `from tda import METRIC_LIBRARY_VERSION` reads perfectly natural, and the
# next caller to write it would have stamped a superseded version into a verdict whose numbers the
# current library produced. That is precisely the claim D-EV-04 exists to make checkable.
