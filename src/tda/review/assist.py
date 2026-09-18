"""Putting an officer's question to the reviewer-assist agent, headlessly.

`tda.review.decisions` is the model for this module and the reason it exists: the screen is a shell,
and everything that can be wrong lives somewhere a test can reach without a browser. A question box
wired straight into `st.text_input` would put the fabrication check, the trace append and the
"no cassette" path behind Streamlit, which is to say behind nothing that runs in CI.

## An answer is appended to the run's trace, not held in the session

PRD-93 asks that every answer be recorded with its question, its citations and its tokens. The
`TraceRecord` the runtime already writes carries all three — `output_json` is the whole
`CitedAnswer`, question and citations included — so recording it means appending that record to the
run's own `trace.jsonl`, beside the calls that produced the verdict.

Appending rather than rewriting, and appending **redacted**, through the same `redact` the run's
own artifacts go through. An officer's question is free text typed by a human, which makes it the
only path by which such text enters an artifact after the run has finished.

Be exact about what that buys, because the tempting sentence is wider than the truth: `redact`
matches **structure** — an email address, a phone number, a booking reference — and says in its own
docstring that it does not catch a bare name. A guest's name typed into this box reaches
`trace.jsonl` verbatim, and `tests/unit/test_reviewer_assist.py` asserts that it does, so the gap
is recorded rather than implied away.

The consequence worth stating: **a question changes the trace**, so a run reviewed twice has a
longer trace than a run reviewed once. That is correct — the questions asked about a verdict are
part of how it came to be signed. A reviewer's curiosity is evidence, not noise.

## Why a failure is a value rather than an exception

Four things can stop an answer reaching the screen, and an officer needs to be told which:

| | What the officer should conclude |
|---|---|
| no cassette for this question | the tool has not been recorded for this; ask a human |
| no API key, live mode | the assistant is not configured here |
| an answer with no citation | it had nothing to show; there is no answer here |
| a fabricated citation | **the assistant produced a reference that does not exist** — distrust it |
| any other failure | it broke; the verdict is unaffected |

The last two must never be rendered as a warning beside an answer the officer can still read. An
uncited answer never gets that far — the contract rejects it in the provider, which is the whole
point of `Answered.citations` having `min_length=1` — and a citation that does not exist is not a
caveat but the assistant failing at the one job the citations exist to do. Either way the prose is
discarded rather than shown with a note under it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from tda.agents.provider.base import CassetteMissError, OutputValidationError, ProviderError
from tda.agents.reviewer_assist import AssistError, ask, build_registry, load_clauses
from tda.agents.runtime import AgentRunner
from tda.obs.artifacts import AGENT_TRACE
from tda.obs.redact import redact
from tda.obs.trace import TraceLog

if TYPE_CHECKING:
    from pathlib import Path

    from tda.agents.contracts.reviewer_assist import Citation, CitedAnswer
    from tda.agents.provider.base import LLMProvider
    from tda.contracts import Verdict
    from tda.obs.trace import TraceRecord
    from tda.obs.usage import UsageLedger
    from tda.policy import Policy

# The longest question the box accepts. Not a model limit - a question longer than this is a
# document, and a document pasted into a question box is how a guest list enters a trace.
MAX_QUESTION = 1_000


class Unavailable(StrEnum):
    """Why there is no answer. Each renders as a different sentence on the screen.

    A single "the assistant failed" would collapse *not recorded* into *not trustworthy*, and those
    call for opposite actions from the officer.
    """

    NOT_ASKED = "not_asked"
    NOT_RECORDED = "not_recorded"
    NOT_CONFIGURED = "not_configured"
    UNCITED = "uncited"
    FABRICATED_CITATION = "fabricated_citation"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Answer:
    """One question, its answer, and the record of the call that produced it."""

    question: str
    answer: CitedAnswer
    trace: TraceRecord

    def __bool__(self) -> bool:
        """Falsy when the agent declined, so `if result:` reads correctly - the same idiom as
        `AgentResult`, and safe for the same reason: this is a wrapper this repository owns, and it
        never crosses a library boundary. The *contract* it holds answers `is_answer` instead; see
        `AgentOutput.is_answer` for the bug that distinction exists to prevent."""
        return self.answer.is_answer

    @property
    def text(self) -> str:
        return self.answer.text

    @property
    def citations(self) -> tuple[Citation, ...]:
        return self.answer.citations

    @property
    def cost_line(self) -> str:
        """What this question cost, for the caption under the answer. An assistant whose price is
        invisible is an assistant nobody budgets for."""
        return (
            f"{self.trace.input_tokens} in / {self.trace.output_tokens} out tokens · "
            f"{self.trace.duration_ms} ms · {self.trace.provider_mode}"
        )


@dataclass(frozen=True, slots=True)
class NoAnswer:
    """Why the officer is seeing no answer, in terms they can act on."""

    question: str
    reason: Unavailable
    detail: str

    def __bool__(self) -> bool:
        return False

    @property
    def text(self) -> str:
        return self.detail


type AskResult = Answer | NoAnswer


class QuestionError(ValueError):
    """A question that will not be put to the agent at all."""


def check_question(question: str) -> str:
    """The question as it will be asked, or a refusal to ask it.

    Stripped of surrounding whitespace and nothing else. In particular it is **not** rephrased,
    truncated to a summary or "cleaned up": the trace records the question the officer asked, and a
    question rewritten on the way in is one they cannot recognise when they read it back.
    """
    text = question.strip()
    if not text:
        raise QuestionError("ask a question first.")
    if len(text) > MAX_QUESTION:
        raise QuestionError(
            f"that question is {len(text)} characters; the limit is {MAX_QUESTION}. A question "
            "longer than a paragraph is usually a document, and a document pasted into this box "
            "ends up in the run's trace."
        )
    return text


def append_to_trace(run: Path, record: TraceRecord) -> None:
    """Append one record to the run's trace, redacted, creating the file if the run has none.

    Redacted for the reason the module docstring gives, and with the limit it states: `redact`
    catches structure, not names. The same function `tda.obs.artifacts.write_run` applies, rather
    than a second implementation that would eventually disagree with it about what a leak looks
    like.

    Note what this does **not** cover: the question is sent to the provider unredacted, because the
    agent has to read it. Under `make record` it therefore reaches the cassette's
    `request_canonical` as typed - which is one more reason `tests/cassettes/README.md` asks for a
    cassette diff to be read rather than merged.
    """
    line, _redaction = redact(record.model_dump_json())
    path = run / AGENT_TRACE
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def put_question(
    question: str,
    verdict: Verdict,
    run: Path,
    provider: LLMProvider,
    policy: Policy,
    *,
    record: bool = True,
    usage: UsageLedger | None = None,
) -> AskResult:
    """Ask the agent, check what comes back, and write the call into the run's trace.

    Every failure becomes a value rather than an exception - an empty box included - because the
    caller is a screen, and an officer meeting a traceback learns nothing about whether to trust
    the verdict in front of them.
    The one distinction the table in the module docstring insists on is preserved here: a
    fabricated citation is its own reason, and the answer that carried it is discarded rather than
    shown with a caveat.

    `record=False` exists for the eval harness and for tests that have no run directory. It is not
    a way to ask a question off the record on a real run — the screen never passes it — and the
    default is therefore the recording one.

    `usage` is optional and the screen passes one, so a question's tokens land in the same ledger
    `make run` prints from. A question that costs money and appears in no total is a question
    nobody budgets for.
    """
    try:
        asked = check_question(question)
    except QuestionError as exc:
        return NoAnswer(question=question.strip(), reason=Unavailable.NOT_ASKED, detail=str(exc))

    trace = TraceLog()

    try:
        # Inside the try, and that is the fix for a real defect rather than tidiness: `load_clauses`
        # reads `docs/01-definitions.md`, which does not exist beside an installed wheel, and an
        # `AssistError` escaping from here reached the officer as a Streamlit traceback - from a
        # function whose whole contract is that it never raises.
        clauses = load_clauses()
        runner = AgentRunner(
            provider,
            policy=policy,
            registry=build_registry(verdict, clauses),
            trace=trace,
            usage=usage,
        )
        result = ask(asked, verdict, runner, policy, clauses=clauses)
    except AssistError as exc:
        _record_attempt(run, trace, record=record)
        return NoAnswer(question=asked, reason=Unavailable.FABRICATED_CITATION, detail=str(exc))
    except CassetteMissError as exc:
        _record_attempt(run, trace, record=record)
        return NoAnswer(question=asked, reason=Unavailable.NOT_RECORDED, detail=str(exc))
    except OutputValidationError as exc:
        # The headline guarantee firing. `Answered.citations` has `min_length=1`, so prose with
        # nothing behind it fails validation in the provider and never becomes an answer at all.
        # Caught before `ProviderError` because it is the one failure the officer must not read as
        # "the assistant is not set up here".
        _record_attempt(run, trace, record=record)
        return NoAnswer(question=asked, reason=Unavailable.UNCITED, detail=str(exc))
    except ProviderError as exc:
        _record_attempt(run, trace, record=record)
        return NoAnswer(question=asked, reason=Unavailable.NOT_CONFIGURED, detail=str(exc))
    # Broad on purpose, and last. Anything else is a defect, and the officer needs the screen to
    # stay up and say so rather than to disappear behind a traceback.
    except Exception as exc:
        _record_attempt(run, trace, record=record)
        return NoAnswer(
            question=asked, reason=Unavailable.FAILED, detail=f"{type(exc).__name__}: {exc}"
        )

    if record:
        append_to_trace(run, result.trace)
    return Answer(question=asked, answer=result.output, trace=result.trace)


def _record_attempt(run: Path, trace: TraceLog, *, record: bool) -> None:
    """Write the failed call's records too.

    A trace that carries only the questions that were answered cannot show that the assistant was
    asked something it could not do - which is the half an auditor reading a signed verdict most
    wants. `AgentRunner` has already written a record with the error in it; this puts it on disk.
    """
    if not record:
        return
    for written in trace:
        append_to_trace(run, written)
