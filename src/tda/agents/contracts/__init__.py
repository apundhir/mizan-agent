"""Agent output contracts.

Every class here derives from `AgentOutput` and is checked by the schema lint: no numeric fields
beyond the whitelisted citation integers. Agents pass keys and references, never numbers.

| Contract | Agent | What it carries |
|---|---|---|
| `LabelResolution` | resolution | a country code, or a stated abstention |
| `FindingNarrative` | narrative | one sentence and the finding id it explains |
| `CriticVerdict` | critic | four observations about a narrative; the pass/fail is computed |
| `CitedAnswer` | reviewer_assist | an officer's question, and an answer that cannot exist without a citation |
| `WorkbookMapping` | mapping | lives with the Excel parser (`tda.excel.mapping`), which owns the checks applied to it |

`WorkbookMapping` is the exception and it is a deliberate one: the mapping contract is inseparable
from the geometry checks that make it safe, and splitting the two would put the rules in one
package and the thing they govern in another. The rule this package enforces is the *marker*, not
the location — `tools/guard/agent_schema_lint.py` scans all of `src/`, so a contract is checked
wherever it is declared.

`RoutingDecision` is deliberately **not** here: no model produced it. See `tda.agents.supervisor`.
"""

from tda.agents.contracts.base import Abstention, AgentOutput
from tda.agents.contracts.critic import CriticVerdict
from tda.agents.contracts.narrative import FindingNarrative
from tda.agents.contracts.resolution import LabelResolution
from tda.agents.contracts.reviewer_assist import (
    Citation,
    CitedAnswer,
    ClauseCitation,
    ExcelCitation,
    InventoryCitation,
    PdfCitation,
)

__all__ = [
    "Abstention",
    "AgentOutput",
    "Citation",
    "CitedAnswer",
    "ClauseCitation",
    "CriticVerdict",
    "ExcelCitation",
    "FindingNarrative",
    "InventoryCitation",
    "LabelResolution",
    "PdfCitation",
]
