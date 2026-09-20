"""Turning an upload into a submission `discover()` recognises, one role at a time.

A viewer does not know, and should not have to know, that a monthly report must be named
`pms_2026-01.pdf`. They pick a file and say what it is: the workbook, a monthly report, the
inventory. This module is the translation from "what it is" to "what it must be called" -
`role_names()` decides the target name for each upload, `stage_upload()` writes them.

## Nothing is inferred from content

A report's month comes from its own file name, the same rule `tda.extract.run.month_of` already
enforces once the file is staged - inferring it by opening the PDF and reading a printed date would
let a misfiled upload reconcile against its own totals and pass. `role_names()` raises rather than
guesses when a report's name carries no month, or when two carry the same one: the ambiguity is the
viewer's to resolve, by renaming the file before they upload it again, not this module's to pick a
side of.

## Why this refuses rather than staging invalid uploads

Nothing here is a substitute for `tda.graph.intake`, which is what actually decides whether a
submission is acceptable - a viewer can still upload a workbook with the wrong headers and watch it
be rejected downstream. What this module owns is narrower and comes first: whether the uploaded
files even *could* become a submission, structurally. A `.pdf` with a `.exe` payload, a path that
tries to escape the run directory, a file with no readable extension - none of that reaches intake,
because none of it is a question intake was built to answer.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pdfplumber

from tda.contracts import Period

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

SUBMISSION_DIR: Final = "submission"
MANIFEST_FILE: Final = "manifest.json"

# Generous but bounded. The demo reports run a few hundred KB; ten megabytes is headroom for a
# real PMS export without being large enough to matter for a hosted, memory-limited process.
MAX_UPLOAD_BYTES: Final = 10 * 1024 * 1024
MAX_REPORTS: Final = 6

# An `.xlsx` is a zip archive, and `MAX_UPLOAD_BYTES` bounds only the bytes actually uploaded - the
# *compressed* size. A few megabytes of repetitive sheet XML can decompress to gigabytes, and
# `tda.excel.run.open_submission` loads the result twice with `read_only=False`, which is enough to
# exhaust a process on a file well under the upload cap. Read from `zipfile.infolist()` alone,
# before any member is actually decompressed - a bomb that fails this check is rejected without
# this check itself decompressing the bomb. Fifty megabytes decompressed is still generous for a
# workbook this pipeline describes as "a few hundred cells".
MAX_XLSX_DECOMPRESSED_BYTES: Final = 50 * 1024 * 1024

# What the byte cap above cannot bound: what `openpyxl` builds *from* that text once
# `tda.excel.run.open_submission` actually opens it. Four rounds of a G5 security review tried to
# bound that here too - a real cell count, then a merge-range area, then a dimension hint rescanned
# rather than trusted - and each fix was defeated by the next construction `openpyxl`'s reader
# expands disproportionately (a namespace-prefixed cell tag, a merge needing no `<c>` element, a
# `<hyperlink ref="A1:XFD1048576">` that reached a gigabyte from a kilobyte upload), because every
# one of them was reading the *shape* of the file and guessing what the real load would cost - a
# fifth review round measured that the guessing itself, run unsandboxed in this process to make the
# guess, cost several seconds of CPU and hundreds of megabytes on a file sized to slip under this
# module's own byte cap. `tda.review.sandbox.run_sandboxed` is what actually bounds this now: an
# upload that reaches `open_submission` does so inside a memory- and CPU-limited child process,
# which holds regardless of which OOXML construct a file uses to get there. This module's job ends
# at the byte cap; the cost of what a valid-looking `.xlsx` might expand into is the sandbox's to
# contain, not this one's to keep guessing at. See ADR-0010 §7.

# `tda.extract.pdf` walks every page of every report with no cap of its own - it was built to trust
# `corpus/demo/`, which it always has. A page count is cheap to read and, unlike a byte count, bounds
# the CPU an upload can spend: a small file can still declare thousands of pages.
MAX_PDF_PAGES: Final = 60

_MONTH_TOKEN: Final = re.compile(r"\d{4}-\d{2}")


class UploadError(ValueError):
    """An upload cannot become part of a submission - a role, a name or a size is wrong.

    Raised rather than silently skipped or guessed: an officer who uploaded the wrong file for a
    role needs to be told which one and why, not shown a submission quietly missing a report.
    """


@dataclass(frozen=True, slots=True)
class Upload:
    """One uploaded file, as bytes rather than as whatever the browser handed the runtime.

    Streamlit's `UploadedFile` is a wrapper over a buffer that reflects one widget's current
    selection; holding onto it past the script run that produced it is not something the API
    promises to support. `name` and `data` are read out of it once, at the console's edge, and
    everything in this module works from the plain copy.
    """

    name: str
    data: bytes


def month_in_name(name: str) -> str | None:
    """The first `YYYY-MM` token in `name` that is a real month, or `None`.

    Matches `tda.extract.run.month_of`'s own convention (the text after the last underscore in the
    stem) loosely rather than exactly - an upload's original name is whatever the viewer's browser
    or PMS export called it, and the point here is to find the month a human put in the name, not
    to enforce the pipeline's on-disk convention before the file has even been renamed to follow it.
    """
    for match in _MONTH_TOKEN.finditer(name):
        candidate = match.group()
        if Period.is_valid(candidate):
            return candidate
    return None


# The byte sequence each accepted format actually starts with, independent of its name. `.xlsx`
# is a zip archive, so the PK local-file-header signature is common to every Office Open XML
# format - checked alongside the extension rather than instead of it, since neither alone proves
# the content matches the name a viewer typed.
_MAGIC_BYTES: Final[dict[str, bytes]] = {".pdf": b"%PDF-", ".xlsx": b"PK\x03\x04"}


def _checked_extension(upload: Upload, *, allowed: str, role: str) -> None:
    if not upload.name.lower().endswith(allowed):
        raise UploadError(
            f"{upload.name!r} was uploaded as the {role}, but does not end in {allowed!r}. "
            f"The {role} must be a {allowed} file."
        )
    if len(upload.data) > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"{upload.name!r} is {len(upload.data):,} bytes, over the "
            f"{MAX_UPLOAD_BYTES:,}-byte limit for an uploaded file."
        )
    magic = _MAGIC_BYTES.get(allowed)
    if magic is not None and not upload.data.startswith(magic):
        raise UploadError(
            f"{upload.name!r} was uploaded as the {role}, but its content does not start like a "
            f"{allowed} file. The name and the content must agree."
        )
    if allowed == ".xlsx":
        _checked_xlsx_content(upload, role=role)
    elif allowed == ".pdf":
        _checked_pdf_content(upload, role=role)


def _checked_xlsx_content(upload: Upload, *, role: str) -> None:
    """What `MAX_UPLOAD_BYTES` cannot bound: the size an `.xlsx` archive expands to once unzipped.

    Read from `infolist()` alone - metadata every zip archive's central directory carries, so this
    never decompresses a single byte to answer the question. What a valid-looking archive's content
    might cost *beyond* its raw size is `tda.review.sandbox`'s to contain, not this function's to
    keep parsing further to guess at; see the comment on `MAX_XLSX_DECOMPRESSED_BYTES`.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(upload.data)) as archive:
            decompressed = sum(member.file_size for member in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise UploadError(
            f"{upload.name!r} was uploaded as the {role}, but is not a readable .xlsx archive: "
            f"{exc}."
        ) from None
    if decompressed > MAX_XLSX_DECOMPRESSED_BYTES:
        raise UploadError(
            f"{upload.name!r} decompresses to {decompressed:,} bytes, over the "
            f"{MAX_XLSX_DECOMPRESSED_BYTES:,}-byte limit on a workbook's actual content."
        )


def _checked_pdf_content(upload: Upload, *, role: str) -> None:
    """What a byte count cannot bound: how much work extracting a report costs, which follows its
    page count rather than its size - a small file can still declare thousands of pages."""
    try:
        with pdfplumber.open(io.BytesIO(upload.data)) as document:
            pages = len(document.pages)
    except Exception as exc:  # pdfplumber's underlying parser raises its own exception types
        raise UploadError(
            f"{upload.name!r} was uploaded as the {role}, but is not a readable .pdf file: {exc}."
        ) from None
    if pages > MAX_PDF_PAGES:
        raise UploadError(
            f"{upload.name!r} has {pages} pages, over the {MAX_PDF_PAGES}-page limit for a "
            f"monthly report."
        )


def role_names(
    period: Period,
    workbook: Upload,
    reports: Sequence[Upload],
    inventory: Upload | None,
) -> dict[str, Upload]:
    """The target file name for every upload, or a refusal naming exactly which one is wrong.

    Returns a mapping from the name `discover()` expects to the upload that should be written
    there - `claims_<period>.xlsx`, one `pms_<month>.pdf` per report, `inventory_<period>.csv` when
    given. Nothing is written here; `stage_upload()` does that with this mapping.
    """
    _checked_extension(workbook, allowed=".xlsx", role="workbook")
    if not reports:
        raise UploadError("no monthly report was uploaded. At least one pms_YYYY-MM.pdf is needed.")
    if len(reports) > MAX_REPORTS:
        raise UploadError(
            f"{len(reports)} reports were uploaded; a declared period covers at most "
            f"{MAX_REPORTS} months."
        )

    targets: dict[str, Upload] = {f"claims_{period}.xlsx": workbook}
    months_seen: dict[str, Upload] = {}
    for report in reports:
        _checked_extension(report, allowed=".pdf", role="monthly report")
        month = month_in_name(report.name)
        if month is None:
            raise UploadError(
                f"{report.name!r} does not carry a month in its name (YYYY-MM). Rename it so the "
                "month it covers is clear, e.g. 2026-01, and upload it again - a month read from "
                "the file's printed content rather than its name is exactly what this system "
                "refuses to trust."
            )
        if month in months_seen:
            raise UploadError(
                f"{report.name!r} and {months_seen[month].name!r} both carry {month!r}. Two "
                "reports for one month is an ambiguity this system will not resolve on a "
                "viewer's behalf - remove one and upload again."
            )
        months_seen[month] = report
        targets[f"pms_{month}.pdf"] = report

    if inventory is not None:
        _checked_extension(inventory, allowed=".csv", role="inventory")
        targets[f"inventory_{period}.csv"] = inventory

    return targets


def stage_upload(
    run_dir: Path,
    period: Period,
    workbook: Upload,
    reports: Sequence[Upload],
    inventory: Upload | None = None,
) -> Path:
    """Write every upload to its target name under `run_dir/submission/`, and return that
    directory - the same shape `tda.review.scenes.Scene.prepare` returns, so the console calls
    this or a scene's `prepare()` interchangeably."""
    targets = role_names(period, workbook, reports, inventory)
    destination = run_dir / SUBMISSION_DIR
    destination.mkdir(parents=True)
    for name, upload in targets.items():
        (destination / name).write_bytes(upload.data)
    return destination


def write_manifest(run_dir: Path, hotel_id: str, period: str) -> Path:
    """The declaration `tda.cli.declaration_from_manifest` reads: one level **above**
    `submission/`, because intake exists to check the files against a declaration made somewhere
    other than the files - see `tda.cli.declaration_from_manifest` for why that placement matters.
    """
    path = run_dir / MANIFEST_FILE
    path.write_text(
        json.dumps({"hotel_id": hotel_id, "period": period}, indent=2), encoding="utf-8"
    )
    return path
