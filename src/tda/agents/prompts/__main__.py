"""`python -m tda.agents.prompts --write-manifest`.

A separate entry point rather than `python -m ...registry`, which triggers a `runpy` warning
because the package's `__init__` already imported that module. The warning is harmless and a
developer tool that prints a warning on every run teaches people to ignore warnings.
"""

from tda.agents.prompts.registry import main

if __name__ == "__main__":
    raise SystemExit(main())
