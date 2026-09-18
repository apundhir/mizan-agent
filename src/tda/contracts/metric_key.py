"""The canonical metric key.

Computed values and parsed claims are keyed identically so the reconciliation join is
**exact rather than fuzzy** (D-KEY-01..03). Keeping the key a structured object rather
than a string makes three rules structural instead of hopeful:

- A key is never assembled by string concatenation at a call site, so it cannot drift.
- A dimension value is the **canonical code**, not the label the document used —
  `CZ` whether the workbook said *Czechia*, *Czech Republic* or *CZE* (D-KEY-02).
- **A key is never constructed from a model output** (D-KEY-03). The enum below is closed,
  so an agent cannot invent a metric by returning an unexpected string; and because agent
  output contracts carry no numeric fields, an agent cannot supply a period either.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tda.contracts.period import Period


class Metric(StrEnum):
    """The four metrics in scope. Closed by design — see `policy.scope.metrics_in_scope`."""

    OCCUPANCY_PCT = "occupancy_pct"
    ROOM_NIGHTS_SOLD = "room_nights_sold"
    ROOM_NIGHTS_AVAILABLE = "room_nights_available"
    GUESTS_BY_NATIONALITY = "guests_by_nationality"

    @property
    def is_percentage(self) -> bool:
        """Percentage metrics take the ±0.10pp tolerance; everything else is exact (D-TOL-02)."""
        return self is Metric.OCCUPANCY_PCT

    @property
    def requires_dimension(self) -> bool:
        """A nationality figure without a country is not a claim about anything."""
        return self is Metric.GUESTS_BY_NATIONALITY


class Dimension(StrEnum):
    NATIONALITY_ISO2 = "nationality_iso2"


_ISO2 = re.compile(r"^[A-Z]{2}$")


class MetricKey(BaseModel):
    """A metric, a period, and optionally one dimension value.

    Renders to the canonical string form used in `verdict.json` and in every finding:

        occupancy_pct:2026-01
        guests_by_nationality:2026-02:nationality_iso2=DE
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: Metric
    period: str = Field(
        description="YYYY-MM, YYYY-Qn or YYYY. A quarter is a separate key, never a month (D-KEY-01).",
        examples=["2026-01", "2026-Q1", "2026"],
    )
    dimension: Dimension | None = None
    value: str | None = Field(
        default=None, description="The canonical code, never the source label (D-KEY-02)."
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        # Delegated to `Period` so the accepted forms are defined once. Two copies of this rule
        # would agree until somebody added a fourth form to one of them, and the symptom would be
        # a key the metric library can build and the reconciliation join cannot parse.
        if not Period.is_valid(self.period):
            raise ValueError(f"period must be YYYY-MM, YYYY-Qn or YYYY, got {self.period!r}")

        if (self.dimension is None) != (self.value is None):
            raise ValueError("dimension and value must be given together or not at all")

        if self.metric.requires_dimension and self.dimension is None:
            raise ValueError(f"{self.metric} requires a dimension")
        if not self.metric.requires_dimension and self.dimension is not None:
            raise ValueError(f"{self.metric} takes no dimension, got {self.dimension}")

        # The whole point of normalisation is that only canonical codes get this far. An
        # unmappable label is blocking and never reaches a key (D-NAT-12).
        if (
            self.dimension is Dimension.NATIONALITY_ISO2
            and self.value is not None
            and not _ISO2.match(self.value)
        ):
            raise ValueError(
                f"nationality_iso2 must be an ISO 3166-1 alpha-2 code, got {self.value!r}"
            )
        return self

    @property
    def rendered(self) -> str:
        base = f"{self.metric.value}:{self.period}"
        if self.dimension is None:
            return base
        return f"{base}:{self.dimension.value}={self.value}"

    @classmethod
    def parse(cls, rendered: str) -> Self:
        """Inverse of `rendered`. Round-trip stability is asserted by a unit test."""
        parts = rendered.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"malformed metric key: {rendered!r}")

        metric, period = parts[0], parts[1]
        if len(parts) == 2:
            return cls(metric=Metric(metric), period=period)

        dim_name, _, dim_value = parts[2].partition("=")
        if not dim_value:
            raise ValueError(f"dimension segment must be name=value: {parts[2]!r}")
        return cls(
            metric=Metric(metric),
            period=period,
            dimension=Dimension(dim_name),
            value=dim_value,
        )

    def __str__(self) -> str:
        return self.rendered
