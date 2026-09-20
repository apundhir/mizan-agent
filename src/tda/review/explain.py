"""The page that explains this system to somebody with nobody standing next to them.

The Run console shows a verification happening and the Review screen shows an officer deciding
about it. Both assume a viewer who already knows what the system is for. This page is the one that
does not: it is written for a stranger who opened a link, and it says what the product does, where
the models sit, what they are forbidden to touch, and what the demonstration is not entitled to
claim.

## It renders copy and the roster, and reads nothing else

`ui.STAGES` owns the five stage labels and blurbs, and `tda.agents.roster` owns each agent's job
and tool allowlist. Both are read here rather than retyped, because a second copy of either is a
copy that goes stale quietly: an allowlist widened in the roster and still narrow on this page
would be this page telling a prospect something that stopped being true.

## The disclosure paragraphs are the console's own, word for word

The two captions at the foot of this page are the ones `tda.review.console.render` shows above
every run. They exist to satisfy a responsible-AI gate, and a paraphrase is not the thing that was
reviewed, so they are quoted rather than rewritten.
"""

from __future__ import annotations

from typing import Final

import streamlit as st

from tda.agents.roster import CRITIC, MAPPING, NARRATIVE, RESOLUTION, REVIEWER_ASSIST, ROSTER
from tda.review import ui

# The roster's keys are identifiers this codebase argues with itself in. A page written for a
# hotel executive spells them the way the architecture document does.
AGENT_NAMES: Final[dict[str, str]] = {
    MAPPING: "Mapping",
    RESOLUTION: "Resolution",
    NARRATIVE: "Narrative",
    REVIEWER_ASSIST: "Reviewer-assist",
    CRITIC: "Critic",
}

# Where each agent is reached from in this application today, which is a narrower statement than
# the roster's `runs_in` and is deliberately the one shown. `runs_in` records the node an agent
# belongs to by design; these sentences record what actually calls it.
AGENT_TODAY: Final[dict[str, str]] = {
    MAPPING: "Called once in every verification run, at the stage that reads the workbook.",
    RESOLUTION: (
        "Built, tested and cassette-backed, exercised by the fixture evals. A verification run "
        "does not call it: a country label the committed lookup cannot place halts the run under "
        "clause D-NAT-12 instead of being resolved by a model."
    ),
    NARRATIVE: (
        "Runs when the Run console's Grade the prose button is pressed on a finished run, and "
        "under the fixture evals."
    ),
    REVIEWER_ASSIST: "Runs on the Review screen, when an officer types a question about a run.",
    CRITIC: (
        "Grades what the narrative agent wrote, on the same button and in the same evals. It has "
        "no tools at all, so it cannot go looking for evidence that would make an ungrounded "
        "sentence read as a grounded one."
    ),
}


def render() -> None:
    """The whole How it works page. Takes nothing, reads nothing, draws copy."""
    st.title("How it works")
    _what_this_does()
    _stages()
    _the_models()
    _replay()
    _limits()
    _disclosures()


def _what_this_does() -> None:
    st.write(
        "A hotel files a quarterly return: occupancy, room-nights, guests by nationality, typed "
        "into an Excel workbook. Somebody then has to decide whether those figures are right. "
        "Done by hand, that means opening the workbook beside the property's own reports and "
        "checking a sample, because checking all of it is not affordable."
    )
    st.write(
        "The property management system that produced those reservations can export them as PDF "
        "reports. Mizan reads both sides. Every figure the workbook states is recomputed from the "
        "reservation rows in plain code, then compared to the workbook cell by cell."
    )
    st.write(
        "What comes back is not a score. It is each disagreement with the evidence for both "
        "sides: the report page and rows the recomputed figure was built from, and the cell the "
        "claimed figure was read from. A submission that cannot be read produces a finding naming "
        "what could not be read, and the run stops there rather than filling the gap with a guess."
    )


def _stages() -> None:
    st.markdown("### What happens to a submission")
    st.caption("Five stages, in the order they run. A stage that cannot finish stops the run.")
    for number, stage in enumerate(ui.STAGES.values(), start=1):
        with st.container(border=True):
            st.markdown(f"**{number}. {stage.label}**")
            st.caption(stage.blurb)


def _the_models() -> None:
    st.markdown("### Where the models are, and what they may not do")
    st.write(
        "Language models are used where the problem is linguistic: which sheet holds which "
        "metric, what a country label means, how to word a finding. They are kept out of the "
        "arithmetic entirely. Six agents do that work, each with a versioned prompt, a typed "
        "output contract, a tool allowlist, an eval set and a trace record. An agent missing any "
        "of those does not type-check."
    )

    with st.container(border=True):
        st.markdown("**The rule every agent is held to**")
        st.markdown(
            "- An agent returns keys and references, never numbers: a cell range, a country "
            "code, a finding id, a permutation id.\n"
            "- The only numeric fields an agent's output contract may declare are `page`, `row`, "
            "`row_start` and `row_end`. Those are citations rather than quantities, and nothing "
            "downstream does arithmetic on a page number.\n"
            "- A tool call outside an agent's allowlist raises. It never comes back as an empty "
            "result, which would turn a refused call into a retry that eventually succeeds with "
            "nothing on the record.\n"
            "- None of this rests on convention. An import guard refuses a model import or file "
            "access inside the packages that compute and reconcile the figures, and an agent "
            "schema lint refuses any numeric field beyond those four. Both run in CI, so crossing "
            "the boundary fails the build rather than the review."
        )

    with st.container(border=True):
        st.markdown("**Supervisor**")
        st.caption(
            "Code. No prompt, no model, no tools. It decides which agent runs where, holds the "
            "per-run call budget set in policy.yaml, and records every decision it takes, skips "
            "included, so a reviewer can ask later why an agent did not run. It is absent from "
            "the roster table on purpose: there is nothing to write down for it."
        )

    for name, entry in ROSTER.items():
        with st.container(border=True):
            st.markdown(f"**{AGENT_NAMES.get(name, name)}**")
            st.caption(entry.job)
            tools = ", ".join(f"`{tool}`" for tool in sorted(entry.tools))
            st.caption(f"May call: {tools}" if tools else "May call: nothing. No tools, by design.")
            st.caption(AGENT_TODAY[name])


def _replay() -> None:
    st.markdown("### What you are looking at is a recording, unless it says otherwise")
    st.write(
        "Every model call on this deployment replays a committed cassette by default. A cassette "
        "is one real answer from a real model, recorded once and stored in the repository under a "
        "key derived from the prompt version, the exact messages sent, the output schema and the "
        "model id. Change any of those and the key changes with them."
    )
    st.write(
        "So there is no API call behind what you are watching, which is why this demonstration is "
        "free to run and why two runs of the same submission agree. A missing cassette is a hard "
        "error that names what was asked for. It never falls through to the network quietly, "
        "because a replay that reaches the API on a miss would look reproducible while no longer "
        "being either reproducible or free."
    )
    st.write(
        "Live calls are refused unless whoever operates this deployment turns live mode on and "
        "supplies a credential, and a request for a live provider that is not available is "
        "refused with a reason rather than downgraded to replay behind your back. The Run page's "
        "sidebar states which of the two is in force, and each run's own trace records the "
        "provider it used."
    )


def _limits() -> None:
    st.markdown("### What this does not prove")
    st.write(
        "The fixture scorecard in this repository reads: 6 passed, 0 failed, 0 not recorded, 0 "
        "errored, with 10 of 10 planted material errors found and 0 false positives on the "
        "control fixture. Here is what that result is not entitled to claim."
    )
    st.markdown(
        "- **This project authored both sides of the corpus.** The PDF reports and the workbook "
        "come from one ledger this repository generated. Extraction working here proves the "
        "mechanics work end to end against a known-correct answer. It does not prove the same "
        "code survives a real export from a real property management system, with that system's "
        "own column drift, encoding quirks and inconsistent labelling.\n"
        "- **Whether a real export even carries reservation-level rows is unresolved.** If it "
        "arrives as a pre-aggregated monthly summary, recomputation is not possible at all and "
        "the approach narrows to matching one summary against another. This is the largest open "
        "question in the design.\n"
        "- **The refusal case is one planted example, not a sweep.** It shows that an "
        "unrecognised label halts the run before a model is consulted. It says nothing about how "
        "many distinct label variants or malformed rows a real export could throw at that same "
        "code path.\n"
        "- **The definitions are this project's own, not a regulator's.** Eight assumptions are "
        "written down in the assumption register with the value chosen, why, and what moves if a "
        "regulator rules otherwise. Nothing here should be read as any of them having been "
        "ratified.\n"
        "- **No time-saving figure appears anywhere in this project**, because the manual "
        "baseline this system would replace has never been measured. A number quoted before it is "
        "measured invites a challenge that lands on the whole result rather than on the number."
    )


def _disclosures() -> None:
    st.markdown("### Disclosures")
    st.caption(
        "The Run page carries these above every run. The first applies to a deployment that "
        "accepts uploads and the second to one that does not, so a viewer reads whichever is true "
        "of the link they were sent. Both are quoted here word for word."
    )
    st.caption(
        "Synthetic demonstration data only. Do not upload real guest or property records - "
        "uploaded files are written to this server's disk and stay there until the app next "
        "restarts, where whoever operates this deployment can read them. Other viewers of this "
        "link are shown only the runs they started themselves."
    )
    st.caption(
        "Synthetic demonstration data only. Every scene below runs against this project's "
        "own committed corpus, generated from a ledger this repository authored. No real hotel "
        "or guest data is involved, and this deployment accepts no uploads."
    )
    st.caption(
        "The arithmetic below is deterministic code, not a model. Agents map a workbook's "
        "layout, resolve an ambiguous label, and write the prose beside a finding - they never "
        "compute a figure, and no finding is final until a named reviewer accepts, rejects or "
        "amends it on the Review page."
    )
