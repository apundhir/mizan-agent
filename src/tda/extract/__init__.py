"""Extraction — read the submitted documents, or refuse to.

**Derive, never trust.** Every figure a report prints is a cross-check, never an input. `nights` comes
from the dates (D-RNS-01), `room_nights` from `nights × rooms` (D-RNS-02), availability from
`rooms_total − rooms_out_of_order` (D-RNA-03). Each has a printed column beside it, and each printed
column is compared and discarded.

That sounds like extra work for no gain until you notice what adopting them would cost: the printed
columns are the *hotel's* arithmetic, so a system that read them would agree with the hotel by
construction on precisely the values it exists to check — and the agreement would be invisible. The
numbers would reconcile perfectly and mean nothing.

**The refusal is the feature.** Three layers, and every failure in any of them is a blocking V7:

| Layer | Refuses when |
|---|---|
| `layout` | the column header is not the one the committed map was measured against |
| `normalise` | a country, status or rate code is not in the committed lookup (never guessed) |
| `reconcile_totals` | the extracted rows disagree with the totals the report prints about itself |

None of it is ever reported as a hotel error. `Finding.is_hotel_error` is false for V7 by construction,
so even a caller who tries cannot count "I could not read this" as "you got this wrong". A system that
guessed at an unreadable page would eventually accuse a property of an error that does not exist, and
one such accusation costs more trust than a hundred correct findings earn.

| Module | Owns |
|---|---|
| `reference/` | the three committed lookup tables — data, not code |
| `normalise` | one matching rule (D-NAT-10) and one refusal (D-NAT-12, D-QUAL-03) |
| `layout` | the committed column map, and the check that it still applies |
| `pdf` | rows to canonical records, printed columns cross-checked |
| `totals` | the grand-total block, parsed positionally because the page is two columns |
| `reconcile_totals` | five checks against the printed totals, each catching what the others cannot |
| `inventory` | the room inventory reference, contiguity enforced |
| `run` | the whole read, and the blocking findings that halt it |
"""

from tda.extract.inventory import InventoryError, InventoryReference, read_inventory
from tda.extract.layout import BANDS, EXPECTED_HEADER, LayoutMismatchError, verify_layout
from tda.extract.normalise import (
    Lookups,
    LookupTableError,
    UnmappableLabelError,
    load_lookups,
    nationality,
    normalise_key,
    rate_code,
    status,
)
from tda.extract.pdf import (
    ParsedPage,
    ReadReport,
    RowDefect,
    parse_page,
    parse_row,
    read_report,
    row_bands,
)
from tda.extract.reconcile_totals import Disagreement, reconcile
from tda.extract.run import ExtractionResult, blocking_finding, extract, month_of
from tda.extract.totals import PrintedTotals, TotalsParseError, parse_totals, split_columns

__all__ = [
    "BANDS",
    "EXPECTED_HEADER",
    "Disagreement",
    "ExtractionResult",
    "InventoryError",
    "InventoryReference",
    "LayoutMismatchError",
    "LookupTableError",
    "Lookups",
    "ParsedPage",
    "PrintedTotals",
    "ReadReport",
    "RowDefect",
    "TotalsParseError",
    "UnmappableLabelError",
    "blocking_finding",
    "extract",
    "load_lookups",
    "month_of",
    "nationality",
    "normalise_key",
    "parse_page",
    "parse_row",
    "parse_totals",
    "rate_code",
    "read_inventory",
    "read_report",
    "reconcile",
    "row_bands",
    "split_columns",
    "status",
    "verify_layout",
]
