"""Byte-reproducibility — the part that is easy to claim and easy to get wrong.

`make repro` runs the pipeline twice and diffs the verdict. If the corpus is not itself
byte-reproducible, that target measures the *generator's* entropy rather than the pipeline's
determinism, and it would pass or fail for reasons nobody could trace. So the corpus has to be
reproducible first, and the two document formats each default to not being.

Both hazards below were confirmed by running them, not by reading documentation:

**PDF — reportlab stamps a creation date and a document ID.** Two identical renders a second
apart produce different bytes. `Canvas(invariant=1)` fixes both. Verified: with `invariant=1` the
digests match, without it they do not.

**XLSX — three separate things move, and fixing the obvious one hides the others.** Each was found
by generating twice and diffing, in this order:

1. `Workbook.save` writes every zip member with `ZipFile.writestr`, which stamps `time.localtime()`
   into the entry header. So two saves a second apart differ even when every byte of content is
   identical. `normalise_zip` rewrites the archive with a pinned `date_time`.
2. `docProps/core.xml` carries `dcterms:created`, which `wb.properties.created` pins.
3. `dcterms:modified` does **not** stay pinned: `openpyxl.writer.excel.save_workbook` assigns
   `properties.modified = datetime.now(UTC)` on the way out, *after* anything the caller set. This
   is the one that looks solved and is not — the first two fixes leave a file that still differs by
   one timestamp, one element deep in one member. `_patch_core_properties` rewrites it after the
   save, which is the only point at which it can be rewritten.

`finalise_xlsx` does all three, so there is one call to make and one place where the reasoning lives.

Nothing here is a workaround for a bug in either library. A modification timestamp is correct
behaviour for a document somebody authored; it is wrong for a document a build produces, and a build
that wants reproducibility has to say so.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from typing import TYPE_CHECKING, Final

from datagen.spec import PINNED_OOXML_MODIFIED, PINNED_ZIP_DATE_TIME

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Written with a trailing newline and `\n` line endings everywhere, so a corpus regenerated on a
# different platform is the same corpus. `newline=""` on the CSV writer does the same job for
# csv.writer, which would otherwise emit `\r\n`.
LINE_ENDING: Final = "\n"


class ReproducibilityError(RuntimeError):
    """A document could not be made reproducible, and the build must not pretend otherwise.

    Raised rather than warned. A silently-skipped fix here would surface later as `make repro`
    failing for a reason nobody could trace, which is the failure mode this whole module exists
    to prevent.
    """


# The one element openpyxl rewrites on save. Matched surgically rather than by re-serialising the
# XML: an ElementTree round-trip renames namespace prefixes, and an OOXML reader that accepts
# `dcterms:modified` may not accept `ns0:modified`.
_MODIFIED_ELEMENT: Final = re.compile(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)")


def normalise_zip(path: Path, patch: Callable[[str, bytes], bytes] | None = None) -> None:
    """Rewrite a zip archive with pinned entry timestamps, optionally patching members as it goes.

    Member order is preserved rather than sorted. `[Content_Types].xml` is expected first by some
    readers of OOXML, and reordering an archive to make it tidier is exactly the kind of change that
    works in one spreadsheet application and not another. The order openpyxl chose is deterministic
    already; only the timestamps are not.

    Also pins `create_system`, so an archive built on Windows and one built on Linux agree. That
    matters here for the same reason the timestamps do: the committed corpus carries a digest in
    `manifest.json`, and a digest that depends on the build machine is not one anybody can check.
    """
    with zipfile.ZipFile(path) as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]

    temporary = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
        for info, payload in members:
            pinned = zipfile.ZipInfo(filename=info.filename, date_time=PINNED_ZIP_DATE_TIME)
            pinned.compress_type = zipfile.ZIP_DEFLATED
            pinned.external_attr = info.external_attr
            pinned.internal_attr = info.internal_attr
            pinned.create_system = 3  # Unix, regardless of where this ran
            target.writestr(pinned, patch(info.filename, payload) if patch else payload)

    temporary.replace(path)


def _patch_core_properties(name: str, payload: bytes) -> bytes:
    """Pin `dcterms:modified`, which openpyxl stamps with the wall clock on the way out.

    Asserted rather than attempted: if the element is not found exactly once, this raises. A
    substitution that silently matched nothing is precisely how a file goes back to being
    non-reproducible without anybody noticing — the fix would still be in the code, doing nothing.
    """
    if name != "docProps/core.xml":
        return payload

    patched, count = _MODIFIED_ELEMENT.subn(
        rb"\g<1>" + PINNED_OOXML_MODIFIED.encode() + rb"\g<2>", payload
    )
    if count != 1:
        raise ReproducibilityError(
            f"expected exactly one <dcterms:modified> in docProps/core.xml, found {count}. "
            "openpyxl's serialisation has changed. Without this substitution the workbook carries "
            "the wall-clock time of the build and `make corpus` will fail on every machine."
        )
    return patched


def finalise_xlsx(path: Path) -> None:
    """Make a saved workbook byte-reproducible. All three fixes, in one call.

    Called after `Workbook.save` because the `dcterms:modified` stamp is applied *by* the save;
    there is no earlier point at which it can be pinned.
    """
    normalise_zip(path, patch=_patch_core_properties)


def write_text(path: Path, content: str) -> None:
    """Write UTF-8 with `\\n` endings and exactly one trailing newline.

    Explicit `newline=LINE_ENDING` rather than the platform default: text written with `\\r\\n` on
    Windows would give the same file a different digest, and the digests are committed.
    """
    body = content if content.endswith(LINE_ENDING) else content + LINE_ENDING
    path.write_text(body, encoding="utf-8", newline=LINE_ENDING)


def write_json(path: Path, payload: object) -> None:
    """Canonical JSON: sorted keys, two-space indent, one trailing newline.

    Sorted so the file is diffable and a refactor of the producing code cannot reorder it. Indented
    rather than compact because these files are read by humans reviewing a PR — `truth_metrics.json`
    is the document a reviewer checks a number against.
    """
    write_text(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def digest(path: Path) -> str:
    """SHA-256 of a file, as `sha256:<hex>`.

    Prefixed with the algorithm because an unlabelled 64-character hex string in a manifest is a
    small mystery for whoever has to verify it later.
    """
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
