"""`verdict.json` — the artefact a downstream system consumes, and the one an officer files behind.

The `Verdict` contract has existed since M1 and nothing has ever written it to a file. This is
that writer, and it adds exactly one thing: a **summary block that cannot disagree with the arrays
it summarises**.

## Why a summary block at all, when every count is derivable

Because the reader who most needs it will not derive it. `verdict.json` is what a downstream system
consumes and what somebody opens when a hotel disputes a fee, and "how many material findings?" is
the first question either of them asks. A consumer that has to iterate `findings`, filter by
severity and remember that `definitional_items` is a *separate array* will get it wrong eventually —
and the way it gets it wrong is by counting policy disagreements as hotel errors, which is exactly
the failure D-MAT-06 exists to prevent.

## Why the summary is validated rather than merely computed

A derived field written once and trusted forever is a field that drifts. `VerdictDocument` recomputes
the summary on validation and **refuses a document whose summary does not match its own arrays** —
so a hand-edited `verdict.json` claiming three material findings over a list of five fails to load
rather than being believed. The file cannot lie about its own contents, which is the only useful
sense in which a JSON file can be trusted.

## Why `computed_field` was not used

It was the obvious approach — annotate `Verdict.severity_counts` and let pydantic serialise it — and
it breaks the round trip. `Verdict` is `extra="forbid"`, so a dump carrying computed fields fails to
re-validate: the artefact would be write-only, which is a strange property for the file that exists
to be read back. A subclass with a real field keeps `model_validate_json(path.read_text())` working,
and that is what `tda.review` and the eval harness's repro diff will both do.

## The review records are exempt from redaction, and that is the point of them

Everything else here is redacted on the way out. `review_records` is not, and the reason is that
redacting it **inverts the requirement it exists to satisfy**. FR-11 asks for the name of the person
who decided; `tda.obs.redact`'s `titled_name` pattern fires on `Dr. Jane Doe` and its `email`
pattern on `a.officer@tda.gov.ae`, so an officer signing off used to be written to disk as
`[redacted:titled_name]` — an accountability record recording that a button was pressed by nobody,
with two different titled reviewers becoming indistinguishable.

The asymmetry is principled rather than convenient. Redaction exists for text this system copied out
of somebody else's file — a hotel's workbook labels reaching a prompt. A reviewer's name is typed
into this system, by that reviewer, for the express purpose of being recorded against their
decision. Removing it protects nobody and destroys the one thing the gate is for.

## Redaction, and the one artefact that is deliberately exempt

The verdict is redacted on the way out like everything in `tda.obs` — `ExtractionSummary.unmapped_labels`
carries label text straight from the submitted workbook, and `Finding.narrative` is model prose. The
**annotated workbook is not** redacted, and the asymmetry is deliberate: that file is a copy of the
hotel's own submission going back to the hotel, so removing content from it destroys evidence
without withholding anything the recipient does not already have. See `tda.outputs.workbook`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tda.contracts import Severity, Verdict
from tda.obs.redact import redact

if TYPE_CHECKING:
    from pathlib import Path

    from tda.obs.redact import Redaction

VERDICT_FILE = "verdict.json"


class VerdictSummary(BaseModel):
    """The counts a reader needs before reading anything else.

    Every number here is derivable from the verdict's own arrays, and `VerdictDocument` checks that
    it still is. The block exists so the first question anyone asks has an answer in the first
    twenty lines of the file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: int = Field(ge=0, description="Clerical and blocking. Never includes a V2.")
    definitional_items: int = Field(
        ge=0,
        description="Policy disagreements, counted apart. A reader who adds this to `findings` to "
        "get 'how many things did the hotel get wrong' has the wrong number - D-MAT-06.",
    )
    hotel_errors: int = Field(
        ge=0,
        description="V1 and V5 only. What a fee or a follow-up letter could legitimately rest on.",
    )
    by_severity: dict[str, int] = Field(
        default_factory=dict, description="Across findings and definitional items together."
    )
    not_verifiable: int = Field(ge=0)
    out_of_scope_claims: int = Field(
        ge=0,
        description="Claims nobody checked, counted so silence is not read as approval (D-SCOPE-02).",
    )
    undecided_findings: int = Field(
        ge=0, description="Findings with no recorded human decision yet (FR-10)."
    )

    @classmethod
    def of(cls, verdict: Verdict) -> VerdictSummary:
        return cls(
            findings=len(verdict.findings),
            definitional_items=len(verdict.definitional_items),
            hotel_errors=verdict.hotel_error_count,
            by_severity=verdict.severity_counts,
            not_verifiable=len(verdict.not_verifiable),
            out_of_scope_claims=len(verdict.out_of_scope_claims),
            undecided_findings=len(verdict.undecided_findings),
        )


class VerdictDocument(Verdict):
    """A verdict plus its summary — what `verdict.json` holds.

    A subclass rather than a wrapper, so the verdict's own fields stay at the top level of the file.
    A consumer reading `status` or `findings` reads them where the contract says they are, and the
    summary is one more key beside them rather than a second nesting level to unwrap.
    """

    summary: VerdictSummary

    @model_validator(mode="after")
    def _the_summary_matches_the_arrays(self) -> Self:
        """A document whose summary disagrees with its own contents does not load.

        The failure this prevents is quiet and expensive: somebody edits `findings` out of a
        verdict, the summary keeps saying five, and every reader downstream believes the summary
        because reading it is cheaper than counting.
        """
        expected = VerdictSummary.of(Verdict(**{f: getattr(self, f) for f in Verdict.model_fields}))
        if self.summary != expected:
            raise ValueError(
                f"the summary block does not match the verdict it summarises. "
                f"Recorded {self.summary.model_dump()}, computed {expected.model_dump()}. "
                "A file that can misreport its own contents is worse than one with no summary, "
                "because the summary is what gets read."
            )
        return self

    @classmethod
    def of(cls, verdict: Verdict) -> VerdictDocument:
        fields = {name: getattr(verdict, name) for name in Verdict.model_fields}
        return cls(**fields, summary=VerdictSummary.of(verdict))

    def render(self) -> str:
        """The first block, as a person reads it. Shares its wording with the memo's first block."""
        reason = f" ({self.rejection_reason.value})" if self.rejection_reason else ""
        lines = [
            f"  {self.status.value}{reason}  {self.hotel_id}  {self.period}",
            f"  {self.claims_checked} claim(s) checked  "
            f"{self.summary.hotel_errors} hotel error(s)  "
            f"{self.summary.definitional_items} definitional item(s)  "
            f"{self.summary.not_verifiable} not verifiable",
        ]
        if blocking := self.summary.by_severity.get(Severity.BLOCKING.value, 0):
            lines.append(f"  {blocking} blocking finding(s) - this run did not verify everything")
        return "\n".join(lines)


def write_verdict(directory: Path, verdict: Verdict) -> tuple[Path, Redaction]:
    """Write `verdict.json` into one run's artifact directory, redacted on the way out.

    Sorted keys and a trailing newline, matching `tda.obs.artifacts`: two runs of one submission
    produce byte-identical files, which is what the eval harness's repro diff compares.
    """
    directory.mkdir(parents=True, exist_ok=True)
    document = VerdictDocument.of(verdict)
    payload = document.model_dump(mode="json")
    # Lifted out before redaction and put back after. See the module docstring: redacting a
    # reviewer's name inverts FR-11, and it is the one field here this system did not copy out of
    # somebody else's file.
    records = payload.pop("review_records")
    text, redaction = redact(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    restored = json.loads(text)
    restored["review_records"] = records
    path = directory / VERDICT_FILE
    path.write_text(
        json.dumps(restored, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path, redaction


def read_verdict(path: Path) -> VerdictDocument:
    """Read one back. Raises if the summary disagrees with the arrays — see the validator."""
    return VerdictDocument.model_validate_json(path.read_text(encoding="utf-8"))
