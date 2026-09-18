"""CONTROL FIXTURE - a compliant agent contract.

Keys, references and classifications. Citation integers are whitelisted. A boolean is a
classification, and classification is what agents are for. If the lint flags this, the lint is wrong.
"""

from tda.agents.contracts.base import AgentOutput


class SheetMapping(AgentOutput):
    sheet: str
    metric: str
    cell_range: str
    page: int
    row_start: int
    row_end: int
    is_total_row: bool


class LabelResolution(AgentOutput):
    iso2: str | None
    refused: bool
    reason: str
