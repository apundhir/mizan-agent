# Mizan - an agentic reconciliation and verification system (POC)
#
# `make ci` is the same command CI runs. If it passes here and fails there, that
# discrepancy is a bug worth fixing in its own right.

PY       := .venv/bin/python
PIP      := .venv/bin/pip
VENV     := .venv
PYTHON   ?= python3.12

.DEFAULT_GOAL := help
.PHONY: help setup ci lint fmt fmt-check types test guard policy prompts \
        datagen corpus fixtures fixtures-verify run trace review eval repro demo \
        record bundle clean

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

$(VENV)/bin/python:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

setup: $(VENV)/bin/python  ## Create the venv and install everything (python 3.12)
	$(PIP) install --quiet -e ".[dev,extract,outputs,agents,review,datagen]"
	@echo "setup complete - run 'make ci'"

# ── the gate ─────────────────────────────────────────────────────────────────
# Ordered cheapest-first so a trivial mistake fails in two seconds rather than
# after the eval. `guard` and `policy` are non-negotiable: see ADR-0001.
#
# `fixtures` runs before `fixtures-verify` because nothing about the fixtures is committed, so
# there is no digest on disk to check a rebuild against the way `corpus` checks one. Building then
# verifying compares two builds from the same code, which is the determinism claim the target is
# really making. `corpus` is the stronger check and stays as it is: it compares a rebuild against
# digests a human committed.
ci: lint fmt-check types guard policy corpus fixtures fixtures-verify test eval repro  ## Everything CI runs, in CI's order
	@echo ""
	@echo "  ci green"

lint:  ## ruff check
	$(PY) -m ruff check src tools tests

fmt:  ## ruff format (writes)
	$(PY) -m ruff format src tools tests

fmt-check:  ## ruff format --check
	$(PY) -m ruff format --check src tools tests

types:  ## mypy --strict
	$(PY) -m mypy

test:  ## pytest
	$(PY) -m pytest

# ── the guards that make the deterministic-core claim real (ADR-0001) ────────
guard:  ## Import guard + agent schema lint + secret guard
	$(PY) tools/guard/import_guard.py
	$(PY) tools/guard/agent_schema_lint.py
	$(PY) tools/guard/secret_guard.py

policy:  ## Validate policy.yaml against its schema, with adversarial cases
	$(PY) tools/policy/validate_policy.py

# ── the synthetic corpus ─────────────────────────────────────────────────────
# PYTHONPATH=tools rather than a console script: the generator is build tooling, not part of the
# shipped package, and a console script would put `datagen` on the PATH of anything that
# pip-installs mizan.
datagen:  ## Regenerate the synthetic corpus, byte-reproducibly
	PYTHONPATH=tools $(PY) -m datagen --out corpus/demo

corpus:  ## Verify the committed corpus still matches the generator (part of `ci`)
	PYTHONPATH=tools $(PY) -m datagen --out corpus/demo --verify

# ── pipeline targets (implemented by their own stories) ──────────────────────
run:  ## Verify one submission end to end (SUBMISSION=... to point elsewhere)
	$(PY) -m tda.cli run $(SUBMISSION)

trace:  ## Render the most recent run as a tree (RUN=<run_id> for an older one)
	$(PY) -m tda.cli trace $(RUN)

review:  ## The officer's screen (RUN=<run_id>, SUBMISSION=<dir> if not the demo corpus)
	MIZAN_RUN=$(RUN) MIZAN_SUBMISSION=$(SUBMISSION) $(PY) -m streamlit run src/tda/review/app.py

# ── the scored fixtures, and the harness over them ───────────────────────────
# `fixtures` mutates a COPY of corpus/demo and derives what each mutation should produce. The demo
# corpus is digested before and after, so "we only write to the copy" is checked rather than
# promised. corpus/fixtures/ is gitignored: only the mutation spec is tracked, because a fixture
# rebuilt from its spec is evidence and a committed binary is an assertion.
fixtures:  ## Build the scored fixtures from corpus/demo (which is never mutated)
	PYTHONPATH=tools $(PY) -m fixtures --out corpus/fixtures

fixtures-verify:  ## The fixtures still match their mutation spec (part of `ci`)
	PYTHONPATH=tools $(PY) -m fixtures --out corpus/fixtures --verify

eval: fixtures  ## Score every fixture against its derived expectation
	$(PY) -m tda.eval --fixtures corpus/fixtures --out artifacts/eval

repro:  ## Run one submission twice and diff the verdict, volatile paths excluded
	$(PY) -m tda.eval.repro

demo: fixtures  ## The three scenes: pass, catch, refusal - against the real pipeline, replay mode, no key
	$(PY) tools/demo/run_demo.py

record: fixtures  ## Refresh model cassettes against the live API (needs ANTHROPIC_API_KEY or .env)
	$(PY) -m tda.agents.provider
	$(PY) -m tda.eval.record_narratives

prompts:  ## Regenerate prompts/MANIFEST.txt after adding a prompt version
	$(PY) -m tda.agents.prompts --write-manifest

bundle:  ## Build the versioned release artefact: dist/mizan-<version>.tar.gz
	$(PY) tools/release/bundle.py

# A target that does not exist yet says so and exits non-zero. A no-op that
# exits 0 is worse than an error: it makes an unbuilt pipeline look green.
_todo:
	@echo "make $(TARGET): not implemented yet - $(ISSUE)"
	@echo "The target is declared here so the interface is settled before the"
	@echo "implementation, and so CI cannot mistake 'absent' for 'passing'."
	@exit 2

clean:  ## Remove caches and run artefacts (corpus is regenerated by datagen)
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov .coverage artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
