"""The canonical reservation record — the only structure the metric library accepts.

Three of this POC's promises are enforced here, at construction, rather than downstream
where they would be checks somebody could forget to run:

- **`room_nights` is always derived** as `nights × rooms` (D-RNS-02). A record whose
  `room_nights` disagrees cannot be constructed, so a printed column can never be silently
  adopted as an input. The parser compares the printed value and raises a blocking finding;
  it does not get the option of trusting it.
- **`nights` is `departure − arrival`** (D-RNS-01). A record cannot carry a night count that
  contradicts its own dates.
- **No guest name enters state, output or logs** (D-EV-03). `guest_ref` is constrained to an
  opaque pattern, so a parser that reaches for the name column fails immediately and loudly
  instead of leaking a name into a verdict that gets emailed to a hotel.

Everything is frozen. A record that could be mutated after extraction is a record whose
citation might no longer describe it.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tda.contracts.refs import InventoryRef, PdfRef


class Status(StrEnum):
    """Reservation status. Closed set — an unknown vendor string is blocking (D-QUAL-03).

    Never coerced, never similarity-matched, never treated as CHECKED_OUT. A PMS that emits
    `Checked Out` or `CO` is a normalisation problem for the parser's committed lookup, not
    a guessing problem for this enum.
    """

    CHECKED_OUT = "CHECKED_OUT"
    IN_HOUSE = "IN_HOUSE"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"


class RateCode(StrEnum):
    """Rate code. Drives the complimentary and house-use rules (D-QUAL-04, D-QUAL-05)."""

    BAR = "BAR"
    CORP = "CORP"
    GOV = "GOV"
    OTA = "OTA"
    GROUP = "GROUP"
    COMP = "COMP"
    HOUSE = "HOUSE"


class ReservationRecord(BaseModel):
    """One reservation, normalised. The only structure the metric library accepts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reservation_id: str = Field(
        min_length=1, description="Used only for duplicate detection (D-QUAL-07)."
    )
    hotel_id: str = Field(
        min_length=1,
        description="Must match across all four submitted files; a mismatch is an intake rejection.",
    )
    guest_ref: str = Field(
        pattern=r"^g_[0-9a-f]{8,32}$",
        description=(
            "Opaque reference. The pattern is the enforcement of D-EV-03: a parser that puts a "
            "guest name here raises a validation error rather than leaking it downstream."
        ),
        examples=["g_4f2a91c07b3e"],
    )
    nationality_iso2: str = Field(
        pattern=r"^[A-Z]{2}$",
        description="ISO 3166-1 alpha-2. Unmapped values are blocking, never guessed (D-NAT-12).",
    )

    adults: int = Field(
        ge=1, description="At least one adult; a reservation with none is malformed."
    )
    children: int = Field(
        ge=0,
        description="Held separately from adults so guest-count definitions can be tested both "
        "ways without regenerating the corpus (D-NAT-04, assumption A-05).",
    )

    rooms: int = Field(ge=1)
    nights: int = Field(ge=0, description="0 means day-use (D-QUAL-08).")
    room_nights: int = Field(
        ge=0, description="Always derived as nights x rooms. Validated below (D-RNS-02)."
    )

    arrival_date: date
    departure_date: date

    status: Status
    rate_code: RateCode

    source: PdfRef = Field(description="Required. Evidence citation depends on it (D-EV-01).")

    @model_validator(mode="after")
    def _derivations_hold(self) -> Self:
        if self.departure_date < self.arrival_date:
            raise ValueError(
                f"departure {self.departure_date} precedes arrival {self.arrival_date}"
            )

        expected_nights = (self.departure_date - self.arrival_date).days
        if self.nights != expected_nights:
            raise ValueError(
                f"nights={self.nights} contradicts the dates "
                f"({self.arrival_date} to {self.departure_date} is {expected_nights} nights) "
                "- D-RNS-01"
            )

        expected_room_nights = self.nights * self.rooms
        if self.room_nights != expected_room_nights:
            raise ValueError(
                f"room_nights={self.room_nights} is not nights x rooms "
                f"({self.nights} x {self.rooms} = {expected_room_nights}). A printed column is a "
                "cross-check, never an input - D-RNS-02"
            )
        return self

    # ── derived properties used by the metric library ────────────────────────
    # Deliberately properties rather than stored fields: a stored value is a second place
    # for the truth to live, and these are cheap.

    @property
    def is_day_use(self) -> bool:
        """D-QUAL-08. Zero room-nights, but still a guest of the destination (D-QUAL-10)."""
        return self.nights == 0

    @property
    def guests(self) -> int:
        """Adults plus children (D-NAT-02). Whether children *count* is policy, applied
        by the metric library — this property reports what the record contains."""
        return self.adults + self.children

    def occupied_nights(self) -> list[date]:
        """The nights this reservation occupied: arrival through departure - 1, inclusive.

        The night of the departure date is not occupied (D-RNS-03). This is the list that
        makes month apportionment a fact rather than an interpretation — and it is empty
        for a day-use stay, which is exactly why day-use is invisible to occupancy.
        """
        return [
            date.fromordinal(ordinal)
            for ordinal in range(self.arrival_date.toordinal(), self.departure_date.toordinal())
        ]


class InventoryDay(BaseModel):
    """One row of the per-hotel room inventory reference.

    Occupancy cannot be verified without this. Rooms available is a **property attribute**,
    not a reservation attribute, and does not appear in a reservation export — so it is a
    required input, never inferred from a room count observed in the reservations (D-RNA-04).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hotel_id: str = Field(min_length=1)
    day: date
    rooms_total: int = Field(ge=0)
    rooms_out_of_order: int = Field(
        ge=0,
        description="Excluded from the occupancy denominator (D-RNA-03, assumption A-08).",
    )
    source: InventoryRef = Field(
        description=(
            "Required, symmetrically with ReservationRecord.source. `room_nights_available` is a "
            "claim a workbook can make, so a variance on it needs a citation - and because rooms "
            "available appears in no reservation export (D-RNA-01), that citation is a row of the "
            "inventory reference rather than a PDF page."
        )
    )

    @model_validator(mode="after")
    def _ooo_within_total(self) -> Self:
        if self.rooms_out_of_order > self.rooms_total:
            raise ValueError(
                f"rooms_out_of_order={self.rooms_out_of_order} exceeds "
                f"rooms_total={self.rooms_total} on {self.day}"
            )
        return self

    @property
    def rooms_available(self) -> int:
        """Net of out-of-order rooms. A room that cannot be sold was not available."""
        return self.rooms_total - self.rooms_out_of_order
