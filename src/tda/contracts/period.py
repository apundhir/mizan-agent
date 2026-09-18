"""A reporting period, and the date range it actually covers.

`MetricKey.period` is a string — `2026-02`, `2026-Q1`, `2026` — because that is what a key has to
be. But every metric function needs to ask one question of it, *is this night inside the period*,
and answering that from a string means each function re-deriving month boundaries from a substring.
Which is how a quarter ends up computed as three months in one place and as `Q1 = months 1..3` in
another, with the leap-year case handled in only one of them.

So the string is parsed **once**, into a first and last day, and the metric library asks
`period.contains(day)`. There is no month arithmetic anywhere in `tda/metrics/`.

Two things are enforced at construction rather than trusted:

- **The bounds must agree with the rendered form.** A `Period` cannot be built claiming to be
  `2026-02` while covering March, so a caller cannot hand the metric library a period whose label
  and range disagree — which would produce a number correctly computed for the wrong month.
- **A quarter is not three months.** `Period.months()` exists and returns them, and
  `room_nights_available("2026-Q1")` sums over the quarter's own days directly. Both routes reach
  the same number here, and they are not the same statement: D-KEY-01 makes a quarter a separate
  key, and a quarterly occupancy is its own ratio rather than the mean of three monthly ones.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MONTH = re.compile(r"^(?P<year>\d{4})-(?P<month>0[1-9]|1[0-2])$")
_QUARTER = re.compile(r"^(?P<year>\d{4})-Q(?P<quarter>[1-4])$")
_YEAR = re.compile(r"^(?P<year>\d{4})$")


class PeriodKind(StrEnum):
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class Period(BaseModel):
    """A period, its kind, and its inclusive date bounds.

    Build with `Period.parse("2026-02")` rather than the constructor: the bounds are derived from
    the rendered form, and deriving them at one call site is the whole point.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rendered: str = Field(
        description="YYYY-MM, YYYY-Qn or YYYY. The same string a MetricKey carries.",
        examples=["2026-02", "2026-Q1", "2026"],
    )
    kind: PeriodKind
    first_day: date
    last_day: date = Field(description="Inclusive.")

    @model_validator(mode="after")
    def _bounds_match_the_label(self) -> Self:
        """The bounds must be the ones the rendered form implies.

        Without this, `Period(rendered="2026-02", first_day=March 1, …)` is constructible, and every
        number computed under it would be correct arithmetic attributed to the wrong month — the
        hardest class of error to spot in a verdict, because nothing about the number looks wrong.
        """
        expected = _bounds(self.rendered)
        if expected is None:
            raise ValueError(f"period must be YYYY-MM, YYYY-Qn or YYYY, got {self.rendered!r}")
        kind, first_day, last_day = expected
        if (self.kind, self.first_day, self.last_day) != (kind, first_day, last_day):
            raise ValueError(
                f"{self.rendered} covers {first_day}..{last_day} as a {kind.value}, but this "
                f"Period claims {self.first_day}..{self.last_day} as a {self.kind.value}. "
                "Build periods with Period.parse()."
            )
        return self

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def parse(cls, rendered: str) -> Self:
        """The only way to build a `Period` that should appear at a call site."""
        bounds = _bounds(rendered)
        if bounds is None:
            raise ValueError(f"period must be YYYY-MM, YYYY-Qn or YYYY, got {rendered!r}")
        kind, first_day, last_day = bounds
        return cls(rendered=rendered, kind=kind, first_day=first_day, last_day=last_day)

    @classmethod
    def of_month(cls, day: date) -> Self:
        """The month a date falls in. Used to answer "which month did this stay arrive in"."""
        return cls.parse(f"{day.year:04d}-{day.month:02d}")

    @classmethod
    def is_valid(cls, rendered: str) -> bool:
        """Whether a string is a well-formed period.

        `MetricKey` validates its `period` field through this, so there is one definition of the
        accepted forms rather than two that agree until somebody adds a fourth.
        """
        return _bounds(rendered) is not None

    # ── the question the metric library asks ─────────────────────────────────

    def contains(self, day: date) -> bool:
        """Whether a date falls inside the period. Two comparisons, no month arithmetic."""
        return self.first_day <= day <= self.last_day

    def days(self) -> list[date]:
        """Every date in the period, in order. The occupancy denominator iterates this."""
        return [
            self.first_day + timedelta(days=offset)
            for offset in range((self.last_day - self.first_day).days + 1)
        ]

    def months(self) -> tuple[Period, ...]:
        """The months this period spans, in order. A month yields itself.

        Provided for callers that genuinely want a per-month breakdown. Metric functions do **not**
        use it to compute a quarter: they sum over the quarter's own days, because a quarter is a
        separate key and not a derived total (D-KEY-01).
        """
        if self.kind is PeriodKind.MONTH:
            return (self,)
        result: list[Period] = []
        year, month = self.first_day.year, self.first_day.month
        while (year, month) <= (self.last_day.year, self.last_day.month):
            result.append(Period.parse(f"{year:04d}-{month:02d}"))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return tuple(result)

    def __str__(self) -> str:
        return self.rendered


def _bounds(rendered: str) -> tuple[PeriodKind, date, date] | None:
    """Parse a period string to its kind and inclusive bounds, or `None` if malformed.

    `calendar.monthrange` rather than a table of month lengths: February 2028 has 29 days and a
    hand-written table is one of the places that gets fixed late.
    """
    if match := _MONTH.match(rendered):
        year, month = int(match["year"]), int(match["month"])
        last = calendar.monthrange(year, month)[1]
        return PeriodKind.MONTH, date(year, month, 1), date(year, month, last)

    if match := _QUARTER.match(rendered):
        year, quarter = int(match["year"]), int(match["quarter"])
        first_month = 3 * (quarter - 1) + 1
        last_month = first_month + 2
        last = calendar.monthrange(year, last_month)[1]
        return PeriodKind.QUARTER, date(year, first_month, 1), date(year, last_month, last)

    if match := _YEAR.match(rendered):
        year = int(match["year"])
        return PeriodKind.YEAR, date(year, 1, 1), date(year, 12, 31)

    return None
