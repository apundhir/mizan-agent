"""The pieces both screens draw with, so one finding does not render two ways.

Before this module the Run console and the officer's Review screen described the same finding at
wildly different fidelity: the console as a markdown bullet carrying neither the figures nor the
evidence, the review screen as a bordered card with three metrics and the PDF crop beside the
workbook cell. Same finding, same data available to both, two different answers to "what am I
looking at". Everything here is the second answer, factored so the console can give it too.

## Nothing in here reads a file or derives a number

Every function takes what it renders. `finding_card` is handed an `Evidence`; it does not call
`evidence_for`. `summary_metrics` is handed a `VerdictSummary`; it does not count anything. That is
what keeps the modules underneath this one testable without a browser, and it is why the one
opinionated thing this module *does* own - which words and which colour a state gets - can live
here without dragging a policy decision into a render call.

## Colour is asked for by name

`.streamlit/config.toml` carries the palette, in a light and a dark variant. Nothing here writes a
hex. `st.badge` takes a colour *name* and the theme supplies the value, which is the whole reason
dark mode works now: previously every status colour was a literal chosen against a white page, and
there was no second value to reach for when the page was not white.

The one deliberate exception lives in `tda.review.present.chip_colour`, which returns the fill
`tda.outputs.workbook` writes into the annotated spreadsheet. An officer comparing the screen
against that file has to meet one visual language, and that constraint predates this one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

import streamlit as st

from tda.contracts import VerdictStatus
from tda.graph.nodes import NODE_ORDER
from tda.obs.redact import redact
from tda.review.present import cause_line, cell_table, citations, figures, headline, tone

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from tda.contracts import Finding
    from tda.outputs.verdict import VerdictSummary
    from tda.review.evidence import Evidence


# The colour vocabulary `st.badge` accepts, named here so the tables below are checked against
# it rather than against `str`. A palette entry outside the theme's vocabulary is then a type
# error where it is written, not a silent fallback at render time.
BadgeColour = Literal["red", "orange", "yellow", "blue", "green", "violet", "gray", "primary"]

_FALLBACK: Final[BadgeColour] = "gray"


class Stage:
    """A pipeline node as a viewer meets it: what it is called, and what it was for.

    The node's own name is an identifier this codebase argues with itself in - `claim_parse`,
    `recompute_reconcile` - and putting it on a demo screen asks a hotel executive to learn the
    repository's vocabulary before they can read the result. The label is what the stage *did*.
    """

    __slots__ = ("blurb", "label")

    def __init__(self, label: str, blurb: str) -> None:
        self.label = label
        self.blurb = blurb


# In `NODE_ORDER`'s own order, because the rail is a sequence and a reader follows it left to
# right. `claim_parse`'s blurb says out loud where the model is and what it is not allowed to do:
# that sentence is the product's whole argument, and it belongs beside the stage it is true of
# rather than only in a paragraph at the top of the page.
STAGES: Final[dict[str, Stage]] = {
    "intake": Stage(
        "Check the paperwork",
        "Is every month of the declared quarter here, and does the submission match what it says "
        "it is?",
    ),
    "extract": Stage(
        "Read the reports",
        "Every reservation lifted off the PDF pages. The totals printed on the report have to "
        "match what was read, or the run stops here.",
    ),
    "claim_parse": Stage(
        "Find the claimed figures",
        "Which sheet and which cells hold which metric. This is where a language model helps: it "
        "labels the layout, and it never computes a figure.",
    ),
    "recompute_reconcile": Stage(
        "Recompute and compare",
        "Every metric recomputed from the reservation records in plain code, then compared to the "
        "workbook cell by cell.",
    ),
    "publish": Stage(
        "Write the verdict",
        "Findings, the memo and the annotated workbook, each carrying a citation back to the page "
        "and the cell it rests on.",
    ),
}

# The state words a viewer reads, and the colour each asks the theme for. `ok` is deliberately
# "done" rather than "passed": a node finishing says nothing about the verdict, and a rail of five
# green "passed" chips above a FAIL verdict is a screen arguing with itself.
STATE_WORDS: Final[dict[str, str]] = {
    "pending": "waiting",
    "running": "working",
    "ok": "done",
    "skipped": "not reached",
    "halted": "halted",
    "rejected": "refused",
    "failed": "failed",
}
STATE_TONES: Final[dict[str, BadgeColour]] = {
    "pending": "gray",
    "running": "orange",
    "ok": "green",
    "skipped": "gray",
    "halted": "red",
    "rejected": "orange",
    "failed": "red",
}

# What each verdict means, in one sentence, for somebody who has never seen this system. The
# wording rules `tda.review.present` enforces apply here too: ESCALATED is not a milder failure,
# it is a disagreement about a definition, and saying so is the difference between a finding and
# an accusation.
STATUS_TONES: Final[dict[str, BadgeColour]] = {
    VerdictStatus.PASS.value: "green",
    VerdictStatus.FAIL.value: "red",
    VerdictStatus.ESCALATED.value: "orange",
    VerdictStatus.REJECTED.value: "orange",
    VerdictStatus.HALTED.value: "red",
}
STATUS_LINES: Final[dict[str, str]] = {
    VerdictStatus.PASS.value: (
        "Every figure the workbook states was reproduced from the reservation records."
    ),
    VerdictStatus.FAIL.value: (
        "At least one figure does not match the records behind it. Each one below names the cell "
        "it was read from and the rows it should have come from."
    ),
    VerdictStatus.ESCALATED.value: (
        "Nothing here is a hotel error. At least one figure is reproduced exactly by a different "
        "reading of the definitions, which is a question for the policy owner rather than for the "
        "property."
    ),
    VerdictStatus.REJECTED.value: (
        "The submission was refused before any figure was checked, because what arrived was not "
        "what was declared."
    ),
    VerdictStatus.HALTED.value: (
        "The run stopped rather than guess. Something in the submission could not be resolved "
        "against what this system has been taught, and a wrong answer would have been worse than "
        "no answer."
    ),
}


def status_banner(status: str, *, hotel_id: str, period: str) -> None:
    """The verdict, given the weight it earns.

    It used to render as bold body text in the middle of a page whose reds and greens were all
    spent on stage chips above it, which meant the single most important output of the whole system
    was the least visible thing on the screen.
    """
    with st.container(border=True):
        st.badge(status, color=STATUS_TONES.get(status, _FALLBACK), width="content")
        st.markdown(f"#### {redact(hotel_id)[0]} · {period}")
        line = STATUS_LINES.get(status)
        if line:
            st.write(line)


def summary_metrics(summary: VerdictSummary, *, claims_checked: int) -> None:
    """The counts, with the one caption that stops them being read wrongly.

    `definitional_items` sits beside `hotel_errors` and a reader who adds them together has the
    wrong number, which is D-MAT-06's whole point. The metric labels carry that distinction and the
    caption says it in words when there is anything to say it about.
    """
    columns = st.columns(5)
    columns[0].metric("Claims checked", claims_checked, border=True)
    columns[1].metric("Hotel errors", summary.hotel_errors, border=True)
    columns[2].metric("Definitional items", summary.definitional_items, border=True)
    columns[3].metric("Not verifiable", summary.not_verifiable, border=True)
    columns[4].metric("Undecided", summary.undecided_findings, border=True)
    notes = []
    if summary.definitional_items:
        notes.append(
            f"{summary.definitional_items} definitional item(s) are policy disagreements, not "
            "hotel errors, and are counted apart from them on purpose."
        )
    if summary.undecided_findings:
        notes.append(
            f"{summary.undecided_findings} finding(s) have no recorded human decision. This "
            "verdict is not final until a named reviewer accepts, rejects or amends each one."
        )
    for note in notes:
        st.caption(note)


def what_happened(
    *, records: int, pages: int, files: int, totals_matched: bool, claims: int
) -> str:
    """One sentence a hotel executive can read without knowing anything about this system.

    Built from `ExtractionSummary`, which already carries every number in it. `printed_total_matched`
    is the clause worth keeping: it is the check that decides whether anything below it is worth
    reading at all.
    """
    reconciled = (
        "and the totals printed on those reports matched what was read"
        if totals_matched
        else "but the totals printed on those reports did not match what was read"
    )
    return (
        f"{records:,} reservation rows were read from {pages} pages across {files} report(s), "
        f"{reconciled}. {claims:,} claimed figures were then recomputed from those rows."
    )


def stage_rail(states: Mapping[str, str], *, blurbs: bool = True) -> None:
    """The five stages, in order, as a viewer watches them go.

    Replaces five hand-written HTML divs that hard-coded `color:#111` on pale fills, which is the
    single reason the previous screen was unreadable on a dark background. Nothing here writes a
    colour; `st.badge` asks the theme for one by name.
    """
    columns = st.columns(len(NODE_ORDER))
    for column, node in zip(columns, NODE_ORDER, strict=True):
        state = states.get(node, "pending")
        stage = STAGES[node]
        with column, st.container(border=True):
            st.badge(
                STATE_WORDS.get(state, state),
                color=STATE_TONES.get(state, _FALLBACK),
                width="content",
            )
            st.markdown(f"**{stage.label}**")
            if blurbs:
                st.caption(stage.blurb)


# `present.tone` is headless and returns a plain string, because that module may not import
# Streamlit. This is where that string re-enters a checked vocabulary.
_SEVERITY_TONES: Final[dict[str, BadgeColour]] = {
    "orange": "orange",
    "red": "red",
    "blue": "blue",
    "gray": "gray",
}


def severity_badge(finding: Finding) -> None:
    """The severity, coloured by who is at fault rather than by how bad the number is.

    A definitional item takes amber whatever its severity says, because the answer to "who got this
    wrong" is nobody. `tda.review.present.tone` owns that judgement; this only draws it.
    """
    st.badge(
        finding.severity.value, color=_SEVERITY_TONES.get(tone(finding), _FALLBACK), width="content"
    )


def evidence_pair(finding: Finding, evidence: Evidence) -> None:
    """Both sides of the disagreement, side by side. The whole point of the screen.

    The report pane is wider than the workbook pane because the evidence it carries is a landscape
    page of small print and the other is a handful of cells. Equal columns look tidier and make the
    half that actually needs reading the half nobody can read.

    `Evidence.missing` is drawn out loud beneath both panes. A side that could not be shown leaves
    its caption empty and puts the reason there, so without this the pane falls back to the citation
    and a reviewer meets a correct caption over a blank box - including in the one case this whole
    module exists to make impossible, where the file on disk is not the file the run hashed.
    """
    source_citation, cell_citation = citations(finding)
    source, claim = st.columns([3, 2])

    source.markdown("**What the records say**")
    if evidence.source_png is not None:
        source.image(evidence.source_png, caption=source_citation)
    else:
        source.info(evidence.source_caption or source_citation)

    claim.markdown("**What the hotel claimed**")
    if evidence.cell is not None:
        claim.markdown(cell_table(evidence.cell), unsafe_allow_html=True)
        claim.caption(cell_citation)
    else:
        claim.info(evidence.cell_caption or cell_citation)

    for problem in evidence.missing:
        st.warning(problem)


def finding_card(
    finding: Finding,
    evidence: Evidence | None = None,
    *,
    decide: Callable[[], None] | None = None,
    standing: str | None = None,
) -> None:
    """One finding, entire: what is in dispute, the two figures and their difference, and the
    evidence for each side.

    `decide` is how the Review screen injects its accept/reject/amend flow without this module
    knowing anything about recording a decision. The console passes nothing and gets the same card,
    read-only, which is the point: a viewer on the Run page and an officer on the Review page are
    looking at the same finding and should see the same thing.
    """
    with st.container(border=True):
        left, right = st.columns([4, 1])
        left.markdown(f"**{redact(headline(finding))[0]}**")
        with right:
            severity_badge(finding)
        st.caption(redact(cause_line(finding))[0])

        for column, (label, value) in zip(st.columns(3), figures(finding), strict=True):
            column.metric(label, value, border=True)

        if evidence is not None:
            evidence_pair(finding, evidence)
        if finding.narrative:
            st.caption(redact(finding.narrative)[0])
        if standing is not None:
            st.success(standing, icon=":material/check:")
        if decide is not None:
            decide()


def downloads(run_dir: Path, *, key_prefix: str) -> None:
    """The three artefacts a hotel actually receives, offered where the verdict is read.

    The pipeline has written all three since M5 and no screen has ever offered them. A demo that
    shows a verdict and cannot hand over the memo is describing a product rather than being one.
    Each is skipped silently when absent, because a rejected submission never reaches `publish` and
    a missing memo there is correct rather than broken.
    """
    wanted = [
        ("verdict.json", "The verdict, with every finding and citation", "application/json"),
        (
            "memo.docx",
            "The one-page memo",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    ]
    annotated = sorted(run_dir.glob("annotated_*.xlsx"))
    if annotated:
        wanted.append(
            (
                annotated[0].name,
                "The hotel's own workbook, marked",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )

    offered = [(name, label, mime) for name, label, mime in wanted if (run_dir / name).is_file()]
    if not offered:
        return
    with st.container(border=True):
        st.markdown("**Take it away**")
        st.caption("The same files a hotel receives, written by this run.")
        for column, (name, label, mime) in zip(st.columns(len(offered)), offered, strict=True):
            column.download_button(
                label,
                data=(run_dir / name).read_bytes(),
                file_name=name,
                mime=mime,
                key=f"{key_prefix}-download-{name}",
                width="stretch",
            )
