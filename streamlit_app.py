"""The entrypoint Streamlit execs, locally or on Streamlit Community Cloud.

`streamlit run streamlit_app.py` from a plain git checkout, with no editable install: this file
puts `src/` and `tools/` on `sys.path` itself, because every path this codebase resolves from
`__file__` assumes the checkout layout (`policy.yaml`, `corpus/`, `docs/`, `tests/cassettes/` one
level above `src/tda/`) rather than a site-packages install. A hosted deployment has no `pip
install -e .` step to rely on for that; this file is what makes one unnecessary.

Two pages, through `st.navigation`: the Run console (`tda.review.console`) and the officer's
Review screen (`tda.review.app`), sharing one `st.set_page_config` call - Streamlit allows exactly
one per script run, which is why `tda.review.app.main` takes `configure_page=False` here.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent
for _folder in ("src", "tools"):
    _path = str(REPO_ROOT / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import streamlit as st  # noqa: E402

from tda.review import app as review_app  # noqa: E402
from tda.review import console, explain  # noqa: E402
from tda.review.scenes import catalogue  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable

    from tda.review.scenes import SceneCatalogue

_FIXTURE_LOCK = threading.Lock()


@st.cache_resource(show_spinner="Preparing the scenes...")
def ensure_fixtures() -> Path:
    """`corpus/fixtures/F2` and `F3`, built once per server process rather than once per viewer.

    `st.cache_resource` already memoises across sessions in one process; the lock is a second,
    cheaper guard against two sessions racing the very first call before the cache has a result to
    return - `materialise_all` deletes and rebuilds each fixture directory, and two concurrent
    builds of the same directory is not a race worth risking for the cost of a lock.
    """
    from fixtures.materialise import materialise_all

    out = REPO_ROOT / "corpus" / "fixtures"
    with _FIXTURE_LOCK:
        if not all((out / name / "submission").is_dir() for name in ("F2", "F3")):
            materialise_all(demo=REPO_ROOT / "corpus" / "demo", out=out)
    return out


def _refusal_builder() -> Callable[[Path], Path] | None:
    """`tools.demo.run_demo.build_refusal_submission`, or `None` when `reportlab` - the `datagen`
    extra it needs to re-render a report - is not installed. Four working scenes beat a page that
    refuses to open over a fifth."""
    try:
        from demo.run_demo import build_refusal_submission
    except ImportError:
        return None
    return build_refusal_submission


def _catalogue() -> SceneCatalogue:
    return catalogue(
        demo_submission=REPO_ROOT / "corpus" / "demo" / "submission",
        fixtures_root=ensure_fixtures(),
        refusal_builder=_refusal_builder(),
    )


def _run_page() -> None:
    console.render(
        _catalogue(),
        review_page=REVIEW_PAGE,
        artifacts_root=review_app.ARTIFACTS,
    )


RUN_PAGE = st.Page(_run_page, title="Verify a return", url_path="run", default=True)
REVIEW_PAGE = st.Page(review_app.page, title="Review a verdict", url_path="review")
# Third, and not an afterthought: this demo is a link somebody opens cold, and a stranger will not
# read a paragraph of small print but will click a tab. It is also where both responsible-AI
# disclosures live verbatim, so moving them off the Run page's first paint did not move them out
# of the app.
EXPLAIN_PAGE = st.Page(explain.render, title="How it works", url_path="how")

st.set_page_config(page_title="Mizan", page_icon="⚖️", layout="wide")
st.navigation([RUN_PAGE, REVIEW_PAGE, EXPLAIN_PAGE]).run()
