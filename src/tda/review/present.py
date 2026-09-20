"""How a finding reads on the screen — the part that has opinions about words, not about widgets.

Separated from `tda.review.app` so it can be tested without driving a browser. Streamlit is a
rendering detail; **what a finding says to the officer deciding on it is not**, and the wording
below carries two rules that the rest of this repository has been enforcing since M1:

**A definitional item is never described as an error.** D-MAT-06 keeps V2 out of the hotel error
count in the verdict, out of the findings table in the memo and in its own section here. A screen
that used one word for both would undo all three in the place a human actually reads.

**A number is never shown without what it is a number of.** `1,295` under a heading is a fact about
nothing. Every figure here arrives with its metric key, its period and the side it came from.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Final

from tda.contracts import ExcelRef, NotReached, Severity, VarianceClass
from tda.review.assist import Unavailable

if TYPE_CHECKING:
    from tda.contracts import Finding, ReviewRecord
    from tda.review.assist import Answer, NoAnswer
    from tda.review.evidence import CellView

# Severity as a word plus a colour a reviewer can scan, and the colours are the workbook's so the
# two artefacts do not describe one finding in two visual languages.
SEVERITY_COLOURS: Final[dict[str, str]] = {
    Severity.BLOCKING.value: "#BDD7EE",
    Severity.MATERIAL.value: "#FFC7CE",
    Severity.INFORMATIONAL.value: "#D9D9D9",
}
DEFINITIONAL_COLOUR: Final = "#FFE699"

DECISION_WORDS: Final[dict[str, str]] = {
    "accept": "Accepted — the finding stands",
    "reject": "Rejected — the finding does not stand",
    "amend": "Amended — a corrected figure was recorded",
}


def headline(finding: Finding) -> str:
    """One line identifying what is in dispute. What the reviewer reads first."""
    return f"{finding.finding_id} · {finding.key.rendered}"


def chip_colour(finding: Finding) -> str:
    """The colour for this finding's badge.

    A definitional item takes the amber of the annotated workbook rather than the red its
    `material` severity would otherwise give it. The severity is what policy assigned; the colour
    is what the reviewer reads as *who is at fault*, and the answer for a V2 is nobody.
    """
    if finding.variance_class is VarianceClass.DEFINITIONAL:
        return DEFINITIONAL_COLOUR
    return SEVERITY_COLOURS.get(finding.severity.value, "#EEEEEE")


def tone(finding: Finding) -> str:
    """The same judgement `chip_colour` makes, as a theme colour name rather than a hex.

    `chip_colour` above returns the annotated workbook's own fill for this finding, and must keep
    doing so: an officer reading the spreadsheet beside the screen has to meet one visual language,
    not two. But those fills are Excel conditional-formatting pastels chosen to sit under black
    text on white paper, and a screen that hard-codes them also hard-codes the dark text they need,
    which is how this codebase ended up unreadable in dark mode.

    So the screen asks for a *name* and lets `.streamlit/config.toml` supply the value, in whichever
    direction the viewer's own theme runs. The mapping is the same argument `chip_colour` makes: a
    definitional item is amber because nobody is at fault, not red, and `SEVERITY_COLOURS`' own
    reason for existing (D-MAT-06 keeps a V2 out of the hotel error count) is why.
    """
    if finding.variance_class is VarianceClass.DEFINITIONAL:
        return "orange"
    return {
        Severity.BLOCKING.value: "blue",
        Severity.MATERIAL.value: "red",
        Severity.INFORMATIONAL.value: "gray",
    }.get(finding.severity.value, "gray")


def cause_line(finding: Finding) -> str:
    """The cause, in the words the reviewer needs to act on it.

    A definitional item names the rule that reproduces the claim, and says out loud that it is not
    a hotel error — without that sentence the amber reads as a milder accusation rather than as no
    accusation at all.
    """
    if finding.variance_class is VarianceClass.DEFINITIONAL:
        also = (
            f", and also by {', '.join(finding.also_explained_by)}"
            if finding.also_explained_by
            else ""
        )
        return (
            f"Reproduced exactly by {finding.explaining_permutation}{also}. "
            "A policy difference, not a hotel error (D-MAT-06)."
        )
    if finding.variance_class is VarianceClass.EXTRACTION_LIMIT:
        return f"Could not be checked ({finding.clause}). The run did not verify this figure."
    if finding.variance_class is VarianceClass.COMPLETENESS and finding.claimed is None:
        return f"The workbook states no figure for this ({finding.clause})."
    return f"{finding.variance_class.value} · {finding.clause}"


def figures(finding: Finding) -> tuple[tuple[str, str], ...]:
    """Claimed, computed and the difference, each labelled with the side it came from."""
    return (
        ("Hotel claimed", _number(finding.claimed)),
        ("Records support", _number(finding.computed)),
        ("Difference", _number(finding.difference)),
    )


def citations(finding: Finding) -> tuple[str, str]:
    """What the two evidence panes are showing, as text, for the caption under each."""
    source = finding.source_ref.citation
    cell = (
        finding.excel_ref.citation
        if isinstance(finding.excel_ref, ExcelRef)
        else _absence(finding.excel_ref)
    )
    return source, cell


def answer_citations(result: Answer) -> tuple[str, ...]:
    """An assistant answer's citations as a reader follows them.

    Rendered through each citation's own `citation` property rather than reassembled here, so the
    string under an answer is the same string under the finding it points at. Two renderings of one
    reference is how an officer ends up comparing `p.4 rows 12-18` with `page 4, rows 12 to 18` and
    deciding they are different places.
    """
    return tuple(citation.citation for citation in result.citations)


UNAVAILABLE_WORDS: Final[dict[str, str]] = {
    Unavailable.NOT_ASKED.value: "Nothing was asked.",
    Unavailable.NOT_RECORDED.value: (
        "The assistant has no recorded answer for this question, and this run replays recordings "
        "rather than calling a model. Nothing is wrong with the verdict; the assistant is simply "
        "unavailable here."
    ),
    Unavailable.NOT_CONFIGURED.value: (
        "The assistant is not configured on this machine. Nothing is wrong with the verdict."
    ),
    Unavailable.UNCITED.value: (
        "The assistant produced an answer with nothing behind it, so there is no answer. It cannot "
        "tell you anything it cannot show you."
    ),
    Unavailable.FABRICATED_CITATION.value: (
        "The assistant cited evidence this run does not contain, so its answer has been withheld. "
        "Treat anything else it has told you today with suspicion and check it against the "
        "findings themselves."
    ),
    Unavailable.FAILED.value: (
        "The assistant failed. The verdict and your recorded decisions are unaffected."
    ),
}


def unavailable_line(result: NoAnswer) -> str:
    """What the officer reads instead of an answer.

    Every reason gets its own sentence, and the fabricated-citation one is deliberately the
    strongest: an assistant that invented a reference once has told the officer something about
    every other answer it has given, and a screen that said "something went wrong" would be hiding
    exactly that.
    """
    return UNAVAILABLE_WORDS.get(result.reason.value, UNAVAILABLE_WORDS[Unavailable.FAILED.value])


def decision_summary(record: ReviewRecord, superseded_count: int = 0) -> str:
    """What a decided finding says at the top of its card."""
    words = DECISION_WORDS.get(record.decision.value, record.decision.value)
    amended = f" → {record.amended_value}" if record.amended_value else ""
    note = f" — {record.note}" if record.note else ""
    changed = f"  (superseded {superseded_count} earlier decision(s))" if superseded_count else ""
    return (
        f"{words}{amended} by {record.reviewer} on "
        f"{record.decided_at:%Y-%m-%d %H:%M %Z}{note}{changed}"
    )


def cell_table(view: CellView) -> str:
    """The workbook neighbourhood as an HTML table, with the cited cell outlined.

    HTML rather than a dataframe because the one thing this has to do is make **one** cell
    unmistakable, and a grid where the reviewer has to find `B5` by reading the axis labels is a
    grid that costs the seconds the whole screen is budgeted in.
    """
    header = "".join(f"<th style='{_TH}'>{escape(column)}</th>" for column in view.columns)
    body = []
    for row in view.rows:
        cells = []
        for column in view.columns:
            target = f"{column}{row}" == view.target
            style = _TD_TARGET if target else _TD
            cells.append(f"<td style='{style}'>{escape(view.value_at(column, row))}</td>")
        body.append(f"<tr><th style='{_TH}'>{row}</th>{''.join(cells)}</tr>")
    return (
        f"<table style='{_TABLE}'><tr><th style='{_TH}'></th>{header}</tr>{''.join(body)}</table>"
    )


# Theme-neutral on purpose. These used to pin dark text on a white fill, which meant the one grid
# a reviewer has to read was unreadable on a dark background - the screen's own evidence, lost to a
# hard-coded `#fff`. `color:inherit` and translucent greys take whatever the active theme provides,
# so the grid follows `.streamlit/config.toml` in both directions. The target cell keeps a real red,
# because "this is the cell in dispute" is the one thing here that must not be subtle, and it is
# stated by a border and a wash rather than by a text colour that a dark theme would fight.
_TABLE: Final = "border-collapse:collapse;font-size:13px;font-family:inherit;"
_TH: Final = (
    "border:1px solid rgba(128,128,128,0.35);background:rgba(128,128,128,0.12);color:inherit;"
    "padding:4px 8px;font-weight:600;text-align:center;"
)
_TD: Final = (
    "border:1px solid rgba(128,128,128,0.35);padding:4px 8px;color:inherit;background:transparent;"
)
_TD_TARGET: Final = (
    "border:2px solid #c53030;padding:4px 8px;color:inherit;background:rgba(197,48,48,0.14);"
    "font-weight:700;"
)


def _number(value: object) -> str:
    """An em dash rather than `None`, as in the memo: a blank numeric field reads as a defect in
    the tool, and a dash reads as the absence it is."""
    return "—" if value is None else f"{value}"


def _absence(reference: NotReached) -> str:
    return f"no cell — {reference.reason}"
