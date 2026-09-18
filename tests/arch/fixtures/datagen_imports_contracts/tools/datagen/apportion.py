"""VIOLATION FIXTURE - the tautology, wearing a hat.

The obvious shortcut is importing `tda.metrics`, and the fixture next door covers it. This is the
one somebody reaches for *after* being told not to do that: "I am not using the metric library,
I am only using the contracts." But `ReservationRecord.occupied_nights()` is exactly D-RNS-03's
month apportionment — the clause the whole apportionment test rests on — so a generator that calls
it derives ground truth from the code that ground truth is meant to check.

This is why rule 2 forbids all of `tda` rather than just `tda.metrics`. The narrower rule would
have accepted this file.
"""

from collections import Counter

from tda.contracts import ReservationRecord


def room_nights_by_month(records: list[ReservationRecord]) -> Counter[str]:
    totals: Counter[str] = Counter()
    for record in records:
        for night in record.occupied_nights():
            totals[f"{night.year:04d}-{night.month:02d}"] += record.rooms
    return totals
