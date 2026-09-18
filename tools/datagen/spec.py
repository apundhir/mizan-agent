"""The corpus specification — every arbitrary number in one readable place.

A generator with its constants scattered through the code that uses them is a generator nobody
can review. Every quantity that *could* have been different lives here, so "what does the demo
corpus actually contain?" is answered by reading one file rather than by running it.

The edge-case quotas are the load-bearing part. A corpus that merely *happens* to contain a few
month-spanning stays is a corpus whose edges vanish the first time the seed changes — and the
permutation engine in S7 would then have nothing to find, silently. So the quotas are asserted
after generation (`ledger.check_quotas`) and generation fails if any is unmet. The edges are a
requirement of the corpus, not a property of the random draw.

Everything here is synthetic. The hotel does not exist, the guest references are opaque
by construction, and no value in this file derives from a real property or a real export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Final

# ── the hotel and the period ─────────────────────────────────────────────────

# Deliberately fictional. `MZN` is this repo, not an IATA or PMS code in use anywhere.
HOTEL_ID: Final = "MZN-DXB-001"
HOTEL_NAME: Final = "Marina Vista Hotel (synthetic)"
HOTEL_CITY: Final = "Dubai"

QUARTER: Final = "2026-Q1"
PERIOD_START: Final = date(2026, 1, 1)
PERIOD_END: Final = date(2026, 3, 31)  # inclusive
MONTHS: Final[tuple[str, ...]] = ("2026-01", "2026-02", "2026-03")

# One seed, fixed forever. Changing it regenerates every byte of the corpus and invalidates every
# digest in manifest.json, which is why it is a constant here and not a command-line option.
SEED: Final = 20260913


# ── inventory ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OutOfOrderWindow:
    """A period during which some rooms could not be sold.

    These exist so the occupancy denominator has a value that **cannot be inferred** from a room
    count (D-RNA-04). Without them, `rooms_total × days_in_month` would be a correct shortcut, the
    inventory reference would be decorative, and the `P-OOO-INCLUDED` permutation would be a
    no-op that still looked like a passing test.
    """

    first_day: date
    last_day: date  # inclusive
    rooms: int
    reason: str

    def covers(self, day: date) -> bool:
        return self.first_day <= day <= self.last_day


# 60 rooms rather than a larger property: with ~1,200 reservations over the quarter it lands the
# occupancy in the 60-80% band a reader recognises as plausible. A 300-room hotel at the same
# reservation count would report ~15% occupancy and read as a bug in the generator.
ROOMS_TOTAL: Final = 60

OUT_OF_ORDER: Final[tuple[OutOfOrderWindow, ...]] = (
    OutOfOrderWindow(date(2026, 2, 10), date(2026, 2, 24), 6, "guest-floor refurbishment"),
    OutOfOrderWindow(date(2026, 3, 5), date(2026, 3, 7), 2, "water-ingress remediation"),
)


# ── the reservation mix ──────────────────────────────────────────────────────

TOTAL_RESERVATIONS: Final = 1_200

# Weights, not counts: the draw is seeded so the realised mix is fixed, and the quotas below are
# what actually guarantee the edges. Expressing the bulk as weights keeps the shape readable.
#
# `IN_HOUSE` is deliberately **absent** from this draw, and its absence is the correction to a real
# defect. Drawn at random it landed on stays that had departed weeks earlier — a reservation marked
# "in house" with a departure date of 14 January, in a report covering a quarter that has ended.
# The status has a meaning ("still occupying a room now"), so the only coherent place for it is the
# reporting boundary: it comes exclusively from `OPEN_AT_PERIOD_END` below, where the departure runs
# past 31 March. A reviewer who spots one incoherent row stops trusting every other row.
STATUS_WEIGHTS: Final[dict[str, int]] = {
    "CHECKED_OUT": 85,
    "CANCELLED": 10,
    "NO_SHOW": 5,
}

RATE_CODE_WEIGHTS: Final[dict[str, int]] = {
    "BAR": 34,
    "OTA": 24,
    "CORP": 16,
    "GROUP": 12,
    "GOV": 8,
    "COMP": 3,  # a real guest paying nothing — counts (D-QUAL-04)
    "HOUSE": 3,  # the property's own use — does not count (D-QUAL-05)
}

# Length of stay. Weighted to short stays with a long tail, because the tail is where
# month-spanning happens and where a 21-night stay tests apportionment hardest.
NIGHTS_WEIGHTS: Final[dict[int, int]] = {
    1: 18, 2: 22, 3: 19, 4: 13, 5: 8, 6: 5, 7: 6,
    8: 2, 9: 2, 10: 1, 11: 1, 12: 1, 14: 1, 18: 1, 21: 1,
}  # fmt: skip

ROOMS_WEIGHTS: Final[dict[int, int]] = {1: 88, 2: 9, 3: 2, 4: 1}
ADULTS_WEIGHTS: Final[dict[int, int]] = {1: 34, 2: 52, 3: 10, 4: 4}
CHILDREN_WEIGHTS: Final[dict[int, int]] = {0: 72, 1: 14, 2: 11, 3: 3}

# A Gulf-destination mix. `CZ` is present because `Czechia`/`Czech Republic` is the canonical
# normalisation variant pair (D-NAT-11) and the workbook spells it the long way.
NATIONALITY_WEIGHTS: Final[dict[str, int]] = {
    "GB": 130, "IN": 120, "RU": 95, "DE": 80, "SA": 70, "US": 60, "CN": 55,
    "FR": 45, "PK": 40, "IT": 38, "EG": 34, "PH": 30, "KZ": 26, "NL": 24,
    "CH": 20, "AU": 18, "PL": 16, "JP": 14, "KR": 12, "CZ": 10,
}  # fmt: skip

# Present in exactly one month, on purpose. A nationality that appears in every month cannot
# exercise the completeness path (V5) in S8: a workbook that omits a row the corpus never had
# is not an omission. Placed in March so the omission is at the end of the period, where a
# hand-built expectation is most likely to stop looking.
SINGLE_MONTH_NATIONALITY: Final = "IS"
SINGLE_MONTH_MONTH: Final = 3
SINGLE_MONTH_COUNT: Final = 3


@dataclass(frozen=True, slots=True)
class EdgeQuotas:
    """The minimum the corpus must contain for the downstream stories to have anything to test.

    Each entry is a story's precondition, not a style preference:

    - `month_spanning` — S7's `P-MONTH-ARRIVAL` / `P-MONTH-DEPARTURE` permutations, and the
      single most likely source of live definitional variance (D-RNS-03). 25 is the PRD's number.
    - `comp` / `house` — the pair that makes `P-COMP-EXCLUDED` and `P-HOUSE-INCLUDED` move a
      number rather than silently returning the baseline.
    - `day_use` — D-QUAL-08/09/10: zero room-nights but a real guest. The one edge where a
      reservation contributes to one metric family and not the other.
    - `arriving_before_period` — D-NAT-08: room-nights in January, no guests in January. The case
      where the two metric families are *expected* not to reconcile, which is the case a
      hand-written test fixture almost never includes.
    - `in_house_at_period_end` — a stay still open at the reporting boundary, so `IN_HOUSE` is a
      status with a consistent meaning rather than a label sprinkled at random.
    - `multi_room` — `room_nights = nights × rooms` (D-RNS-02) is untested when every reservation
      has one room, because then the two are the same number.
    """

    month_spanning: int = 25
    comp: int = 25
    house: int = 25
    day_use: int = 30
    arriving_before_period: int = 12
    in_house_at_period_end: int = 8
    multi_room: int = 100
    nationalities: int = 20


QUOTAS: Final = EdgeQuotas()

# Deliberate boundary-crossing arrivals. Left to chance, month-spanning stays would depend on the
# seed: a draw that happened to put few long stays near a month end would quietly drop below
# quota. These arrival dates are seeded explicitly so the edge is structural.
SPANNING_ARRIVALS: Final[tuple[tuple[date, int], ...]] = tuple(
    [(date(2026, 1, 28), n) for n in (4, 5, 6, 7, 9, 12, 21)]
    + [(date(2026, 1, 29), n) for n in (3, 4, 6, 8, 14)]
    + [(date(2026, 1, 30), n) for n in (2, 3, 5, 7)]
    + [(date(2026, 1, 31), n) for n in (1, 2, 4, 10)]
    + [(date(2026, 2, 25), n) for n in (4, 5, 7, 11, 18)]
    + [(date(2026, 2, 26), n) for n in (3, 4, 6, 9)]
    + [(date(2026, 2, 27), n) for n in (2, 3, 5, 8)]
    + [(date(2026, 2, 28), n) for n in (1, 2, 4, 7, 13)]
)

# Stays that began before the reporting period. They contribute room-nights to January and no
# guests to January (D-NAT-08).
PRE_PERIOD_ARRIVALS: Final[tuple[tuple[date, int], ...]] = (
    (date(2025, 12, 27), 6), (date(2025, 12, 28), 5), (date(2025, 12, 28), 9),
    (date(2025, 12, 29), 4), (date(2025, 12, 29), 7), (date(2025, 12, 30), 3),
    (date(2025, 12, 30), 6), (date(2025, 12, 30), 12), (date(2025, 12, 31), 2),
    (date(2025, 12, 31), 4), (date(2025, 12, 31), 8), (date(2025, 12, 31), 15),
)  # fmt: skip

# Stays open at the period end, carried as IN_HOUSE. The departure runs past 31 March, so the
# status is consistent with the dates rather than decorative.
OPEN_AT_PERIOD_END: Final[tuple[tuple[date, int], ...]] = (
    (date(2026, 3, 27), 6), (date(2026, 3, 28), 5), (date(2026, 3, 28), 8),
    (date(2026, 3, 29), 4), (date(2026, 3, 30), 3), (date(2026, 3, 30), 7),
    (date(2026, 3, 31), 2), (date(2026, 3, 31), 10),
)  # fmt: skip


# ── document rendering ───────────────────────────────────────────────────────

# Fixed rows per page, and a header repeated on every page. `PdfRef.row_start` is 1-indexed
# "within the page's table" (docs/01-definitions.md §7), so the row a citation names has to be
# something the renderer decides rather than something a layout engine decides for it. That is
# also why the table is drawn directly onto a canvas instead of flowed by platypus.
ROWS_PER_PAGE: Final = 30

# Pinned into every document. `make datagen` must not embed the day it ran, or the corpus changes
# whenever anybody regenerates it and every digest in the repo churns.
PINNED_TIMESTAMP: Final = "2026-03-31T23:59:59+04:00"
PINNED_ZIP_DATE_TIME: Final[tuple[int, int, int, int, int, int]] = (2026, 3, 31, 23, 59, 58)
# The same instant in the form OOXML wants for `dcterms:modified` — W3CDTF, UTC, no offset. The
# local time above is UTC+04:00 (Asia/Dubai, no daylight saving), so this is four hours behind it.
PINNED_OOXML_MODIFIED: Final = "2026-03-31T19:59:58Z"
PINNED_DOC_AUTHOR: Final = "Mizan synthetic corpus generator"


# ── the workbook the hotel "submits" ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkbookSpec:
    """Sheet names and the deliberate wrinkles in the claim workbook.

    The demo workbook claims the **correct** figures — it is the clean-pass scene, and a corpus
    the system was rendered from is not evidence of catching anything (the scored fixtures with
    planted errors are S11). But "correct" is not the same as "tidy", and three wrinkles are
    here on purpose:

    - `Rate & Revenue` holds metrics that are **out of scope** (D-SCOPE-02). A claim the system
      neither verifies nor mentions would be read as approval, so out-of-scope claims have to
      exist in order to be recorded as out of scope.
    - The nationality sheet spells countries as **labels, not codes**, including
      `Czech Republic` for `CZ` — the normalisation path (D-NAT-09..11) is untested against a
      workbook that helpfully contains ISO codes already.
    - Values sit under a two-row header block with the hotel name above them, so the mapping
      agent has to find the header rather than assume `A1`.
    """

    summary_sheet: str = "Summary"
    occupancy_sheet: str = "Occupancy"
    nationality_sheet: str = "Nationality"
    out_of_scope_sheet: str = "Rate & Revenue"
    header_row: int = 4
    submitted_by: str = "Revenue Office, Marina Vista Hotel"


WORKBOOK: Final = WorkbookSpec()

# Labels the workbook uses for each ISO code, so the claim parser has to normalise (D-NAT-09).
# Mixed deliberately: official short names, common short forms, and one variant pair.
NATIONALITY_LABELS: Final[dict[str, str]] = {
    "GB": "United Kingdom",
    "IN": "India",
    "RU": "Russian Federation",
    "DE": "Germany",
    "SA": "Saudi Arabia",
    "US": "United States",
    "CN": "China",
    "FR": "France",
    "PK": "Pakistan",
    "IT": "Italy",
    "EG": "Egypt",
    "PH": "Philippines",
    "KZ": "Kazakhstan",
    "NL": "Netherlands",
    "CH": "Switzerland",
    "AU": "Australia",
    "PL": "Poland",
    "JP": "Japan",
    "KR": "Korea, Republic of",
    "CZ": "Czech Republic",  # → CZ, the D-NAT-11 variant pair
    "IS": "Iceland",
}

# Out-of-scope claims. Values are invented outright: they are never verified, so a derived value
# would imply the generator had an opinion about revenue.
OUT_OF_SCOPE_CLAIMS: Final[dict[str, dict[str, float]]] = {
    "Average Daily Rate (AED)": {"2026-01": 612.40, "2026-02": 698.15, "2026-03": 664.80},
    "RevPAR (AED)": {"2026-01": 418.90, "2026-02": 502.33, "2026-03": 471.06},
    "Average Length of Stay (nights)": {"2026-01": 3.21, "2026-02": 3.44, "2026-03": 3.18},
}


# ── output layout ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Layout:
    """Where each file lands, and why the split exists.

    `submission/` is exactly what a hotel sends: three monthly PDFs, one workbook, one inventory
    reference. `ground_truth/` is what the generator knows and the hotel does not. The pipeline is
    pointed at `submission/` and never at `ground_truth/` — a split of directories rather than a
    convention, because "don't read the answers" is the one rule in this repo that would be
    easiest to break by accident and hardest to notice afterwards.
    """

    submission: str = "submission"
    ground_truth: str = "ground_truth"
    manifest: str = "manifest.json"
    readme: str = "README.md"
    truth_metrics: str = "truth_metrics.json"
    ledger_csv: str = "reservations.csv"
    inventory_csv: str = "inventory_2026-Q1.csv"
    workbook: str = "claims_2026-Q1.xlsx"

    def pdf_name(self, month: str) -> str:
        return f"pms_{month}.pdf"


LAYOUT: Final = Layout()


@dataclass(frozen=True, slots=True)
class CorpusSpec:
    """The whole specification as one hashable value.

    Digested into `truth_metrics.json` so a corpus on disk can be matched to the specification it
    came from. A corpus whose numbers cannot be traced to a spec and a seed is a corpus somebody
    will eventually have to regenerate on faith.
    """

    hotel_id: str = HOTEL_ID
    quarter: str = QUARTER
    seed: int = SEED
    rooms_total: int = ROOMS_TOTAL
    total_reservations: int = TOTAL_RESERVATIONS
    months: tuple[str, ...] = MONTHS
    quotas: EdgeQuotas = field(default_factory=lambda: QUOTAS)


SPEC: Final = CorpusSpec()
