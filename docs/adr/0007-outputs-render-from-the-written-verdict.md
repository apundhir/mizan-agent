# ADR-0007 · Outputs render from the written verdict, and the submission is never touched

- **Status:** Accepted
- **Date:** 2026-09-14
- **Related:** [ADR-0001](0001-deterministic-core-agentic-edges.md) · [ADR-0006](0006-observability-redaction-and-recorded-runtime.md)
- **Issues:** PRD-92

## Context

Three artefacts leave this system and go to people: `verdict.json` to a downstream system, an
annotated copy of the workbook to the verification officer, and a one-page memo to whoever signs
behind the number. Each is a document somebody acts on, and two of them may end up in front of the
hotel.

Four decisions inside that had plausible opposites, and three of them are about what an artefact
must **not** be able to say.

**A summary is the part that gets read.** `verdict.json` could carry only the arrays and let each
consumer count. Every consumer would then re-implement the one filter that matters — definitional
items are a separate array, and adding them to the findings count turns a policy disagreement into
a hotel error (D-MAT-06).

**The memo and the workbook restate what the verdict says.** PRD-90's review found `mizan run`
printing an unredacted ledger beside a redacted file. The same shape is available here and worse:
a Word document is forwarded, filed and printed, so a memo that says more than the JSON next to it
is a leak with a long life.

**The memo has a signature block.** PRD-92 asks for it to name the reviewer. At the moment a run
finishes there is no reviewer — the review gate is PRD-91 and `Verdict.review_records` is empty.

**The workbook has to be annotated.** The obvious implementation opens the submitted file, colours
the cells and saves. That destroys the evidence the whole comparison rests on, and it does it
silently.

## Decision

### 1 · The summary block is derived on write and re-derived on read

`VerdictDocument` extends `Verdict` with a `summary` field, and a validator **recomputes it and
refuses a document whose summary disagrees with its own arrays**. A hand-edited `verdict.json`
claiming three material findings over a list of five does not load.

A subclass with a real field rather than `computed_field`, because a dump carrying computed fields
fails to re-validate under `extra="forbid"` — the artefact would be write-only, which is a strange
property for the file that exists to be read back.

### 2 · The memo is rendered from the file, not from memory — and the workbook's cells are not

`write_outputs` writes `verdict.json`, **reads it back**, and renders the memo from what came off
the disk. Redaction therefore happens once, and the memo cannot say more than the verdict it
accompanies. If the redacted verdict ever fails to re-validate, nothing further is written.

The **workbook is the exception, and it is forced.** A sheet name can carry an honorific —
`Dr. Ahmed Occupancy` is a legal Excel sheet name — and redaction rewrites it. Looking cells up
through the redacted verdict therefore found no sheet and drew nothing for such a finding, while
the claim underneath it stayed green with *"the reservation records agree"* on it: a material
variance coloured green **by the privacy control**. So the workbook takes its cell references from
the verdict as it was, and redacts the **comment text** on the way into the cell.

`verdict.json` still redacts the sheet name, and the cost is real: a citation reading
`[redacted:titled_name]!B5` cites nothing. That is one more reason for the runbook's advice to ask
the property to keep contact details out of the sheet, and it is recorded here rather than
discovered by somebody following a citation that leads nowhere.

### 3 · The signature block never claims a review that did not happen

It names whoever is in `review_records`, and when there is nobody it says **"No human review has
been recorded against this verdict"** above a blank signature line. Printing a name nobody supplied
would be a forged sign-off on the one page a supervisor actually reads.

### 4 · The submitted workbook is copied, and the copy is proved

`annotate` digests the original before and after and **raises `OriginalModifiedError` if the two
differ**. The check costs one hash of a file the run ledger already hashes, and it converts "we
annotate a copy" from a claim in a docstring into a postcondition.

### 5 · Every generated document is byte-reproducible, and neither format is by accident

`make repro` (PRD-94) compares two runs of one submission. `verdict.json` is byte-stable by
construction; a `.docx` and an `.xlsx` are not. Both writers stamp `dcterms:modified` with the wall
clock **inside `save()`** — openpyxl discards whatever the caller set beforehand, which looks like
it should work and does not — and every zip entry carries a DOS timestamp of its own, at
two-second granularity, so stability would otherwise be a coin toss on when the two runs landed.

`tda.outputs.ooxml.make_reproducible` repacks both afterwards: the properties are pinned to a fixed
instant and every entry gets the same timestamp. A run's real time belongs in `run.json`, where
ADR-0006 already marks it as excluded from the diff.

### 6 · A finding that cannot be drawn is reported, whatever the reason

Not only the ones with no cell by construction. A finding naming a sheet the workbook does not have
used to be dropped silently, under a legend that then said *"Every finding in this verdict is
marked on a cell"* — an incomplete artefact positively asserting it is complete, which is worse
than an incomplete one. Every undrawn finding now reaches `AnnotatedWorkbook.unplaced` **with its
reason**, because "the hotel wrote nothing there" and "this names a sheet that does not exist" are
a correct refusal and a defect respectively.

The same applies to two claims read from one cell: recorded rather than collapsed, on the argument
`tda.contracts.claim.index_by_key` already makes about duplicate keys.

### 7 · The annotated workbook is exempt from redaction; everything else is not

`verdict.json` and the memo are redacted by `tda.obs.redact` on the way out. The workbook's **cells**
are not, and the asymmetry is deliberate: that file is a copy of the hotel's own submission going
back to the hotel. Removing content from it destroys evidence without withholding anything the
recipient does not already have. The comments Mizan adds are rendered from the redacted verdict like
everything else.

## Enforcement

| Decision | What holds it in place |
|---|---|
| The counts cannot lie | `VerdictDocument._the_summary_matches_the_arrays`, and a test that edits a written file and asserts it no longer loads. **This is a count checksum, not an integrity check** — evidence references and provenance can be edited and the file still loads. It catches the failure it was built for, which is a summary drifting from its own arrays. |
| The memo cannot exceed the verdict | `write_outputs` reads the file back; `test_the_memo_is_rendered_from_the_written_verdict_rather_than_the_one_in_memory` puts an address in a narrative and asserts it reaches neither. |
| No forged sign-off | `test_the_signature_block_does_not_claim_a_review_that_did_not_happen`. |
| The original is untouched | `_check_untouched`, extracted so it can be tested **directly**: nothing in `annotate` opens the original for writing, so the check cannot fire through the public path, and a test asserting only that the digest is unchanged passes with the whole check deleted. |
| Documents are reproducible | `test_all_three_artefacts_are_byte_identical_across_two_runs`, with a deliberate one-second sleep between the runs — without it the test passes whether or not anything is normalised. |
| A redacted sheet name cannot invert a finding | `test_a_redacted_sheet_name_does_not_turn_a_material_finding_green`. |
| Definitional items stay separate | Enforced in `Verdict` since M1; asserted again in the file and in the memo's tables, because this is the rule most likely to be undone by a well-meaning layout change. |

## Consequences

### Accepted costs

- **Cached formula values are lost in the annotated copy.** openpyxl rewrites the file from its own
  model, and a formula's last-computed value is not part of that model. Excel recalculates on open,
  so a person sees the right number; a *script* reading the copy with `data_only=True` sees `None`.
  The original is the file to read values from, which is the right default anyway.
- **A fifth colour.** PRD-92 names four. Grey is added for a finding that is neither definitional
  nor material — a V6 rounding difference — because green would say a cell verified clean when it
  produced a finding, and red would escalate a rounding artefact into a material variance.
- **The memo is not always one page.** A verdict with forty findings does not fit and should not
  pretend to. The first block is fixed-size and always fits; the table grows.
- **Findings with no cell cannot be drawn.** A V7 or a missing-claim V5 has no cell by construction.
  They are listed on the legend sheet and returned in `AnnotatedWorkbook.unplaced` rather than
  quietly omitted — a workbook that dropped them would be all green with the blocking findings
  invisible.
- **A redacted sheet name breaks that finding's citation in `verdict.json`.** The annotated
  workbook still draws it correctly; the JSON's `excel_ref` points at a sheet name that no longer
  exists. Privacy wins in the artefact that leaves the building, and the remedy is upstream: contact
  details do not belong in a statistical return.
- **A corrupt workbook costs the annotation only.** `openpyxl` raises `zipfile.BadZipFile`, which is
  not an `OSError`; `write_outputs` catches it, records the reason and still writes the verdict and
  the memo. Broad catches are a smell, and here the alternative is ending a completed run in a
  traceback with an exit code that collides with `FINDINGS`.
- **Redaction could in principle break the verdict.** Every pattern fires on a structural signal no
  enum value or clause id contains, so it should be impossible; if it happens, `write_outputs`
  raises rather than falling back to the unredacted verdict.

### What is bought

An officer gets the hotel's own workbook back with every claimed figure marked, a memo whose first
block is the verdict, and a JSON file that cannot misreport its own contents. A downstream consumer
gets counts it does not have to derive — and cannot get the D-MAT-06 filter wrong by default.

### What this does not claim

Not a review workflow: nothing here decides anything, and the signature block is explicit that
nobody has. Not a document template system — the memo's layout is code, and a house style would be
a different story. Not a general Excel writer: it colours cells and attaches notes, and it is not
where anybody should add a chart.

## Alternatives considered

| Alternative | Why it lost |
|---|---|
| No summary block; let consumers count | Every consumer re-implements the D-MAT-06 filter, and the first one that forgets reports policy disagreements as hotel errors. |
| `computed_field` on `Verdict` | Breaks the round trip under `extra="forbid"`. A verdict file that cannot be read back is not an artefact, it is an export. |
| Render the memo from the in-memory verdict | Exactly the defect PRD-90's review found in the console output, made durable in a Word file. |
| Annotate the submitted workbook in place | Destroys the evidence the comparison rests on, and silently. |
| Copy the workbook but trust the copy | A promise in a docstring. The digest check is four lines and makes it a fact. |
| Print the operator's name in the signature block | A forged sign-off. The person who signs is the person who signs. |
| Redact the annotated workbook's cells too | Removes the hotel's own data from a file going back to the hotel: evidence destroyed, nothing protected. |
