"""An eval case, and how to hand one to the agent it belongs to.

Two consumers need this and they must not drift apart: `make record`, which calls every case once
against the live API to produce a cassette, and `tests/eval/agents/`, which replays those cassettes
and scores what comes back. If each had its own idea of how to run a case, the cassettes would be
recorded against one question and replayed against another — the cassette key would miss, every
case would read as unrecorded, and the harness would report a gap that is really a bug.

So *running* a case lives here, in the runtime, because handing an input to an agent is the
runtime's job. *Scoring* the answer lives in `tests/eval/agents/harness.py`, because what counts as
a good answer is a question about the test suite's expectations. The cases themselves are committed
JSON under `tests/eval/agents/cases/` — data, not code, and reviewable as data.

## Why a case states its figures even though the agent never sees them

A narrative case's `input` carries `claimed` and `computed`. The narrative agent is shown neither:
`tda.agents.narrative.read_finding` omits every figure by construction, and the prompt says why. The
case states them anyway because a `Finding` is not constructible without them — and because a case
file that supplies figures the agent provably never receives is a better demonstration of that
guarantee than a sentence claiming it.

## Why a reviewer-assist case carries a whole verdict

The other four agents are asked about one thing — a label, a finding, a workbook. Reviewer-assist
is asked about a *run*, and its citations are checked against what that run contains, so a case
that supplied only a question would be checking the answer against nothing. `verdict_from` builds
the verdict a case describes, and each finding in it may carry its own references precisely so a
fabricated citation is distinguishable from a real one — with one shared placeholder for every
finding, any well-formed reference would pass.

## Placeholder citations, named as such

Narrative and critic cases build a `Finding` with a fixed `PdfRef` and `ExcelRef`. Those are
placeholders and prove nothing about extraction: the agent is shown them only as citation strings,
and what is being evaluated is the prose. The real evidence chain is `tda.extract` and
`tda.excel`'s, and it has its own tests. Naming this here rather than letting a reader assume the
citations were meaningful is the point of the paragraph.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tda.agents import roster
from tda.agents.contracts.narrative import FindingNarrative
from tda.agents.tools import ToolRegistry
from tda.contracts import (
    Dimension,
    ExcelRef,
    ExtractionSummary,
    Finding,
    InventoryRef,
    Metric,
    MetricKey,
    PdfRef,
    Severity,
    VarianceClass,
    Verdict,
    VerdictStatus,
)

if TYPE_CHECKING:
    from tda.agents.contracts.base import AgentOutput
    from tda.agents.runtime import AgentRunner
    from tda.policy import Policy

# See the module docstring. Fixed, and fixed deliberately: a case whose citation varied would
# change the rendered message and therefore the cassette key, re-recording every narrative case
# whenever somebody touched an unrelated fixture.
PLACEHOLDER_PDF = PdfRef(file="pms_2026-01.pdf", page=3, row_start=12, row_end=12)
PLACEHOLDER_EXCEL = ExcelRef(sheet="Occupancy", cell="D14")


class CaseError(Exception):
    """A case file that cannot be run as written."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One committed eval case.

    `expect` holds **property checks** rather than a golden answer. A golden `FindingNarrative`
    would compare a model's prose to one particular sentence, which measures similarity to a
    recording rather than quality — and would fail on every improvement. Properties survive a
    better answer and still catch a wrong one.

    `expect_example` is an answer that must pass every check and `counterexample` one that must
    fail at least one. Neither is evidence about the model: they are evidence about the *scorer*,
    which is a smaller claim and is labelled as such wherever it is used.
    """

    agent: str
    name: str
    why: str
    inputs: dict[str, Any]
    expect: dict[str, Any]
    expect_example: dict[str, Any]
    counterexample: dict[str, Any]

    @property
    def ref(self) -> str:
        return f"{self.agent}/{self.name}"

    @classmethod
    def load(cls, path: Path) -> EvalCase:
        payload = json.loads(path.read_text(encoding="utf-8"))
        missing = {"why", "input", "expect", "expect_example", "counterexample"} - set(payload)
        if missing:
            raise CaseError(
                f"eval case {path} is missing {sorted(missing)}. Every key is required: a case "
                "without a counterexample cannot show the scorer rejects anything, and a case "
                "without a `why` is a check nobody can decide whether to delete."
            )
        return cls(
            agent=path.parent.name,
            name=path.stem,
            why=str(payload["why"]),
            inputs=dict(payload["input"]),
            expect=dict(payload["expect"]),
            expect_example=dict(payload["expect_example"]),
            counterexample=dict(payload["counterexample"]),
        )


def load_cases(root: Path, agent: str | None = None) -> tuple[EvalCase, ...]:
    """Every committed case under `root`, sorted so a run reports in a stable order.

    `root` is a required argument rather than a module constant: the cases live in the test tree,
    and a runtime module that hard-coded a path into `tests/` would be a runtime module that
    cannot be installed.
    """
    directories = sorted(root.iterdir()) if agent is None else [root / agent]
    return tuple(
        EvalCase.load(path)
        for directory in directories
        if directory.is_dir()
        for path in sorted(directory.glob("*.json"))
    )


def finding_from(inputs: dict[str, Any], policy: Policy) -> Finding:
    """Build the `Finding` a narrative or critic case describes.

    `source_ref` and `excel_ref` are optional and default to the placeholders the module
    docstring names. A reviewer-assist case states them, because its agent's answer is checked
    against them; a narrative or critic case does not, because its agent is graded on prose.

    Severity and escalation are **derived from policy** rather than stated in the case file, and
    that is not a shortcut. `Finding` refuses a definitional variance routed to the hotel
    (D-MAT-06); a case file free to state its own escalation could describe a finding the
    classifier would never produce, and the agent would then be graded on a situation that cannot
    occur.
    """
    try:
        variance_class = VarianceClass(inputs["variance_class"])
        key = MetricKey(
            metric=Metric(inputs["metric"]),
            period=str(inputs["period"]),
            dimension=Dimension(inputs["dimension"]) if inputs.get("dimension") else None,
            value=inputs.get("dimension_value"),
        )
    except (KeyError, ValueError) as exc:
        raise CaseError(f"case input is not a well-formed finding: {exc}") from exc

    source = inputs.get("source_ref")
    excel = inputs.get("excel_ref")
    # A source with no `page` is the inventory reference. Dispatching on the field rather than on
    # a `kind` discriminator because that is the actual difference between the two: D-RNA-01 makes
    # rooms available a property attribute, so there is no page anywhere in the system to state.
    source_ref: PdfRef | InventoryRef | None = None
    if source:
        source_ref = InventoryRef(**source) if "page" not in source else PdfRef(**source)

    claimed = Decimal(str(inputs["claimed"])) if inputs.get("claimed") is not None else None
    computed = Decimal(str(inputs["computed"])) if inputs.get("computed") is not None else None
    difference = claimed - computed if claimed is not None and computed is not None else None

    return Finding(
        finding_id=str(inputs["finding_id"]),
        key=key,
        variance_class=variance_class,
        severity=policy.severity_for(variance_class),
        escalates_to=policy.escalation_for(variance_class),
        claimed=claimed,
        computed=computed,
        difference=difference,
        explaining_permutation=inputs.get("explaining_permutation"),
        source_ref=source_ref if source_ref is not None else PLACEHOLDER_PDF,
        excel_ref=ExcelRef(**excel) if excel else PLACEHOLDER_EXCEL,
        clause=str(inputs.get("clause", "D-OCC-01")),
    )


# The run metadata a reviewer-assist case's verdict carries. Fixed, for the same reason the
# placeholder citations are: it is rendered into the agent's first tool call and therefore into the
# cassette key, so a run id that varied would re-record every case on every run.
CASE_RUN_ID = "run-evalcase0001"
CASE_HOTEL = "MZN-DXB-001"
CASE_PERIOD = "2026-Q1"


def verdict_from(inputs: dict[str, Any], policy: Policy) -> Verdict:
    """Build the verdict a reviewer-assist case describes.

    The findings come from `input.findings`, each in the shape `finding_from` reads, so a case
    states its own references and a citation can be checked against them. The status is derived
    rather than stated: a verdict with findings is never PASS, and a case free to claim otherwise
    would describe a run the system cannot produce — the same argument `finding_from` makes about
    deriving severity from policy.

    `policy_version` comes from the loaded policy, so a clause cited under the wrong version is
    caught by the check that exists for it rather than by the case file happening to agree.
    """
    findings = tuple(finding_from(dict(item), policy) for item in inputs.get("findings", ()))
    definitional = tuple(f for f in findings if f.variance_class is VarianceClass.DEFINITIONAL)
    ordinary = tuple(f for f in findings if f.variance_class is not VarianceClass.DEFINITIONAL)
    blocking = any(f.severity is Severity.BLOCKING for f in ordinary)

    if not findings:
        status = VerdictStatus.PASS
    elif blocking or definitional:
        status = VerdictStatus.ESCALATED
    else:
        status = VerdictStatus.FAIL

    return Verdict(
        run_id=CASE_RUN_ID,
        status=status,
        hotel_id=CASE_HOTEL,
        period=CASE_PERIOD,
        policy_version=policy.version,
        metric_library_version="1.0.0",
        model_id="eval",
        provider_mode="replay",
        extraction=ExtractionSummary(
            files=("pms_2026-01.pdf",),
            records_extracted=1200,
            pages_read=30,
            printed_total_matched=True,
            duplicate_ids=0,
        ),
        claims_checked=int(inputs.get("claims_checked", 94)),
        findings=ordinary,
        definitional_items=definitional,
    )


def _formula_view(case: EvalCase) -> Any:
    """The `data_only=False` view of a mapping case's workbook.

    `open_submission` returns both views because the *reader* needs both — in the value view an
    empty cell and an uncached formula are both `None` and mean opposite things. The mapping agent
    needs only this one, and specifically this one: with `data_only=False` a formula cell yields
    its formula text, which `tda.excel.tools.render_cell` shows as `<formula>` rather than as
    `<value>`. Both are redactions and neither can leak a figure; the distinction is that the agent
    can tell a computed cell from a typed one, which is information it uses to find a total row.
    """
    from tda.excel.run import open_submission

    _values, formulas = open_submission(Path(str(case.inputs["workbook"])))
    return formulas


def run_case(case: EvalCase, runner: AgentRunner, policy: Policy) -> AgentOutput:
    """Hand one case to its agent and return the validated contract.

    Dispatches on `case.agent`, and an unknown one raises rather than being skipped. A case for an
    agent nobody wired would otherwise sit in the directory being counted and never run, which is
    the same failure the harness exists to catch in the agents themselves.

    Every path goes through the agent's ordinary driver — `resolve_label`, `narrate`, `grade`,
    `map_workbook_traced` — and therefore through the same allowlists, the same fabrication checks
    and the same trace. An eval that bypassed those would be measuring a different system from the
    one that runs.
    """
    match case.agent:
        case roster.RESOLUTION:
            from tda.agents.resolution import resolve_label

            return resolve_label(str(case.inputs["raw_label"]), runner, policy).output
        case roster.NARRATIVE:
            from tda.agents.narrative import narrate

            return narrate(finding_from(case.inputs, policy), runner, policy).output
        case roster.CRITIC:
            from tda.agents.critic import grade

            finding = finding_from(case.inputs, policy)
            narrative = FindingNarrative(
                finding_id=finding.finding_id,
                sentence=str(case.inputs["sentence"]),
                cites_permutation=case.inputs.get("explaining_permutation"),
                is_definitional=finding.variance_class is VarianceClass.DEFINITIONAL,
            )
            return grade(finding, narrative, runner, policy).output
        case roster.MAPPING:
            from tda.excel.agent import map_workbook_traced

            return map_workbook_traced(_formula_view(case), policy, runner).output
        case roster.REVIEWER_ASSIST:
            from tda.agents.reviewer_assist import ask

            return ask(
                str(case.inputs["question"]), verdict_from(case.inputs, policy), runner, policy
            ).output
        case _:
            raise CaseError(
                f"no way to run a case for agent {case.agent!r}. Add a branch here when the agent "
                "is wired - a case that is counted and never run is the failure this harness "
                "exists to catch."
            )


def registry_for(case: EvalCase, policy: Policy) -> ToolRegistry:
    """The tool registry one case needs, bound to that case's own data.

    Per case rather than per run, because the narrative and mapping agents' tools are bound to one
    finding and one workbook. A registry shared across cases would let a case be answered from the
    previous one's data, which is exactly the bug the per-run construction rule in
    `tda.agents.tools` exists to prevent.
    """
    match case.agent:
        case roster.RESOLUTION:
            from tda.agents.resolution import build_registry

            return build_registry()
        case roster.NARRATIVE:
            from tda.agents.narrative import build_registry as narrative_registry

            return narrative_registry(finding_from(case.inputs, policy))
        case roster.CRITIC:
            # The critic's allowlist is empty, so an empty registry is not an oversight - it is
            # the roster entry made concrete. See `tda.agents.critic`.
            return ToolRegistry()
        case roster.MAPPING:
            from tda.excel.agent import build_registry as mapping_registry

            return mapping_registry(_formula_view(case))
        case roster.REVIEWER_ASSIST:
            from tda.agents.reviewer_assist import build_registry as assist_registry

            return assist_registry(verdict_from(case.inputs, policy))
        case _:
            raise CaseError(f"no tool registry defined for agent {case.agent!r}")
