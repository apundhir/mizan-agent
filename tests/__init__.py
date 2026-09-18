"""Test packages.

`tests`, `tests/eval` and `tests/eval/agents` carry `__init__.py` so that
`tests/eval/agents/harness.py` has exactly one module name. Without them, `mypy --strict` sees the
same file as both `harness` and `tests.eval.agents.harness` and refuses to check anything.

`tests/unit` and `tests/arch` deliberately do not, and nothing imports across them - each test
file is collected on its own.
"""
