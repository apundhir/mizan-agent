"""Stage one: what the mapping agent may say, and what code does with it.

The agent's entire job is **which range holds which metric**. It answers in sheet names, A1 ranges
and metric names — never a value, because `tools.py` gives it no way to see one, and never a number
at all, because `AgentOutput` forbids numeric fields and the schema lint enforces it.

Everything it says is then checked here before anything is read. Three checks, each closing a
different way a plausible-looking mapping goes wrong:

1. **The metric name must be in the policy's vocabulary.** D-KEY-03 says a metric key is never
   constructed from a model output, and this is the one place a model could influence which metric a
   number belongs to. `Metric` is a closed enum and `policy.scope` holds the out-of-scope names, so a
   returned name is either one the configuration already knew about or it is not accepted at all. An
   invented `room_nights_booked` does not become a metric; it becomes a mapping a human must look at.

2. **The ranges must fit the sheet, and fit each other.** A block whose value range has four columns
   and whose period labels have three is not a mapping, it is an off-by-one, and the damage it does
   is silent: every claim after the mismatch is attributed to the wrong period.

3. **Every figure must be accounted for.** Not by asking the agent to be exhaustive — by checking,
   at two levels. `unaccounted_sheets` compares the workbook's sheet list against everything the
   mapping mentions. `uncovered_values` then goes further and finds cells holding a figure that no
   mapped range covers, which is the case that actually occurs: a header block missed *inside* a
   sheet the mapping does talk about. "Never silently skipped" is then a property of the code rather
   than of the model's diligence, which is the only version of that promise worth stating.

## Out of scope is a decision, not a gap

A hotel's workbook has sheets this POC does not verify — average daily rate, RevPAR, length of stay.
Treating those the same as a sheet nobody recognises would be wrong in both directions: it would put
a known, already-decided case in front of a human every single run, and it would bury the genuinely
unknown case in the noise. So the vocabulary has two halves and the outcome has three:

| the agent returns | outcome |
|---|---|
| a metric in `scope.metrics_in_scope` | claims are parsed from the range |
| a metric in `scope.metrics_out_of_scope` | recorded as out-of-scope, no claims, no finding (D-SCOPE-02) |
| anything else, or nothing | flagged for human mapping |
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import Field

from tda.agents.contracts.base import AgentOutput
from tda.contracts import Dimension, Metric
from tda.excel.geometry import RangeError, Rect
from tda.excel.tools import looks_numeric

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openpyxl.workbook.workbook import Workbook

    from tda.policy import Policy


class Orientation(StrEnum):
    """Which way round the table is.

    Both forms appear in one real workbook, which is why this cannot be assumed. `Occupancy` runs
    periods down the rows with a metric per column; `Nationality` runs periods across the columns
    with a country per row. Asking the agent to say which is asking it to read a layout, which is
    what a model is for.
    """

    PERIODS_DOWN_ROWS = "periods_down_rows"
    PERIODS_ACROSS_COLUMNS = "periods_across_columns"


class MetricBlock(AgentOutput):
    """One rectangle of values, and what they are figures *of*.

    Every field is a name or an A1 range. Nothing here is a number, and nothing here is a value.
    """

    sheet: str = Field(min_length=1)
    metric: str = Field(
        min_length=1,
        description=(
            "A metric name from the vocabulary given in the prompt. Checked against policy before "
            "use - an unrecognised name is a mapping for a human, never a new metric (D-KEY-03)."
        ),
    )
    orientation: Orientation
    value_range: str = Field(description="A1 range of the value cells only, e.g. 'B5:B8'.")
    period_label_range: str = Field(
        description="A1 range of the cells holding the period labels, e.g. 'A5:A8' or 'B4:E4'."
    )
    dimension: str | None = Field(
        default=None, description="'nationality_iso2', or null for an undimensioned metric."
    )
    dimension_label_range: str | None = Field(
        default=None, description="A1 range of the dimension labels, e.g. 'A5:A25'."
    )
    dimension_total_range: str | None = Field(
        default=None,
        description=(
            "A1 range of a stated total *across the dimension* - the 'Total' row under a list of "
            "countries. Excluded from the value range, because a total is not one more country."
        ),
    )


class CoverSheet(AgentOutput):
    """The sheet that identifies the submission rather than carrying figures.

    Mapped as a role rather than left unmapped: the cover sheet is *used* — its property code and
    period are checked against the PDFs before any figure is compared — so flagging it for a human
    every run would be reporting a correct workbook as incomplete.
    """

    sheet: str = Field(min_length=1)
    property_code_cell: str = Field(description="A1 cell holding the property/hotel code.")
    period_cell: str = Field(description="A1 cell holding the reporting period.")


class UnmappedBlock(AgentOutput):
    """Something the agent could not place, said out loud.

    The `reason` is what a human reads when deciding what to do about it, so it is a sentence. A
    mapping agent that returns an empty `unmapped` list for a workbook it did not understand is
    caught by `unaccounted_sheets`, not by this type.
    """

    sheet: str = Field(min_length=1)
    cells: str = Field(description="A1 range covering what could not be mapped.")
    reason: str = Field(min_length=1)


class WorkbookMapping(AgentOutput):
    """The agent's whole answer."""

    blocks: tuple[MetricBlock, ...] = ()
    cover: CoverSheet | None = None
    unmapped: tuple[UnmappedBlock, ...] = ()


# ── what code makes of it ────────────────────────────────────────────────────


class Disposition(StrEnum):
    """What happens to a block once policy has been consulted."""

    IN_SCOPE = "in_scope"
    OUT_OF_SCOPE = "out_of_scope"
    NEEDS_HUMAN_MAPPING = "needs_human_mapping"


@dataclass(frozen=True, slots=True)
class ResolvedBlock:
    """A block after checking, carrying either a usable geometry or the reason it is not usable.

    `metric` is a `Metric` enum member only for an in-scope block. For an out-of-scope one the name
    is kept as text — it is a real, policy-declared name, but deliberately not a member of the closed
    enum the metric library computes over.
    """

    block: MetricBlock
    disposition: Disposition
    metric: Metric | None = None
    out_of_scope_name: str | None = None
    values: Rect | None = None
    periods: Rect | None = None
    dimension: Dimension | None = None
    dimension_labels: Rect | None = None
    dimension_totals: Rect | None = None
    problem: str | None = None

    @property
    def usable(self) -> bool:
        return self.disposition is Disposition.IN_SCOPE


def _rect(text: str | None, *, what: str) -> Rect:
    if text is None:
        raise RangeError(f"{what} is required for this block but was null")
    return Rect.parse(text)


def resolve(block: MetricBlock, workbook: Workbook, policy: Policy) -> ResolvedBlock:
    """Check one block against policy and against the sheet it claims to describe.

    Never raises for bad agent output: an unusable block comes back as `NEEDS_HUMAN_MAPPING`
    carrying the reason, because one unmappable block must not take down the parse of the other
    three. A malformed *call* — a sheet that is not in the workbook — is the same thing from the
    caller's point of view and is reported the same way.
    """

    def needs_human(problem: str) -> ResolvedBlock:
        return ResolvedBlock(
            block=block, disposition=Disposition.NEEDS_HUMAN_MAPPING, problem=problem
        )

    if block.sheet not in workbook.sheetnames:
        return needs_human(
            f"mapped to sheet {block.sheet!r}, which is not in the workbook ({workbook.sheetnames})"
        )

    if block.metric not in policy.scope.vocabulary:
        return needs_human(
            f"{block.metric!r} is not a metric this system knows. Policy declares "
            f"{sorted(policy.scope.vocabulary)}. A name outside that set is never accepted as a "
            "metric - D-KEY-03."
        )

    if block.metric in policy.scope.metrics_out_of_scope:
        # Recorded rather than parsed, and rather than ignored. D-SCOPE-02: silence would be
        # mistaken for approval.
        return ResolvedBlock(
            block=block,
            disposition=Disposition.OUT_OF_SCOPE,
            out_of_scope_name=block.metric,
        )

    metric = Metric(block.metric)
    worksheet = workbook[block.sheet]
    used = Rect(
        min_col=1,
        min_row=1,
        max_col=max(worksheet.max_column, 1),
        max_row=max(worksheet.max_row, 1),
    )

    try:
        values = _rect(block.value_range, what="value_range")
        periods = _rect(block.period_label_range, what="period_label_range")
        dimension_labels = (
            Rect.parse(block.dimension_label_range) if block.dimension_label_range else None
        )
        dimension_totals = (
            Rect.parse(block.dimension_total_range) if block.dimension_total_range else None
        )
    except RangeError as problem:
        return needs_human(str(problem))

    for name, rect in (
        ("value_range", values),
        ("period_label_range", periods),
        ("dimension_label_range", dimension_labels),
        ("dimension_total_range", dimension_totals),
    ):
        if rect is not None and not used.contains(rect):
            return needs_human(
                f"{name} {rect} extends past the used range of sheet {block.sheet!r} "
                f"({worksheet.dimensions}). A range beyond the data reads empty cells as figures."
            )

    # ── the two orientations, and what each requires of the shapes ───────────
    if block.orientation is Orientation.PERIODS_DOWN_ROWS:
        if not periods.is_single_column:
            return needs_human(
                f"periods run down the rows, so period_label_range must be one column; "
                f"{periods} spans {periods.cols}"
            )
        if periods.rows != values.rows:
            return needs_human(
                f"{values.rows} row(s) of values against {periods.rows} period label(s) "
                f"({values} vs {periods}). Every value must have a period, and an unequal count "
                "means values would be attributed to the wrong one."
            )
        if not values.is_single_column:
            return needs_human(
                f"periods run down the rows, so one metric occupies one column; value_range "
                f"{values} spans {values.cols}. Map each metric column as its own block."
            )
    else:
        if not periods.is_single_row:
            return needs_human(
                f"periods run across the columns, so period_label_range must be one row; "
                f"{periods} spans {periods.rows}"
            )
        if periods.cols != values.cols:
            return needs_human(
                f"{values.cols} column(s) of values against {periods.cols} period label(s) "
                f"({values} vs {periods})."
            )

    # ── the dimension, which must be present exactly when the metric needs one ──
    dimension: Dimension | None = None
    if block.dimension is not None:
        try:
            dimension = Dimension(block.dimension)
        except ValueError:
            return needs_human(
                f"{block.dimension!r} is not a dimension this system knows "
                f"({[d.value for d in Dimension]})"
            )

    if metric.requires_dimension:
        if dimension is None:
            return needs_human(
                f"{metric.value} is a figure *per* something and the mapping names no dimension"
            )
        if dimension_labels is None:
            return needs_human(
                f"{metric.value} needs dimension_label_range - without it there is no way to say "
                "which row is which country"
            )
        if not dimension_labels.is_single_column:
            return needs_human(
                f"dimension_label_range must be one column; {dimension_labels} spans "
                f"{dimension_labels.cols}"
            )
        if dimension_labels.rows != values.rows:
            return needs_human(
                f"{values.rows} row(s) of values against {dimension_labels.rows} dimension "
                f"label(s) ({values} vs {dimension_labels})"
            )
    elif dimension is not None:
        return needs_human(
            f"{metric.value} takes no dimension, but the mapping supplies {dimension}"
        )

    if dimension_totals is not None and dimension is None:
        return needs_human(
            "dimension_total_range was given for a block with no dimension - a total across "
            "nothing is not a total"
        )

    return ResolvedBlock(
        block=block,
        disposition=Disposition.IN_SCOPE,
        metric=metric,
        values=values,
        periods=periods,
        dimension=dimension,
        dimension_labels=dimension_labels,
        dimension_totals=dimension_totals,
    )


def unaccounted_sheets(mapping: WorkbookMapping, workbook: Workbook) -> tuple[str, ...]:
    """Sheets the mapping does not mention at all, in workbook order.

    This is where "an unmapped sheet is never silently skipped" stops being a request made of the
    model and becomes a property of the system. An agent that returns two blocks and an empty
    `unmapped` list for a four-sheet workbook has skipped two sheets; it did not say so, and it does
    not have to, because this does not ask it.
    """
    mentioned = {block.sheet for block in mapping.blocks}
    mentioned |= {block.sheet for block in mapping.unmapped}
    if mapping.cover is not None:
        mentioned.add(mapping.cover.sheet)
    return tuple(name for name in workbook.sheetnames if name not in mentioned)


def uncovered_values(
    workbook: Workbook, resolved: Sequence[ResolvedBlock]
) -> dict[str, tuple[str, ...]]:
    """Cells holding a figure that no mapped range covers, by sheet.

    `unaccounted_sheets` catches a sheet nobody mentioned. This catches the other half of D-XLS-06,
    and it is the half that actually happens: a **header block** missed inside a sheet that *was*
    mapped. The demo workbook has exactly that shape — `Rate & Revenue` carries average daily rate,
    RevPAR and length of stay, and a mapping naming only the first leaves six figures unmentioned on
    a sheet the mapping talks about. Sheet-level accounting reports that workbook as fully handled.

    So coverage is checked at the cell: every cell whose *content reads as a figure* must fall inside
    some mapped range. `tools.looks_numeric` makes that judgement, and it is the same function the
    redactor uses — which means the set of cells the agent was shown as `<value>` is exactly the set
    that must end up covered. A cell it could not see is a cell it must account for.

    Out-of-scope blocks count as coverage. They were mapped; the decision not to verify them is
    D-SCOPE-02's, already taken and recorded. Blocks that need a human mapping do not count, because
    they are already being reported.
    """
    covered: dict[str, list[Rect]] = {}
    for outcome in resolved:
        if outcome.disposition is Disposition.NEEDS_HUMAN_MAPPING:
            continue
        sheet = outcome.block.sheet
        for text in (outcome.block.value_range, outcome.block.dimension_total_range):
            if not text:
                continue
            try:
                covered.setdefault(sheet, []).append(Rect.parse(text))
            except RangeError:  # pragma: no cover - a bad range is already a human mapping
                continue

    uncovered: dict[str, tuple[str, ...]] = {}
    for worksheet in workbook.worksheets:
        rects = covered.get(worksheet.title, [])
        loose: list[str] = []
        for row in worksheet.iter_rows(
            min_row=1, max_row=worksheet.max_row, min_col=1, max_col=worksheet.max_column
        ):
            for cell in row:
                if cell.value is None:
                    continue
                if isinstance(cell.value, str) and not looks_numeric(cell.value):
                    continue  # a label, not a figure
                here = Rect(
                    min_col=cell.column, min_row=cell.row, max_col=cell.column, max_row=cell.row
                )
                if not any(rect.contains(here) for rect in rects):
                    loose.append(str(cell.coordinate))
        if loose:
            uncovered[worksheet.title] = tuple(loose)
    return uncovered
