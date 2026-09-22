"""Evidence references.

Every finding cites a PDF page and row range **and** an Excel cell (D-EV-01). These types
exist so that citation is a property of the type system rather than a convention somebody
remembers to follow: a `Finding` cannot be constructed without them.

The one permitted absence is typed. A blocking finding raised before claims are parsed —
an unreadable page, a failed totals reconciliation — has no Excel cell to point at, and
records `NotReached` rather than an empty string (D-EV-02). An empty string is
indistinguishable from a bug; a `NotReached` is a statement.

Sources come in two kinds and the second is not a PDF. `room_nights_available` is derived from the
room inventory reference, which D-RNA-01 is explicit is a **property attribute, not a reservation
attribute** — so it is a separate CSV and has no page number. `InventoryRef` covers it; see the note
on that class for why forcing it into a `PdfRef` would put a false citation in front of a reviewer.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A1 notation: column letters then a row number. Absolute markers ($A$1) are rejected
# rather than stripped — they would mean the parser read a formula, not a value.
_A1 = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")

type CellRef = Annotated[str, Field(pattern=_A1.pattern, examples=["D14", "AB7"])]


class PdfRef(BaseModel):
    """Where in a PDF a value came from. Page and rows are 1-indexed, as a human reads them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1, description="Filename as submitted, not an absolute path.")
    page: int = Field(ge=1, description="1-indexed, matching what a PDF reader displays.")
    row_start: int = Field(ge=1, description="1-indexed row within the page's table.")
    row_end: int = Field(ge=1, description="Inclusive. Equal to row_start for a single row.")

    @model_validator(mode="after")
    def _rows_ordered(self) -> PdfRef:
        if self.row_end < self.row_start:
            raise ValueError(f"row_end {self.row_end} precedes row_start {self.row_start}")
        return self

    @field_validator("file")
    @classmethod
    def _not_a_path(cls, value: str) -> str:
        # An absolute path in a citation leaks the machine that ran the verification into
        # an artefact that gets forwarded to a hotel.
        if "/" in value or "\\" in value:
            raise ValueError(f"file must be a bare filename, not a path: {value!r}")
        return value

    @property
    def citation(self) -> str:
        """What a reviewer reads: `march.pdf p.4 rows 12-18`."""
        rows = (
            f"row {self.row_start}"
            if self.row_start == self.row_end
            else f"rows {self.row_start}-{self.row_end}"
        )
        return f"{self.file} p.{self.page} {rows}"


class InventoryRef(BaseModel):
    """Where in the room inventory reference a value came from.

    This type exists because of a gap the metric library walked straight into, and the gap mirrors
    something real about the domain. `room_nights_available` is in scope, so a workbook can claim it
    and the system must be able to cite what it checked the claim against — but D-RNA-01 is explicit
    that rooms available is a **property attribute, not a reservation attribute**, and therefore does
    not appear in the reservation export at all. It comes from a separate CSV.

    So the one metric whose source is not a PDF is exactly the one the definitions single out as not
    being a reservation attribute. A `PdfRef` cannot describe it: there is no page, and inventing
    `page=1` to satisfy a type would put a false citation in front of a reviewer.

    No `page`, therefore, and rows are 1-indexed **data** rows — the header is row 0, so row 1 is the
    first day. That matches how a spreadsheet application numbers the file a reviewer will open.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1, description="Filename as submitted, not an absolute path.")
    row_start: int = Field(ge=1, description="1-indexed data row; the header is not row 1.")
    row_end: int = Field(ge=1, description="Inclusive. Equal to row_start for a single day.")

    @model_validator(mode="after")
    def _rows_ordered(self) -> InventoryRef:
        if self.row_end < self.row_start:
            raise ValueError(f"row_end {self.row_end} precedes row_start {self.row_start}")
        return self

    @field_validator("file")
    @classmethod
    def _not_a_path(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError(f"file must be a bare filename, not a path: {value!r}")
        return value

    @property
    def citation(self) -> str:
        """What a reviewer reads: `inventory_2026-Q1.csv rows 32-59`."""
        rows = (
            f"row {self.row_start}"
            if self.row_start == self.row_end
            else f"rows {self.row_start}-{self.row_end}"
        )
        return f"{self.file} {rows}"


# What a computed value can cite as its source: reservation rows on a PDF page, or days of the
# inventory reference. Deliberately closed — a third kind of source would be a new input to the
# system, which is a decision worth making explicitly rather than by adding a member here.
type SourceRef = PdfRef | InventoryRef


class ExcelRef(BaseModel):
    """Where in the submitted workbook a claim was read from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sheet: str = Field(min_length=1)
    cell: CellRef

    @property
    def citation(self) -> str:
        """Canonical A1 form: `Nationality!D14`."""
        return f"{self.sheet}!{self.cell}"


class NotReached(BaseModel):
    """A typed absence for the one case where no Excel cell exists yet (D-EV-02).

    Raised when extraction halts before the workbook is parsed. Carrying a reason means
    the memo can say *why* there is no cell reference instead of leaving a blank that
    reads like a defect.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["not_reached"] = "not_reached"
    reason: str = Field(min_length=1, examples=["extraction halted before claim parsing"])

    @property
    def citation(self) -> str:
        return f"not reached ({self.reason})"


# An Excel citation is either a real cell or an explicit, explained absence. There is no
# third option, and in particular there is no `None`.
type ExcelCitation = ExcelRef | NotReached

# What a finding cites on the source side. Three members, and each was added because the two before
# it could not describe a case that actually arose.
#
# **`PdfRef`** is the ordinary one: a reservation figure traced to rows on a page.
#
# **`NotReached`** came from PDF extraction in the Excel direction and the Excel claim parser in this one. A run that halts
# while reading the PDFs has no workbook cell; and the Excel claim parser's self-consistency checks —
# does each stated total equal the sum of its own components — run *before* the workbook is compared
# to anything, so a finding from one has a cell and no page, by construction.
#
# **`InventoryRef`** came from reconciliation and classification, and it is the same asymmetry a third time. `room_nights_available`
# is an in-scope metric that a workbook claims and the system must check, and D-RNA-01 is explicit
# that rooms available is a **property attribute, not a reservation attribute** — so its only source
# is the inventory CSV and there is no page anywhere in the system to cite. Without this member a
# finding about it could not be constructed at all, and the alternatives are both worse: dropping the
# finding hides a real variance, and dressing the inventory row as `PdfRef(page=1)` puts a citation in
# front of a reviewer that leads nowhere.
#
# The field is therefore `source_ref`, not `pdf_ref`. A field named for one of its three members
# reads as a small lie every time a reviewer meets an inventory citation in it.
#
# A finding may carry **one** typed absence. What it may never carry is two on both sides at once,
# because a finding citing nothing anywhere is not evidence of anything — `Finding` enforces that.
type SourceCitation = PdfRef | InventoryRef | NotReached
