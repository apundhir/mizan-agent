"""The three artefacts a verification officer can hold, file and forward.

| File | Who reads it | What it is for |
|---|---|---|
| `verdict.json` | a downstream system, and anybody auditing later | the complete result, with a summary block that cannot disagree with its own arrays |
| `annotated_<workbook>.xlsx` | the verification officer | the hotel's own figures, marked against what the records say |
| `memo.docx` | the supervisor who reads one page | the verdict in the first block, then the detail |

## One redaction point, and everything downstream inherits it

`write_outputs` writes `verdict.json` first, **reads it back**, and renders the memo and the
workbook comments from the copy that came off the disk. That is not a detour: `tda.obs.redact`
removes personal data on the way out, and rendering the memo from the in-memory verdict instead
would produce a document that says what the JSON beside it does not — which is the defect observability's
review found in `mizan run`'s console output, made durable in a Word file this time.

The annotated workbook's **cells** are exempt, and deliberately: that file is a copy of the hotel's
own submission going back to the hotel. Removing content from it destroys evidence without
withholding anything the recipient does not already have. The comments Mizan adds to it are
rendered from the redacted verdict like everything else.

## Why this package does not import `tda.graph`

Same discipline as `tda.obs`: an output describes a verdict, and a verdict is a contract. A writer
that took a `RunResult` would make the shape of every artefact depend on the shape of the pipeline,
and then a change to how a run is orchestrated could silently change what a hotel receives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from tda.outputs.memo import MEMO_FILE, write_memo
from tda.outputs.verdict import (
    VERDICT_FILE,
    VerdictDocument,
    VerdictSummary,
    read_verdict,
    write_verdict,
)
from tda.outputs.workbook import (
    LEGEND_SHEET,
    AnnotatedWorkbook,
    OriginalModifiedError,
    annotate,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from tda.contracts import Claim, Verdict
    from tda.obs.redact import Redaction

__all__ = [
    "LEGEND_SHEET",
    "MEMO_FILE",
    "VERDICT_FILE",
    "AnnotatedWorkbook",
    "OriginalModifiedError",
    "RedactionBrokeTheVerdictError",
    "VerdictDocument",
    "VerdictSummary",
    "WrittenOutputs",
    "annotate",
    "read_verdict",
    "write_memo",
    "write_outputs",
    "write_verdict",
]


@dataclass(frozen=True, slots=True)
class WrittenOutputs:
    """Where the three artefacts landed, and what was removed from them on the way.

    `workbook` is `None` when the run never got as far as a workbook — an intake rejection for an
    incomplete file set, say. A missing annotated copy is then the correct output rather than a
    failure, and the caller says so instead of producing an empty one.

    `workbook_error` is set when there *was* a workbook and it could not be annotated. The two are
    different facts and the render below says which: "there was nothing to annotate" is an ordinary
    outcome, and "the submitted workbook would not open" is the thing intake already refused the
    submission for.
    """

    verdict: Path
    memo: Path
    workbook: AnnotatedWorkbook | None
    redaction: Redaction
    workbook_error: str | None = None

    @property
    def files(self) -> tuple[Path, ...]:
        paths = (self.verdict, self.memo)
        return paths if self.workbook is None else (*paths, self.workbook.path)

    def render(self) -> str:
        lines = [f"  verdict: {self.verdict}", f"  memo: {self.memo}"]
        if self.workbook is not None:
            lines.append(self.workbook.render())
        elif self.workbook_error is not None:
            lines.append(f"  no annotated workbook: {self.workbook_error}")
        else:
            lines.append(
                "  no annotated workbook: this run never read one (see the verdict's status)"
            )
        if not self.redaction.clean:
            lines.append(
                f"  redacted from the outputs: {self.redaction.render()}"
                "  (personal data in the submitted workbook, not from this system)"
            )
        return "\n".join(lines)


class RedactionBrokeTheVerdictError(RuntimeError):
    """Redacting the verdict produced something the contract refuses.

    It should be impossible — every pattern in `tda.obs.redact` fires on a structural signal that
    no enum value, clause id or finding id contains. If it ever happens, the honest outcome is a
    loud failure: the alternative is rendering the memo from the unredacted verdict, which would
    put in a Word document exactly what was kept out of the JSON beside it.
    """


def write_outputs(
    directory: Path,
    verdict: Verdict,
    claims: Sequence[Claim],
    workbook: Path | None,
) -> WrittenOutputs:
    """Write all three artefacts into one run's directory.

    The order is load-bearing: `verdict.json` is written and read back first, and the memo and the
    workbook comments are rendered from what came off the disk. See the module docstring.
    """
    verdict_path, redaction = write_verdict(directory, verdict)
    try:
        as_written = read_verdict(verdict_path)
    except ValidationError as exc:
        raise RedactionBrokeTheVerdictError(
            f"{verdict_path} does not validate after redaction: {exc}. The memo and the workbook "
            "are rendered from the written verdict so they cannot say more than it does, so "
            "nothing further is written."
        ) from exc

    memo = write_memo(directory, as_written)
    annotated, failure = _annotated(directory, verdict, claims, workbook)
    return WrittenOutputs(
        verdict=verdict_path,
        memo=memo,
        workbook=annotated,
        redaction=redaction,
        workbook_error=failure,
    )


def _annotated(
    directory: Path, verdict: Verdict, claims: Sequence[Claim], workbook: Path | None
) -> tuple[AnnotatedWorkbook | None, str | None]:
    """Annotate the workbook, or report why not — without costing the other two artefacts.

    The verdict written by an intake rejection for an **unreadable file** is exactly the case where
    the workbook will not open: `openpyxl` raises `BadZipFile` on a corrupt `.xlsx`, which is not an
    `OSError`, and letting it out of here would end a completed run in a traceback with the verdict
    and the memo already on disk. `tda.graph.intake` widened the same catch for the same reason.

    The references come from the **unredacted** verdict; see `tda.outputs.workbook` on why a
    redacted sheet name cannot be looked up and what it did when it was tried.
    """
    if workbook is None or not workbook.is_file():
        return None, None
    try:
        return annotate(workbook, verdict, claims, directory), None
    # Broad on purpose: the reason is reported and the run keeps its verdict.
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
