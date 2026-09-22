"""The per-agent eval set, and an honest account of what it currently measures.

An agent is five things, and the fifth is an eval set. These tests are that fifth thing, and they
make three distinct assertions — which is more useful than one aggregate, because two of them can
run today and the third cannot.

**1. Every agent has cases, and every case is well-formed.** Runs on every push. An agent in the
roster with no eval set is an agent nobody has a way of judging, and it fails here rather than
being noticed a sprint later.

**2. The scorer distinguishes a right answer from a wrong one.** Runs on every push, fully
deterministic, no model involved. Each case carries an `expect_example` that must score as a pass
and a `counterexample` that must score as a fail. This is the classic failure of an eval harness —
a scorer that passes everything reports a number that looks like evidence — and it is the one
property that can be proven without an API key. So it is proven on every push.

**3. The agents themselves score against their cases.** Measurable for every case that has a
cassette, and `NOT_RECORDED` **in words** for every case that does not — a skip reads as a pass in a
CI summary, and an unmeasured case is not a pass. The assertion is only that nothing *recorded* is
allowed to fail, so the file needed no edit when the first cassettes landed: it simply started
measuring.

That first recording is worth knowing about, because it changed this file more than any review did.
Four of eight recorded cases failed, and **not one of the four was the model's fault** — the mapping
case named a metric the corpus does not contain, and all three narrative cases failed a leak check
that flagged `January 2026` and the word `two`, both of which `prompts/narrative/v1.md` explicitly
asks the agent to write. A scorer nothing has ever been scored against is a scorer nobody has
tested, and the checks it got wrong were the ones no hand-written example happened to exercise.

The distinction between "unmeasured" and "passing" is the whole reason this file is shaped this
way. `tests/cassettes/README.md` takes the same position about the layer below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tda.agents.provider import ReplayProvider
from tda.agents.roster import REVIEWER_ASSIST, ROSTER
from tda.policy import load_policy
from tests.eval.agents.harness import (
    CASES_ROOT,
    Case,
    Outcome,
    contains_a_number,
    load_cases,
    render_report,
    run_and_score,
    score_answer,
    score_case,
    verdict_for,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.eval

CASES = load_cases()

# Every agent in the roster now has an eval set. The exemption that used to sit here named
# reviewer-assist and was deleted when the reviewer-assist agent landed it, which is what an exemption listed by name
# rather than a loosened assertion buys: it has to be removed, not merely stopped applying.
AWAITING_IMPLEMENTATION: frozenset[str] = frozenset()


def test_every_implemented_agent_has_an_eval_set() -> None:
    """An agent is five things. Without the fifth, "the agent works" is a sentence somebody typed
    after watching it work once."""
    expected = set(ROSTER) - AWAITING_IMPLEMENTATION
    have = {case.agent for case in CASES}

    assert have == expected, f"no eval cases for {sorted(expected - have)}"


def test_every_case_states_what_it_defends_against() -> None:
    """A case without a `why` is a check nobody can decide whether to delete, so it survives every
    cleanup and eventually nobody knows what it is for."""
    for case in CASES:
        assert len(case.why.split()) >= 10, f"{case.agent}/{case.name} has no real rationale"


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.agent}/{c.name}")
def test_the_scorer_accepts_a_right_answer(case: Case) -> None:
    """Half of the harness's own proof. A scorer that fails a correct answer is as useless as one
    that passes everything, and it is the easier mistake when the checks are properties."""
    result = score_answer(case, case.expect_example)

    assert result.passed, (
        f"the model answer this case calls correct does not score:\n{result.render()}"
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.agent}/{c.name}")
def test_the_scorer_rejects_a_wrong_answer(case: Case) -> None:
    """The other half, and the one that matters. This is the assertion that makes every other
    number this harness reports mean something."""
    result = score_answer(case, case.counterexample)

    assert not result.passed, (
        f"the answer this case calls wrong scores as a pass. A scorer that accepts "
        f"{case.agent}/{case.name}'s counterexample is not measuring anything."
    )


def test_a_case_with_no_checks_fails_rather_than_passing_vacuously() -> None:
    """There is no partial credit and there is no empty credit. A case that scores nothing must
    not report success."""
    assert verdict_for(()) is Outcome.FAILED


def test_an_unknown_expectation_raises_rather_than_being_ignored() -> None:
    """An expectation nobody scores is a test that passes by not running — which is precisely the
    failure this harness exists to catch in the agents."""
    case = Case(
        agent="narrative",
        name="invented",
        why="x" * 60,
        inputs={},
        expect={"vibes_are_good": True},
        expect_example={},
        counterexample={},
    )

    with pytest.raises(ValueError, match="unknown expectation"):
        score_answer(case, {})


@pytest.mark.parametrize(
    ("sentence", "leaks"),
    [
        ("The counts differ.", False),
        ("The January figure is 3.2 points higher.", True),
        ("The figure is roughly a tenth higher.", True),
        ("About forty room-nights are unaccounted for.", True),
        ("A handful of reservations span the month end.", True),
        ("The February occupancy figures differ on complimentary rooms.", False),
    ],
)
def test_the_number_check_catches_words_as_well_as_digits(sentence: str, leaks: bool) -> None:
    """Digits are the easy case. A magnitude written in words reads as prose, and it is the one
    that gets through a review."""
    assert contains_a_number(sentence) is leaks


def test_a_period_is_never_mistaken_for_a_leaked_figure() -> None:
    """A narrative must name its period, and `prompts/narrative/v1.md` asks for it **in words**.

    This used to assert the opposite - that a period counted as a leak unless the case remembered
    to list it in `ignore` - and that workaround held only because no real answer had ever been
    scored. The first recording failed all three narrative cases: the agent wrote `January 2026`,
    exactly as instructed, and the scorer called it a leaked figure. A check that punishes the
    behaviour its own prompt demands measures obedience to the scorer rather than quality, so the
    detector now knows a period in any of its forms is a label.
    """
    for period in ("2026-01", "2026-Q1", "January 2026", "Q1 2026"):
        sentence = f"The {period} occupancy figure does not reconcile with the reservation rows."
        assert contains_a_number(sentence) is False, f"{period!r} read as a leaked figure"

    # And the thing it exists to catch, in both the forms it takes.
    assert contains_a_number("Occupancy for January 2026 is overstated by 3.6 points.") is True
    assert contains_a_number("The January 2026 count is out by about forty room-nights.") is True


def test_prose_may_name_the_two_sides_of_a_comparison() -> None:
    """The narrative prompt says, in so many words, *"write 'the two counts differ', not how much
    by"* - and the scorer failed that sentence for the word `two`.

    A comparison has two sides and prose has to be able to say so. `three` upward stays a leak,
    because a nationality count really can be three.
    """
    assert contains_a_number("The two counts differ.") is False
    assert contains_a_number("One of the two figures does not reconcile.") is False
    assert contains_a_number("The hotel overstated by three room nights.") is True


def test_the_agents_score_against_their_cases() -> None:
    """The measurement this harness exists for. Correct whether or not cassettes are committed.

    Every case is served through **replay** and scored — the same `run_case` the recorder used, so
    a case cannot be recorded against one question and replayed against another. What comes back
    is one of three things, and the distinction is the whole point:

    | Outcome | Means |
    |---|---|
    | `PASSED` / `FAILED` | a real model answered this and it did or did not hold up |
    | `NOT_RECORDED` | no cassette for this request — **unmeasured, which is not a pass** |
    | `ERRORED` | the call blew up; also not a pass |

    The assertion is that **nothing recorded is allowed to fail**. Every committed case now has a
    cassette, so it bites on twenty real answers, and it needed no edit when those landed: it was
    written as a claim about recorded answers rather than as a check on whether the directory is
    empty. A case added without a recording reports `NOT_RECORDED` in words rather than hiding
    behind a skip, which is the position `tests/cassettes/README.md` takes about the layer below.
    """
    policy = load_policy()
    provider = ReplayProvider()
    results = tuple(run_and_score(case, provider, policy) for case in CASES)
    report = render_report(results)

    failed = [r for r in results if r.outcome in (Outcome.FAILED, Outcome.ERRORED)]
    assert not failed, f"a recorded answer no longer holds up:\n{report}"

    unmeasured = [r for r in results if r.outcome is Outcome.NOT_RECORDED]
    if unmeasured:
        # Not an assertion failure - it is the honest state of a case nobody has recorded yet.
        # Printed so a CI log says so out loud rather than showing a silent green.
        print(f"\n{report}")


def test_an_unrecorded_case_is_reported_rather_than_skipped() -> None:
    """A skip reads as a pass in a CI summary, and "we have not measured this" is not a pass.

    Asserted against an empty cassette directory rather than the real one, so the check keeps
    working after `make record` has run - at which point the real directory no longer exercises
    this path at all.
    """
    policy = load_policy()
    empty = ReplayProvider(cassette_dir=CASES_ROOT.parent / "no-cassettes-here")
    result = run_and_score(CASES[0], empty, policy)

    assert result.outcome is Outcome.NOT_RECORDED
    assert not result.passed
    assert "not the same as passing" in render_report((result,))


def test_the_cases_live_where_the_story_says_they_do() -> None:
    """`tests/eval/agents/`, named in the agent runtime. A convention nobody can find is not a convention."""
    assert CASES_ROOT.is_dir()
    assert CASES_ROOT.parent.parts[-3:] == ("tests", "eval", "agents")


# ── the recorder ─────────────────────────────────────────────────────────────


def test_the_recorder_refuses_without_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recording is the one operation here that costs money. It says so and stops, rather than
    failing somewhere inside the SDK with a stack trace about authentication.

    **The no-key condition is forced, and that is not belt-and-braces.** This test used to call
    `main` with the ambient environment, which was harmless only while nobody had a key. The first
    time a developer followed `.env.example` exactly as the recorder tells them to, this test found
    the key, took the live path and **spent money inside `make ci`** - asserting on an exit code it
    reached by recording every committed case first.

    So: the variable is cleared, `--no-dotenv` stops the file being read, and `--env-file` points
    at nothing. Any one of the three would do; all three are here because the failure mode is
    silent, costs real money, and is only visible on a bill.
    """
    from tda.agents.provider.record import main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    exit_code = main(
        [
            "--out",
            str(tmp_path / "never-written"),
            "--no-dotenv",
            "--env-file",
            str(tmp_path / "no-such.env"),
        ]
    )

    assert exit_code == 2
    assert not (tmp_path / "never-written").exists()


def test_the_recorder_would_call_every_committed_case(capsys: pytest.CaptureFixture[str]) -> None:
    """`--dry-run` is the check that the recording set and the eval set are the same set.

    They must be: the cassettes are recordings of the calls the eval suite replays, and a case the
    recorder skips is a case that reports `not_recorded` forever with nobody able to say why.
    """
    from tda.agents.provider.record import main

    assert main(["--dry-run"]) == 0
    listed = {
        line.split("would record ")[1].strip()
        for line in capsys.readouterr().err.splitlines()
        if "would record " in line
    }

    assert listed == {case.ref for case in CASES}


def test_every_case_builds_a_real_request_rather_than_erroring_first() -> None:
    """A case that blows up before the model is asked is unmeasurable, and it would sit in the
    directory reporting `not_recorded` looking exactly like one that merely lacks a cassette.

    So: serve every case through replay with no cassettes and require the outcome to be a clean
    miss. A miss proves the whole path ran — the finding was built, the tools were called, the
    allowlist held, the request was rendered — and stopped at the one step that needs a key.
    """
    policy = load_policy()
    empty = ReplayProvider(cassette_dir=CASES_ROOT.parent / "no-cassettes-here")
    results = tuple(run_and_score(case, empty, policy) for case in CASES)

    errored = [r for r in results if r.outcome is Outcome.ERRORED]
    assert not errored, render_report(results)


def test_a_check_that_cannot_fail_is_refused_rather_than_counted() -> None:
    """`cites_at_least: 0` is satisfied by every answer, including one with no citations at all.

    It is worse than no check, because it appears in the scored list as a passing property and so
    makes a case look better defended than it is. A case that expects no citations is a decline
    case, and `declines` is how it says so.
    """
    vacuous = Case(
        agent=REVIEWER_ASSIST,
        name="vacuous",
        why="a check nobody can fail",
        inputs={},
        expect={"cites_at_least": 0},
        expect_example={},
        counterexample={},
    )

    with pytest.raises(ValueError, match="which every answer satisfies"):
        score_case(vacuous, {})
