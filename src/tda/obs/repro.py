"""Which fields are allowed to differ between two runs of the same submission.

`make repro` (PRD-94) runs the pipeline twice and diffs. Something has to say which differences are
expected, and the decision taken in M1 — on `Verdict.decided_at` — is that the field says it
itself, in its own description. A parallel list kept somewhere else drifts out of step with the
model it describes, and the drift is silent: the diff simply starts passing over a field that has
begun to vary, or failing on one that no longer does.

## Why this is a module rather than a classmethod

The first version of this walked `RunLedger.model_fields` and stopped there. It missed the two
fields that actually vary on every run — `NodeTiming.duration_ms` and `AgentUsage.duration_ms` —
because both are *nested* inside the ledger, and a diff excluding only the top level would have
reported a reproducibility defect on every single run. That is the failure this module exists to
prevent, and it was found by reading the output of two runs rather than by reading the code.

So the marker lives here, where `tda.obs.usage`, `tda.obs.trace`, `tda.obs.nodes` and
`tda.obs.ledger` can all reach it without importing each other, and the walker recurses.

## Dotted paths, and what a caller does with them

`volatile_paths(RunLedger)` returns `{"run_id", "duration_ms", "nodes.duration_ms",
"usage.duration_ms"}`. A path with a dot names a field of a nested model, however many of that
model the parent holds — `nodes.duration_ms` covers every row in `nodes`, because a diff harness
excludes a *field*, not one occurrence of it.

`trace.jsonl` and `nodes.jsonl` need the same answer about their own record types, so the walker
takes any model rather than being a method on the ledger.
"""

from __future__ import annotations

import typing
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Iterator

# The sentence a field's description carries to say it varies between two runs of the same
# submission. Matched literally, because a marker nobody can grep for is a marker that rots.
REPRO_EXCLUDED: Final = "Excluded from the repro diff."


def volatile_paths(
    model: type[BaseModel], *, _prefix: str = "", _seen: frozenset[str] = frozenset()
) -> frozenset[str]:
    """Every field of `model`, nested fields included, whose description carries the marker.

    Recursion is bounded by `_seen`: a model that (directly or otherwise) contains itself would
    otherwise walk forever, and returning a partial answer is better than not returning one.
    """
    if model.__name__ in _seen:
        return frozenset()
    seen = _seen | {model.__name__}

    found: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{_prefix}{name}"
        if REPRO_EXCLUDED in (field.description or ""):
            found.add(path)
        for nested in _models_in(field.annotation):
            found |= volatile_paths(nested, _prefix=f"{path}.", _seen=seen)
    return frozenset(found)


def strip_volatile(payload: object, paths: frozenset[str], *, _prefix: str = "") -> object:
    """A JSON-shaped copy of `payload` with every volatile path removed, lists included.

    What a diff harness actually needs: `volatile_paths` names the fields and this removes them, so
    PRD-94 compares two runs without reimplementing the traversal - and without the two
    implementations drifting, which is the failure this whole module is about.
    """
    if isinstance(payload, list):
        return [strip_volatile(item, paths, _prefix=_prefix) for item in payload]
    if not isinstance(payload, dict):
        return payload
    kept: dict[str, object] = {}
    for key, value in payload.items():
        path = f"{_prefix}{key}"
        if path in paths:
            continue
        kept[key] = strip_volatile(value, paths, _prefix=f"{path}.")
    return kept


def _models_in(annotation: object) -> Iterator[type[BaseModel]]:
    """Every `BaseModel` reachable from one annotation — through tuples, lists, unions and `None`.

    Written against `typing.get_args` rather than against the shapes this repository happens to use
    today, because the field that eventually breaks a repro diff will be the one whose container
    nobody thought to handle.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in typing.get_args(annotation):
        yield from _models_in(argument)
