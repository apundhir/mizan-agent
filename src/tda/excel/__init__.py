"""The Excel claim parser: a model maps sheets to metrics, code reads the values.

The split is the whole point, and it is enforced by the shape of the code rather than by a sentence
in a prompt:

| Stage | Who | What it may touch |
|---|---|---|
| one — mapping | the mapping agent | sheet names, labels, A1 geometry. **No value can reach it** — `tools.py` has no code path that returns one |
| two — reading | `openpyxl` and this package | every value, with the cell reference recorded on every claim |

`tools.py` is worth reading first. It is the clearest demonstration in the repository of the
difference between asking a model not to do something and building something it cannot do.

Layout, in the order a run moves through it:

| Module | Owns |
|---|---|
| `tools` | the agent's view of the workbook, with every value redacted (D-XLS-01) |
| `mapping` | what the agent may answer, and every check applied to it before use |
| `geometry` | A1 ranges as a checked value object |
| `read` | stage two — values into `Claim`s, each carrying its cell (D-XLS-02, D-XLS-03) |
| `selfcheck` | the workbook against itself, before any comparison (D-XLS-04, D-XLS-05) |
| `run` | the five steps in order, and the refusals (D-XLS-06) |
"""

from tda.excel.geometry import RangeError, Rect
from tda.excel.mapping import (
    CoverSheet,
    Disposition,
    MetricBlock,
    Orientation,
    ResolvedBlock,
    UnmappedBlock,
    WorkbookMapping,
    resolve,
    unaccounted_sheets,
    uncovered_values,
)
from tda.excel.read import (
    BlockReading,
    ReadDefect,
    StatedTotal,
    parse_period_label,
    parse_value,
    read_block,
)
from tda.excel.run import ClaimSet, OutOfScopeClaim, open_submission, parse_claims
from tda.excel.selfcheck import (
    IdentityMismatch,
    Inconsistency,
    check_cover,
    check_dimension_totals,
    check_occupancy_consistency,
    check_period_rollups,
)
from tda.excel.tools import (
    REDACTED_FORMULA,
    REDACTED_VALUE,
    SheetPeek,
    SheetSummary,
    digest,
    list_sheets,
    looks_numeric,
    peek_headers,
    render_cell,
)

__all__ = [
    "REDACTED_FORMULA",
    "REDACTED_VALUE",
    "BlockReading",
    "ClaimSet",
    "CoverSheet",
    "Disposition",
    "IdentityMismatch",
    "Inconsistency",
    "MetricBlock",
    "Orientation",
    "OutOfScopeClaim",
    "RangeError",
    "ReadDefect",
    "Rect",
    "ResolvedBlock",
    "SheetPeek",
    "SheetSummary",
    "StatedTotal",
    "UnmappedBlock",
    "WorkbookMapping",
    "check_cover",
    "check_dimension_totals",
    "check_occupancy_consistency",
    "check_period_rollups",
    "digest",
    "list_sheets",
    "looks_numeric",
    "open_submission",
    "parse_claims",
    "parse_period_label",
    "parse_value",
    "peek_headers",
    "read_block",
    "render_cell",
    "resolve",
    "unaccounted_sheets",
    "uncovered_values",
]
