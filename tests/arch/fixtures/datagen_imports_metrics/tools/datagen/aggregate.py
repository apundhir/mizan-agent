"""VIOLATION FIXTURE - the tautology.

Reusing the product metric library to build truth_metrics.json is the single most tempting
shortcut in the corpus story, and it would make the POC prove that the code equals itself.
"""

from tda.metrics.occupancy import occupancy_pct


def truth(records: list[object]) -> dict[str, float]:
    return {"occupancy_pct:2026-01": occupancy_pct(records)}
