"""The model layer must be reproducible, offline, and loud when it cannot be.

The cassette-key tests carry the weight. If the key is not stable across processes, every run is
a cassette miss, CI either fails or silently goes live, and `make repro` measures nothing — so
stability is asserted against a *fresh interpreter*, not just within this one. A key that is
stable only inside a single process is not stable.

The rest is about failing loudly: a replay miss must never become a live call, a stale cassette
must never deserialise into something almost right, and a stub must never invent a default.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tda.agents.contracts import AgentOutput
from tda.agents.provider import (
    KEY_SCHEMA_VERSION,
    CassetteMissError,
    Effort,
    Message,
    ModelRequest,
    ModelResponse,
    OutputValidationError,
    ProviderError,
    ProviderMode,
    ReplayProvider,
    StubProvider,
    TokenUsage,
    cassette_path,
    write_cassette,
)
from tda.agents.provider.anthropic_client import build_request_params


class Mapping(AgentOutput):
    """A stand-in for the real mapping contract, which lands in the agent runtime."""

    sheet: str
    cell_range: str
    page: int


class Wider(AgentOutput):
    sheet: str
    cell_range: str
    page: int
    axis: str


def request(**overrides: object) -> ModelRequest:
    base: dict[str, object] = {
        "agent": "mapping",
        "prompt_version": "v1",
        "system": "You map sheets to metrics.",
        "messages": (Message(role="user", content="Sheet names: Cover, Occupancy, Nationality"),),
        "output_schema": Mapping.model_json_schema(),
        "model_id": "claude-opus-5",
        "effort": Effort.LOW,
    }
    return ModelRequest(**(base | overrides))  # type: ignore[arg-type]


# ── the cassette key ─────────────────────────────────────────────────────────


def test_key_is_stable_for_identical_requests() -> None:
    assert request().cassette_key == request().cassette_key


def test_key_is_stable_across_processes() -> None:
    """The test that actually matters.

    Python's `hash()` is salted per process, and a key built from it would look perfectly stable
    inside one test run and change on every CI invocation — turning every replay into a miss.
    This shells out to a fresh interpreter to prove the key does not depend on process state.
    """
    script = (
        "from tda.agents.provider import ModelRequest, Message, Effort;"
        "import json;"
        "print(ModelRequest(agent='mapping', prompt_version='v1',"
        " system='You map sheets to metrics.',"
        " messages=(Message(role='user', content='Sheet names: Cover, Occupancy, Nationality'),),"
        " output_schema=json.loads('''" + json.dumps(Mapping.model_json_schema()) + "'''),"
        " model_id='claude-opus-5', effort=Effort.LOW).cassette_key)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == request().cassette_key


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent", "narrative"),
        ("prompt_version", "v2"),
        ("system", "A different system prompt."),
        ("model_id", "claude-sonnet-5"),
        ("effort", Effort.HIGH),
        ("max_tokens", 32_000),
    ],
)
def test_every_answer_affecting_input_changes_the_key(field: str, value: object) -> None:
    """Each of these can change the answer, so each must change the key. A key that ignores one
    of them serves a cassette recorded under different conditions."""
    assert request(**{field: value}).cassette_key != request().cassette_key


def test_changing_the_output_schema_changes_the_key() -> None:
    """The subtle one. Same prompt, wider contract, different answer — and a key that ignored the
    schema would serve the narrow recording for the wide request, deserialising into a contract
    that is missing a field the caller now relies on."""
    assert request(output_schema=Wider.model_json_schema()).cassette_key != request().cassette_key


def test_changing_message_content_changes_the_key() -> None:
    other = (Message(role="user", content="Sheet names: Cover, Occupancy"),)
    assert request(messages=other).cassette_key != request().cassette_key


def test_key_schema_version_is_hashed() -> None:
    """Bumping the version must invalidate every cassette, which is the point of having it."""
    assert f'"key_schema_version":{KEY_SCHEMA_VERSION}' in request().canonical()


def test_canonical_form_is_already_canonical() -> None:
    """Round-trip, rather than scanning for separators — message content legitimately contains
    commas and colons, so a substring check would be testing the fixture's prose.

    Re-serialising the parsed form with canonical settings must reproduce the string exactly. That
    proves sorted keys and compact separators without caring what the payload says, and it is the
    property that matters: dict insertion order must not reach the digest.
    """
    canonical = request().canonical()
    reserialised = json.dumps(
        json.loads(canonical), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )

    assert reserialised == canonical
    keys = list(json.loads(canonical))
    assert keys == sorted(keys)


def test_non_ascii_content_hashes_as_itself() -> None:
    """`ensure_ascii=False`, so a country name with a diacritic hashes as the text rather than as
    an escape sequence whose exact form could vary between serialisers."""
    canonical = request(
        messages=(Message(role="user", content="Côte d'Ivoire, Türkiye"),)
    ).canonical()
    assert "Côte d'Ivoire" in canonical
    assert "\\u" not in canonical


# ── replay ───────────────────────────────────────────────────────────────────


def test_a_miss_raises_rather_than_calling_the_live_api(tmp_path: Path) -> None:
    """The single most important behaviour in this module.

    A replay mode that falls through to the network on a miss would let CI pass, spend money, and
    stop being reproducible, without one line of output saying so.
    """
    provider = ReplayProvider(cassette_dir=tmp_path)
    with pytest.raises(CassetteMissError) as excinfo:
        provider.complete(request(), Mapping)

    message = str(excinfo.value)
    assert request().cassette_key in message, "the miss must print the key so it can be recorded"
    assert "make record" in message, "and must say how to fix it"
    assert "never calls the live API" in message


def test_a_hit_returns_the_validated_contract(tmp_path: Path) -> None:
    answer = Mapping(sheet="Nationality", cell_range="C5:N40", page=2)
    write_cassette(tmp_path, request(), answer.model_dump_json(), TokenUsage(120, 40))

    response = ReplayProvider(cassette_dir=tmp_path).complete(request(), Mapping)

    assert isinstance(response.parsed, Mapping)
    assert response.parsed == answer
    assert response.mode is ProviderMode.REPLAY
    assert response.usage.input_tokens == 120
    assert response.cassette_key == request().cassette_key


def test_a_stale_cassette_fails_loudly(tmp_path: Path) -> None:
    """A cassette recorded before a contract gained a required field must not deserialise into
    something almost right. That failure would surface later as a wrong number in a verdict,
    with nothing pointing back here.
    """
    stale = Mapping(sheet="Nationality", cell_range="C5:N40", page=2)
    wider = request(output_schema=Wider.model_json_schema())
    write_cassette(tmp_path, wider, stale.model_dump_json(), TokenUsage())

    with pytest.raises(OutputValidationError, match="recorded against an older version"):
        ReplayProvider(cassette_dir=tmp_path).complete(wider, Wider)


def test_an_unreadable_cassette_is_a_provider_error(tmp_path: Path) -> None:
    path = cassette_path(tmp_path, "mapping", request().cassette_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(ProviderError, match="unreadable"):
        ReplayProvider(cassette_dir=tmp_path).complete(request(), Mapping)


def test_cassettes_are_one_file_per_call_namespaced_by_agent(tmp_path: Path) -> None:
    """A single large cassette file makes every diff a merge conflict and every review a scroll.
    Per-call files mean a changed prompt is one added and one removed file."""
    write_cassette(
        tmp_path,
        request(),
        Mapping(sheet="S", cell_range="A1", page=1).model_dump_json(),
        TokenUsage(),
    )
    written = list(tmp_path.rglob("*.json"))

    assert len(written) == 1
    assert written[0].parent.name == "mapping"
    assert written[0].stem == request().cassette_key


def test_a_cassette_records_what_was_asked_not_only_the_answer(tmp_path: Path) -> None:
    """`request_canonical` is redundant with the key and worth its few hundred bytes: it is the
    difference between a reviewable cassette diff and an opaque hash change."""
    path = write_cassette(
        tmp_path,
        request(),
        Mapping(sheet="S", cell_range="A1", page=1).model_dump_json(),
        TokenUsage(),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["request_canonical"]["system"] == "You map sheets to metrics."
    assert payload["prompt_version"] == "v1"
    assert payload["model_id"] == "claude-opus-5"


def test_a_rewritten_cassette_is_byte_identical(tmp_path: Path) -> None:
    """`make record` re-run on an unchanged prompt must produce no diff, or every recording session
    churns the whole directory and real changes become invisible."""
    answer = Mapping(sheet="S", cell_range="A1", page=1).model_dump_json()
    first = write_cassette(tmp_path, request(), answer, TokenUsage(10, 5)).read_bytes()
    second = write_cassette(tmp_path, request(), answer, TokenUsage(10, 5)).read_bytes()

    assert first == second


# ── the live adapter's request, without a network ────────────────────────────


def test_the_built_request_has_no_temperature() -> None:
    """The PRD asked for `temperature 0`. It is rejected with HTTP 400 on current models and never
    guaranteed determinism. This test exists because that is exactly the kind of parameter someone
    re-adds from memory while debugging."""
    params = build_request_params(request(), Mapping)

    assert "temperature" not in params
    assert "top_p" not in params
    assert "top_k" not in params


def test_the_built_request_pins_the_model_and_sets_effort() -> None:
    params = build_request_params(request(effort=Effort.HIGH), Mapping)

    assert params["model"] == "claude-opus-5"
    assert params["output_config"] == {"effort": "high"}
    assert params["thinking"] == {"type": "adaptive"}


def test_the_built_request_always_declares_the_output_contract() -> None:
    """Structured output on every call, so free text cannot enter the numeric path."""
    assert build_request_params(request(), Mapping)["output_format"] is Mapping


def test_the_built_request_does_not_use_the_deprecated_budget_tokens() -> None:
    """`{"type": "enabled", "budget_tokens": N}` is rejected on this model family."""
    assert "budget_tokens" not in json.dumps(build_request_params(request(), Mapping)["thinking"])


# ── the stub ─────────────────────────────────────────────────────────────────


def test_the_stub_serves_registered_answers_and_records_calls() -> None:
    stub = StubProvider()
    stub.register(Mapping(sheet="Occupancy", cell_range="B2:M3", page=1))

    response = stub.complete(request(), Mapping)

    assert response.parsed.sheet == "Occupancy"
    assert response.mode is ProviderMode.STUB
    assert len(stub.calls) == 1
    assert stub.calls[0].agent == "mapping"


def test_the_stub_refuses_to_invent_a_default() -> None:
    """A stub that returns a plausible default is the worst test double available: the test passes
    and proves nothing, and the failure surfaces later as a wrong number nobody traces back."""
    with pytest.raises(ProviderError, match="will not invent"):
        StubProvider().complete(request(), Mapping)


# ── contracts ────────────────────────────────────────────────────────────────


def test_a_contract_is_never_falsy_however_the_agent_answered() -> None:
    """The regression test for the most expensive bug in this repository's history.

    Every contract used to override `__bool__` so `if resolution:` read nicely. The Anthropic SDK
    populates `parsed_output` and then reads it back with `if content.parsed_output:` - so a falsy
    contract was silently discarded, and the caller was told the model returned nothing parseable
    from a response whose JSON was exactly right. It cost a full recording run to find, and it hid
    behind a second bug for the whole of the first one.

    So: contracts answer `is_answer`, and `bool(contract)` is always True. `AgentResult.__bool__`
    is where `if result:` lives, because an `AgentResult` never crosses a library boundary.
    """
    from tda.agents.contracts.resolution import LabelResolution
    from tda.agents.contracts.reviewer_assist import CitedAnswer

    abstained = LabelResolution(raw_label="Qqq", outcome="abstained", reason="nothing close")
    declined = CitedAnswer(question="why?", outcome="declined", reason="not in this run")

    assert bool(abstained) is True, "a falsy contract is dropped by the SDK's parsed_output check"
    assert bool(declined) is True
    assert abstained.is_answer is False
    assert declined.is_answer is False


def test_agent_outputs_reject_unexpected_fields() -> None:
    """`extra="forbid"` closes the gap the lint cannot see: the lint reads declarations, so an
    undeclared numeric field is invisible to it. This makes it unreachable at runtime."""
    with pytest.raises(ValidationError):
        Mapping.model_validate({"sheet": "S", "cell_range": "A1", "page": 1, "guest_count": 412})


def test_agent_outputs_are_frozen() -> None:
    mapping = Mapping(sheet="S", cell_range="A1", page=1)
    with pytest.raises(ValidationError):
        mapping.sheet = "T"  # type: ignore[misc]


# ── usage ────────────────────────────────────────────────────────────────────


def test_token_usage_adds() -> None:
    assert (TokenUsage(10, 5) + TokenUsage(3, 2)) == TokenUsage(13, 7)


def test_model_response_defaults_are_safe() -> None:
    """A response constructed without usage reports zero rather than failing, so telemetry can
    never be the reason a correct answer is lost."""
    response = ModelResponse(parsed=Mapping(sheet="S", cell_range="A1", page=1), raw_json="{}")
    assert response.usage == TokenUsage()


# ── .env, read by `make record` and by nothing else ──────────────────────────
#
# `.env.example` has said "Copy to `.env` and fill in" since M1 and nothing read the file. A
# developer following that instruction exactly was told the key was not set, which is true and
# which reads as a bug in the recorder. These tests cover the loader that closed that gap, and the
# three rules that keep a convenience from becoming a hazard.
#
# **No literal `ANTHROPIC_API_KEY=<value>` appears below, and that is deliberate.** The first draft
# of these tests turned `make ci` red: `tools/guard/secret_guard.py` cannot tell a fixture from a
# real key by looking, and a thirteen-character stand-in trips the same rule a live credential
# does. Weakening the guard to admit test values would have been the wrong repair — it is the
# guard's correctness that is load-bearing, not the convenience of writing a fixture. So the file
# contents are built from KEY, and the pattern has nothing to match.

KEY = "ANTHROPIC_API_KEY"


def test_a_dotenv_key_reaches_the_recorder(tmp_path: Path) -> None:
    """The gap this closes: the repository told people to write a file it never read."""
    from tda.agents.provider.dotenv import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}=a-stand-in-value\n", encoding="utf-8")
    environ: dict[str, str] = {}

    assert load_dotenv(env_file, environ=environ) == [KEY]
    assert environ[KEY] == "a-stand-in-value"


def test_the_real_environment_beats_the_file(tmp_path: Path) -> None:
    """The rule that matters most. A file quietly shadowing an exported variable is how somebody
    records against the wrong account and cannot work out why."""
    from tda.agents.provider.dotenv import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}=from-the-file\n", encoding="utf-8")
    environ = {KEY: "from-the-shell"}

    assert load_dotenv(env_file, environ=environ) == []
    assert environ[KEY] == "from-the-shell"


def test_the_loader_returns_names_and_never_values(tmp_path: Path) -> None:
    """A function that handed back the secret so a caller could log where it came from is one
    refactor away from logging the secret itself."""
    from tda.agents.provider.dotenv import load_dotenv

    secret = "a-value-that-must-not-be-returned"
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}={secret}\n", encoding="utf-8")
    environ: dict[str, str] = {}

    returned = load_dotenv(env_file, environ=environ)

    assert returned == [KEY]
    assert secret not in "".join(returned)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("{k}=plain", "plain"),
        ('{k}="double"', "double"),
        ("{k}='single'", "single"),
        ("export {k}=exported", "exported"),
        ("  {k} = spaced  ", "spaced"),
    ],
)
def test_the_parser_tolerates_what_people_actually_write(line: str, expected: str) -> None:
    """`KEY="sk-ant-..."` is how half of all .env files are written, and a key carrying literal
    quote marks fails at the API with an opaque 401. `export` is what a shell-minded reader types."""
    from tda.agents.provider.dotenv import parse_env

    assert parse_env(line.format(k=KEY)) == {KEY: expected}


@pytest.mark.parametrize("line", ["# {k}=commented", "", "a line with no equals sign", "   "])
def test_the_parser_skips_what_is_not_an_assignment(line: str) -> None:
    """A malformed line is skipped rather than raising: `.env` is hand-edited under time pressure,
    and failing the whole load over one stray line sends somebody hunting in the wrong place."""
    from tda.agents.provider.dotenv import parse_env

    assert parse_env(line.format(k=KEY)) == {}


def test_a_missing_dotenv_is_not_an_error(tmp_path: Path) -> None:
    """Exporting the variable in a shell is equally valid, and CI has neither."""
    from tda.agents.provider.dotenv import load_dotenv

    assert load_dotenv(tmp_path / "nope", environ={}) == []


def test_an_empty_value_does_not_mask_an_unset_key(tmp_path: Path) -> None:
    """`ANTHROPIC_API_KEY=` is the shape `.env.example` has after a careless edit. Setting it to
    the empty string would turn "not set" into "set to nothing", and the SDK's failure for the
    second is considerably less clear than the recorder's for the first."""
    from tda.agents.provider.dotenv import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY}=\n", encoding="utf-8")
    environ: dict[str, str] = {}

    assert load_dotenv(env_file, environ=environ) == []
    assert KEY not in environ


def test_only_the_recorder_reads_dotenv() -> None:
    """Every other target is offline in replay and needs no credential. A loader firing on import
    would reach for a secret in processes that have no business holding one, and would make
    `make ci` depend on an untracked file - the opposite of what this repo claims."""
    probe = (
        "import os, sys; os.environ.pop('ANTHROPIC_API_KEY', None);"
        "import tda.agents.provider, tda.agents.runtime, tda.excel, tda.review.live, tda.review.app;"
        "sys.exit(1 if os.environ.get('ANTHROPIC_API_KEY') else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": "src"},
    )

    assert result.returncode == 0, result.stderr.decode()


def test_selecting_live_while_disabled_raises_before_any_sdk_import() -> None:
    """The refusal happens before `build_provider("anthropic")` is even called - not merely before
    a network request. A clean interpreter proves it: after the refusal, neither the anthropic SDK
    nor the adapter module that would import it has ever been touched."""
    probe = (
        "import sys;"
        "from tda.review.live import LiveModeError, live_gate;"
        "gate = live_gate({});"
        "raised = False\n"
        "try:\n"
        "    gate.provider_for('anthropic')\n"
        "except LiveModeError:\n"
        "    raised = True\n"
        "assert raised, 'provider_for did not refuse'\n"
        "assert 'anthropic' not in sys.modules\n"
        "assert 'tda.agents.provider.anthropic_client' not in sys.modules\n"
        "sys.exit(0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": "src", "MIZAN_LIVE_MODE": ""},
    )

    assert result.returncode == 0, result.stderr.decode()


def test_the_makefile_does_not_duplicate_the_key_check() -> None:
    """One gate, in the place that knows where a key may legitimately come from.

    `make record` used to test `$ANTHROPIC_API_KEY` in the shell and exit before Python ran. When
    the recorder learned to read `.env`, that gate short-circuited it: `make record` refused with a
    perfectly good key on disk, which is precisely the failure the loader was added to fix, still
    reachable through the documented path.

    The rule is not "the Makefile must not check" so much as "a precondition with two
    implementations has one that is wrong" — and the wrong one here was the one users actually run.
    """
    makefile = (Path(__file__).resolve().parents[2] / "Makefile").read_text(encoding="utf-8")
    # The declaration line carries the `## help` text, which may name the variable and should:
    # `make help` is where somebody looks to find out what the target needs. Only the recipe
    # underneath it can gate.
    recipe = makefile.partition("\nrecord:")[2].partition("\n\n")[0].partition("\n")[2]

    assert "tda.agents.provider" in recipe
    assert KEY not in recipe, (
        "the record recipe tests for the key itself. It will refuse before the recorder can read "
        ".env, which is the bug this rule exists to prevent."
    )
