"""Nothing personal reaches an artifact, and what was removed is counted.

observability asks for a test that scans the trace for name-shaped content from the corpus. **The corpus
contains no names**, by a design decision made in M2: `tools/datagen/ledger.py` produces a salted
`guest_ref` hash and says why — *"a generator that invented plausible names and then withheld them
from the output would be one careless `print` away from putting a name in a log."* So a scan for
corpus names would find nothing and pass for the wrong reason, which is worse than not having one.

The real exposure is elsewhere, and it is demonstrable. `tda.excel.tools.digest` copies **every
label cell** of the submitted workbook into the mapping agent's prompt verbatim — redaction there
is semantic and suppresses only cells that *read as numbers*, because the agent needs the labels to
do its job. A hotel that types

    A9: Prepared by Jane Doe, jane.doe@hotel.ae, +971 50 123 4567

into a header cell has put that string in the prompt, hence in the cassette's `request_canonical`,
and it can re-emerge through `UnmappedBlock.reason` — a free-prose field — into `trace.jsonl`.

That is the leak worth closing, and no amount of care inside this repository prevents it: the text
comes from a file somebody else wrote.

## Redaction, not refusal

The obvious design is to refuse to write an artifact containing personal data. It is wrong: the
trace is the evidence that answers *"why did it say that?"*, and withholding it because one header
cell had a phone number in it destroys the record to protect it.

So the text is redacted in place and **the fact of redaction is recorded** — which pattern fired
and how many times, never the content. A reader of the ledger can see that something was removed
and ask the submitting property about it, which is the correct next step and is not available if
the artifact is simply missing.

## The patterns are narrow on purpose

Every one of them fires on a *structural* signal — an `@`, an international dialling prefix, an
honorific, a `SURNAME/FORENAME` pair — rather than on "looks like a name". A pattern matching
capitalised word pairs would fire on `Room Nights`, `Rate Revenue` and half the workbook, and a
guard that cries wolf on correct data is a guard somebody switches off. `tools/guard/secret_guard.py`
makes the same argument about its own regexes, and this follows its shape deliberately.

The cost of narrowness is stated plainly: **this does not catch a bare name.** `A9: Jane Doe` alone
passes. Nothing structural distinguishes it from `A9: Deluxe King`, and a rule that caught the first
would flag the second on every run. What closes that gap is the corpus design — no names exist to
leak — and, for a real pilot, the pseudonymisation boundary the build plan defers until a real-file
pilot is agreed.

## One pattern here is not personal data

`api_key` exists for a different reason than the other four. The Run console accepts uploads and
questions from whoever holds its link, and a credential-shaped string can reach an artifact the
same way a phone number can: typed into a workbook cell, or into a question for the assistant.
Nothing in this codebase ever writes its own key into a workbook or a trace, so a hit here always
means a person typed one, and the advice is to rotate it regardless of how it got there.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True, slots=True)
class Pattern:
    """One structural signal, with the replacement that stands in for what it matched.

    `advice` is what a human reads when the guard reports a hit, and it names the *next action*
    rather than restating the finding. A report that says "possible phone number" and nothing else
    leaves the reader to work out whose problem it is.
    """

    name: str
    regex: re.Pattern[str]
    advice: str

    @property
    def placeholder(self) -> str:
        return f"[redacted:{self.name}]"


PATTERNS: Final[tuple[Pattern, ...]] = (
    Pattern(
        name="api_key",
        # Not personal data - the one pattern here that is not. The Run console (v0.6.0) accepts
        # uploads and questions from whoever holds the app's link, so a credential-shaped string
        # can arrive in a workbook cell or a typed question the same way a phone number can.
        # Nothing in this system writes its own key into a workbook or a trace; this catches one
        # that a person typed. Identical regex to `tools/guard/secret_guard.py`'s Anthropic key
        # pattern - see `tests/unit/test_obs.py` for the test that keeps the two in step.
        regex=re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
        advice=(
            "A credential-shaped string reached an artifact. Nothing in this system writes its own "
            "key, so it was typed into a workbook cell or a question. Rotate it in the Anthropic "
            "Console regardless: a key in an artifact is a key in a file somebody will share."
        ),
    ),
    Pattern(
        name="email",
        # Unambiguous: an `@` between two label-shaped runs with a dotted TLD. No workbook header
        # legitimately contains one.
        regex=re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]*[a-zA-Z]{2,}\b"),
        advice=(
            "An email address reached an artifact. It came from the submitted workbook, not from "
            "this system - ask the property to remove contact details from the sheet."
        ),
    ),
    Pattern(
        name="phone",
        # An international dialling prefix followed by enough digits to be a number rather than a
        # date or a range. Requires the leading `+`, which is what keeps `2026-01` and `1,285` out.
        regex=re.compile(r"\+\d{1,3}[\s.-]?\(?\d{1,4}\)?[\s.-]?\d[\d\s.-]{5,}\d"),
        advice=(
            "A telephone number reached an artifact, from the submitted workbook. The same "
            "remedy: contact details do not belong in a statistical return."
        ),
    ),
    Pattern(
        name="titled_name",
        # An honorific is a strong structural signal and a short closed set. This is the one
        # pattern that catches a person rather than a channel.
        #
        # **It takes up to three capitalised words, not one.** The first version took one, so
        # `Ms. Jane Doe` became `[redacted:titled_name] Doe` - the surname, the most identifying
        # half, written straight to the artifact. The test guarding this asserted `"Jane Doe" not
        # in text` and passed, because redaction had split the string rather than removed it. Three
        # covers `Dr. Jane Marie Doe`; a bound rather than `+` because an unbounded run would eat
        # `Dr. Smith Room Nights Q1` whole.
        regex=re.compile(
            r"\b(?:Mr|Mrs|Ms|Miss|Dr|Prof|Sheikh|Sheikha|Eng)\.?(?:\s+[A-Z][a-zA-Z'`-]+){1,3}"
        ),
        advice=(
            "A titled personal name reached an artifact. Check the submitted workbook's header "
            "cells - `tda.excel.tools.digest` copies every label verbatim, by design."
        ),
    ),
    Pattern(
        name="slashed_name",
        # `DOE/JANE`, the form a PMS export uses. Three letters each side, so `RN/Mo` and `Q1/Q2`
        # do not fire.
        regex=re.compile(r"\b[A-Z]{3,}/[A-Z]{3,}\b"),
        advice=(
            "A `SURNAME/FORENAME` pair reached an artifact - the shape a PMS passenger-name "
            "record uses. `ReservationRecord.guest_ref` rejects this form; something upstream of "
            "the contract carried it."
        ),
    ),
)

BY_NAME: Final[dict[str, Pattern]] = {pattern.name: pattern for pattern in PATTERNS}


@dataclass(frozen=True, slots=True)
class Redaction:
    """What was removed from one artifact, by kind and by count. Never the content.

    Recording the content would defeat the entire exercise — a ledger field listing the phone
    numbers it redacted from the trace is a ledger that leaks them.
    """

    counts: tuple[tuple[str, int], ...] = ()

    @property
    def clean(self) -> bool:
        return not self.counts

    @property
    def total(self) -> int:
        return sum(count for _, count in self.counts)

    def render(self) -> str:
        if self.clean:
            return "nothing redacted"
        return ", ".join(f"{name} x{count}" for name, count in self.counts)


def scan(text: str) -> Redaction:
    """What personal data is in this text, by kind and count. Changes nothing.

    Defined as `redact(text)[1]` rather than as its own loop, and the reason is a disagreement the
    two versions actually had. `redact` substitutes in sequence, so a later pattern sees the text
    the earlier ones left; an independent `findall` per pattern does not. On
    `+971.50.123.4567@hotel.ae` the loop counted an email *and* a phone, while the writer recorded
    only the email - so the ledger's counts and a guard's counts disagreed about identical bytes.

    One of the two had to be authoritative, and it has to be the one that decides what is written.
    """
    return redact(text)[1]


def redact(text: str) -> tuple[str, Redaction]:
    """The text with every match replaced by its placeholder, and a record of what went.

    Applied to the whole serialised artifact rather than per field, because the field a leak
    arrives in is not predictable: it came from a workbook cell, and which contract field carries
    it depends on how the mapping agent chose to describe the sheet.
    """
    cleaned = text
    found: Counter[str] = Counter()
    for pattern in PATTERNS:
        cleaned, hits = pattern.regex.subn(pattern.placeholder, cleaned)
        if hits:
            found[pattern.name] = hits
    return cleaned, Redaction(counts=tuple(sorted(found.items())))


def redact_lines(lines: Iterable[str]) -> tuple[list[str], Redaction]:
    """`redact` over many lines, with one combined record. For JSON Lines artifacts."""
    cleaned: list[str] = []
    found: Counter[str] = Counter()
    for line in lines:
        text, redaction = redact(line)
        cleaned.append(text)
        for name, count in redaction.counts:
            found[name] += count
    return cleaned, Redaction(counts=tuple(sorted(found.items())))
