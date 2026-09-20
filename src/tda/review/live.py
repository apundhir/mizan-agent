"""Live mode: the one place the Run console names `ANTHROPIC_API_KEY`, and how it refuses safely.

Deployed on Streamlit Community Cloud, this process is reachable by whoever holds the app's link.
Replay is the only provider offered by default, and that default is enforced here rather than
trusted to be remembered at every call site: `provider_for()` raises for `anthropic` unless the
gate is explicitly on, and it never falls back to something safer instead. A misconfigured
deployment shows an error, not a quiet downgrade that looks like it is working.

## What this module does not do

It never reads `.env` (`tda.agents.provider.dotenv.load_dotenv` is called by `make record` alone,
and this module does not import it). It never touches `st.secrets` - on Streamlit Community Cloud a
root-level secret is already exported to the process environment before the script runs, so `os
.environ` is the one place to look, local or hosted. It never binds the key to a variable that
outlives the call that needs it: `provider_for()` hands `tda.cli.build_provider` the string
`"anthropic"` and nothing else, and the adapter reads the credential itself.

## The budget this module does not enforce

`MIZAN_LIVE_RUNS_PER_SESSION` bounds how many live runs one browser session may start, and
`MIZAN_LIVE_RUNS_PER_PROCESS` bounds how many one server process will ever start, because a new
browser tab is a new session - `st.session_state` resets, the per-session cap resets with it, and
without a process-wide ceiling a viewer could buy unlimited live runs three at a time. Neither cap
says anything about how many model calls one run may make - that is `policy.yaml`'s budget,
enforced by the supervisor on every call regardless of how the run was started. All three are
independent limits on different things, and none substitutes for another.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from tda.agents.provider import ProviderMode
from tda.cli import build_provider

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping

    from tda.agents.provider.base import LLMProvider

LIVE_MODE_ENV: Final = "MIZAN_LIVE_MODE"
RUNS_PER_SESSION_ENV: Final = "MIZAN_LIVE_RUNS_PER_SESSION"
RUNS_PER_PROCESS_ENV: Final = "MIZAN_LIVE_RUNS_PER_PROCESS"
API_KEY_ENV: Final = "ANTHROPIC_API_KEY"

DEFAULT_RUNS_PER_SESSION: Final = 3
DEFAULT_RUNS_PER_PROCESS: Final = 20

REPLAY: Final = ProviderMode.REPLAY.value
LIVE: Final = ProviderMode.ANTHROPIC.value

_TRUE: Final = frozenset({"true"})
_FALSE: Final = frozenset({"false", ""})

SESSION_KEY: Final = "console.live_runs_used"


class LiveModeError(RuntimeError):
    """Live mode is off, misconfigured, or its session cap is spent.

    A `RuntimeError` rather than a `ValueError`: the request was well formed (a valid provider
    name, a sane environment variable), and what refuses it is the state of this deployment, not a
    mistake in what was asked for.
    """


@dataclass(frozen=True, slots=True)
class LiveGate:
    """What the console is allowed to do with the model layer on this deployment.

    Booleans and one integer, deliberately. Nothing on this type can hold the key itself, and a
    test can assert that by walking `dataclasses.fields()` and checking every type is `bool` or
    `int` - see `tests/unit/test_live_gate.py`.
    """

    enabled: bool
    key_present: bool
    runs_per_session: int
    runs_per_process: int = DEFAULT_RUNS_PER_PROCESS

    def offered_providers(self) -> tuple[str, ...]:
        """What the provider selector shows. `stub` is never offered here: it answers with a
        fixture built for testing this codebase, not evidence about a submission, and offering it
        on a page that presents itself as a verification tool would be presenting a prop as real."""
        if self.enabled and self.key_present:
            return (REPLAY, LIVE)
        return (REPLAY,)

    def provider_for(self, name: str) -> LLMProvider:
        """Build the named provider, or refuse with a reason a viewer can read.

        Refuses rather than substitutes: asking for `anthropic` when it is not available never
        quietly returns a replay provider instead. A caller that wanted to fall back would do so
        itself, having been told plainly that is what it is doing.
        """
        if name == REPLAY:
            return build_provider(REPLAY)
        if name != LIVE:
            raise LiveModeError(
                f"the console does not offer a {name!r} provider. It offers "
                f"{self.offered_providers()}."
            )
        if not self.enabled:
            raise LiveModeError(
                f"live mode is off on this deployment ({LIVE_MODE_ENV} is not 'true'). The "
                f"{LIVE!r} provider is refused; {REPLAY!r} is the only one offered. Nothing was "
                "sent to the API."
            )
        if not self.key_present:
            raise LiveModeError(
                f"live mode is on but {API_KEY_ENV} is not set in this app's environment. Live "
                "runs are refused until the key is added as a root-level secret and the app is "
                "rebooted - a secret inside a [section] does not become an environment variable."
            )
        return build_provider(LIVE)

    def render(self) -> str:
        """The one sidebar line. Names booleans and a count, never the key."""
        if not self.enabled:
            return f"Live mode: off. Provider: {REPLAY} (committed cassettes, no API call)."
        if not self.key_present:
            return "Live mode: on. Credential: MISSING. Live runs refused."
        return (
            f"Live mode: on. Credential: present. Live runs per session: {self.runs_per_session}. "
            f"Per process: {self.runs_per_process}."
        )


def live_gate(environ: Mapping[str, str] | None = None) -> LiveGate:
    """Read the three variables once, and build the gate the rest of the console reads.

    `environ` defaults to `os.environ`, read fresh on every call rather than cached, so a
    deployment's secrets reach the gate the moment the process sees them.
    """
    env = environ if environ is not None else os.environ
    return LiveGate(
        enabled=_parse_bool(env.get(LIVE_MODE_ENV, "")),
        key_present=bool(env.get(API_KEY_ENV)),
        runs_per_session=_parse_cap(
            env.get(RUNS_PER_SESSION_ENV), RUNS_PER_SESSION_ENV, DEFAULT_RUNS_PER_SESSION
        ),
        runs_per_process=_parse_cap(
            env.get(RUNS_PER_PROCESS_ENV), RUNS_PER_PROCESS_ENV, DEFAULT_RUNS_PER_PROCESS
        ),
    )


def _parse_bool(value: str) -> bool:
    normalised = value.strip().lower()
    if normalised in _TRUE:
        return True
    if normalised in _FALSE:
        return False
    raise LiveModeError(
        f"{LIVE_MODE_ENV} must be 'true' or 'false' (case-insensitive), got {value!r}. A "
        "deployment that cannot be read as configured must say so rather than guess."
    )


def _parse_cap(value: str | None, env_name: str, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        raise LiveModeError(f"{env_name} must be a whole number, got {value!r}.") from None
    if parsed < 1:
        raise LiveModeError(f"{env_name} must be at least 1, got {parsed}.")
    return parsed


def take_live_run(state: MutableMapping[str, object], gate: LiveGate) -> int:
    """Spend one of this session's live runs, or refuse.

    Increments **before** the run is attempted, so a run that then crashes still counts against the
    cap. A cap that only charged for a finished run would let a crash loop spend it for free.
    """
    stored = state.get(SESSION_KEY, 0)
    used = stored if isinstance(stored, int) else 0
    if used >= gate.runs_per_session:
        raise LiveModeError(
            f"this session has used its {gate.runs_per_session} live run(s). Replay runs are "
            f"unlimited. Start a new session later, or raise {RUNS_PER_SESSION_ENV}."
        )
    used += 1
    state[SESSION_KEY] = used
    return used


class ProcessLiveRuns:
    """A live-run counter shared by every session in this server process.

    `take_live_run` bounds one browser session, but a new tab is a new session: `st.session_state`
    starts over and so does its cap. Built once per process behind `st.cache_resource`
    (`tda.review.console`), this counter is what actually bounds a deployment's total live spend
    between reboots, no matter how many tabs ask for a run. The lock matters here in a way it does
    not for `take_live_run`: Streamlit script runs are single-threaded per session, but two sessions
    in the same process can call this concurrently.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._used = 0

    def take(self, gate: LiveGate) -> int:
        """Spend one of this process's live runs, or refuse. Increments before the run is
        attempted, matching `take_live_run`'s reasoning: a crash must still count."""
        with self._lock:
            if self._used >= gate.runs_per_process:
                raise LiveModeError(
                    f"this deployment has used its {gate.runs_per_process} live run(s) for this "
                    f"server process. Replay runs are unlimited. Raise {RUNS_PER_PROCESS_ENV}, or "
                    "restart the app to reset the count."
                )
            self._used += 1
            return self._used
