"""The live-mode gate: refuses rather than falls back, and never holds the key.

Three claims, each with its own test group.

**Off by default, and a malformed setting is an error, not a guess.** A deployment that cannot be
read as configured must say so rather than silently choose replay - which happens to be safe here,
but the point is that guessing is the wrong instinct to build in general.

**A refusal names why and constructs nothing.** `provider_for("anthropic")` either returns a
working provider or raises before any SDK object exists - never a replay provider standing in for
one that was asked for and refused.

**The gate cannot hold a key, structurally.** Every field on `LiveGate` is a `bool` or an `int`, and
that is asserted here rather than assumed, the same way `tda.obs.redact` asserts its own invariant
about what a stamped count can contain.
"""

from __future__ import annotations

import dataclasses

import pytest

from tda.review.live import (
    API_KEY_ENV,
    LIVE,
    LIVE_MODE_ENV,
    REPLAY,
    RUNS_PER_SESSION_ENV,
    SESSION_KEY,
    LiveGate,
    LiveModeError,
    live_gate,
    take_live_run,
)

PLANTED_KEY = "sk-ant-api03-" + "EXAMPLE" * 4


# ── reading the environment ───────────────────────────────────────────────────


def test_live_mode_is_off_when_the_variable_is_absent() -> None:
    gate = live_gate({})
    assert gate.enabled is False
    assert gate.offered_providers() == (REPLAY,)


def test_a_blank_value_reads_as_off_rather_than_as_an_error() -> None:
    assert live_gate({LIVE_MODE_ENV: ""}).enabled is False
    assert live_gate({LIVE_MODE_ENV: "  "}).enabled is False


@pytest.mark.parametrize("value", ["yes", "1", "on", "enabled", "TRU"])
def test_a_malformed_live_mode_value_is_an_error_not_a_default(value: str) -> None:
    with pytest.raises(LiveModeError, match=LIVE_MODE_ENV):
        live_gate({LIVE_MODE_ENV: value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("True", True), ("TRUE", True), ("false", False), ("False", False)],
)
def test_a_toml_boolean_exported_as_text_still_parses(value: str, expected: bool) -> None:
    """Streamlit exports a TOML boolean as the text `True` or `False`, capitalised. A gate that
    only accepted lowercase would read every hosted deployment as off."""
    assert live_gate({LIVE_MODE_ENV: value}).enabled is expected


def test_the_session_cap_defaults_to_three_and_ignores_a_blank_value() -> None:
    assert live_gate({}).runs_per_session == 3
    assert live_gate({RUNS_PER_SESSION_ENV: ""}).runs_per_session == 3


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5"])
def test_the_session_cap_rejects_nonsense(value: str) -> None:
    with pytest.raises(LiveModeError, match=RUNS_PER_SESSION_ENV):
        live_gate({RUNS_PER_SESSION_ENV: value})


def test_the_session_cap_is_configurable() -> None:
    assert live_gate({RUNS_PER_SESSION_ENV: "7"}).runs_per_session == 7


# ── what is offered, and what is refused ──────────────────────────────────────


def test_live_mode_off_offers_replay_only() -> None:
    gate = live_gate({LIVE_MODE_ENV: "false", API_KEY_ENV: PLANTED_KEY})
    assert gate.offered_providers() == (REPLAY,)


def test_live_mode_on_without_a_key_offers_replay_only() -> None:
    gate = live_gate({LIVE_MODE_ENV: "true"})
    assert gate.key_present is False
    assert gate.offered_providers() == (REPLAY,)


def test_live_mode_on_with_a_key_offers_both() -> None:
    gate = live_gate({LIVE_MODE_ENV: "true", API_KEY_ENV: PLANTED_KEY})
    assert gate.offered_providers() == (REPLAY, LIVE)


def test_stub_is_never_offered_by_the_console() -> None:
    gate = live_gate({LIVE_MODE_ENV: "true", API_KEY_ENV: PLANTED_KEY})
    assert "stub" not in gate.offered_providers()


def test_live_mode_off_refuses_anthropic_rather_than_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal happens before anything is constructed - a caller checking `offered_providers()`
    first would never reach this, but a caller that does not must be refused just as hard."""
    called: list[str] = []

    def fake_build(name: str) -> object:
        called.append(name)
        return object()

    monkeypatch.setattr("tda.review.live.build_provider", fake_build)
    gate = live_gate({LIVE_MODE_ENV: "false"})

    with pytest.raises(LiveModeError, match="refused"):
        gate.provider_for(LIVE)

    assert called == [], "a refused provider must construct nothing"


def test_live_mode_on_without_a_key_names_the_variable_when_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tda.review.live.build_provider", lambda name: object())
    gate = live_gate({LIVE_MODE_ENV: "true"})

    with pytest.raises(LiveModeError, match=API_KEY_ENV):
        gate.provider_for(LIVE)


def test_a_provider_the_console_does_not_offer_is_refused_by_name() -> None:
    gate = live_gate({})
    with pytest.raises(LiveModeError, match="stub"):
        gate.provider_for("stub")


def test_replay_is_always_built_regardless_of_live_mode() -> None:
    gate = live_gate({LIVE_MODE_ENV: "false"})
    provider = gate.provider_for(REPLAY)
    assert provider.mode.value == REPLAY


def test_provider_for_anthropic_is_built_without_being_handed_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`provider_for` calls `build_provider` with the provider name and nothing else. Whatever key
    reaches the SDK, it does not reach it through this call - `AnthropicProvider` reads
    `ANTHROPIC_API_KEY` from the environment itself, inside the adapter this test never touches."""
    calls: list[tuple[object, ...]] = []

    def fake_build(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr("tda.review.live.build_provider", fake_build)
    gate = live_gate({LIVE_MODE_ENV: "true", API_KEY_ENV: PLANTED_KEY})

    gate.provider_for(LIVE)

    assert calls == [((LIVE,), {})]


# ── the gate cannot hold the key ──────────────────────────────────────────────


def test_every_field_on_the_gate_is_a_bool_or_an_int() -> None:
    for field in dataclasses.fields(LiveGate):
        assert field.type in ("bool", "int"), (
            f"{field.name} is typed {field.type!r}; a field that could hold a string could hold "
            "the key"
        )


def test_the_gate_never_holds_the_key_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tda.review.live.build_provider", lambda name: object())
    gate = live_gate({LIVE_MODE_ENV: "true", API_KEY_ENV: PLANTED_KEY})

    assert PLANTED_KEY not in repr(gate)
    assert PLANTED_KEY not in str(dataclasses.asdict(gate))
    assert PLANTED_KEY not in gate.render()
    assert PLANTED_KEY not in str(gate.provider_for(LIVE))


def test_the_render_line_says_credential_missing_without_leaking_that_it_is_missing_how() -> None:
    assert "MISSING" in live_gate({LIVE_MODE_ENV: "true"}).render()
    assert "off" in live_gate({}).render()
    assert "present" in live_gate({LIVE_MODE_ENV: "true", API_KEY_ENV: PLANTED_KEY}).render()


# ── the per-session cap ────────────────────────────────────────────────────────


def test_the_fourth_live_run_in_a_session_is_refused() -> None:
    gate = LiveGate(enabled=True, key_present=True, runs_per_session=3)
    state: dict[str, object] = {}

    for _ in range(3):
        take_live_run(state, gate)

    with pytest.raises(LiveModeError, match="3 live run"):
        take_live_run(state, gate)


def test_a_crashed_live_run_still_counts() -> None:
    """Charged before the run is attempted. A cap that only counted a finished run would let a
    crash loop spend it for free."""
    gate = LiveGate(enabled=True, key_present=True, runs_per_session=1)
    state: dict[str, object] = {}

    take_live_run(state, gate)

    assert state[SESSION_KEY] == 1
    with pytest.raises(LiveModeError):
        take_live_run(state, gate)


def test_a_fresh_session_state_starts_at_zero() -> None:
    gate = LiveGate(enabled=True, key_present=True, runs_per_session=2)
    state: dict[str, object] = {}

    assert take_live_run(state, gate) == 1
    assert take_live_run(state, gate) == 2
