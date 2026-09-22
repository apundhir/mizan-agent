"""`make demo`: three real runs of the actual pipeline, in replay mode, no key anywhere [the demo scenes].

Three scenes, each a full `verify_directory` call against the real graph, not a mock of it:

1. **A clean quarter.** The demo corpus, unmutated. Zero findings.
2. **A mistyped guest count.** A hand-keyed transposition, caught at the cell and at the
   quarter roll-up it feeds, with a proposed correction and a citation.
3. **A nationality code the lookup will never resolve.** Extraction halts before the mapping
   agent is ever invoked, so this scene makes zero model calls and needs no API key.

Scene 2 needs `corpus/fixtures/F2/`, which `make fixtures` derives from `corpus/demo/` and never
commits (a rebuilt fixture is evidence; a committed binary would only be an assertion). The `demo`
Makefile target depends on `fixtures` for exactly that reason, the same way `eval` already does.

Scene 3 has no committed corpus at all. `build_refusal_submission` re-renders one month's report
from a mutated copy of `corpus/demo`'s own ledger, into a fresh temporary directory, every time
this runs. See its docstring for why re-rendering was chosen over editing the finished PDF's bytes.

Nothing printed here names a fixture by its internal label. A demo is not the place to tell an
audience which row is a planted mutation; `corpus/fixtures/F1..F3/README.md` says so already, and
the scene descriptions below are written from what a viewer can see, not from how it was built.
"""

from __future__ import annotations

import csv
import dataclasses
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:  # so `import datagen` works when run as a plain script
    sys.path.insert(0, str(TOOLS_DIR))

from tda.agents.provider import ReplayProvider  # noqa: E402
from tda.contracts import Period, VerdictStatus  # noqa: E402
from tda.graph import RunResult, new_run_id, verify_directory  # noqa: E402
from tda.policy import Policy, load_policy  # noqa: E402

if TYPE_CHECKING:
    from datagen.ledger import InventoryRow, LedgerRow

DEMO_SUBMISSION = REPO_ROOT / "corpus" / "demo" / "submission"
GROUND_TRUTH = REPO_ROOT / "corpus" / "demo" / "ground_truth"
CATCH_SUBMISSION = REPO_ROOT / "corpus" / "fixtures" / "F2" / "submission"

HOTEL_ID = "MZN-DXB-001"
PERIOD_TEXT = "2026-Q1"
REFUSAL_MONTH = "2026-01"

# Two letters, so it occupies the "Nat" column exactly the way a real ISO code does, and goes
# through the same D-NAT-09..12 matching a real label would. Not in the committed lookup: checked
# by a unit test that deletes this guarantee (tests/unit/test_demo.py).
UNRESOLVABLE_NATIONALITY = "XX"


@dataclass(frozen=True, slots=True)
class Scene:
    """One demo scene: what it verifies, what came back, and the one thing it demonstrates."""

    title: str
    demonstrates: str
    expected_status: VerdictStatus
    result: RunResult

    @property
    def matched_expectation(self) -> bool:
        return self.result.verdict.status is self.expected_status

    def render(self) -> str:
        verdict = self.result.verdict
        lines = [
            self.title,
            f"  demonstrates: {self.demonstrates}",
            f"  verdict: {verdict.status.value}"
            + (f" ({verdict.rejection_reason.value})" if verdict.rejection_reason else "")
            + f"  ·  {len(verdict.findings)} finding(s)"
            f"  ·  {len(verdict.definitional_items)} definitional item(s)",
            f"  model calls made: {len(self.result.context.trace.records)}",
        ]
        for finding in verdict.findings[:1]:
            lines.append(
                f"  first finding: {finding.key} {finding.variance_class.value} "
                f"({finding.clause}) at {finding.excel_ref}"
            )
        for item in verdict.definitional_items[:1]:
            lines.append(
                f"  first definitional item: {item.key} explained by {item.explaining_permutation}"
            )
        if not self.matched_expectation:
            lines.append(
                f"  ** expected {self.expected_status.value}, the run reported "
                f"{verdict.status.value} - this is a real regression, not a formatting issue **"
            )
        return "\n".join(lines)


def run_pass_scene(policy: Policy) -> Scene:
    """Scene 1: an unmutated quarter, recomputed from the PDFs and checked cell by cell."""
    result = verify_directory(
        DEMO_SUBMISSION,
        HOTEL_ID,
        Period.parse(PERIOD_TEXT),
        policy,
        ReplayProvider(),
        run_id=new_run_id(),
    )
    return Scene(
        title="Scene 1 - a clean quarter",
        demonstrates=(
            "the pipeline recomputes a full quarter from the PDFs and agrees with the workbook to "
            "the cell. Zero findings, because a system that reports something on every run has "
            "taught its reviewer to skim, and then the one real finding gets skimmed too"
        ),
        expected_status=VerdictStatus.PASS,
        result=result,
    )


def run_catch_scene(policy: Policy) -> Scene:
    """Scene 2: a hand-keyed transposition, caught at the cell and at the roll-up it feeds."""
    if not CATCH_SUBMISSION.is_dir():
        raise SystemExit(
            "the second scene's submission is missing. `make fixtures` derives it from "
            "corpus/demo/ and `make demo` depends on that target; run `make fixtures` first if "
            "this scene is being run on its own."
        )
    result = verify_directory(
        CATCH_SUBMISSION,
        HOTEL_ID,
        Period.parse(PERIOD_TEXT),
        policy,
        ReplayProvider(),
        run_id=new_run_id(),
    )
    return Scene(
        title="Scene 2 - a mistyped guest count",
        demonstrates=(
            "a transposition in one hand-keyed nationality count is caught at the cell it was "
            "typed in and, independently, at the quarter roll-up that sums it. Each finding "
            "carries a proposed correction and a citation back to the reservation rows behind it"
        ),
        expected_status=VerdictStatus.FAIL,
        result=result,
    )


def _read_ledger(path: Path) -> list[LedgerRow]:
    from datagen.ledger import LedgerRow

    rows: list[LedgerRow] = []
    with path.open(encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            rows.append(
                LedgerRow(
                    reservation_id=record["reservation_id"],
                    hotel_id=record["hotel_id"],
                    guest_ref=record["guest_ref"],
                    nationality_iso2=record["nationality_iso2"],
                    adults=int(record["adults"]),
                    children=int(record["children"]),
                    rooms=int(record["rooms"]),
                    nights=int(record["nights"]),
                    room_nights=int(record["room_nights"]),
                    arrival_date=date.fromisoformat(record["arrival_date"]),
                    departure_date=date.fromisoformat(record["departure_date"]),
                    status=record["status"],
                    rate_code=record["rate_code"],
                )
            )
    return rows


def _read_inventory(path: Path) -> list[InventoryRow]:
    from datagen.ledger import InventoryRow

    rows: list[InventoryRow] = []
    with path.open(encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            rows.append(
                InventoryRow(
                    hotel_id=record["hotel_id"],
                    day=date.fromisoformat(record["day"]),
                    rooms_total=int(record["rooms_total"]),
                    rooms_out_of_order=int(record["rooms_out_of_order"]),
                    note=record["note"],
                )
            )
    return rows


def pick_refusal_target(rows: list[LedgerRow]) -> LedgerRow:
    """The reservation whose nationality gets replaced with a code the lookup cannot resolve.

    Chosen by rule rather than by a literal reservation id, so the scene survives a corpus
    regenerated under a different seed: a single-room, single-month, checked-out stay, ordinary in
    every other respect. Single-month avoids `merge_across_reports` entirely (a spanning stay
    prints on two months' reports, and mutating only one would make the two printings disagree
    about a field the merge step checks). Single-room and checked-out just keep the scene ordinary,
    an unremarkable booking except for one field.
    """
    candidates = [
        row
        for row in sorted(rows, key=lambda r: r.reservation_id)
        if not row.is_day_use
        and row.rooms == 1
        and row.status == "CHECKED_OUT"
        and row.rate_code != "HOUSE"
        and row.arrival_date.month == 1
        and row.departure_date.month == 1
    ]
    if not candidates:
        raise RuntimeError(
            "no eligible reservation for the refusal scene. The corpus's shape changed; "
            "pick_refusal_target's rule needs revisiting rather than a hard-coded id."
        )
    return candidates[0]


def build_refusal_submission(tmp_dir: Path) -> Path:
    """A fourth corpus variant: one reservation's nationality is a label nothing can resolve.

    Materialised at runtime and never committed. It exists to demonstrate D-NAT-12, not to be
    scored against it, so there is no mutation spec and no `expected.json` to derive.

    **Re-rendering beats editing the finished PDF.** `tools/datagen/render_pdf.py` draws every cell
    with `reportlab`'s canvas into a compressed content stream; there is no text run to find and
    swap without re-implementing enough of a PDF writer to make the exercise pointless. Rendering
    from a mutated copy of the ledger uses the same generator the committed corpus was built from,
    against a target chosen by `pick_refusal_target`, and produces a document indistinguishable in
    shape from a real PMS export with one field a human mistyped.

    Only January's report is re-rendered. February and March, the workbook and the inventory
    reference are byte copies of `corpus/demo/submission/`, because nothing about them changes.
    """
    from datagen.aggregate import load_policy_view
    from datagen.render_pdf import render_month

    submission = tmp_dir / "submission"
    submission.mkdir(parents=True)

    ledger = _read_ledger(GROUND_TRUTH / "reservations.csv")
    inventory = _read_inventory(DEMO_SUBMISSION / "inventory_2026-Q1.csv")
    policy_view = load_policy_view()

    target = pick_refusal_target(ledger)
    mutated = [
        dataclasses.replace(row, nationality_iso2=UNRESOLVABLE_NATIONALITY)
        if row.reservation_id == target.reservation_id
        else row
        for row in ledger
    ]
    january = [row for row in mutated if REFUSAL_MONTH in row.months_touched()]
    render_month(submission / "pms_2026-01.pdf", REFUSAL_MONTH, january, inventory, policy_view)

    for name in (
        "pms_2026-02.pdf",
        "pms_2026-03.pdf",
        "claims_2026-Q1.xlsx",
        "inventory_2026-Q1.csv",
    ):
        (submission / name).write_bytes((DEMO_SUBMISSION / name).read_bytes())

    return submission


def run_refusal_scene(policy: Policy, tmp_dir: Path) -> Scene:
    """Scene 3: a nationality label the committed lookup has never seen."""
    submission = build_refusal_submission(tmp_dir)
    result = verify_directory(
        submission,
        HOTEL_ID,
        Period.parse(PERIOD_TEXT),
        policy,
        ReplayProvider(),
        run_id=new_run_id(),
    )
    return Scene(
        title="Scene 3 - a nationality label nobody taught the system",
        demonstrates=(
            "a label the committed ISO lookup cannot resolve halts extraction before the mapping "
            "agent is ever invoked. Zero model calls on this scene, and the blocking finding names "
            "the exact string that failed rather than guessing at the nearest country"
        ),
        expected_status=VerdictStatus.HALTED,
        result=result,
    )


PREAMBLE = f"""Three scenes against the real pipeline, replay mode, no ANTHROPIC_API_KEY anywhere.

Verifying: hotel {HOTEL_ID}, period {PERIOD_TEXT}.
"""


def main() -> int:
    policy = load_policy()
    print(PREAMBLE)
    print(f"policy {policy.version}  provider replay")

    scenes: list[Scene] = [run_pass_scene(policy), run_catch_scene(policy)]
    with tempfile.TemporaryDirectory(prefix="mizan-demo-") as tmp:
        scenes.append(run_refusal_scene(policy, Path(tmp)))
        for scene in scenes:
            print()
            print(scene.render())

    failed = [scene for scene in scenes if not scene.matched_expectation]
    if failed:
        print()
        print(f"{len(failed)} scene(s) did not report the outcome this demo asserts.")
        return 1

    print()
    print("all three scenes reported their expected outcome.")
    return 0


__all__ = [
    "CATCH_SUBMISSION",
    "DEMO_SUBMISSION",
    "Scene",
    "build_refusal_submission",
    "main",
    "pick_refusal_target",
    "run_catch_scene",
    "run_pass_scene",
    "run_refusal_scene",
]

if __name__ == "__main__":
    raise SystemExit(main())
