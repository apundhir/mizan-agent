"""Making a `.docx` and an `.xlsx` byte-reproducible, because neither is by accident.

Both are zip archives, and two things inside them carry the wall clock no matter what the caller
does:

**The document properties.** openpyxl overwrites `dcterms:modified` with the current time on every
save — setting `workbook.properties.modified` before saving does not survive, which is worth knowing
because it looks like it should. python-docx does the same on its own core properties.

**The zip entries.** Every member carries a DOS timestamp, and the writer takes it from the clock.
Two runs a second apart differ in the container even when every byte of content matches.

Neither is content in any sense a reader cares about, and both defeat `make repro`, which
exists to demonstrate that the same submission produces the same answer. A diff harness that has to
know which differences are meaningless is a diff harness somebody eventually teaches to ignore a
difference that was not.

So the file is repacked once, after the library that wrote it has finished: the properties are
pinned to a fixed instant and every entry gets the same timestamp. The result is byte-identical
across runs, across machines, and across the second boundary the container's two-second granularity
would otherwise make a coin toss.

## Why repack rather than normalise in the writers

Because the writers do not offer the choice. The value is stamped inside `save()`, after the last
hook either library exposes. Rewriting the archive afterwards is the only place the decision can be
made, and doing it in one function means the memo and the workbook cannot drift into being
reproducible to different degrees.
"""

from __future__ import annotations

import re
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

# The instant every generated document claims to have been created and modified at. Arbitrary and
# fixed: a real timestamp belongs in the run ledger, which records when the run happened and is
# excluded from the repro diff for exactly that reason (ADR-0006).
EPOCH: Final = datetime(2020, 1, 1, tzinfo=UTC)

CORE_PROPERTIES: Final = "docProps/core.xml"

# `<dcterms:created …>2026-09-14T10:48:22Z</dcterms:created>`, and the same for `modified`. Matched
# rather than parsed: the surrounding attributes differ between the two writers, and an XML
# round-trip would reformat parts of the file this has no business touching.
#
# The closing tag is a backreference to the opening one's name, so the pattern cannot match from a
# `<dcterms:created>` to a `</dcterms:modified>` and swallow everything between them.
_TIMESTAMP = re.compile(rb"(<dcterms:(created|modified)\b[^>]*>)[^<]*(</dcterms:\2>)")


def make_reproducible(path: Path, *, stamp: datetime = EPOCH) -> None:
    """Repack an OOXML file so two runs of one submission produce identical bytes.

    Reads the whole archive into memory first: these documents are tens of kilobytes, and the
    alternative — writing the new archive while reading the old one — is a partial file on disk if
    anything raises halfway.
    """
    with zipfile.ZipFile(path) as archive:
        entries = [(info, archive.read(info.filename)) for info in archive.infolist()]

    when = stamp.timetuple()[:6]
    replacement = stamp.strftime("%Y-%m-%dT%H:%M:%SZ").encode()
    temporary = path.with_suffix(f"{path.suffix}.repacking")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as rebuilt:
            for info, payload in entries:
                # `\\g<1>`, not `\\1`: the replacement is followed by a digit, and `\\12020-…`
                # reads as group 12. The first version of this produced a `core.xml` no XML parser
                # would open, which is a fine way to find out that a template is a string.
                data = (
                    _TIMESTAMP.sub(rb"\g<1>" + replacement + rb"\g<3>", payload)
                    if info.filename == CORE_PROPERTIES
                    else payload
                )
                # A fresh `ZipInfo` rather than the original: the original carries the timestamp
                # this function exists to remove.
                pinned = zipfile.ZipInfo(info.filename, date_time=when)
                pinned.compress_type = info.compress_type
                pinned.external_attr = info.external_attr
                pinned.create_system = info.create_system
                rebuilt.writestr(pinned, data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
