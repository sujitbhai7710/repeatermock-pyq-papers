# Coverage & Validation

## Coverage identity

```text
placed + skipped_hindi + unclassified + flagged_papers_questions = 130020 + 3456 + 8614 + 0 = 142090
in-scope questions                                        = 142090
balanced = True
```

| Bucket | Questions | Meaning |
|---|---|---|
| placed | 130,020 | subject assigned AND concept mapped to the taxonomy |
| unclassified | 8,614 | subject assigned, concept not mappable (incl. source label `Unidentified`) |
| skipped_hindi | 3,456 | Devanagari present in the question prompt or one of its options |
| flagged_papers_questions | 0 | paper failed signature validation and no confident layout could be derived |

## Section-signature validation

- Papers checked: **1,322**
- Validated with section-level agreement >= 95% (primary) or question-level agreement >= 95% when the paper has no declared section: **1,318** (99.7%)
- Flagged `NEEDS_AI_REVIEW`: **4** (0.3%)
- Placeable (subject assigned to every non-Hindi question): **1,322**

Two independent signals are compared.  **Primary (section level)**: for every positional section of the declared layout, the modal keyword subject of its questions must equal the declared subject — one or two noisy source labels cannot flip a whole section.  **Secondary (question level)**: the per-question agreement, kept for the report and used as the verdict only when the paper has no declared layout at all.

| Exam | Papers | Validated | Flagged | Questions | Skipped Hindi |
|---|---|---|---|---|---|
| CGL | 235 | 232 | 3 | 23,900 | 1 |
| CHSL | 303 | 303 | 0 | 30,440 | 27 |
| CPO | 65 | 65 | 0 | 13,000 | 3 |
| GD | 260 | 260 | 0 | 26,000 | 3,421 |
| MTS | 294 | 293 | 1 | 27,750 | 1 |
| SELECTION_POST | 120 | 120 | 0 | 12,000 | 1 |
| STENO | 45 | 45 | 0 | 9,000 | 2 |

## Flagged papers

| Exam | Year | Paper | Q | Section agreement | Sections | Agreement (question level) | Comparable | Layout used | Layout agreement |
|---|---|---|---|---|---|---|---|---|---|
| CGL | 2020 | SSC CGL (2020) Finance and Accounting (Held on: 28 Jan 2022) | 100 | 0.0 | 0/1 | 0.041667 | 24 | detected | 0.0 |
| CGL | 2023 | SSC CGL Tier-II (JSO) 2023 Official Paper-II (Held On: 27 Oc | 100 | - | 0/1 | 0.0 | 0 | canonical-unverified | 0.0 |
| CGL | 2023 | SSC CGL Tier-II (AAO) 2023 Official Paper-III (Held On: 27 O | 100 | - | 0/1 | 0.0 | 0 | canonical-unverified | 0.0 |
| MTS | 2021 | SSC MTS Previous Year Paper (Held on: 14 Oct 2021 Shift 2) | 100 | - | 0/4 | 0.0 | 0 | canonical-unverified | 0.0 |

A flagged paper is **not** silently mis-assigned: the layout is re-derived from the paper's own `n` resets plus per-section keyword majorities and the result is recorded in `state/papers.json` (`detected.spans`). Papers where no confident layout could be derived contribute to `flagged_papers_questions` and are excluded from the subject databases.

## Hindi detection variants

| Detector | Questions flagged as Hindi |
|---|---|
| question+options+solution (raw range) | 16,613 |
| question+options (raw range) | 3,456 |
| question only (raw range) | 3,421 |
| question+options (range minus danda) | 3,431 |

The run uses `question + options` with the specified `[\u0900-\u097F]` range. Including the solution field is **not** equivalent: geometry solutions in this corpus use the Devanagari danda `।।` as a parallel symbol, which the raw range matches.

## Out-of-scope papers

- Papers dropped by the year filter (detected year outside the configured range): **287**
