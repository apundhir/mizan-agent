"""Per-agent eval: a committed case file, a deterministic scorer, and an honest gap.

An agent is five things, and the fifth is an eval set. Without one, "the mapping agent works" is a
sentence somebody typed after watching it work once.

## What a case is

A JSON file under `cases/<agent>/<name>.json` with three parts:

| Key | What it is |
|---|---|
| `input` | what the agent is given, in whatever shape that agent's driver takes |
| `expect` | the outcome, expressed as **checks** rather than as a literal answer |
| `why` | the sentence saying what this case is defending against |

`expect` holds checks rather than a golden answer, and that is the design decision this file
exists to hold. A golden `FindingNarrative` would compare a model's prose to one particular
sentence, which measures *similarity to a recording*, not quality — and it would fail on every
improvement. So the checks are properties: this resolution must abstain; this one must land on
`CZ`; this narrative must contain no digits and must name the assigned permutation. Properties
survive a better answer and still catch a wrong one.

## The scorer is deterministic and it is not the critic

`score_case` is arithmetic over the checks. Nothing here calls a model. The critic (`tda.agents.
critic`) is a *graded subject* in this harness like any other agent, not the grader of it — a
harness that used a model to score itself would report a number that looks like evidence and is
not.

## What is measured, and how a gap is reported

Running a case against a live model needs a cassette, and a cassette needs `make record`, which
needs an API key. **Every committed case has one**: twenty cassettes across five agents, recorded
and reviewed. So all three of the measurements below are live, and a case that arrives without a
recording is reported as `NOT_RECORDED` rather than as passing:

- `run_case` with a provider serves the case and scores the answer, whatever the provider is.
- `tests/eval/agents/test_agent_evals.py` runs every case against **replay**, and asserts on the
  scores that exist. A case with no cassette is reported, and counted, and not silently skipped.
- The same tests run every case against a **stub** holding the case's own `expect_example`, which
  proves the *scorer* distinguishes a right answer from a wrong one. That part is fully
  deterministic and runs on every push.

A scorer that passes everything is the classic failure of an eval harness, and it is the one thing
that can be proven without a key. So it is proven on every push, and anything unmeasured is marked
absent in words rather than being reported as a pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tda.agents.cases import EvalCase, registry_for, run_case
from tda.agents.cases import load_cases as _load_cases
from tda.agents.contracts import AgentOutput
from tda.agents.provider import CassetteMissError, LLMProvider
from tda.agents.runtime import AgentRunner
from tda.obs import TraceLog

# Re-exported under the name the tests already use. The definition lives in the runtime because
# `make record` needs it too, and two definitions of "an eval case" would drift the moment one
# gained a field: cassettes recorded against one question, replayed against another.
Case = EvalCase

if TYPE_CHECKING:
    from tda.policy import Policy

CASES_ROOT = Path(__file__).resolve().parent / "cases"

# A digit anywhere in prose. Deliberately blunt: the narrative agent is shown no figures, so a
# digit in its sentence came from somewhere it should not have. Periods are the one legitimate
# source of digits and are excluded before this runs.
_DIGIT = re.compile(r"\d")

# A period, in every form an agent legitimately writes one. Stripped before the digit scan, and
# this is a correctness fix rather than a convenience: `prompts/narrative/v1.md` instructs the agent
# to name "the metric and the period **in words**, not as keys", and the scorer then failed it for
# writing `January 2026`. A check that punishes the behaviour its own prompt demands measures
# obedience to the scorer, not quality - and it failed all three narrative cases the first time
# real answers were recorded.
#
# A period is a label, not a quantity, for the same reason `page` is on the citation whitelist:
# nothing downstream does arithmetic on it.
# This system's own identifiers. Every one of them contains a digit and none of them is a
# quantity: a clause is a rule, a permutation is a named alternative reading, a finding id is a
# handle, and a variance class is a category. The agents are *given* these tokens and told to cite
# them, and the leak check then read `D-MAT-06` and `V2` as leaked figures - failing answers for
# doing exactly what the prompt asks.
#
# The rule is the same one the citation whitelist applies to `page`: an identifier is a label, and
# nothing downstream does arithmetic on it. Listed as patterns rather than per-case `ignore`
# entries because a per-case list is a list somebody forgets to update, and the failure is silent.
_IDENTIFIER = re.compile(
    r"""
    \bD-[A-Z]+-[0-9]{2}\b          # clause id: D-MAT-06
  | \bP-[A-Z0-9]+(?:-[A-Z0-9]+)*\b # permutation id: P-COMP-EXCLUDED
  | \bF-[0-9]{4}\b                 # finding id: F-0001
  | \bV[1-9]\b                     # variance class: V1, V2, V7
    """,
    re.VERBOSE,
)

# A citation, written out in prose. `page 11, rows 40-44`, `p.4`, `Occupancy!D5`,
# `inventory_2026-Q1.csv rows 1-31`. Every number in one is a **position**, which is precisely what
# `ALLOWED_FIELDS` in `tools/guard/agent_schema_lint.py` already exempts on the contracts: nothing
# downstream does arithmetic on a page number.
#
# The reviewer-assist agent is told to cite what it used and to cite it exactly. When it then
# renders those coordinates into the sentence - which is helpful, and what an officer wants to read
# - the leak check counted them as figures. Three cases failed on it, all of them correct answers.
_CITATION = re.compile(
    r"""
    \bp\.\s*[0-9]+                            # p.4
  | \bpages?\s+[0-9]+(?:\s*[-\u2013]\s*[0-9]+)?    # page 11, pages 3-9
  | \brows?\s+[0-9]+(?:\s*[-\u2013]\s*[0-9]+)?     # row 5, rows 40-44
  | \b[A-Za-z][A-Za-z ]*![A-Z]{1,3}[0-9]+      # Occupancy!D5
  | \b\S+\.(?:pdf|csv|xlsx)\b                 # inventory_2026-Q1.csv
    """,
    re.VERBOSE | re.IGNORECASE,
)

_PERIOD = re.compile(
    r"""
    \b\d{4}-(?:0[1-9]|1[0-2])\b          # 2026-01
  | \b\d{4}-Q[1-4]\b                     # 2026-Q1
  | \b(?:January|February|March|April|May|June|July|August|September|October|November
       |December)\s+\d{4}\b               # January 2026
  | \bQ[1-4]\s+\d{4}\b                   # Q1 2026
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Magnitudes spelled out. Not exhaustive and not meant to be — the critic agent is what catches
# the inventive cases; this catches the common ones deterministically, so a regression on them
# fails without a model in the loop.
#
# `one` and `two` are deliberately **absent**. `prompts/narrative/v1.md` instructs the agent in so
# many words to *"write 'the two counts differ', not how much by"* — and the scorer then failed the
# sentence for the word `two`. A comparison has two sides and prose has to be able to say so; "the
# two records", "one of the two figures" and "no one" are structure, not magnitude. `three` upward
# stays, because a nationality count really can be three and "three room nights" really is a leak.
#
# `quarter` is deliberately absent too, and it is the other entry worth explaining. In ordinary
# English it is a fraction; in this system it is a period — `2026-Q1`, "the quarter under review",
# "a quarter this run did not read" — and that sense dominates every sentence an agent here will
# write. Keeping it failed correct answers for using the vocabulary the domain runs on, which is a
# detector that punishes fluency in the subject. "Overstated by a quarter" is a real leak and it is
# the critic's to catch.
_WORD_NUMBERS = frozenset(
    {
        "zero",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "twenty",
        "thirty",
        "forty",
        "fifty",
        "hundred",
        "thousand",
        "dozen",
        "half",
        "third",
        "tenth",
        "handful",
        "several",
        "few",
    }
)


class Outcome(StrEnum):
    """How a case came out.

    `NOT_RECORDED` is distinct from `FAILED` on purpose. "We have not measured this" and "this is
    wrong" are different statements, and a harness that reports the first as the second teaches
    people to ignore its output.
    """

    PASSED = "passed"
    FAILED = "failed"
    NOT_RECORDED = "not_recorded"
    ERRORED = "errored"


@dataclass(frozen=True, slots=True)
class Check:
    """One property the answer must have, and the reason it matters."""

    name: str
    passed: bool
    detail: str = ""

    def render(self) -> str:
        mark = "ok  " if self.passed else "FAIL"
        return f"    {mark} {self.name}{f' - {self.detail}' if self.detail else ''}"


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case, scored."""

    agent: str
    case: str
    outcome: Outcome
    checks: tuple[Check, ...] = ()
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED

    def render(self) -> str:
        head = f"  {self.agent}/{self.case}: {self.outcome.value}"
        if self.detail:
            head += f" ({self.detail})"
        return "\n".join([head, *(check.render() for check in self.checks if not check.passed)])


def load_cases(agent: str | None = None, root: Path | None = None) -> tuple[Case, ...]:
    """Every committed case, sorted so a run reports in a stable order.

    A thin wrapper over the runtime's loader that supplies this suite's own case directory. The
    runtime takes the root as an argument rather than hard-coding one, because a runtime module
    that pointed into `tests/` would be a runtime module that cannot be installed.
    """
    return _load_cases(root or CASES_ROOT, agent)


def run_and_score(case: Case, provider: LLMProvider, policy: Policy) -> CaseResult:
    """Serve one case through a provider and score what comes back.

    The provider decides what this measures. `ReplayProvider` scores a recorded answer from a real
    model, which is the measurement this harness exists for. A `CassetteMissError` is reported as
    `NOT_RECORDED` rather than as a failure, because "we have not measured this" and "this is
    wrong" are different statements — and a harness that reports the first as the second teaches
    people to ignore its output.

    Every other exception is `ERRORED`, and is also not a pass. A case that blew up on a
    fabrication check has told us something real about the answer; swallowing it into the same
    bucket as a miss would hide it.
    """
    runner = AgentRunner(
        provider, policy=policy, registry=registry_for(case, policy), trace=TraceLog()
    )
    try:
        answer = run_case(case, runner, policy)
    except CassetteMissError:
        return CaseResult(
            agent=case.agent,
            case=case.name,
            outcome=Outcome.NOT_RECORDED,
            detail="no cassette for this request; run `make record`",
        )
    except Exception as exc:
        return CaseResult(
            agent=case.agent,
            case=case.name,
            outcome=Outcome.ERRORED,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return score_answer(case, answer)


# ── the checks ───────────────────────────────────────────────────────────────


def contains_a_number(sentence: str, *, ignore: tuple[str, ...] = ()) -> bool:
    """Whether prose states a figure, in digits or in words.

    Two families of label are removed first, because the agents are told to write both and a
    scorer that punished them would be measuring obedience to itself rather than quality:

    - **periods**, in every form — `2026-01`, `2026-Q1`, `January 2026`;
    - **identifiers** — clause ids, permutation ids, finding ids, variance classes;
    - **citations written out in prose** — `page 11, rows 40-44`, `Occupancy!D5`, a filename. Every
      number in one is a position, which is exactly what the contracts' own numeric whitelist
      exempts.

    `ignore` removes anything else a particular case legitimately expects. After the known-good
    tokens are gone, any digit is a leak.
    """
    stripped = sentence
    for pattern in (_CITATION, _PERIOD, _IDENTIFIER):
        stripped = pattern.sub(" ", stripped)
    for token in ignore:
        stripped = stripped.replace(token, " ")
    if _DIGIT.search(stripped):
        return True
    words = {word.strip(".,;:()'\"").lower() for word in stripped.split()}
    return bool(words & _WORD_NUMBERS)


def _citations(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """A `CitedAnswer`'s citations, as JSON. Empty for a decline, which the contract enforces
    rather than merely expects: a refusal rests on nothing."""
    citations = answer.get("citations", [])
    return [c for c in citations if isinstance(c, dict)]


def _rendered_citations(answer: dict[str, Any]) -> list[str]:
    """Each citation as a reader sees it: `pms_2026-01.pdf p.4 rows 12-18`, `Occupancy!B5`,
    `policy 1.3.0 clause D-MAT-06`.

    Rendered here rather than read from the answer, because the contract's `citation` property is
    not serialised — and a case file that had to state four raw fields per citation would be a case
    file nobody could read as a citation.
    """
    rendered: list[str] = []
    for citation in _citations(answer):
        match citation.get("kind"):
            case "pdf" | "inventory" | "excel":
                ref = citation.get("ref", {})
                if citation["kind"] == "excel":
                    rendered.append(f"{ref.get('sheet')}!{ref.get('cell')}")
                    continue
                start, end = ref.get("row_start"), ref.get("row_end")
                rows = f"row {start}" if start == end else f"rows {start}-{end}"
                page = f" p.{ref['page']}" if citation["kind"] == "pdf" else ""
                rendered.append(f"{ref.get('file')}{page} {rows}")
            case "clause":
                rendered.append(
                    f"policy {citation.get('policy_version')} clause {citation.get('clause')}"
                )
    return rendered


def _answer_text(answer: dict[str, Any]) -> str:
    """What the officer reads, whichever way the answer went - the prose, or the decline's reason."""
    if answer.get("outcome") == "answered":
        return str(answer.get("prose") or "")
    return str(answer.get("reason") or "")


def score_case(case: Case, answer: dict[str, Any]) -> tuple[Check, ...]:
    """Score one answer against one case's expectations. No model, no randomness.

    Every `expect` key is a check name, and an unrecognised one raises rather than being ignored.
    A silently-skipped expectation is a test that passes by not running, which is the failure this
    whole harness is supposed to catch in the agents.
    """
    checks: list[Check] = []
    for name, wanted in case.expect.items():
        checks.append(_apply(name, wanted, answer))
    return tuple(checks)


def _apply(name: str, wanted: Any, answer: dict[str, Any]) -> Check:
    match name:
        case "abstains":
            got = answer.get("outcome") == "abstained"
            return Check(name, got is bool(wanted), f"outcome={answer.get('outcome')}")
        case "iso_alpha2":
            got = answer.get("iso_alpha2")
            return Check(name, got == wanted, f"got {got!r}, wanted {wanted!r}")
        case "matched_candidate_offered":
            got = answer.get("matched_candidate")
            return Check(name, (got in wanted) is True, f"got {got!r}, offered {wanted}")
        case "reason_mentions":
            reason = str(answer.get("reason") or "").lower()
            missing = [term for term in wanted if term.lower() not in reason]
            return Check(name, not missing, f"absent: {missing}")
        case "finding_id":
            return Check(
                name, answer.get("finding_id") == wanted, f"got {answer.get('finding_id')!r}"
            )
        case "cites_permutation":
            got = answer.get("cites_permutation")
            return Check(name, got == wanted, f"got {got!r}, wanted {wanted!r}")
        case "is_definitional":
            got = answer.get("is_definitional")
            return Check(name, got is bool(wanted), f"got {got!r}")
        case "sentence_states_no_number":
            sentence = str(answer.get("sentence", ""))
            leaked = contains_a_number(sentence, ignore=tuple(wanted) if wanted else ())
            return Check(name, not leaked, f"sentence: {sentence!r}")
        case "sentence_avoids":
            sentence = str(answer.get("sentence", "")).lower()
            found = [term for term in wanted if term.lower() in sentence]
            return Check(name, not found, f"present: {found}")
        case "sentence_mentions":
            sentence = str(answer.get("sentence", "")).lower()
            missing = [term for term in wanted if term.lower() not in sentence]
            return Check(name, not missing, f"absent: {missing}")
        case "grounded" | "leaks_a_number" | "names_an_unassigned_cause" | "reads_as_an_accusation":
            got = answer.get(name)
            return Check(name, got is bool(wanted), f"got {got!r}, wanted {wanted!r}")
        case "verdict_passes":
            verdict = (
                bool(answer.get("grounded"))
                and not answer.get("leaks_a_number")
                and not answer.get("names_an_unassigned_cause")
                and not answer.get("reads_as_an_accusation")
            )
            return Check(name, verdict is bool(wanted), f"computed {verdict}")
        case "declines":
            got = answer.get("outcome") == "declined"
            return Check(name, got is bool(wanted), f"outcome={answer.get('outcome')}")
        case "cites":
            # Every citation, rendered the way `Citation.citation` renders it, so a case file
            # states what a reader would see rather than a field-by-field dictionary.
            got = _rendered_citations(answer)
            missing = [wanted_one for wanted_one in wanted if wanted_one not in got]
            return Check(name, not missing, f"absent: {missing}; cited: {got}")
        case "citation_kinds":
            # A **subset** check, not an exact set. The prompt encourages citing the clause *and*
            # the evidence rows, and an exact match failed the better answer for being more
            # thorough - a scorer that penalises the behaviour its own prompt asks for.
            got = {str(c.get("kind")) for c in _citations(answer)}
            missing = sorted(set(wanted) - got)
            return Check(name, not missing, f"absent: {missing}; cited kinds: {sorted(got)}")
        case "cites_at_least":
            # Refuses zero, because `>= 0` is a check that cannot fail and reads like one that can.
            # A case that wants no citations is a decline case, and `declines` says so.
            if int(wanted) < 1:
                raise ValueError(
                    f"eval case declares cites_at_least={wanted}, which every answer satisfies. A "
                    "check that cannot fail is worse than no check: it reports as a passing "
                    "property. Use `declines` for a case that expects no citations."
                )
            got = len(_citations(answer))
            return Check(name, got >= int(wanted), f"got {got} citation(s), wanted >= {wanted}")
        case "answer_states_no_number":
            # The reviewer-assist agent is shown no figures either, and its answer is read on the
            # same screen as figures that were computed. Same rule, same reason as the narrative's.
            prose = _answer_text(answer)
            leaked = contains_a_number(prose, ignore=tuple(wanted) if wanted else ())
            return Check(name, not leaked, f"answer: {prose!r}")
        case "answer_mentions":
            prose = _answer_text(answer).lower()
            missing = [term for term in wanted if term.lower() not in prose]
            return Check(name, not missing, f"absent: {missing}")
        case "answer_avoids":
            prose = _answer_text(answer).lower()
            found = [term for term in wanted if term.lower() in prose]
            return Check(name, not found, f"present: {found}")
        case "echoes_question":
            got = answer.get("question")
            return Check(name, got == wanted, f"got {got!r}")
        case "blocks":
            blocks = answer.get("blocks", [])
            return Check(name, len(blocks) == wanted, f"got {len(blocks)} blocks, wanted {wanted}")
        case "metrics_mapped":
            got = sorted({block.get("metric") for block in answer.get("blocks", [])})
            return Check(name, got == sorted(wanted), f"got {got}, wanted {sorted(wanted)}")
        case "unmapped":
            got = len(answer.get("unmapped", []))
            return Check(name, got == wanted, f"got {got}, wanted {wanted}")
        case _:
            raise ValueError(
                f"eval case declares an unknown expectation {name!r}. Add a check for it in "
                "`harness._apply` - an expectation nobody scores is a test that passes by not "
                "running, which is the failure this harness exists to catch."
            )


def verdict_for(checks: tuple[Check, ...]) -> Outcome:
    """A case passes only if every check does. There is no partial credit, because a partial score
    invites somebody to decide which failures are acceptable in a spreadsheet nobody reviews."""
    if not checks:
        return Outcome.FAILED
    return Outcome.PASSED if all(check.passed for check in checks) else Outcome.FAILED


def score_answer(case: Case, answer: AgentOutput | dict[str, Any]) -> CaseResult:
    """Score one already-obtained answer. The entry point tests use."""
    payload = answer.model_dump(mode="json") if isinstance(answer, AgentOutput) else dict(answer)
    checks = score_case(case, payload)
    return CaseResult(agent=case.agent, case=case.name, outcome=verdict_for(checks), checks=checks)


def render_report(results: tuple[CaseResult, ...]) -> str:
    """A report that says what was measured and what was not, in that order.

    `NOT_RECORDED` is counted separately and printed, never folded into a pass rate. A pass rate
    computed over the cases that happened to have cassettes is a number that improves when
    recordings are deleted.
    """
    tallies = {outcome: sum(1 for r in results if r.outcome is outcome) for outcome in Outcome}
    lines = [
        f"agent evals: {tallies[Outcome.PASSED]} passed, {tallies[Outcome.FAILED]} failed, "
        f"{tallies[Outcome.NOT_RECORDED]} not recorded, {tallies[Outcome.ERRORED]} errored",
    ]
    lines.extend(result.render() for result in results if not result.passed)
    if tallies[Outcome.NOT_RECORDED]:
        lines.append(
            "  Not recorded means no cassette exists for that case. Run `make record` with an "
            "API key and review the cassette diff; until then the case is unmeasured, which is "
            "not the same as passing."
        )
    return "\n".join(lines)
