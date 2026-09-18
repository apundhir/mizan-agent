"""VIOLATION FIXTURE - a "pure" function reading a file.

Reads like a convenience. Makes the function's result depend on a path on disk, so two runs on
two machines can disagree and `make repro` becomes meaningless.
"""

from pathlib import Path


def rooms_available(day: str) -> int:
    return int(Path("/etc/inventory.csv").read_text().splitlines()[0])
