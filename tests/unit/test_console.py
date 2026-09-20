"""Source-level checks over the whole `tda.review` package and `streamlit_app.py`.

Some rules are easier to state as "this string does not appear in this file" than to derive from
behaviour, and cheaper to keep true that way too - a source scan runs in milliseconds and cannot be
fooled by a code path a behavioural test happened not to exercise. Three rules live here:

**Only `tda.review.live` names the three live-mode variables**, so an accidental second reader
cannot drift out of step with the one place that knows the rules for reading them.

**Nothing under `tda.review` loads `.env`.** The rule is `tda.agents.provider.dotenv`'s own
(`make record` alone reads it), and a console running as a long-lived hosted process is exactly the
kind of process that must not go looking for a credential on disk.

**Nothing under `tda.review` renders the process environment wholesale.** `os.environ.get(...)` for
a named, documented variable is fine and everywhere; `st.write(os.environ)`, `st.json(st.secrets)`,
`dict(os.environ)` and the like are not, because they would put every secret on the page at once.

Glob-based over `src/tda/review/*.py` rather than a fixed file list, so a module added later
(`scenes.py`, `staging.py`, `console.py`, ...) is covered automatically rather than by remembering
to add a line here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REVIEW_PACKAGE = REPO_ROOT / "src" / "tda" / "review"
ENTRYPOINT = REPO_ROOT / "streamlit_app.py"

# Files allowed to name a live-mode environment variable directly. Everything else in the console
# reads live-ness through `tda.review.live`'s functions, not through `os.environ` itself.
LIVE_VARIABLE_NAMES = ("MIZAN_LIVE_MODE", "MIZAN_LIVE_RUNS_PER_SESSION", "ANTHROPIC_API_KEY")
ALLOWED_TO_NAME_THEM = {REVIEW_PACKAGE / "live.py"}

# Every documented, narrow `os.environ.get(...)` call already in the codebase. A file naming one of
# these is reading one variable by name, which is the opposite of rendering the environment.
# `tda.review.app.env_path` is the one indirection allowed: it reads a single variable named by its
# caller rather than by a literal here, so it is checked separately below, by call site.
KNOWN_ENV_NAMES = (
    "MIZAN_ARTIFACTS",
    "MIZAN_SUBMISSION",
    "MIZAN_RUN",
    "MIZAN_LIVE_MODE",
    "MIZAN_LIVE_RUNS_PER_SESSION",
    "ANTHROPIC_API_KEY",
)
DOCUMENTED_ENV_READS = re.compile(
    r"os\.environ\.get\(\s*(?:name\s*,|[\"'](?:" + "|".join(KNOWN_ENV_NAMES) + r")[\"'])"
)
ENV_PATH_CALL = re.compile(r"\benv_path\(\s*[\"'](\w+)[\"']")

# Statement shapes that would load .env or put the whole environment on the page. Matched against
# real code syntax rather than a bare word, so a docstring explaining *why a file does not do this*
# - which necessarily names the very things it is disclaiming, the same problem
# `tools/guard/secret_guard.py`'s own docstring notes about itself - is not itself a violation.
DOTENV_IMPORT = re.compile(r"^\s*(?:from\s+\S*dotenv\S*\s+import|import\s+\S*dotenv)", re.MULTILINE)
ENVIRONMENT_RENDERING_SHAPES = (
    "st.write(os.environ",
    "st.json(os.environ",
    "st.json(st.secrets",
    "dict(os.environ)",
    "dict(os.environ.items",
    "os.environ.items()",
    "os.environ.copy(",
    "vars(os.environ)",
    "st.secrets.to_dict(",
)

# tda.review.sandbox copies os.environ to build a *subprocess's* environment - inherited by
# mizan run, running as a genuinely separate process, never rendered by this one. A sandboxed
# upload still needs ANTHROPIC_API_KEY to reach its own live call when live mode is on, the same
# variable every other live call in this codebase already depends on, so the copy is required, not
# incidental. What this test exists to catch is os.environ reaching a Streamlit call; a
# subprocess's environment is a different thing entirely, never displayed anywhere.
ALLOWED_TO_COPY_THE_ENVIRONMENT = {REVIEW_PACKAGE / "sandbox.py"}


def _review_files() -> list[Path]:
    files = sorted(REVIEW_PACKAGE.glob("*.py"))
    if ENTRYPOINT.is_file():
        files.append(ENTRYPOINT)
    return files


def test_only_live_py_names_a_live_mode_variable() -> None:
    for path in _review_files():
        if path in ALLOWED_TO_NAME_THEM:
            continue
        text = path.read_text(encoding="utf-8")
        for name in LIVE_VARIABLE_NAMES:
            assert name not in text, (
                f"{path.relative_to(REPO_ROOT)} names {name!r} directly. Live-mode state should be "
                "read through tda.review.live, the one place that knows the rules for it."
            )


def test_the_console_never_loads_dotenv() -> None:
    for path in _review_files():
        text = path.read_text(encoding="utf-8")
        assert not DOTENV_IMPORT.search(text), (
            f"{path.relative_to(REPO_ROOT)} imports dotenv. Only make record's two entrypoints "
            "read .env; a long-lived console process must not go looking for one."
        )


def test_the_console_never_renders_the_environment_wholesale() -> None:
    for path in _review_files():
        text = path.read_text(encoding="utf-8")

        # Every literal os.environ.get(...) call must be one of the documented, named reads.
        for match in re.finditer(r"os\.environ\.get\([^)]*\)", text):
            assert DOCUMENTED_ENV_READS.match(match.group(0)), (
                f"{path.relative_to(REPO_ROOT)} has an undocumented os.environ.get(...) call: "
                f"{match.group(0)!r}. Add the variable name to this test's allowlist if it is "
                "meant to be read directly, or route it through tda.review.live if it is live-mode "
                "state."
            )

        for shape in ENVIRONMENT_RENDERING_SHAPES:
            if shape == "dict(os.environ)" and path in ALLOWED_TO_COPY_THE_ENVIRONMENT:
                continue
            assert shape not in text, (
                f"{path.relative_to(REPO_ROOT)} contains {shape!r}, which would put the process "
                "environment or the secrets store on the page."
            )

        # env_path's own generic os.environ.get(name, ...) is sanctioned - but only when every
        # call site names a literal, known variable. A call passing a computed name would defeat
        # the whole point of the check above.
        for match in ENV_PATH_CALL.finditer(text):
            name = match.group(1)
            assert name in KNOWN_ENV_NAMES, (
                f"{path.relative_to(REPO_ROOT)} calls env_path({name!r}, ...), which this test "
                "does not recognise. Add it to KNOWN_ENV_NAMES if it is a real, documented "
                "variable."
            )


STREAMLIT_IMPORT = re.compile(
    r"^\s*(?:from\s+streamlit\S*\s+import|import\s+streamlit\b)", re.MULTILINE
)


def test_the_file_allowed_to_copy_the_environment_cannot_render_it() -> None:
    """What makes `ALLOWED_TO_COPY_THE_ENVIRONMENT` safe: the one file on it never imports
    Streamlit at all, so `dict(os.environ)` there has nothing capable of displaying it - a
    structural guarantee this pins, rather than a comment asking to be trusted. Matched as an
    import statement, not a bare word, the same reasoning `DOTENV_IMPORT` above already states:
    this file's own docstring names Streamlit in prose, explaining exactly why it cannot use it.
    """
    for path in ALLOWED_TO_COPY_THE_ENVIRONMENT:
        text = path.read_text(encoding="utf-8")
        assert not STREAMLIT_IMPORT.search(text), (
            f"{path.relative_to(REPO_ROOT)} is allowed to copy os.environ on the understanding "
            "that it cannot render anything - importing streamlit would break that."
        )
