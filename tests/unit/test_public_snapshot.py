"""The release audit, which is the last thing standing between this repository and a public one.

The tool had no test until the day an audit of the published repository found three internal
documents still fetchable from the first public commit. They had been denied from the tip and
nothing checked the tip against anything else, because a path denylist only knows about paths.

So these tests are about the guard failing, not the guard passing. A clean tree producing no
problems proves only that `audit` did not crash. Every case below plants something and asserts the
release is refused, which is the behaviour the tool exists for.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "release"))

from public_snapshot import EXCLUDED, audit


def tree_with(tmp_path: Path, **files: str) -> Path:
    """An exported tree carrying the given files. Directories are created as needed."""
    root = tmp_path / "export"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text("# Mizan\n\nA reference implementation.\n", encoding="utf-8")
    for name, body in files.items():
        path = root / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def test_a_clean_tree_is_publishable(tmp_path: Path) -> None:
    """The control. Without it, a rule with a broken pattern would look like a passing audit."""
    assert audit(tree_with(tmp_path)) == []


def test_an_excluded_document_refuses_the_release(tmp_path: Path) -> None:
    """The original path rule. `docs/00-BUILD-PLAN.md` names a client PRD as its source."""
    tree = tree_with(tmp_path, **{EXCLUDED[0].replace("/", "__"): "# build plan\n"})

    problems = audit(tree)

    assert any("excluded path present" in p for p in problems)


def test_a_credential_file_refuses_the_release(tmp_path: Path) -> None:
    """Belt and braces. `.env` is gitignored, so this can only fire if something force-added it."""
    tree = tree_with(tmp_path, **{".env": "ANTHROPIC_API_KEY=whatever\n"})

    assert any("credential file present" in p for p in audit(tree))


def test_an_internal_tracker_key_refuses_the_release(tmp_path: Path) -> None:
    """The rule that would have caught this scrub regressing.

    A tracker key discloses the backlog's shape and numbering. It is also the thing most likely to
    come back, because it arrives one changelog entry at a time rather than as a new document.
    """
    tree = tree_with(tmp_path, **{"docs__new.md": "Landed in PRD-99, which closed the gap.\n"})

    problems = audit(tree)

    assert any("internal tracker key" in p and "PRD-99" in p for p in problems)


def test_a_client_region_pin_refuses_the_release(tmp_path: Path) -> None:
    """`me-central-1` plus this repository's subject matter narrows the client considerably."""
    tree = tree_with(tmp_path, **{"docs__adr__0011-x.md": "Deploy into me-central-1.\n"})

    assert any("client region pin" in p for p in audit(tree))


def test_a_client_acronym_refuses_the_release(tmp_path: Path) -> None:
    """Removed from this repository before its first public push, and never to come back."""
    tree = tree_with(tmp_path, **{"docs__brief.md": "Prepared for TDA review.\n"})

    assert any("client acronym" in p for p in audit(tree))


def test_a_committed_rate_card_refuses_the_release(tmp_path: Path) -> None:
    """Prices are an operator input. A rate in the source is wrong for most readers by next
    quarter, and it discloses one account's commercial terms."""
    tree = tree_with(tmp_path, **{"src__x.py": 'INPUT_PER_MTOK = Decimal("5.00")\n'})

    assert any("committed rate card" in p for p in audit(tree))


def test_a_currency_amount_refuses_the_release(tmp_path: Path) -> None:
    """Spend figures are commercial detail, and they arrive in sample console output where nobody
    is looking for them."""
    tree = tree_with(tmp_path, **{"docs__sample.md": "  total  1 call(s)  $0.0575\n"})

    assert any("currency amount" in p for p in audit(tree))


def test_the_recorded_cassettes_are_not_scanned(tmp_path: Path) -> None:
    """Machine artefacts, not prose. A false positive across 47 recorded JSON files would make this
    guard something a release engineer learns to switch off, which is worse than not having it."""
    tree = tree_with(
        tmp_path, **{"tests__cassettes__critic__abc.json": '{"note": "PRD-99", "cost": "$1.00"}'}
    )

    assert audit(tree) == []


def test_the_audit_reports_every_independent_reason_at_once(tmp_path: Path) -> None:
    """A release refused three times in a row, learning one new reason each time, is a release that
    gets pushed with `--force` by somebody in a hurry."""
    tree = tree_with(
        tmp_path,
        **{
            "docs__a.md": "PRD-99\n",
            "docs__b.md": "me-central-1\n",
            ".env": "KEY=x\n",
        },
    )

    problems = audit(tree)

    assert len(problems) >= 3
    assert any("internal tracker key" in p for p in problems)
    assert any("client region pin" in p for p in problems)
    assert any("credential file present" in p for p in problems)


def test_absolute_a1_notation_is_not_mistaken_for_a_price(tmp_path: Path) -> None:
    """`$A$1` is a spreadsheet reference and this repository is full of them. A rule that fired on
    every dollar sign would block every release, which is a rule that gets deleted."""
    tree = tree_with(tmp_path, **{"docs__refs.md": "Absolute markers ($A$1) are rejected.\n"})

    assert audit(tree) == []
