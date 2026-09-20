"""The guards must fail on a deliberate violation.

This is the load-bearing test file in the repository. ADR-0001 claims no model output can reach
a metric function, and the import guard plus the schema lint are the enforcement. A guard that
has never been watched reject anything is indistinguishable from a guard with a bug in it, so
each rule below is fed code that breaks it and asserted to fail — by exit code *and* by the
message naming the offending file, because a guard that fails without saying why costs the next
engineer an hour.

Every fixture under `fixtures/` is a minimal repo tree containing exactly one violation. They
are deliberately realistic: each one is the shortcut somebody would actually take at 6pm before
a demo, which is when this matters.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "guard"))

import agent_schema_lint
import import_guard
import secret_guard

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.arch


# ── the import guard ─────────────────────────────────────────────────────────

VIOLATIONS = [
    pytest.param(
        "model_client_in_metrics",
        "occupancy.py",
        id="metrics imports the model client directly",
    ),
    pytest.param(
        "agent_import_in_reconcile",
        "classify.py",
        id="reconcile asks an agent to classify a variance",
    ),
    pytest.param(
        "io_in_metrics",
        "inventory.py",
        id="a pure metric function reads a file from disk",
    ),
    pytest.param(
        "transitive_model_client",
        "nationality.py",
        id="metrics reaches the model client through a helper",
    ),
    pytest.param(
        "datagen_imports_metrics",
        "aggregate.py",
        id="the generator reuses the product metric library",
    ),
    pytest.param(
        "datagen_imports_contracts",
        "apportion.py",
        id="the generator reaches for 'just the contracts' instead",
    ),
    pytest.param(
        "product_imports_datagen",
        "columns.py",
        id="the extractor reads its column map out of the generator",
    ),
    pytest.param(
        "fixtures_imports_tda",
        "derive.py",
        id="the fixture builder derives its expectation with the code under test",
    ),
]


@pytest.mark.parametrize(("fixture", "offending_file"), VIOLATIONS)
def test_import_guard_rejects(
    fixture: str, offending_file: str, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = import_guard.main(["--root", str(FIXTURES / fixture)])

    assert exit_code == 1, (
        f"the import guard ACCEPTED {fixture}. Every rule in ADR-0001 rests on this guard "
        "failing here; a passing run means the architectural claim is unenforced."
    )
    message = capsys.readouterr().err
    assert offending_file in message, (
        f"the guard failed but did not name {offending_file}. A guard that fails without saying "
        f"where costs the next engineer an hour. Got:\n{message}"
    )
    assert "why it matters" in message, "each violation must explain itself, not just flag itself"


def test_import_guard_accepts_a_compliant_module(capsys: pytest.CaptureFixture[str]) -> None:
    """The control. A guard that rejects everything is as useless as one that rejects nothing —
    and much more annoying, because it teaches people to bypass it."""
    exit_code = import_guard.main(["--root", str(FIXTURES / "clean")])

    assert exit_code == 0, f"the guard rejected a compliant module:\n{capsys.readouterr().err}"


def test_import_guard_passes_on_the_real_repository() -> None:
    """The guard runs against this repo on every push, so it must be green here now."""
    assert import_guard.main(["--root", str(REPO_ROOT), "--quiet"]) == 0


def test_every_rule_has_a_violation_fixture() -> None:
    """A guard rule with no fixture is a rule nobody has proven.

    Counting fixtures would not do: two of them exercise extra branches of the metrics rule
    (transitive reach, and I/O) rather than rules of their own. So this checks coverage per
    guarded package, which is what actually matters — add a rule without a fixture and it
    fails here rather than silently shipping a guard branch that has never executed.
    """
    covered: set[str] = set()
    for param in VIOLATIONS:
        fixture_root = FIXTURES / str(param.values[0])
        covered |= {r.package for r in import_guard.RULES if (fixture_root / r.package).is_dir()}

    missing = {r.package for r in import_guard.RULES} - covered
    assert not missing, (
        f"guard rules with no violation fixture: {sorted(missing)}. A guard nobody has watched "
        "reject anything is a comment, not a guard."
    )


# ── the agent schema lint ────────────────────────────────────────────────────


SCHEMA_VIOLATIONS = [
    pytest.param(
        "numeric_agent_field",
        "guest_count",
        "numeric field",
        id="an agent contract declares an integer count",
    ),
    pytest.param(
        "unmarked_contract",
        "ResolutionResult",
        "not derived from AgentOutput",
        id="a contract dodges the rule by not deriving from AgentOutput",
    ),
    pytest.param(
        "nested_numeric_field",
        "minor_units",
        "Reached from an agent contract by",
        id="a contract declares no number and points at a model that does",
    ),
]


@pytest.mark.parametrize(("fixture", "offender", "expected_reason"), SCHEMA_VIOLATIONS)
def test_schema_lint_rejects(
    fixture: str, offender: str, expected_reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two distinct failures, and each must report its own reason.

    The numeric case is the obvious one: `guest_count: int` is one word and makes a model the
    source of a number. The unmarked case is the dodge somebody reaches for *after* the lint has
    blocked them once — declare the contract as a bare `BaseModel` and the numeric rule no longer
    applies. Reporting the second with the first's message would send a reader hunting for a
    number that is not there.
    """
    exit_code = agent_schema_lint.main(["--root", str(FIXTURES / fixture), "--package", "src"])

    assert exit_code == 1, (
        f"the schema lint ACCEPTED {fixture}. This is the route into the numeric path that the "
        "import guard cannot see."
    )
    message = capsys.readouterr().err
    assert offender in message, f"the lint must name the offender. Got:\n{message}"
    assert expected_reason in message, (
        f"the lint reported the wrong reason for {fixture}. Got:\n{message}"
    )


def test_schema_lint_catches_numerics_inside_containers(capsys: pytest.CaptureFixture[str]) -> None:
    """`counts: list[int]` and `occupancy: Decimal | None` are the same violation as `n: int`.

    Worth its own assertion because the obvious implementation — matching the annotation string
    against a set of type names — passes the simple case and misses both of these.
    """
    agent_schema_lint.main(["--root", str(FIXTURES / "numeric_agent_field"), "--package", "src"])
    message = capsys.readouterr().err

    assert "list[int]" in message
    assert "Decimal | None" in message


def test_schema_lint_names_the_path_from_the_contract_to_the_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A violation in another package is only actionable if the message says how a contract
    reaches it. "Money.amount: float" on its own sends a reader to a file that is not wrong."""
    agent_schema_lint.main(["--root", str(FIXTURES / "nested_numeric_field"), "--package", "src"])
    message = capsys.readouterr().err

    assert "Quote.total" in message
    assert "Money.amount: float" in message


def test_schema_lint_still_allows_a_citation_reference_on_a_contract() -> None:
    """The check must not be strict by being useless. `PdfCitation.ref: PdfRef` is the pattern the
    recursion was written for, and `PdfRef` carries `page`, `row_start` and `row_end` - citations
    rather than quantities. If this ever fails, the whitelist stopped applying through nesting and
    every reviewer-assist citation became a violation."""
    assert agent_schema_lint.main(["--quiet"]) == 0


def test_schema_lint_accepts_a_compliant_contract(capsys: pytest.CaptureFixture[str]) -> None:
    """The control. Keys, references, citation integers and booleans — a contract that does
    everything an agent legitimately does.

    A guard that rejects everything is as useless as one that rejects nothing and much more
    annoying, because it teaches people to work around it.
    """
    exit_code = agent_schema_lint.main(
        ["--root", str(FIXTURES / "clean_agent_contract"), "--package", "src"]
    )

    assert exit_code == 0, f"the lint rejected a compliant contract:\n{capsys.readouterr().err}"


def test_schema_lint_does_not_flag_telemetry(capsys: pytest.CaptureFixture[str]) -> None:
    """`tda.obs.AgentUsage` carries token counts as integers, and must not be flagged.

    This is why the lint targets the `AgentOutput` marker rather than scanning a package: a count
    of what a call cost is not an agent's answer. Scoping the rule by directory would have forced
    a choice between weakening the guard and mislabelling the data.
    """
    exit_code = agent_schema_lint.main(["--root", str(REPO_ROOT), "--package", "src"])

    assert exit_code == 0, capsys.readouterr().err
    from tda.obs import AgentUsage

    assert "input_tokens" in AgentUsage.model_fields, "the telemetry this test guards still exists"


def test_schema_lint_passes_on_the_real_repository() -> None:
    assert agent_schema_lint.main(["--root", str(REPO_ROOT), "--quiet"]) == 0


def test_schema_lint_whitelist_is_citations_only() -> None:
    """Pin the whitelist.

    Widening it is how this guard dies: `count` looks as harmless as `page` in a diff, and it is
    the entire thing being prevented. Changing this set should require changing this test, and
    changing this test should require an argument.
    """
    assert frozenset({"page", "row", "row_start", "row_end"}) == agent_schema_lint.ALLOWED_FIELDS


def test_schema_lint_knows_the_numeric_types() -> None:
    assert {"int", "Decimal", "float"} <= agent_schema_lint.NUMERIC_NAMES


# ── the secret guard ─────────────────────────────────────────────────────────


# A credential-shaped string, assembled here rather than committed. The fixture's `.template` carries
# a placeholder and this fills it in at runtime, so nothing matchable is ever a tracked file — see the
# template's docstring for why an allowlist would have been the worse fix.
PLANTED_KEY = "sk-ant-api03-" + "EXAMPLE" * 4


def _as_repository(source: Path, destination: Path, *, plant: bool = True) -> Path:
    """Copy a fixture into a real git repository, fill in the template, and stage it.

    The secret guard scans `git ls-files`, because what git tracks is what leaves the machine. So the
    fixture has to be a repository — created here, in `tmp_path`, rather than committed as a nested
    `.git` inside this one.
    """
    shutil.copytree(source, destination)

    template = destination / "planted_key.py.template"
    if plant:
        (destination / "planted_key.py").write_text(
            template.read_text(encoding="utf-8").replace("@@KEY@@", PLANTED_KEY), encoding="utf-8"
        )
    template.unlink()

    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(destination), *argv], check=True, capture_output=True)
    return destination


def test_the_secret_guard_rejects_a_tracked_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The paste that happens once, at six in the evening, to check a call works.

    Nobody commits a key deliberately. They add it to a source file to see whether the thing works at
    all, intend to move it to the environment, and then the branch gets pushed — so the guard exists
    *before* any key does. A guard added after the first recording session was absent for the one
    commit that mattered.
    """
    root = _as_repository(FIXTURES / "tracked_secret", tmp_path / "repo")

    exit_code = secret_guard.main(["--root", str(root)])

    assert exit_code == 1, "the secret guard ACCEPTED a tracked Anthropic key"
    message = capsys.readouterr().err
    assert "planted_key.py" in message, f"the guard must name the file. Got:\n{message}"
    assert "Revoke it" in message, "a key in git history is burned; the advice has to say so"


def test_the_secret_guard_does_not_flag_a_committed_env_example(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control, and the one that decides whether this guard survives.

    `.env.example` is *meant* to be committed, and it names `ANTHROPIC_API_KEY` with a placeholder. A
    guard that flagged correct setup would be switched off within a week — and then the real leak would
    go unnoticed. So the placeholder set is narrow and this asserts the line passes.
    """
    root = _as_repository(FIXTURES / "tracked_secret", tmp_path / "repo", plant=False)

    exit_code = secret_guard.main(["--root", str(root)])

    assert exit_code == 0, (
        "the guard flagged a committed .env.example carrying only a placeholder:\n"
        f"{capsys.readouterr().err}"
    )


def test_the_secret_guard_fails_loudly_outside_a_repository(tmp_path: Path) -> None:
    """A guard that silently finds no files reports success, which is worse than no guard."""
    assert secret_guard.main(["--root", str(tmp_path)]) == 2


def test_the_secret_guard_passes_on_the_real_repository() -> None:
    """It runs in `make ci` on every push, so it must be green here now."""
    assert secret_guard.main(["--root", str(REPO_ROOT), "--quiet"]) == 0


def test_the_guards_fixtures_carry_no_credential_of_their_own() -> None:
    """The fixture that caught me out, pinned.

    The first version of the violation fixture was a real `.py` carrying a credential-shaped literal,
    and `make ci` went red on the commit that added it — the guard caught its own fixture, because a
    fixture is a tracked file and "a key pasted into a test fixture" is one of the threats in the
    guard's own docstring.

    The tempting fix is an allowlist exempting the fixtures directory. That is worse: it is a hole that
    needs maintaining, and it would exempt exactly the directory a careless paste is most likely to
    land in. So the tracked file is a `.template` with a placeholder, and this asserts no exemption
    was quietly added instead.
    """
    assert secret_guard.SKIP_SUFFIXES.isdisjoint(
        {".py", ".yaml", ".yml", ".json", ".md", ".txt"}
    ), "the guard must not skip text formats - that is where a pasted key lands"
    assert not (FIXTURES / "tracked_secret" / "planted_key.py").exists(), (
        "the violation fixture must stay a .template; a tracked .py here would fail the guard it tests"
    )


def test_the_secret_guard_covers_the_anthropic_key_formats() -> None:
    """Pin the patterns. Narrowing this set is how the guard quietly stops guarding."""
    names = {pattern.name for pattern in secret_guard.PATTERNS}

    assert "Anthropic API key" in names
    assert "Anthropic admin key" in names
    assert secret_guard.PATTERNS[0].regex.search("sk-ant-api03-" + "A" * 24)
    assert not secret_guard.PATTERNS[0].regex.search("sk-ant-xxx")


# ── the path pattern: a secrets file is a finding whatever it contains ───────


def _bare_repository(tmp_path: Path, files: dict[str, str]) -> Path:
    """A minimal git repository holding exactly the files named, all tracked.

    Separate from `_as_repository`, which copies a fixed fixture tree: these tests plant a specific
    path rather than a specific credential, and the path is the whole point of each one.
    """
    root = tmp_path / "repo"
    root.mkdir()
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for argv in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)
    return root


def test_the_secret_guard_rejects_a_tracked_secrets_toml_even_with_no_key_in_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The file itself is the finding. A `secrets.toml` holding only `MIZAN_LIVE_MODE = "false"`
    is still a file that exists to hold a credential, and a developer testing live mode locally is
    one `git add -A` away from tracking the version that does hold one."""
    root = _bare_repository(tmp_path, {".streamlit/secrets.toml": 'MIZAN_LIVE_MODE = "false"\n'})

    exit_code = secret_guard.main(["--root", str(root)])

    assert exit_code == 1
    message = capsys.readouterr().err
    assert ".streamlit/secrets.toml" in message
    assert "Secrets settings" in message


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.local.bak", "deploy/secrets.toml"])
def test_the_secret_guard_rejects_any_tracked_env_file_except_the_example(
    tmp_path: Path, name: str
) -> None:
    root = _bare_repository(tmp_path, {name: "placeholder\n"})

    assert secret_guard.main(["--root", str(root), "--quiet"]) == 1


def test_a_tracked_streamlit_config_is_not_flagged(tmp_path: Path) -> None:
    """The control. `config.toml` holds server settings, not secrets, and must stay committed."""
    root = _bare_repository(tmp_path, {".streamlit/config.toml": "[server]\nheadless = true\n"})

    assert secret_guard.main(["--root", str(root), "--quiet"]) == 0


def test_a_toml_style_key_assignment_in_a_tracked_note_is_caught(tmp_path: Path) -> None:
    """The content pattern already matches the TOML form, not only `KEY=value`. Checked here
    rather than assumed, since the console's secrets are written as `NAME = "value"`."""
    root = _bare_repository(tmp_path, {"notes.md": f'ANTHROPIC_API_KEY = "{PLANTED_KEY}"\n'})

    assert secret_guard.main(["--root", str(root), "--quiet"]) == 1


def test_the_artifact_redactor_and_the_secret_guard_agree_on_what_a_key_looks_like() -> None:
    """One shape, checked in two places for two different reasons: the guard stops a key reaching
    the repository, `tda.obs.redact` stops one reaching an artifact. Divergence here would mean a
    key that one catches and the other misses."""
    from tda.obs.redact import BY_NAME

    assert BY_NAME["api_key"].regex.pattern == secret_guard.PATTERNS[0].regex.pattern
