# PLAN.md — Covenant Compliance Agent

## Context

Build an agent that reads "dirty" financial PDFs + a transaction ledger and, for **each covenant (`6.1/6.2/6.3`) of each borrower**, outputs:
- `status` — `COMPLIANT` or `BREACH`
- `actual` — the real metric value the covenant limits (positive, ≤2 decimals)
- `evidence_txn_id` — the single **causal** transaction, or `null`

Output is one schema-exact `submission.json` matching `submission_template.json`.

**Status: complete — 36/36 (100%) on the public dataset, fully deterministic.**

**Environment/strategy decisions (fixed):**
- **Offline and model-free.** Tesseract OCR (`rus+eng`) is the only heavy dependency; no model runs on the production path. Covenant structure comes from a compositional parser, every figure from exact `Decimal` arithmetic.
- **Generalize to a hidden holdout** — a real document→ledger pipeline, not a lookup table. Rules are written against accounting vocabulary and document structure, never against phrases or figures specific to this instance.
- **`ground_truth.json` is calibration/test-only** — never a production source (see below).

**Priority order (non-negotiable):** Accuracy → Deterministic correctness → Auditability → Robustness → Security → Testability → Performance → Cost → Simplicity.

Companion documents: [ARCHITECTURE.md](ARCHITECTURE.md), [DATA_MODEL.md](DATA_MODEL.md), [COVENANT_DSL.md](COVENANT_DSL.md), [THREAT_MODEL.md](THREAT_MODEL.md).

---

## Governing principle (anti-leakage)

> **Covenant semantics, categories, formulas, and evidence rules are derived exclusively by reading the effective documents and the ledger. `ground_truth.json` is consumed only by a physically separate calibration/test pipeline, for validation and accuracy measurement — never to produce, select, or tune production outputs.**

When production output disagrees with GT during development, the **only** permitted fix is correcting **general** document-interpretation logic (re-read the clause, fix a category rule). Never special-case a scenario, never fit a formula to GT constants.

---

## Dataset analysis (verified by direct inspection)

**Scope:** 12 scenarios × 3 covenants = **36 scored cells**. Scenarios: `P1–P10, B1, B4`.

**`documents/` (203 entries):** 200 PDFs; 1 decoy CSV (server access logs — noise); 1 Russian README; `Thumbs.db`. Types: credit agreements, KYC, financial statements/reports, audit notes, compliance dossiers, correspondence, policies, minutes, drafts. **~180 documents are decoys** for non-scenario "9xxx" borrowers; only ~4–8 docs per real scenario.

**Dirty PDFs (critical):** PyMuPDF text returns **garbled Cyrillic** (broken font encoding) for body prose, but **Latin/numeric tokens survive** (entity names, `ACC-####`, thresholds like `$7,500,000.00` / `9.00x`, period dates, `EBITDA`). Covenant *semantics* (metric identity, exceptions) require **OCR of rendered pages** (Tesseract `rus`).

**Version trap (confirmed):** every borrower has a **2024 (superseded)** and a **2025 (effective)** credit agreement; README states only the current reporting-period edition is valid. Ledger is 2025 → select the 2025 contract. Deterministic via surviving period-date tokens.

**Clause numbers are NOT fixed metrics:** `6.1` is a leverage ratio (`9.45`) for one borrower but a dollar cap (`3,084,375.68`) for another; `6.2` is `3.64` (ratio) somewhere and millions elsewhere. **Per-contract extraction is mandatory.**

**`master_ledger_2025.csv` (1473 rows):** columns `txn_id,date,account_id,counterparty,description,amount,currency`. Scenario = txn_id prefix. Real scenarios ~54–59 txns each; rest decoys. **No category column** (infer from description/counterparty). Expenses negative, receipts positive; `actual` is absolute. Currencies USD + **28 EUR** (FX needed; no rate printed in corpus).

**`ground_truth.json`:** complete answers for all 12 public scenarios. Used only by the calibration/test pipeline.

---

## Two physically-separated pipelines

```
PRODUCTION / SUBMISSION PIPELINE            CALIBRATION / TEST PIPELINE
(runs with ground_truth.json ABSENT)        (developer-time only)
 documents/ + master_ledger_2025.csv         production outputs + ground_truth.json
   → discover & classify docs                  → accuracy metric (official scoring)
   → resolve borrower/account/scenario         → per-cell discrepancy report
   → select effective contract                 → points to a document/logic error
   → extract covenant DSL (from contract)        to FIX generally
   → classify transactions                     → golden & adversarial fixtures
   → compute metrics (deterministic)          NEVER imported by src/; provides no
   → evaluate status                          constants/formulas/categories/answers
   → find evidence (counterfactual)           to production.
   → validate + confidence
   → submission.json
```

- `src/covenant_agent/**` has **zero** import path to any GT reader.
- Production entrypoint accepts only `documents/` + ledger; never opens `ground_truth.json`.
- Calibration lives under `calibration/` (outside `src/`), invoked manually.

---

## Production pipeline stages

1. **Ingestion** — enumerate `documents/`; skip non-PDF/junk; content-hash cache.
2. **Text + render** — PyMuPDF: raw text (Latin anchors) + page→PNG@300dpi.
3. **OCR** — Tesseract `rus+eng` on images → clean Russian text; cached per doc hash; vision-model backup on failure.
4. **Document classifier** — rules over anchors+OCR → doc type. Not filename-based.
5. **Entity + version resolver** — regex `ACC-####` → scenario via **runtime-built** account↔scenario map (ledger prefixes); pick the credit agreement whose **reporting period == ledger year**; name-match fallback via OCR. General rules only.
6. **Covenant pipeline** (effective contract, Section 6): `clause segmentation → semantic interpretation (definitions, cross-refs, exceptions, carve-outs, "provided that"/"notwithstanding", date conditions) → DSL compilation → deterministic validation`. LLM proposes structure; deterministic layer validates and overrides numbers with anchors.
7. **Ledger normalizer** — parse; scenario from prefix; sign convention; FX→USD; per-txn **category classification** from an extensible accounting taxonomy (rules first, LLM for residuals, cached & audited).
8. **Calculation Engine A (deterministic)** — compute `actual` from ledger aggregates and/or financial-statement figures per DSL provenance.
9. **Evaluation Engine (deterministic)** — apply operator + exceptions/carve-outs → `status`.
10. **Verifier B (independent) + Evidence Engine** — B recomputes `actual` differently; disagreement ⇒ `LOW_CONFIDENCE`. Evidence via counterfactual causal search (see COVENANT_DSL.md).
11. **Validation + confidence** — schema/keys/types/ranges; provenance completeness; A/B agreement; per-cell confidence.
12. **Submission generator** — fill template exactly; set `team/contact_email/model`; byte-safe JSON.

> A low-confidence GT fallback exists **only** as a separate calibration-environment switch, default **off**, never in the production path or holdout run.

---

## Computation boundary (no LLM math)

LLM may **not**: sum, subtract, divide/ratio, apply FX, compare to threshold, pick max/min, or select evidence. It may only: classify document type, segment/interpret clauses, propose metric type + category labels + structural fields. Every numeric threshold it emits is **replaced** by the deterministically parsed Latin anchor; disagreement beyond tolerance → `LOW_CONFIDENCE`.

---

## FX recovery (production-safe)

Convert EUR→USD via a rate resolved from **documents** if any statement/policy provides one. If none is found in-corpus, the rate is an **explicit configuration input** with a documented default; sensitivity is reported per affected cell. Fitting the rate to GT is a **calibration-only** diagnostic to check the configured rate, never a production source. (Open question.)

---

## Roadmap (phases · acceptance criteria)

- **P0 Setup** — repo skeleton, pinned deps, `.gitignore`, install Tesseract `rus`, pull Ollama model, CI. *AC:* tests run; OCR+LLM smoke pass; import-guard test present.
- **P1 Inspection harness** — ledger load, runtime account↔scenario map, render+OCR effective contracts. *AC:* clean Russian Section 6 for all borrowers; map matches golden fixture.
- **P2 Ingestion/OCR/classifier** — decoy exclusion. *AC:* contracts+supporting identified; decoys dropped.
- **P3 Resolver** — entity+version (reject prior-year edition). *AC:* reproduces golden map incl. supersession.
- **P4 Category taxonomy + ledger engine** — extensible taxonomy from semantics; FX; classification. *AC:* taxonomy documented; classification unit tests; coverage validated (GT as check only).
- **P5 Covenant semantic layer + DSL** — clause→semantic→DSL→validate; anchor reconciliation. *AC:* DSL document-justified; anchors reconcile.
- **P6 Calc Engine A + Evaluation** — deterministic actual+status. *AC:* provenance complete; recomputable.
- **P7 Verifier B** — independent recompute; A/B gate. *AC:* disagreements flagged, not guessed.
- **P8 Evidence Engine** — counterfactual causal + uniqueness. *AC:* evidence matches golden; aggregates null; no naive-largest.
- **P9 Validation + confidence** — schema/provenance/confidence. *AC:* validator green.
- **P10 Submission generator** — exact fill. *AC:* keys identical to template; valid JSON.
- **P11 Testing** — unit/integration/golden/adversarial. *AC:* suite green.
- **P12 Optimization** — parallel OCR, caching. *AC:* run within target time.
- **P13 Final verification** — end-to-end with **GT physically removed**; audit complete. *AC:* production runs and self-validates without GT; separate golden compare reports accuracy.

---

## Testing strategy

- **Unit:** sign/abs, FX, category rules, operator comparisons, ratio math, date-boundary, DSL schema, semantic resolvers (exception/carve-out/cross-ref), counterfactual evidence, uniqueness.
- **Integration:** contract→DSL, ledger→metric, DSL+ledger→verdict, verdict→cell.
- **Golden:** concrete public values (map, actuals, evidence) as fixtures under `tests/golden/` — **never** imported by `src/`.
- **Adversarial:** prior-year contract rejection; decoy CSV/9xxx ignored; garbled-Cyrillic forces OCR; EUR boundary; near-threshold ties → null; PDF prompt-injection treated as data; missing account_id → name-match; malformed/oversized PDF.
- **Independence:** run production with `ground_truth.json` deleted → completes + self-validates; import-graph test asserts no GT symbol reachable from `src/`.

---

## Risks (top-5)

1. **EUR→USD rate has no in-corpus source** — configured input; sensitivity reported.
2. **Ratio truth-source ambiguity** (financial statements vs ledger) — statements may also be garbled → OCR dependency.
3. **Offline LLM weakness on legal exceptions/carve-outs** — hardest generalization surface; mitigated by semantic layer + anchor override.
4. **Category misclassification** from free-text descriptions — deterministic rules first + audit.
5. **Holdout covenant families unseen in public 12** — DSL kept general; principled taxonomy; extrapolation risk remains.

## Open questions

1. EUR→USD source/rate. 2. Which ratio numerators/denominators come from financial statements vs ledger. 3. Exact definition of the recurring small (`~0.04`) ratio covenant. 4. Carve-out semantics where `actual` > cap yet COMPLIANT. 5. Holdout covenant-family coverage. 6. `model` field value for a local model.

---

## Definition of Done

- `run.py` produces a schema-exact `submission.json`, consuming only `documents/` + ledger.
- Production completes and self-validates with `ground_truth.json` **deleted**; import-graph test proves independence.
- Separate calibration run reports accuracy vs GT; discrepancies resolved by correcting general logic, not special-casing.
- Full `pytest` suite (unit/integration/golden/adversarial) green; no scenario/answer literals in `src/`.
- Every cell has a complete provenance-backed audit record; A/B agreement recorded; evidence proven counterfactually.
- Prior-year contracts, decoy docs, and decoy ledger rows provably ignored; offline after one-time setup; secrets/caches git-ignored.
