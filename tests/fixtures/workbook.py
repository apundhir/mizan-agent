"""The known-good mapping for the committed demo workbook.

What the mapping agent is expected to return for `corpus/demo/submission/claims_2026-Q1.xlsx`,
written by hand and committed. It is a **test fixture, not evidence about the agent** — the agent's
own answers are scored by `tests/eval/agents/`, and using this as a stand-in there would be marking
its homework against itself.

Its job here is narrower and worth having: it lets the claim parser, the reconciliation engine and
the whole orchestrated run be exercised end to end without a model in the loop, so a failure in any
of them is unambiguously theirs.
"""

from __future__ import annotations

from tda.excel import CoverSheet, MetricBlock, Orientation, WorkbookMapping


def demo_mapping() -> WorkbookMapping:
    return WorkbookMapping(
        cover=CoverSheet(sheet="Summary", property_code_cell="B5", period_cell="B6"),
        blocks=(
            *[
                MetricBlock(
                    sheet="Occupancy",
                    metric=metric,
                    orientation=Orientation.PERIODS_DOWN_ROWS,
                    value_range=column,
                    period_label_range="A5:A8",
                )
                for metric, column in (
                    ("room_nights_sold", "B5:B8"),
                    ("room_nights_available", "C5:C8"),
                    ("occupancy_pct", "D5:D8"),
                )
            ],
            MetricBlock(
                sheet="Nationality",
                metric="guests_by_nationality",
                orientation=Orientation.PERIODS_ACROSS_COLUMNS,
                value_range="B5:E25",
                period_label_range="B4:E4",
                dimension="nationality_iso2",
                dimension_label_range="A5:A25",
                dimension_total_range="B26:E26",
            ),
            *[
                MetricBlock(
                    sheet="Rate & Revenue",
                    metric=metric,
                    orientation=Orientation.PERIODS_ACROSS_COLUMNS,
                    value_range=row,
                    period_label_range="B4:D4",
                )
                for metric, row in (
                    ("average_daily_rate", "B5:D5"),
                    ("revpar", "B6:D6"),
                    ("average_length_of_stay", "B7:D7"),
                )
            ],
        ),
    )
