"""`make datagen` — build the corpus, then prove it is reproducible.

The order here is the argument of the whole story:

1. **Ledger first.** Reservations and inventory, from a fixed seed, with the edge quotas checked.
2. **Truth second**, aggregated independently from the ledger (`aggregate.py`).
3. **Documents third**, rendered from the same ledger — the PDFs and the claim workbook.
4. **Manifest last**, digesting every file that was written.

Step 4 is what makes the reproducibility claim checkable rather than asserted. `--verify`
regenerates into a temporary directory and compares digests against the committed corpus, so
`make datagen --verify` answers "does this corpus still match the code that claims to produce it"
without a human diffing two binaries.

Exit codes: 0 wrote (or verified) the corpus · 1 the corpus on disk does not match · 2 bad
invocation or a failed quota.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from datagen import GENERATOR_VERSION
from datagen.aggregate import compute_truth, load_policy_view
from datagen.ledger import (
    GeneratorError,
    InventoryRow,
    LedgerRow,
    generate_inventory,
    generate_ledger,
)
from datagen.render_pdf import render_month
from datagen.render_workbook import render_workbook
from datagen.reproducible import digest, finalise_xlsx, write_json, write_text
from datagen.spec import (
    HOTEL_ID,
    LAYOUT,
    MONTHS,
    PERIOD_END,
    PERIOD_START,
    QUARTER,
    SEED,
    SPEC,
    TOTAL_RESERVATIONS,
)

DEFAULT_OUT = Path("corpus/demo")

LEDGER_COLUMNS = (
    "reservation_id",
    "hotel_id",
    "guest_ref",
    "nationality_iso2",
    "adults",
    "children",
    "rooms",
    "nights",
    "room_nights",
    "arrival_date",
    "departure_date",
    "status",
    "rate_code",
)

INVENTORY_COLUMNS = (
    "hotel_id",
    "day",
    "rooms_total",
    "rooms_out_of_order",
    "rooms_available",
    "note",
)


@dataclasses.dataclass(frozen=True, slots=True)
class Manifest:
    """What was written, and the digest of each file.

    A dataclass rather than a bare dict so `--verify` compares typed fields instead of indexing
    into JSON and hoping. The serialised form is the same either way; the difference is that a
    renamed key fails at type-check rather than at three o'clock in the morning.
    """

    generator_version: str
    spec_digest: str
    seed: int
    hotel_id: str
    period: str
    pdf_pages: dict[str, int]
    files: dict[str, str]

    @classmethod
    def parse(cls, payload: object) -> Manifest:
        if not isinstance(payload, dict):
            raise GeneratorError("manifest.json did not parse to a mapping")
        try:
            return cls(
                generator_version=str(payload["generator_version"]),
                spec_digest=str(payload["spec_digest"]),
                seed=int(payload["seed"]),
                hotel_id=str(payload["hotel_id"]),
                period=str(payload["period"]),
                pdf_pages={str(k): int(v) for k, v in dict(payload["pdf_pages"]).items()},
                files={str(k): str(v) for k, v in dict(payload["files"]).items()},
            )
        except (KeyError, TypeError, ValueError) as error:
            raise GeneratorError(f"manifest.json is malformed: {error}") from error


def _spec_digest() -> str:
    """A digest of the specification, so a corpus can be traced to the shape that produced it.

    Over the spec dataclass rather than over `spec.py`: a comment rewritten in that file should not
    invalidate a corpus, and a changed room count should.
    """
    payload = json.dumps(dataclasses.asdict(SPEC), sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def _write_ledger_csv(path: Path, rows: list[LedgerRow]) -> None:
    """The ledger as CSV — human-auditable ground truth, and not a pipeline input.

    Committed so a reviewer can check a number without a PDF parser. It lives under
    `ground_truth/`, which the pipeline never reads: see the README the generator writes.
    """
    lines = [",".join(LEDGER_COLUMNS)]
    for row in rows:
        record = dataclasses.asdict(row)
        lines.append(",".join(str(record[column]) for column in LEDGER_COLUMNS))
    write_text(path, "\n".join(lines))


def _write_inventory_csv(path: Path, rows: list[InventoryRow]) -> None:
    """The inventory reference, which *is* a pipeline input (D-RNA-01).

    Written with `csv.writer` rather than by joining, because the note column carries free text and
    a comma in a reason string would otherwise shift every column after it. `newline=""` keeps the
    line endings `\\n` on every platform, which the committed digest depends on.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(INVENTORY_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row.hotel_id,
                    row.day.isoformat(),
                    row.rooms_total,
                    row.rooms_out_of_order,
                    row.rooms_available,
                    row.note,
                ]
            )


README = f"""# `corpus/demo` — the synthetic demonstration corpus

**Classification: Green. Synthetic data only.** No real property, no real export, no real guest.
The hotel does not exist. There is no guest name anywhere in this corpus, and none was ever
generated: `guest_ref` is a digest under a published salt, so there is nothing to leak rather than
something withheld (D-EV-03).

Regenerate with `make datagen`. Byte-identical every time, from seed `{SEED}`.

## Layout, and the one rule

```
submission/     exactly what a hotel submits — the pipeline reads ONLY this
ground_truth/   what the generator knows and the hotel does not
manifest.json   sha256 of every file above
```

**Nothing in the pipeline may read `ground_truth/`.** It holds `truth_metrics.json` and the
reservation ledger both documents were rendered from. A verification that had seen the answers
would not be a verification. The split is two directories rather than a naming convention because
this is the easiest rule in the repository to break by accident and the hardest to notice
afterwards.

## This corpus is frozen, and it is never scored

A clean pass on the data the system was rendered from is a tautology, not evidence. `corpus/demo/`
exists so the happy path is demonstrable and so parsers have a realistic document to be built
against. The **scored** fixtures — the ones with planted errors, where catching something means
something — are built separately in S11 and are the only ones `make eval` reports on.

## What is in it, and why each edge is there

| Edge | Why it exists |
|---|---|
| ≥ 25 month-spanning stays | D-RNS-03 apportionment, and the `P-MONTH-ARRIVAL` / `P-MONTH-DEPARTURE` permutations |
| ≥ 25 COMP and ≥ 25 HOUSE | so `P-COMP-EXCLUDED` and `P-HOUSE-INCLUDED` move a number instead of silently returning the baseline |
| ≥ 30 day-use reservations | D-QUAL-08..10: zero room-nights, but a real guest of the destination |
| 12 stays arriving before 1 Jan | D-NAT-08: room-nights in January, no guests in January — the case the two metric families are *expected* not to reconcile on |
| 8 stays open at 31 March | `IN_HOUSE` with dates that agree with the status |
| Two out-of-order windows | D-RNA-03/04: the occupancy denominator cannot be inferred from a room count |
| One nationality in one month only | S8's completeness path needs a row a workbook can omit |
| Country **labels**, not ISO codes, in the workbook | D-NAT-09..11 normalisation, including the `Czech Republic` → `CZ` variant pair |
| An out-of-scope sheet | D-SCOPE-02: silence on an unverified claim reads as approval |

The claim workbook asserts the **correct** figures. See "never scored", above.

## Files

| File | What it is |
|---|---|
| `submission/pms_2026-01..03.pdf` | Monthly reservation detail reports. Text layer, 30 rows per page, per-page subtotals, and a grand-total block on the last page |
| `submission/{LAYOUT.inventory_csv}` | The room inventory reference — one row per day, with the out-of-order windows |
| `submission/{LAYOUT.workbook}` | The claim workbook: Summary, Occupancy, Nationality, Rate & Revenue |
| `ground_truth/{LAYOUT.truth_metrics}` | Every metric this corpus establishes, keyed canonically |
| `ground_truth/{LAYOUT.ledger_csv}` | The reservation ledger everything was rendered from |

A month-spanning stay appears on **both** months' reports, with `RN Total` (the whole stay) and
`RN Month` (the part in this month) printed separately. The grand total is the **qualifying** total
and the report names what it excluded, so the reconciliation is a check a reader can do by hand.
"""


def build(out: Path) -> Manifest:
    """Build the whole corpus under `out`. Returns the manifest.

    Directories are recreated rather than written into, so a file that a previous version of the
    generator produced and this one does not cannot survive as a stale input nobody notices.
    """
    policy = load_policy_view()

    rows = generate_ledger()
    inventory = generate_inventory()
    truth = compute_truth(rows, inventory, policy)

    submission = out / LAYOUT.submission
    ground_truth = out / LAYOUT.ground_truth
    for directory in (submission, ground_truth):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)

    _write_inventory_csv(submission / LAYOUT.inventory_csv, inventory)
    _write_ledger_csv(ground_truth / LAYOUT.ledger_csv, rows)

    pages: dict[str, int] = {}
    for month in MONTHS:
        in_month = [row for row in rows if month in row.months_touched()]
        locations = render_month(
            submission / LAYOUT.pdf_name(month), month, in_month, inventory, policy
        )
        pages[month] = max(location.page for location in locations) + 1  # + the totals page

    workbook_path = submission / LAYOUT.workbook
    render_workbook(workbook_path, MONTHS, truth)
    finalise_xlsx(workbook_path)

    write_json(
        ground_truth / LAYOUT.truth_metrics,
        {
            "generator_version": GENERATOR_VERSION,
            "spec_digest": _spec_digest(),
            "seed": SEED,
            "hotel_id": HOTEL_ID,
            "period": QUARTER,
            "period_start": PERIOD_START.isoformat(),
            "period_end": PERIOD_END.isoformat(),
            "policy_version": policy.version,
            "reservations": len(rows),
            "inventory_days": len(inventory),
            "metrics": truth,
        },
    )
    write_text(out / LAYOUT.readme, README)

    manifest = Manifest(
        generator_version=GENERATOR_VERSION,
        spec_digest=_spec_digest(),
        seed=SEED,
        hotel_id=HOTEL_ID,
        period=QUARTER,
        pdf_pages=pages,
        files={
            str(path.relative_to(out)): digest(path)
            for path in sorted(out.rglob("*"))
            if path.is_file() and path.name != LAYOUT.manifest
        },
    )
    write_json(out / LAYOUT.manifest, dataclasses.asdict(manifest))
    return manifest


def _verify(out: Path) -> int:
    """Regenerate into a temporary directory and compare digests with what is committed.

    This is the reproducibility claim, checked. It catches both halves of what could go wrong: a
    generator change that moved a byte nobody meant to move, and a corpus that was hand-edited
    after it was generated.
    """
    committed_path = out / LAYOUT.manifest
    if not committed_path.exists():
        print(f"no manifest at {committed_path}; run `make datagen` first", file=sys.stderr)
        return 1
    committed = Manifest.parse(json.loads(committed_path.read_text(encoding="utf-8")))

    with tempfile.TemporaryDirectory() as temporary:
        rebuilt = build(Path(temporary) / "corpus")

    differences = [
        f"  {name}\n"
        f"      committed {committed.files.get(name, '(absent)')}\n"
        f"      rebuilt   {value}"
        for name, value in sorted(rebuilt.files.items())
        if committed.files.get(name) != value
    ]
    missing = sorted(set(committed.files) - set(rebuilt.files))

    if differences or missing:
        print("corpus is NOT reproducible from the current generator:", file=sys.stderr)
        for line in differences:
            print(line, file=sys.stderr)
        for name in missing:
            print(f"  {name}: committed, no longer produced", file=sys.stderr)
        print(
            "\nEither the generator changed and the corpus needs regenerating (`make datagen`, "
            "then commit the result), or a file was edited by hand. A hand-edited corpus is the "
            "worse case: truth_metrics.json would no longer describe the documents.",
            file=sys.stderr,
        )
        return 1

    print(f"corpus verified: {len(rebuilt.files)} files match {committed_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Regenerate into a temporary directory and diff digests against --out.",
    )
    args = parser.parse_args(argv)

    out: Path = args.out
    try:
        if args.verify:
            return _verify(out)

        out.mkdir(parents=True, exist_ok=True)
        manifest = build(out)
    except GeneratorError as error:
        print(f"datagen failed: {error}", file=sys.stderr)
        return 2

    print(
        f"corpus written to {out}  ({TOTAL_RESERVATIONS} reservations, {len(manifest.files)} files)"
    )
    for name in sorted(manifest.files):
        print(f"  {name}")
    print("\nVerify byte-reproducibility with:  make corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
