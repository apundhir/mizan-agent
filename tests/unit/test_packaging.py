"""The version numbers this project stamps into things, and the one that had drifted.

Three version strings reach an artefact and a reader will compare them: the package version, the
policy version, and the metric library version. Two of the three were already pinned by tests. This
file pins the third and closes the gap that let it move.

`tda.__version__` read "0.1.0" at the v0.5.0 tag, four releases behind, and **nothing referenced it
at all**, so no test and no guard could notice. A version that nobody reads is harmless right up to
the release where somebody puts it in a bundle filename.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import tda
import tda.metrics

ROOT = Path(__file__).resolve().parents[2]


def _declared_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version: str = tomllib.load(handle)["project"]["version"]
    return version


def test_the_package_version_matches_the_one_pyproject_declares() -> None:
    """Two sources of truth for one number, so something has to hold them together.

    Read from `pyproject.toml` rather than from installed metadata: an editable install serves
    whatever was built at install time, so a bump nobody reinstalled after would still pass and the
    test would be measuring the wheel rather than the repository.
    """
    assert tda.__version__ == _declared_version()


def test_the_metric_library_version_is_defined_in_exactly_one_place() -> None:
    """The shadow that made this worth a test.

    `tda/__init__.py` carried its own `METRIC_LIBRARY_VERSION = "0.1.0"` while `tda.metrics` had
    moved to "1.0.0". Nothing imported the copy, so the two never had to agree and the drift was
    invisible. The danger was never the dead constant: `from tda import METRIC_LIBRARY_VERSION`
    reads entirely natural, and the first caller to write it would have stamped a superseded
    version onto a verdict produced by the current library, which is the exact claim D-EV-04 makes
    checkable. The constant belongs to the library that knows when a metric moved.
    """
    assert not hasattr(tda, "METRIC_LIBRARY_VERSION"), (
        "METRIC_LIBRARY_VERSION belongs to tda.metrics alone. A second definition on the package "
        "root can disagree with it, and `from tda import ...` would not look wrong to a reviewer."
    )
    assert tda.metrics.METRIC_LIBRARY_VERSION


def test_the_version_a_verdict_records_comes_from_the_library_that_computed_it() -> None:
    """Both stamping sites must take the number from `tda.metrics` and nowhere else.

    Asserted against the import statement rather than the imported value, because the value would
    compare equal to any copy that happened to hold the same string. What needs pinning is where
    the number came from. `tests/unit/test_reviewer_assist.py` reads source text for the same
    reason: some guarantees are about the code, not about a result it produces.
    """
    wanted = "from tda.metrics import METRIC_LIBRARY_VERSION"
    for module in ("graph/run.py", "cli.py"):
        source = (ROOT / "src" / "tda" / module).read_text(encoding="utf-8")
        assert "metric_library_version=METRIC_LIBRARY_VERSION" in source, module
        assert wanted in source, f"{module} must take the version from tda.metrics"
