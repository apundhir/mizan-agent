"""VIOLATION FIXTURE - one word, and a model becomes the source of a number.

`guest_count: int` passes every type check and looks helpful in review. It also means a model
supplies a figure that reaches a verdict, which is the thing the whole architecture forbids.
"""

from decimal import Decimal

from tda.agents.contracts.base import AgentOutput


class SheetMapping(AgentOutput):
    sheet: str
    metric: str
    page: int  # allowed - a citation, not a quantity
    row_start: int  # allowed
    guest_count: int  # VIOLATION


class NestedNumeric(AgentOutput):
    """The container case. The obvious string-matching implementation misses both of these."""

    label: str
    counts: list[int]  # VIOLATION
    occupancy: Decimal | None  # VIOLATION
