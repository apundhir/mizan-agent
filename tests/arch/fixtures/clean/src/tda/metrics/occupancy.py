"""CONTROL FIXTURE - what a compliant metric module looks like.

Typed records in, a policy parameter, a number out. No I/O, no globals, no model client. If
the guard flags this, the guard is wrong.
"""

from decimal import Decimal


def occupancy_pct(sold: int, available: int) -> Decimal:
    if available == 0:
        return Decimal(0)
    return Decimal(100) * Decimal(sold) / Decimal(available)
