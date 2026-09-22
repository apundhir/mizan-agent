"""Intake: refuse a submission that is not what it says it is, before reading a single PDF page.

Everything downstream assumes it is looking at one hotel's quarter. Nothing downstream checks that
assumption, and nothing downstream *can*: the metric library is handed records, and records do not
remember which file set they came from. So the check belongs here, at the only point where the
submission is still a set of files rather than a pile of numbers — and it has to happen before
extraction, because a wrong-property submission that is extracted first produces a verdict about a
hotel nobody asked about, with real figures in it.

## The four reasons, and why they are a closed set

`RejectionReason` has exactly four members and intake produces all four. They were declared in M1
and, until now, **never referenced by anything** — the enum existed, the contract accepted it, and
no code path could ever set it. That is the gap this module closes.

| Reason | What intake checked |
|---|---|
| `UNREADABLE_FILE` | a named file is absent, will not open, or the inventory reference will not parse |
| `INCOMPLETE_FILE_SET` | the declared period has a month with no report |
| `PERIOD_MISMATCH` | a file's period does not belong to the declared one, or cannot be read from its name |
| `HOTEL_MISMATCH` | the inventory reference names a property other than the declared one |

## Where the expected identity comes from, and why it is not the files

`Declaration` is supplied by the caller. A system that derives the hotel and period *from the files
it was given* cannot detect the case this module exists for: a submission that is internally
consistent and is for the wrong property or the wrong quarter. Something outside the files must
assert what they are supposed to be. That is an officer's input, or a case management system's
record — and in this POC it is an argument.

## Why the hotel check reads the inventory and not the PDFs

The PMS reports are the documents **under verification**; reading them is extraction, and intake
runs first on purpose. The inventory reference is a different kind of file — the property's own
statement of its room count, reference data rather than evidence — and it carries a `hotel_id`
column that `tda.extract.inventory` has always collected and nothing has ever checked. So intake
checks it.

**Where there is no inventory, intake cannot check the hotel at all**, and says so rather than
passing silently. The check then falls to `check_cover` at `claim_parse`, which compares the
workbook's own cover sheet against the same declaration and raises an `IdentityMismatch`. Two
different files, two different stages, one declaration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tda.contracts import RejectionReason
from tda.extract.inventory import read_inventory
from tda.extract.run import month_of

if TYPE_CHECKING:
    from pathlib import Path

    from tda.graph.state import Declaration, Submission


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why a submission was refused, in a code and a sentence.

    Both, always. The code is what a downstream system routes on and the sentence is what a human
    reads; a rejection carrying only the first is a support ticket, and one carrying only the
    second cannot be counted.
    """

    reason: RejectionReason
    detail: str

    def render(self) -> str:
        return f"{self.reason.value}: {self.detail}"


def _unreadable(path: Path, what: str) -> Rejection | None:
    if not path.exists():
        return Rejection(RejectionReason.UNREADABLE_FILE, f"{what} {path.name} is not present")
    if not path.is_file():
        return Rejection(RejectionReason.UNREADABLE_FILE, f"{what} {path.name} is not a file")
    return None


def check_reports_open(submission: Submission) -> Rejection | None:
    """Every report and the workbook can actually be opened, not merely found on disk.

    This is what `UNREADABLE_FILE` was declared for, and it has to happen here rather than at
    extraction for a reason the `Finding` contract made unavoidable: a document that will not open
    has **no page to cite**, and D-EV-01 refuses a finding that cites nothing on either side.
    Inventing `page=1` to satisfy the type would put a false citation in front of a reviewer, which
    `tda.contracts.refs` warns about in as many words.

    So a whole-document failure is a *rejection*, and extraction deals only with files already
    known to open. Row-level defects inside a readable document remain findings, which is what
    the orchestrated graph's table means by "halt with a blocking finding": those can cite the page they failed on.

    The open is a page count and nothing more. It costs a fraction of a parse and it is the only
    way to answer the question honestly.
    """
    import openpyxl
    import pdfplumber

    for report in submission.reports:
        try:
            with pdfplumber.open(report) as document:
                if not document.pages:
                    return Rejection(
                        RejectionReason.UNREADABLE_FILE,
                        f"{report.name} opens but has no pages",
                    )
        except Exception as exc:
            return Rejection(
                RejectionReason.UNREADABLE_FILE,
                f"{report.name} cannot be opened: {type(exc).__name__}: {exc}",
            )

    # The workbook too, and for the same reason. A corrupt .xlsx is not a zip file, and
    # `open_submission` would let `BadZipFile` escape from the middle of `claim_parse` - the exact
    # crash this check exists to turn into a stated refusal.
    try:
        openpyxl.load_workbook(submission.workbook, read_only=True).close()
    except Exception as exc:
        return Rejection(
            RejectionReason.UNREADABLE_FILE,
            f"{submission.workbook.name} cannot be opened: {type(exc).__name__}: {exc}",
        )
    return None


def check_files_present(submission: Submission) -> Rejection | None:
    """Every named file exists and is a file. Nothing else can be checked until this holds."""
    for report in submission.reports:
        if found := _unreadable(report, "report"):
            return found
    if found := _unreadable(submission.workbook, "workbook"):
        return found
    if submission.inventory is not None and (
        found := _unreadable(submission.inventory, "inventory")
    ):
        return found
    return None


def check_period(submission: Submission, declared: Declaration) -> Rejection | None:
    """One report per month of the declared period, and nothing from outside it.

    The month comes from the file name, which is the same convention `tda.extract.run.month_of`
    already relies on to decide which month's printed totals a report is reconciled against. A name
    it cannot parse is a period that cannot be established, which is a `PERIOD_MISMATCH` rather
    than an incomplete set: the file is *there*, and what is wrong is that nobody can say what it
    covers.

    A missing month and a foreign month are different rejections on purpose. "You did not send
    February" and "you sent April" send an officer to two different conversations.
    """
    expected = {str(month) for month in declared.period.months()}

    seen: dict[str, Path] = {}
    for report in submission.reports:
        try:
            month = month_of(report)
        except ValueError:
            return Rejection(
                RejectionReason.PERIOD_MISMATCH,
                f"cannot read a period from {report.name}; a report is named pms_YYYY-MM.pdf and "
                "the month in that name is what it is reconciled against",
            )
        if month not in expected:
            return Rejection(
                RejectionReason.PERIOD_MISMATCH,
                f"{report.name} covers {month}, which is not part of {declared.period}",
            )
        if month in seen:
            return Rejection(
                RejectionReason.PERIOD_MISMATCH,
                f"{report.name} and {seen[month].name} both cover {month}; which one is the "
                "submission is not a question this system may answer for a hotel",
            )
        seen[month] = report

    if missing := sorted(expected - set(seen)):
        return Rejection(
            RejectionReason.INCOMPLETE_FILE_SET,
            f"{declared.period} needs a report for each of {sorted(expected)}; "
            f"nothing was submitted for {missing}",
        )

    # A substring test would let `claims_2026-Q1.xlsx` satisfy a declaration for the whole of
    # `2026`, because "2026" is a substring of "2026-Q1". The period is parsed out of the name and
    # compared as a period instead.
    stem = submission.workbook.stem.rpartition("_")[2] or submission.workbook.stem
    if stem != str(declared.period):
        return Rejection(
            RejectionReason.PERIOD_MISMATCH,
            f"the workbook {submission.workbook.name} names {stem!r}, not {declared.period}. Its "
            "cover sheet is checked against the declaration as well, at claim parsing",
        )
    return None


def check_hotel(submission: Submission, declared: Declaration) -> Rejection | None:
    """The inventory reference names the declared property, and only that property.

    Returns `None` when there is no inventory to check — see the module docstring on why that is a
    deferral rather than a pass, and where the check lands instead.
    """
    if submission.inventory is None:
        return None
    try:
        reference = read_inventory(submission.inventory, declared.period)
    except Exception as exc:
        # Deliberately not just `InventoryError`. `read_inventory` opens the file as UTF-8 and does
        # not wrap a decode failure, so a non-UTF-8 CSV raises `UnicodeDecodeError` — which is a
        # `ValueError`, not an `InventoryError`, and would escape intake as a raw traceback. The
        # question intake is answering is "can this file be used?", and every way of failing that
        # question has the same answer.
        return Rejection(
            RejectionReason.UNREADABLE_FILE,
            f"the inventory reference {submission.inventory.name} will not parse: {exc}",
        )

    foreign = sorted(reference.hotel_ids - {declared.hotel_id})
    if foreign:
        return Rejection(
            RejectionReason.HOTEL_MISMATCH,
            f"the inventory reference names {foreign}, and this submission was declared for "
            f"{declared.hotel_id}. A verdict built from another property's room counts would be "
            "wrong in a way nothing downstream could detect",
        )
    return None


def intake(submission: Submission, declared: Declaration) -> Rejection | None:
    """Every intake check, in the only order they can run in.

    Readability first, because nothing can be said about a file that is not there. Period next,
    because it is established from names and needs nothing opened. Hotel last, because it is the
    one check that reads a file — and by then the file is known to exist.

    Returns the **first** rejection rather than all of them. A submission with a missing report and
    the wrong property is not two problems to work through in parallel; it is one submission to
    send back, and the first stated reason is enough to send it.
    """
    return (
        check_files_present(submission)
        or check_period(submission, declared)
        or check_reports_open(submission)
        or check_hotel(submission, declared)
    )
