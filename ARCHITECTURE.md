# ARCHITECTURE.md — Covenant Compliance Agent

A **fully deterministic** pipeline. Covenant structure comes from a
compositional parser over the contract text, and every figure from exact
decimal arithmetic over classified ledger rows plus the adjustments the
authoritative documents disclose. No model is consulted on the production
path, so each answer is reproducible and explainable from its inputs.

Companion documents: [PLAN.md](PLAN.md) · [DATA_MODEL.md](DATA_MODEL.md) ·
[COVENANT_DSL.md](COVENANT_DSL.md) · [THREAT_MODEL.md](THREAT_MODEL.md)

---

## Stages

```
documents/*.pdf ──► ingestion ──► OCR (Tesseract rus+eng, content-hash cached)
                                        │
                                        ▼
                          document catalog (compound-signal type + lifecycle)
                                        │
                                        ▼
                       authority graph (one source per purpose)
                        ├─ covenant_terms        → current loan agreement
                        ├─ financial_disclosures → current audited notes
                        ├─ related_parties       → KYC dossier
                        ├─ treasury_adjustments  → treasury memo
                        └─ reclassifications     → final AUP, reference-gated
                                        │
        ┌───────────────────────────────┴──────────────────────────────┐
        ▼                                                              ▼
 covenant compilation                                        document facts
 section locator → clause split → component parser → AST      reclassifications,
                                                              off-ledger amounts,
                                                              resolved amounts,
                                                              period cut-offs,
                                                              FX settlements,
                                                              one-off items,
                                                              KYC scopes
        └───────────────────────────────┬──────────────────────────────┘
                                        ▼
 master_ledger.csv ──► metric totals (anchored classification + adjustments)
                                        ▼
                     evaluation (Decimal) → boundary reconciliation
                                        ▼
                     evidence (counterfactual causal search)
                                        ▼
                        submission.json + audit records
```

## Components

| Module | Responsibility |
|---|---|
| `ingestion/` | discover PDFs, render pages, OCR with content-hash cache |
| `documents/ocr_text.py` | OCR script canonicalization (Cyrillic/Latin confusables) |
| `documents/doctype.py` | compound-signal type + lifecycle classification |
| `documents/facts.py` | typed adjustments extracted from authoritative documents |
| `resolve/authority.py` | one authority per purpose; reference-gated final AUP |
| `covenants/section_locator.py` | locate the financial-covenants article |
| `covenants/parser.py` | clause splitting + compositional formula parsing |
| `covenants/ast.py` | closed expression AST |
| `ledger/metrics.py` | anchored statement-caption classification |
| `calc/engine.py` | metric totals, Decimal evaluation, boundary reconciliation |
| `evidence/decisive.py` | counterfactual search for the determining transaction |
| `deterministic_pipeline.py` | orchestration |
| `calibration/` *(outside `src/`)* | accuracy measurement against the answer key |

## Three ideas the design rests on

**1. Anchored classification.** The ledger mixes audited statement line
items with a large volume of operational noise. A row contributes to a
metric only when its description is an undecorated statement caption *and*
matches an accounting-semantic pattern. Noise rows carry a site/period
suffix and never qualify.

**2. Documents decide, the ledger supplies.** Captions, related-party
membership, materiality floors, cut-offs and FX rates all come from the
authoritative document that governs that question. The ledger supplies
amounts. Neither is allowed to override the other's role.

**3. Fail closed.** Unparsed clause, unresolved metric, ambiguous
authority, or a non-unique decisive transaction produce an explicit
unresolved/`None` outcome rather than a guess.

## Failure boundaries

| Stage | Failure | Behaviour |
|---|---|---|
| OCR | page unreadable | document skipped, run continues |
| Authority | 0 or >1 candidates | purpose unresolved + warning |
| Parser | unknown clause structure | cell marked unresolved |
| Evaluation | missing metric / zero denominator | cell marked unresolved |
| Evidence | zero or several flipping rows | `evidence_txn_id = null` |

## Trust boundaries

- **Untrusted:** all document content (including OCR output) and ledger
  free text. Treated as data, never as instructions.
- **Trusted:** the deterministic parsers, the AST evaluator, and the
  ledger's structured numeric fields.
- **Out of band:** `ground_truth.json` — read only by `calibration/`, never
  reachable from `src/` (enforced by test).

## Numeric policy

`Decimal` end to end. Amounts parse from the CSV as `Decimal`, adjustments
and FX rates stay exact, and the reported figure is rounded once at the
end with `ROUND_HALF_UP`. Where the exact and rounded values fall on
opposite sides of a threshold, the reported value governs unless rounding
moved the figure by at least the official 5% tolerance, in which case the
exact comparison stands; both are recorded.
