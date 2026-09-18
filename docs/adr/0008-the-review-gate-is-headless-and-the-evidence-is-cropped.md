# ADR-0008 · The review gate is headless, and the evidence is cropped to what was cited

- **Status:** Accepted
- **Date:** 2026-09-14
- **Related:** [ADR-0001](0001-deterministic-core-agentic-edges.md) · [ADR-0007](0007-outputs-render-from-the-written-verdict.md)
- **Issues:** PRD-91

## Context

PRD-91's acceptance criterion is a stopwatch: *"if judging one finding requires opening the PDF in
another window, the screen has failed, regardless of how correct the finding is."* A verification
officer who cannot check the agent in ten seconds will not sign behind it, and then the correctness
of the finding is beside the point.

That turns four ordinary-looking implementation choices into decisions worth recording.

**Where the gate lives.** The obvious build is a Streamlit app that reads the verdict, draws the
findings and writes decisions back — all in one file. PRD-91 also names a console flow as its
fallback, and requires it to *"record the identical decision structure so the verdict schema does
not change with the fallback"*.

**What the evidence pane shows.** A PDF page is the obvious thing to render. It is also 840 points
wide, and scaled into half a browser column it is a grey smudge — the officer opens the PDF, and
the screen has failed on its own terms while looking finished.

**Which numbering the crop uses.** The screen has to turn `pms_2026-01.pdf p.4 rows 12-18` into
pixels, and "which rows are rows 12 to 18" is a question the extractor already answered when it
wrote the citation.

**What happens when an officer changes their mind.** `ReviewRecord` has no uniqueness constraint,
so a second decision on one finding is expressible. Overwriting or appending is a choice.

## Decision

### 1 · The recorder is headless; the screen is a shell over it

`tda.review.decisions.record_decision` takes paths and strings, writes the verdict and returns what
it wrote. `tda.review.app` calls it, and so would the console fallback. One recorder means the
fallback cannot change the schema, because there is nothing for it to change — and it means the
review gate is testable without driving a browser, which is the difference between a gate that is
**asserted** and one that is hoped for.

`tda.review.present` holds the wording for the same reason. What a finding *says* to the officer
deciding on it is not a rendering detail.

### 2 · The evidence is two strips, not a page

The heading block and the cited rows are cropped separately and stacked with a rule between them.
The result is a few hundred pixels tall instead of a thousand, so it renders at a size somebody can
read — and the column headings come along, without which a band of numbers is not evidence of
anything: the reviewer must see that the figure under `RN Total` is the one in dispute.

The rule between the strips is not decoration. Without it the two read as one continuous extract,
and the reviewer would take rows 1–3 to be adjacent to row 14.

### 3 · The crop uses the extractor's own row numbering, by reusing its code

`tda.extract.pdf.row_bands` calls the same generator that assigns `PdfRef.row_start` during
extraction. Two implementations of "which row is row 12" would drift the first time the report's
layout changed, and the symptom is the worst one an evidence screen can have: **a highlighted row
that is not the row the finding is about**, shown to somebody deciding whether to tell a hotel it
miscounted. Correct caption, wrong picture, total confidence.

### 4 · The evidence is checked against the run's own digests before it is shown

`run.json` records a SHA-256 per submitted file. Every crop is checked against it, and a file that
does not match is **refused with both digests named** rather than drawn.

The screen is pointed at a submission by configuration, so pointing it at the wrong one is one
environment variable away: `make run SUBMISSION=/data/hotel-x` followed by `make review` cropped the
demo corpus and captioned it with this run's citations — another property's rows, outlined in red,
under a correct citation, with `Evidence.complete` true and nothing said. The guard at §3 protects
the row and left the *file* wide open.

The same ledger answers which workbook to open, which removes a second guess: the screen used to
take whatever `*.xlsx` sorted first while the pipeline prefers `claims_<period>.xlsx`, so a
submission holding `amended_claims_2026-Q1.xlsx` had one file verified and the other displayed.

### 5 · Every rule about a decision lives in the gate, not in a widget

"Amend only applies to a transcription error" was a `disabled=` on a Streamlit button. That makes it
true of the surface that implements it and false of the gate — so the console fallback §1 exists to
protect would have recorded a corrected figure against a definitional variance, which is telling a
hotel to change a number that is not wrong. The rule, the requirement for a figure, the refusal of
`NaN`, the bound on a reviewer's name: all in `record_decision`.

### 6 · A decision is in both artefacts or in neither

The verdict is written before the memo, so a failure in between left a decision recorded in
`verdict.json` beside a memo still saying *"No human review has been recorded"* — the exact
disagreement §7 below exists to prevent, with the officer seeing a traceback and reasonably
concluding nothing had stuck. The verdict's previous bytes are kept and restored if the memo raises.

### 7 · Decisions are appended; the standing one is derived

An officer who accepts a finding and later rejects it has done something an auditor needs to see.
Both records stay, and `Verdict.standing_decisions` answers "what does this verdict say now" — on
the contract rather than in `tda.review`, because the memo asks the same question and two
implementations would eventually give a supervisor and a screen two different answers about one run.

### 8 · Every decision is written immediately, and the memo is re-issued with it

Not held in session state until some "save". A review interrupted by a closed laptop has lost
nothing, and the verdict is re-read immediately before every write, so two officers on one run
cannot silently drop each other's decisions — appending to what is actually on disk *is* the merge.

The memo is regenerated because its signature block is derived from `review_records`. Leaving it
alone would put a verdict recording three decisions next to a memo saying *"No human review has
been recorded against this verdict."* Both go to the same supervisor; one of them would be lying,
and it is the one written in Word that gets forwarded.

## Enforcement

| Decision | What holds it in place |
|---|---|
| The crop matches the citation | Two tests. `test_the_band_for_a_row_holds_the_record_the_extractor_cited_at_that_row` asks the **parser** what it cited at row 12 and finds that record's id inside the band. `test_the_outline_lands_on_the_cited_rows_and_nowhere_else` then checks **where the red box actually is, in pixels**, which is why `page_region` returns its geometry. Both replaced tests that passed while the box was drawn three rows off. |
| The evidence belongs to this run | `test_evidence_from_another_submission_is_refused_rather_than_shown` points the screen at a same-named file with different bytes. |
| Amend is refused on a definitional item **by the gate** | `test_a_definitional_item_cannot_be_amended_by_the_gate`, which calls the recorder directly rather than the screen. |
| A decision cannot half-happen | `test_a_decision_is_not_recorded_at_all_if_the_memo_cannot_follow` makes `write_memo` raise and asserts the verdict is byte-identical afterwards. |
| The reviewer's name is not redacted | `test_the_reviewers_name_survives_the_artefact_that_records_it`, with `test_the_rest_of_the_verdict_is_still_redacted` holding the exemption to one field. |
| Importing the app renders nothing | `test_importing_the_screen_renders_nothing` imports it in a subprocess against a verdict that does not validate. |
| The cited rows are outlined | Pixel rows carrying red are matched against the reported box geometry. `draw_rect` paints onto a separate canvas, so cropping `original` instead of `annotated` yields the right rows with no box — correct in every way except the one that matters. |
| The gate refuses an anonymous decision | `test_an_anonymous_decision_is_refused`, against the recorder rather than the screen. |
| Two reviewers cannot overwrite each other | `test_a_second_reviewer_cannot_silently_drop_the_first_ones_decisions` records from a deliberately stale copy. |
| The memo cannot disagree with the verdict | `test_the_memo_is_re_issued_so_the_two_artefacts_cannot_disagree`. |
| A definitional item is never called an error | `test_a_definitional_item_is_never_described_as_an_error`, and it takes the workbook's amber rather than the red its `material` severity would give it. |
| Workbook text cannot inject markup | The cell grid is HTML and every value in it was typed outside this system; `test_a_workbook_label_is_escaped_rather_than_rendered` puts a `<script>` in a label. |

Each of the above was verified by removing the behaviour and watching the test fail.

## Consequences

### Accepted costs

- **The console fallback is not built.** PRD-91 names it as fallback #2, to be taken *if the sprint
  tightens*. It did not, so the screen shipped instead — and the recorder it would use is complete
  and tested, so the fallback is a thin front end rather than a rewrite.
- **The screen itself is barely tested.** One import check. Asserting on Streamlit widgets tests
  Streamlit; everything the screen decides is in `evidence`, `decisions` and `present`. What
  verified the screen is a browser and a person looking at it.
- **A citation that overruns the page is reported, not drawn.** `rows 25-35` on a thirty-row page
  shows six rows and says which five are missing; it used to show six under a caption reading
  eleven.
- **The reviewer's name is exempt from redaction**, and that exemption is a decision rather than an
  oversight. Redaction exists for text this system copied out of somebody else's file; a reviewer's
  name is typed into this system, by that reviewer, to be recorded against their decision. Removing
  it protects nobody and inverts FR-11 — two titled reviewers became indistinguishable.
- **Rendering a page costs about a second**, cached per finding. The cache key is the finding's own
  JSON, not its id, because two runs can both have an `F-0001` and showing one run's page crop for
  another's finding is the most expensive mistake this screen could make.
- **Streamlit's cache survives edits to modules it calls.** A developer changing `evidence.py` sees
  the old crop until the server restarts. Harmless in use — a finding's evidence is immutable — and
  confusing exactly once during development.
- **The crop keeps the whole row, including columns irrelevant to the metric in dispute.** Trimming
  them would be editing evidence, and a reviewer who cannot see what was left out cannot judge what
  was kept.

### What is bought

An officer can judge a finding from one card: what was claimed, what the records support, the
difference, the cause, the report rows outlined in red, and the workbook cell outlined in red —
then accept, reject or amend with their name on it. The verdict and the memo update together.

### What this does not claim

Not a workflow engine: no assignment, no queue, no approvals chain. Not multi-user in any real
sense — concurrent decisions merge safely, but nobody is notified of anybody else. Not an audit
system: the trail is in `verdict.json`, and what protects it is a file digest, not this screen.

## Alternatives considered

| Alternative | Why it lost |
|---|---|
| Decisions recorded in the Streamlit app directly | The console fallback would then re-implement the schema, which is exactly what PRD-91 forbids. |
| Render the whole PDF page | Unreadable at column width; the officer opens the PDF and the ten-second test is failed by a screen that looks complete. |
| Link to the PDF at the right page | "Without leaving the screen" is the criterion, and a link leaves the screen. |
| A second implementation of row geometry | Drifts silently, and its failure mode is a confident reviewer looking at the wrong row. |
| Overwrite a previous decision | Destroys the record that somebody changed their mind, which is the kind of thing a review gate exists to capture. |
| Hold decisions in session state until "save" | A closed laptop loses an afternoon, and two officers overwrite each other. |
| Leave the memo alone until the run is re-run | Two artefacts in one directory saying different things about whether a human looked. |
