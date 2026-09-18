"""The grand-total block, and reconciling the extracted rows against it.

This is the check that makes the rest of extraction trustworthy. A parser that recovered 95% of rows
would produce totals 5% low and a verdict full of confident findings about a hotel that did nothing
wrong. So the report prints its own totals, and they are compared against what we read.

**The block is parsed by position, not by line.** `extract_text()` reads the totals page in
document order and the page is two columns, so the occupancy value and a nationality row arrive on one
line:

    'Occupancy 81.70% CN 31'

Splitting that on whitespace gives an occupancy of `81.70%` and a stray `CN 31`, or — worse, with a
slightly different regex — an occupancy of 31. Words are therefore split into the two columns by x
coordinate first and each side parsed separately. A two-column report is not an exotic layout; it is
what a report looks like when somebody fits it on one page.

**What is reconciled** (all of it, because a partial check is a check somebody trusts too far):

| Check | Catches |
|---|---|
| Row count vs the printed count | a lost or duplicated row |
| Σ printed `RN Month` vs the printed sum | a misread numeric column |
| Σ qualifying room-nights vs printed `Room nights sold` | a misread status or rate code |
| Exclusion counts vs ours | a status normalised to the wrong side of the qualifying line |
| Guests per nationality vs the printed table | a misread nationality, or a lost row |

**A disagreement halts the run** (V7, blocking). It is never reported as a hotel error: the hotel's
document is internally consistent with itself, and the disagreement means *we* could not read it. A
system that guessed here would eventually accuse a property of an error that does not exist, and one
such accusation costs more trust than a hundred correct findings earn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

# The left notes column ends at x≈402 and the nationality table starts at x≈442. 430 sits in the gap.
COLUMN_SPLIT_X: Final = 430.0

_SOLD: Final = re.compile(r"Room nights sold\s+(?P<value>[0-9]+)")
_AVAILABLE: Final = re.compile(r"Room nights available\s+(?P<value>[0-9]+)")
_OCCUPANCY: Final = re.compile(r"Occupancy\s+(?P<value>[0-9]+\.[0-9]{2})%")
_LISTED: Final = re.compile(
    r"Sum of the RN Month column over all\s+(?P<rows>[0-9]+)\s+listed rows:\s+(?P<total>[0-9]+)"
)
_EXCLUDED_BLOCK: Final = re.compile(
    r"Rows listed but excluded from the total:\s*(?P<body>.+?)(?:Complimentary|$)", re.S
)
_EXCLUDED_ITEM: Final = re.compile(r"(?P<count>[0-9]+)\s+(?P<name>[a-z][a-z \-()]*?)(?=,|$)")
_NATIONALITY_ROW: Final = re.compile(r"^(?P<code>[A-Z]{2})\s+(?P<count>[0-9]+)$", re.M)
_TOTAL_GUESTS: Final = re.compile(r"Total guests\s+(?P<value>[0-9]+)")


class TotalsParseError(Exception):
    """The grand-total block could not be read.

    Raised rather than defaulted. A missing printed total is not "no check available" — it is the
    absence of the one thing that makes the extracted rows trustworthy, and proceeding without it
    would mean asserting findings against a document nobody verified we had read.
    """


@dataclass(frozen=True, slots=True)
class PrintedTotals:
    """What the report says about itself, for one month.

    Every field is a number the document printed, never one we derived. The whole value of this type
    is that it is an independent statement to check our reading against, so a field computed here
    rather than read would quietly become a comparison of our arithmetic with itself.
    """

    month: str
    page: int
    room_nights_sold: int
    room_nights_available: int
    occupancy_pct: Decimal
    listed_rows: int
    listed_room_nights: int
    excluded: dict[str, int] = field(default_factory=dict)
    guests_by_nationality: dict[str, int] = field(default_factory=dict)
    total_guests: int = 0


def split_columns(words: Sequence[dict[str, object]]) -> tuple[str, str]:
    """Split a page's words into the left and right column texts, preserving line structure.

    Grouped by rounded `top` so words on one visual line stay together, then ordered by x. Rounding
    to one decimal place rather than comparing floats exactly: two words drawn at the same y can
    differ in the last bits, and a stricter grouping would split a line in half.
    """
    left: dict[float, list[tuple[float, str]]] = {}
    right: dict[float, list[tuple[float, str]]] = {}

    for word in words:
        x0 = float(str(word["x0"]))
        top = round(float(str(word["top"])), 1)
        text = str(word["text"])
        target = left if x0 < COLUMN_SPLIT_X else right
        target.setdefault(top, []).append((x0, text))

    def render(grouped: dict[float, list[tuple[float, str]]]) -> str:
        return "\n".join(
            " ".join(text for _, text in sorted(grouped[top])) for top in sorted(grouped)
        )

    return render(left), render(right)


def _require(pattern: re.Pattern[str], text: str, what: str, month: str) -> re.Match[str]:
    match = pattern.search(text)
    if match is None:
        raise TotalsParseError(
            f"{month}: the grand-total block has no {what}. Without the printed totals there is "
            "nothing to reconcile the extracted rows against, and a verdict built on an unverified "
            "read is worse than no verdict."
        )
    return match


def parse_totals(page_text_left: str, page_text_right: str, month: str, page: int) -> PrintedTotals:
    """Parse one month's grand-total block from its two already-separated columns."""
    sold = int(_require(_SOLD, page_text_left, "room-nights-sold line", month)["value"])
    available = int(
        _require(_AVAILABLE, page_text_left, "room-nights-available line", month)["value"]
    )
    occupancy = Decimal(_require(_OCCUPANCY, page_text_left, "occupancy line", month)["value"])

    listed = _require(_LISTED, page_text_left, "listed-rows reconciliation line", month)

    excluded: dict[str, int] = {}
    block = _EXCLUDED_BLOCK.search(page_text_left)
    if block is not None:
        # The exclusion list wraps across lines, so newlines are collapsed before the items are
        # matched — "17 house\nuse, 15 no-show" is one item followed by another, not three.
        body = " ".join(block["body"].split())
        for item in _EXCLUDED_ITEM.finditer(body):
            excluded[item["name"].strip()] = int(item["count"])

    guests = {
        match["code"]: int(match["count"]) for match in _NATIONALITY_ROW.finditer(page_text_right)
    }
    total_guests = int(
        _require(_TOTAL_GUESTS, page_text_right, "total-guests line", month)["value"]
    )

    return PrintedTotals(
        month=month,
        page=page,
        room_nights_sold=sold,
        room_nights_available=available,
        occupancy_pct=occupancy,
        listed_rows=int(listed["rows"]),
        listed_room_nights=int(listed["total"]),
        excluded=excluded,
        guests_by_nationality=guests,
        total_guests=total_guests,
    )
