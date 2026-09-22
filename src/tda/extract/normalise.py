"""Label normalisation against committed lookups. Nothing is ever guessed.

Three lookups, one matching rule, and one failure mode. The matching rule is D-NAT-10 —
case-insensitive, punctuation-insensitive, internal whitespace collapsed — and it is implemented once
here rather than per lookup, because three near-identical normalisers would eventually disagree about
whether a trailing full stop matters.

**The failure mode is the feature.** An unmapped label raises `UnmappableLabelError` carrying the exact
string that did not match. Not the closest match, not a best guess, not a default:

- **Not by edit distance.** `Austria` and `Australia` differ by three characters and by eleven
  thousand kilometres. Any threshold loose enough to catch a real typo is loose enough to catch that.
- **Not by substring.** `Guinea` is a substring of `Papua New Guinea`, and `Niger` of `Nigeria`.
- **Not by a model's best effort.** The resolution agent may *propose* a mapping for a human
  to accept, and its abstention produces the same blocking finding (D-NAT-13). It never resolves one
  silently, because a guest counted under the wrong country is a wrong number presented as a right one
  and nothing downstream can detect it.

A refusal costs a human five minutes and a row in a YAML file. A silent mismapping costs the
credibility of every number in the submission.

**Codes are validated, not trusted.** `ReservationRecord.nationality_iso2` is constrained to
`^[A-Z]{2}$`, which `QQ` satisfies. The alpha-2 and alpha-3 codes are themselves keys in the lookup,
so a document carrying a code resolves through the same path as one carrying a label — and an invented
code is rejected rather than pattern-matched into a metric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml

from tda.contracts import RateCode, Status

REFERENCE_DIR: Final = Path(__file__).parent / "reference"
COUNTRY_LOOKUP_PATH: Final = REFERENCE_DIR / "country_lookup.yaml"
STATUS_LOOKUP_PATH: Final = REFERENCE_DIR / "status_lookup.yaml"
RATE_CODE_LOOKUP_PATH: Final = REFERENCE_DIR / "rate_code_lookup.yaml"

# Everything that is not a letter, a digit or a space is dropped before matching. Deliberately
# aggressive: it collapses `Czech Rep.`, `Korea, Republic of`, `U.S.A.` and `Hong-Kong` onto the same
# keys as their punctuation-free forms, which is exactly D-NAT-10's intent.
_STRIPPABLE: Final = re.compile(r"[^0-9a-z ]+")
_WHITESPACE: Final = re.compile(r"\s+")


class LookupTableError(Exception):
    """A lookup table is malformed. A build-time defect, not a data problem.

    Raised when two entries claim the same alias, or a code does not match its own key. Both are
    silent in a table nobody validates: whichever entry loaded last would win, and every document
    using that alias would resolve to the wrong country for as long as nobody noticed.
    """


@dataclass(frozen=True, slots=True)
class UnmappableLabelError(Exception):
    """A label the committed lookup does not contain (D-NAT-12, D-QUAL-03).

    Carries the raw string and the normalised form it was matched on, because the two together are
    what a human needs: the raw string is what the document says, and the normalised form shows what
    the matcher actually looked for. A message saying only "unmappable label" sends somebody hunting
    for the difference between `Côte d'Ivoire` and `Cote dIvoire`.
    """

    kind: str
    raw: str
    normalised: str

    def __str__(self) -> str:
        return (
            f"unmappable {self.kind}: {self.raw!r} (matched as {self.normalised!r}). "
            f"It is not in the committed lookup and is never guessed - not by edit distance, not by "
            f"substring, not by a model. Add it to the lookup, or record the blocking finding."
        )


def normalise_key(value: str) -> str:
    """The one matching rule, applied everywhere (D-NAT-10).

    Lower-cased, punctuation removed, internal whitespace collapsed, trimmed. `CZECH  REPUBLIC` and
    `Czech Rep.` both become `czech republic`... except that the second becomes `czech rep`, which is
    why `Czech Rep.` is listed as an alias in its own right. Normalisation makes spelling variations
    match; it does not invent abbreviations.
    """
    return _WHITESPACE.sub(" ", _STRIPPABLE.sub("", value.strip().lower())).strip()


def _build_index(
    kind: str, entries: dict[str, dict[str, object]], *, extra_keys: tuple[str, ...] = ()
) -> dict[str, str]:
    """Flatten a lookup table into `normalised alias -> canonical code`, refusing collisions.

    `extra_keys` names the fields whose *values* are also aliases — `alpha3` for countries. The key
    itself is always an alias, so a document already carrying the canonical code resolves through the
    same path rather than through a special case that could drift from it.
    """
    index: dict[str, str] = {}

    def add(alias: str, code: str) -> None:
        key = normalise_key(alias)
        if not key:
            raise LookupTableError(f"{kind} {code}: empty alias {alias!r}")
        existing = index.get(key)
        if existing is not None and existing != code:
            raise LookupTableError(
                f"{kind}: {alias!r} maps to both {existing} and {code}. An ambiguous alias is worse "
                "than a missing one - whichever entry loaded last would win, and every document "
                "using it would resolve to the wrong value until somebody noticed."
            )
        index[key] = code

    for code, entry in entries.items():
        # A non-string key means YAML parsed the code as something else, and there is one specific
        # way that happens: YAML 1.1 reads the unquoted token `NO` as the boolean `false`, so Norway
        # silently stops being a country. It cost half an hour the first time. The tables quote every
        # key, and this says why rather than letting the next person meet an AttributeError on
        # `bool.strip`.
        if not isinstance(code, str):
            raise LookupTableError(
                f"{kind}: key {code!r} is a {type(code).__name__}, not a string. YAML 1.1 parses "
                "unquoted NO, Y, N, ON and OFF as booleans - quote the key."
            )
        if not isinstance(entry, dict):
            raise LookupTableError(
                f"{kind} {code}: entry must be a mapping, got {type(entry).__name__}"
            )
        add(code, code)
        for field in extra_keys:
            value = entry.get(field)
            if isinstance(value, str):
                add(value, code)
        name = entry.get("name")
        if isinstance(name, str):
            add(name, code)
        aliases = entry.get("aliases", [])
        if not isinstance(aliases, list):
            raise LookupTableError(
                f"{kind} {code}: aliases must be a list, got {type(aliases).__name__}"
            )
        for alias in aliases:
            if not isinstance(alias, str):
                raise LookupTableError(f"{kind} {code}: alias {alias!r} is not a string")
            add(alias, code)

    return index


def _load(path: Path, root_key: str) -> tuple[str, dict[str, dict[str, object]]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise LookupTableError(f"{path.name} did not parse to a mapping")
    entries = document.get(root_key)
    if not isinstance(entries, dict) or not entries:
        raise LookupTableError(f"{path.name} has no non-empty `{root_key}` mapping")
    version = str(document.get("version", ""))
    if not version:
        raise LookupTableError(f"{path.name} has no version. A lookup without one cannot be cited.")
    return version, entries


@dataclass(frozen=True, slots=True)
class Lookups:
    """The three committed tables, flattened and validated.

    Versions are carried so a verdict can name the tables that produced it. A normalisation is as
    load-bearing as a metric rule — `Czech Republic → CZ` decides which row a guest lands in — and a
    number whose lookup version is unknown is no more defensible than one whose policy version is.
    """

    country_version: str
    status_version: str
    rate_code_version: str
    countries: dict[str, str]
    statuses: dict[str, str]
    rate_codes: dict[str, str]

    @property
    def canonical_countries(self) -> frozenset[str]:
        """Every alpha-2 code the table knows. Used to validate a code without resolving it."""
        return frozenset(self.countries.values())


@lru_cache(maxsize=1)
def load_lookups() -> Lookups:
    """Load and validate the three tables. Cached: they are committed files and never change in-run.

    Cached rather than re-read because a lookup that could change mid-run would make two identical
    labels in one document resolve differently, which is the kind of defect that survives for years.
    """
    country_version, countries = _load(COUNTRY_LOOKUP_PATH, "countries")
    status_version, statuses = _load(STATUS_LOOKUP_PATH, "statuses")
    rate_version, rate_codes = _load(RATE_CODE_LOOKUP_PATH, "rate_codes")

    for code in statuses:
        if code not in Status.__members__:
            raise LookupTableError(
                f"status_lookup.yaml declares {code!r}, which is not a Status enum member. The enum "
                "is the closed set (D-QUAL-03); the lookup maps onto it and cannot extend it."
            )
    for code in rate_codes:
        if code not in RateCode.__members__:
            raise LookupTableError(
                f"rate_code_lookup.yaml declares {code!r}, which is not a RateCode enum member."
            )

    return Lookups(
        country_version=country_version,
        status_version=status_version,
        rate_code_version=rate_version,
        countries=_build_index("country", countries, extra_keys=("alpha3",)),
        statuses=_build_index("status", statuses),
        rate_codes=_build_index("rate code", rate_codes),
    )


def _resolve(kind: str, raw: str, index: dict[str, str]) -> str:
    key = normalise_key(raw)
    code = index.get(key)
    if code is None:
        raise UnmappableLabelError(kind=kind, raw=raw, normalised=key)
    return code


def nationality(raw: str, lookups: Lookups | None = None) -> str:
    """A label or code to ISO 3166-1 alpha-2 (D-NAT-09). Raises `UnmappableLabelError` on a miss."""
    return _resolve("country", raw, (lookups or load_lookups()).countries)


def status(raw: str, lookups: Lookups | None = None) -> Status:
    """A printed status to the closed enum (D-QUAL-03). Raises `UnmappableLabelError` on a miss."""
    return Status(_resolve("status", raw, (lookups or load_lookups()).statuses))


def rate_code(raw: str, lookups: Lookups | None = None) -> RateCode:
    """A printed rate code to the closed enum (D-QUAL-04). Raises `UnmappableLabelError` on a miss."""
    return RateCode(_resolve("rate code", raw, (lookups or load_lookups()).rate_codes))
