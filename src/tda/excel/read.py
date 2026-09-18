"""Stage two: `openpyxl` walks the mapped ranges and code reads the values. No model involved.

Everything above this module decided *where* to look. This module is the only place a figure the
hotel asserted becomes a `Claim`, and it does so with no model in the call stack at all — the
mapping is already fixed by the time anything here runs.

Every claim carries the cell it came from (`Nationality!D14`), because `Claim.excel_ref` is
non-optional and there is no such thing as an uncited claim.

## Four things a cell can be, and only one of them is a figure

**A number.** Read as `Decimal(str(...))`, never `Decimal(float)`: the second one turns `81.7` into
`81.7000000000000028421709430404007434844970703125` and a system that reported a variance caused by
its own representation error would have no business reporting variances.

**Text that means a number.** `'81.70%'`, `'1,285'`, `'(44)'`, `'AED 612.40'`. Parsed, with the
original kept in `Claim.raw_text` so a reviewer sees what the hotel typed rather than only what we
made of it.

**Empty.** No claim. Not a zero — and the difference is not academic. The demo corpus has Iceland
with three March guests and blank January and February cells, and the ground truth has *no key at
all* for those two months rather than a zero. A parser that read blanks as zeros would manufacture
two claims the workbook never made, and a completeness check that demanded a value in every cell
would report a correct workbook as defective. Whether a blank is hiding a real omission is settled
by arithmetic in `selfcheck.py`, not by assuming.

**A formula with no cached value.** A refusal. `openpyxl` does not evaluate formulas, and a
workbook written by a tool rather than by Excel has no cached result to fall back on. Computing it
ourselves would mean this system deciding what the hotel claimed, which is precisely the thing it
exists not to do.

## The percentage trap

A cell displaying `69.09%` may hold `69.09` or may hold `0.6909` with a percent number format —
Excel's native way of storing a percentage. Reading the second as `0.6909` would produce a claimed
occupancy of 0.69% against a computed 69.09% and a spectacular false finding. So the number format
is consulted: a stored fraction under a `%` format is scaled to percentage points, which is the unit
`occupancy_pct` is defined in. The demo corpus happens to use plain numbers under a `0.00` format,
which is exactly why this needs a test of its own rather than the corpus standing in for one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final

from tda.contracts import Claim, ExcelRef, Metric, MetricKey, Period
from tda.excel.mapping import Disposition, Orientation
from tda.extract.normalise import UnmappableLabelError, nationality

if TYPE_CHECKING:
    from openpyxl.workbook.workbook import Workbook

    from tda.excel.mapping import ResolvedBlock
    from tda.extract.normalise import Lookups

_MONTH_NAMES: Final[dict[str, int]] = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}

# Words a hotel puts on a period label that do not change which period it is. `'2026-Q1 total'` and
# `'2026-Q1'` are the same period; the first is just a spreadsheet saying the column is a roll-up.
_PERIOD_NOISE: Final = re.compile(r"\b(total|totals|sum|all|ytd|period)\b", re.IGNORECASE)

_ISO_MONTH: Final = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER: Final = re.compile(r"^(\d{4})[-\s]?Q([1-4])$", re.IGNORECASE)
_YEAR: Final = re.compile(r"^(\d{4})$")
_MONTH_NAME_YEAR: Final = re.compile(r"^([A-Za-z]+)[\s,\-]+(\d{4})$")
_YEAR_MONTH_NAME: Final = re.compile(r"^(\d{4})[\s,\-]+([A-Za-z]+)$")

# Currency and unit decoration around a figure. Kept in step with `tools.looks_numeric`, which has
# to make the same judgement from the other side - a cell that reads as a value to the redactor and
# fails to parse here would be a cell the agent could not see and the reader could not use.
_CURRENCY: Final = frozenset("$€£¥₹")
_UNIT_WORDS: Final = ("aed", "usd", "eur", "gbp")


class CellValueError(ValueError):
    """A cell could not be read as a figure.

    Carried to the caller as a `ReadDefect` rather than propagated: one unreadable cell must not
    discard the ninety-three claims around it.
    """


def parse_period_label(text: str) -> str:
    """A human period label into the canonical rendered form.

    `'January 2026'` → `'2026-01'`, `'2026-Q1 total'` → `'2026-Q1'`, `'2026-03'` unchanged.

    Deliberately strict. An unrecognised label raises, which becomes a flagged mapping rather than a
    claim filed under a period somebody guessed at — and a claim under the wrong period is worse
    than no claim, because it reconciles against a figure it has nothing to do with.
    """
    cleaned = _PERIOD_NOISE.sub("", text).strip().strip(",;:-\u2013\u2014").strip()
    if not cleaned:
        raise CellValueError(f"period label {text!r} is empty once roll-up wording is removed")

    if match := _ISO_MONTH.match(cleaned):
        rendered = f"{match.group(1)}-{match.group(2)}"
    elif match := _QUARTER.match(cleaned):
        rendered = f"{match.group(1)}-Q{match.group(2)}"
    elif match := _YEAR.match(cleaned):
        rendered = match.group(1)
    else:
        name_year = _MONTH_NAME_YEAR.match(cleaned) or _YEAR_MONTH_NAME.match(cleaned)
        if name_year is None:
            raise CellValueError(
                f"{text!r} is not a period this system recognises. Expected a form like "
                "'January 2026', '2026-01', '2026-Q1' or '2026'."
            )
        first, second = name_year.group(1), name_year.group(2)
        name, year = (first, second) if first.isalpha() else (second, first)
        month = _MONTH_NAMES.get(name.lower().rstrip("."))
        if month is None:
            raise CellValueError(f"{name!r} in {text!r} is not a month name")
        rendered = f"{year}-{month:02d}"

    if not Period.is_valid(rendered):
        raise CellValueError(f"{text!r} parsed to {rendered!r}, which is not a valid period")
    return rendered


def parse_value(raw: object, *, number_format: str = "General") -> tuple[Decimal, str | None]:
    """One cell into a figure, plus the literal text when the cell was not already numeric.

    Raises `CellValueError` for anything that is not a figure. Never returns a default: a cell this
    cannot read is reported, not replaced with zero.
    """
    if raw is None:
        raise CellValueError("cell is empty")

    if isinstance(raw, bool):
        # Before the int branch: `bool` is a subclass of `int`, and `True` would otherwise read as 1.
        raise CellValueError("cell holds a boolean, which is not a figure")

    if isinstance(raw, int | float | Decimal):
        value = Decimal(str(raw))
        if "%" in number_format:
            # Stored as a fraction under a percent format - Excel's native representation. See the
            # module docstring: reading 0.6909 as the claim is a 68-point false variance.
            value *= 100
        return value, None

    if isinstance(raw, datetime | date | time | timedelta):
        raise CellValueError(f"cell holds a {type(raw).__name__}, which is not a figure")

    if not isinstance(raw, str):
        raise CellValueError(f"cell holds a {type(raw).__name__}, which is not a figure")

    text = raw.strip()
    if not text:
        raise CellValueError("cell is empty")
    if text.startswith("="):
        raise CellValueError(
            f"cell holds the formula {text!r} and the workbook carries no cached result for it. "
            "Formulas are not evaluated - computing one here would mean deciding what the hotel "
            "claimed rather than reading it."
        )

    candidate = text
    negative = candidate.startswith("(") and candidate.endswith(")")
    if negative:
        candidate = candidate[1:-1].strip()

    is_percent = candidate.endswith("%")
    candidate = candidate.removesuffix("%").strip()
    candidate = "".join(ch for ch in candidate if ch not in _CURRENCY).strip()
    for word in _UNIT_WORDS:
        if candidate.lower().startswith(word):
            candidate = candidate[len(word) :].strip()
        if candidate.lower().endswith(word):
            candidate = candidate[: -len(word)].strip()
    candidate = re.sub(r"(?<=[0-9]),(?=[0-9]{3}\b)", "", candidate)

    try:
        value = Decimal(candidate)
    except (InvalidOperation, ValueError) as problem:
        raise CellValueError(f"{text!r} is not a figure") from problem

    if negative:
        value = -value
    # A text percentage is already in percentage points - `'81.70%'` means 81.70, the same unit the
    # numeric cells use. It is *not* scaled, unlike the stored-fraction case above.
    _ = is_percent
    return value, text


@dataclass(frozen=True, slots=True)
class ReadDefect:
    """A cell inside a mapped range that could not become a claim, and why.

    Carried rather than raised. One unreadable cell must not discard the other 93 claims — a
    reviewer needs "this cell, this reason" and the rest of the workbook to look at.

    `kind` distinguishes the one defect a caller must treat as blocking (D-NAT-12: a label the
    committed lookup has no entry for) from every other kind here, which are refused claims rather
    than refused submissions. It stays `None` for those, matched by prose in `reason` rather than by
    a tag, because nothing downstream needs to act on them differently from one another. Only
    `run.parse_claims` reads `kind`, and it reads nothing else about a defect to decide whether to
    escalate, which is what keeps that decision a single check rather than a string match on a
    message meant for a human.
    """

    sheet: str
    cell: str
    reason: str
    kind: str | None = None

    @property
    def citation(self) -> str:
        return f"{self.sheet}!{self.cell}"


UNMAPPABLE_LABEL: Final = "unmappable_label"


@dataclass(frozen=True, slots=True)
class StatedTotal:
    """A total the workbook states across the dimension axis — the `Total` row under the countries.

    Not a `Claim`, and the type system agrees: `guests_by_nationality` requires a dimension value and
    `Total` is not a country, so no `MetricKey` can be built for it. It is an internal cross-check
    the hotel wrote down, and `selfcheck.py` is what it is for.
    """

    metric: Metric
    period: str
    value: Decimal
    ref: ExcelRef


@dataclass(frozen=True, slots=True)
class BlockReading:
    """Everything read from one mapped block."""

    resolved: ResolvedBlock
    claims: tuple[Claim, ...] = ()
    stated_totals: tuple[StatedTotal, ...] = ()
    defects: tuple[ReadDefect, ...] = ()


def _labels(workbook: Workbook, sheet: str, cells: list[str]) -> list[tuple[str, object]]:
    worksheet = workbook[sheet]
    return [(cell, worksheet[cell].value) for cell in cells]


def _formula_at(formulas: Workbook | None, sheet: str, cell: str) -> str | None:
    """The formula text in a cell, from the second view of the workbook, or `None`.

    An empty cell and a formula whose result was never cached are **both `None`** in the
    `data_only=True` view, and they mean opposite things: the first is the absence of a claim
    (D-XLS-02) and the second is a claim we decline to compute (D-XLS-03). Telling them apart is
    only possible by also looking at the `data_only=False` view, which is why the caller passes one.

    Without this the refusal in D-XLS-03 would be unreachable — every uncached formula would read as
    a blank cell and disappear from the output entirely, which is the quietest possible way for a
    figure to go missing.
    """
    if formulas is None or sheet not in formulas.sheetnames:
        return None
    raw = formulas[sheet][cell].value
    return raw if isinstance(raw, str) and raw.startswith("=") else None


def read_block(
    resolved: ResolvedBlock,
    workbook: Workbook,
    lookups: Lookups,
    *,
    formulas: Workbook | None = None,
) -> BlockReading:
    """Read one in-scope block into claims.

    A block that is not in scope reads as nothing at all — not an error, because the decision has
    already been taken and recorded; the caller distinguishes the cases by `disposition`.

    `formulas` is the same workbook opened with `data_only=False`. Optional, because a caller
    holding only one view still gets every claim; what it loses is the ability to tell an empty cell
    from an uncached formula, and it loses it silently, so `run.parse_claims` always passes it.
    """
    if resolved.disposition is not Disposition.IN_SCOPE:
        return BlockReading(resolved=resolved)

    assert resolved.metric is not None  # guaranteed by Disposition.IN_SCOPE
    assert resolved.values is not None and resolved.periods is not None
    metric, values, periods = resolved.metric, resolved.values, resolved.periods
    sheet = resolved.block.sheet
    worksheet = workbook[sheet]

    claims: list[Claim] = []
    totals: list[StatedTotal] = []
    defects: list[ReadDefect] = []

    # ── periods, once, in the order the sheet lays them out ──────────────────
    period_cells = (
        periods.column_cells(periods.min_col)
        if resolved.block.orientation is Orientation.PERIODS_DOWN_ROWS
        else periods.row_cells(periods.min_row)
    )
    parsed_periods: list[str | None] = []
    for cell, raw in _labels(workbook, sheet, period_cells):
        if not isinstance(raw, str) or not raw.strip():
            parsed_periods.append(None)
            defects.append(
                ReadDefect(sheet=sheet, cell=cell, reason="period label is empty or not text")
            )
            continue
        try:
            parsed_periods.append(parse_period_label(raw))
        except CellValueError as problem:
            parsed_periods.append(None)
            defects.append(ReadDefect(sheet=sheet, cell=cell, reason=str(problem)))

    # ── dimension values, likewise ───────────────────────────────────────────
    parsed_dimension: list[str | None] = []
    if resolved.dimension_labels is not None:
        labels = resolved.dimension_labels
        for cell, raw in _labels(workbook, sheet, labels.column_cells(labels.min_col)):
            if not isinstance(raw, str) or not raw.strip():
                parsed_dimension.append(None)
                defects.append(
                    ReadDefect(
                        sheet=sheet, cell=cell, reason="dimension label is empty or not text"
                    )
                )
                continue
            try:
                parsed_dimension.append(nationality(raw, lookups))
            except UnmappableLabelError as problem:
                # Never guessed at. D-NAT-12: an unmappable label is blocking, and the closest
                # match is exactly the kind of help that produces a confident wrong answer.
                parsed_dimension.append(None)
                defects.append(
                    ReadDefect(sheet=sheet, cell=cell, reason=str(problem), kind=UNMAPPABLE_LABEL)
                )

    # ── the values ───────────────────────────────────────────────────────────
    for row_index, row in enumerate(values.cells_by_row()):
        for col_index, cell in enumerate(row):
            if resolved.block.orientation is Orientation.PERIODS_DOWN_ROWS:
                period = parsed_periods[row_index]
                dimension_value = parsed_dimension[row_index] if parsed_dimension else None
            else:
                period = parsed_periods[col_index]
                dimension_value = parsed_dimension[row_index] if parsed_dimension else None

            target = worksheet[cell]
            if target.value is None:
                if (formula := _formula_at(formulas, sheet, cell)) is not None:
                    defects.append(
                        ReadDefect(
                            sheet=sheet,
                            cell=cell,
                            reason=(
                                f"cell holds the formula {formula!r} and the workbook carries no "
                                "cached result for it. Formulas are not evaluated - computing one "
                                "here would mean deciding what the hotel claimed rather than "
                                "reading it (D-XLS-03)."
                            ),
                        )
                    )
                    continue
                # Genuinely empty. No claim, and no defect — see the module docstring on Iceland.
                continue
            if period is None or (resolved.dimension is not None and dimension_value is None):
                # The label this value hangs off could not be read; the defect is already recorded
                # against the label cell, and filing the value under a guessed key would be worse
                # than not filing it.
                continue

            try:
                value, raw_text = parse_value(target.value, number_format=str(target.number_format))
            except CellValueError as problem:
                defects.append(ReadDefect(sheet=sheet, cell=cell, reason=str(problem)))
                continue

            key = MetricKey(
                metric=metric,
                period=period,
                dimension=resolved.dimension,
                value=dimension_value,
            )
            claims.append(
                Claim(
                    key=key,
                    value=value,
                    excel_ref=ExcelRef(sheet=sheet, cell=cell),
                    raw_text=raw_text,
                )
            )

    # ── the stated total across the dimension, if the sheet has one ──────────
    if resolved.dimension_totals is not None:
        total_rect = resolved.dimension_totals
        total_cells = (
            total_rect.row_cells(total_rect.min_row)
            if total_rect.is_single_row
            else total_rect.column_cells(total_rect.min_col)
        )
        for index, cell in enumerate(total_cells):
            if index >= len(parsed_periods):
                defects.append(
                    ReadDefect(
                        sheet=sheet,
                        cell=cell,
                        reason="stated total has no period label above it",
                    )
                )
                continue
            period = parsed_periods[index]
            target = worksheet[cell]
            if target.value is None or period is None:
                continue
            try:
                value, _ = parse_value(target.value, number_format=str(target.number_format))
            except CellValueError as problem:
                defects.append(ReadDefect(sheet=sheet, cell=cell, reason=str(problem)))
                continue
            totals.append(
                StatedTotal(
                    metric=metric,
                    period=period,
                    value=value,
                    ref=ExcelRef(sheet=sheet, cell=cell),
                )
            )

    return BlockReading(
        resolved=resolved,
        claims=tuple(claims),
        stated_totals=tuple(totals),
        defects=tuple(defects),
    )
