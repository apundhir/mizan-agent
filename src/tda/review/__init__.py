"""The verification officer's screen, and the gate it records decisions into.

`make review` opens `tda.review.app`, a Streamlit screen over one run's `verdict.json`. Everything
it can do is available without it:

| Module | What it owns |
|---|---|
| `evidence` | the PDF rows and the workbook cell a finding cites, as a picture and a grid |
| `decisions` | recording accept / reject / amend into the verdict, and re-issuing the memo |
| `present` | how a finding reads — the words, not the widgets |
| `app` | the Streamlit shell, which is a thin one on purpose |

## Why the shell is thin

the review screen names a console flow as the fallback if the sprint tightens, and requires it to *"record the
identical decision structure so the verdict schema does not change with the fallback"*. That
guarantee costs nothing when there is one recorder and is unenforceable when there are two — so
`decisions` is headless, takes paths and strings, and is what the tests drive. The screen is what a
human drives.

It also means the review gate is testable without a browser, which is the difference between a gate
that is asserted and one that is hoped for.
"""

from tda.review.decisions import Recorded, ReviewError, record_decision, superseded, undecided
from tda.review.evidence import CellView, Evidence, cell_region, evidence_for, page_region
from tda.review.present import cause_line, cell_table, chip_colour, decision_summary, headline

__all__ = [
    "CellView",
    "Evidence",
    "Recorded",
    "ReviewError",
    "cause_line",
    "cell_region",
    "cell_table",
    "chip_colour",
    "decision_summary",
    "evidence_for",
    "headline",
    "page_region",
    "record_decision",
    "superseded",
    "undecided",
]
