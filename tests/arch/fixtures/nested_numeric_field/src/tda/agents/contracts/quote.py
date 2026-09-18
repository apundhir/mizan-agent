"""A contract that declares no number and returns two.

The dodge this fixture exists for: the lint used to check only the fields written *on* an
`AgentOutput` subclass, so pointing one at a plain `BaseModel` in another package put an agent in
charge of a figure while every line of the contract looked clean.
"""

from tda.agents.contracts.base import AgentOutput
from tda.other.money import Money


class Quote(AgentOutput):
    """Not a single numeric annotation in sight."""

    reference: str
    total: Money
