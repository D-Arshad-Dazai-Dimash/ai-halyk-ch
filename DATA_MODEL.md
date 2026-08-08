# DATA_MODEL.md — Covenant Compliance Agent

All entities are Pydantic v2 models. **Every extracted or derived value carries provenance** so any result is auditable back to a document span or a deterministic computation.

See also: [PLAN.md](PLAN.md), [ARCHITECTURE.md](ARCHITECTURE.md), [COVENANT_DSL.md](COVENANT_DSL.md), [THREAT_MODEL.md](THREAT_MODEL.md).

---

## Source-of-truth hierarchy (strict)

1. **Effective credit agreement** — covenant *semantics* (metric, operator, threshold, period, exceptions, carve-outs).
2. **Financial statements / reports** — reported financial *figures* (e.g., Debt, EBITDA) when a covenant references them.
3. **Ledger** — transactions and their amounts.
4. **OCR** — an *extraction mechanism*, not a source of truth.
5. **LLM** — an *interpretation mechanism*, not a source of truth.
6. **Ground truth** — *test/calibration only*, never a production source.

When sources conflict, the higher-ranked source wins; for numbers, deterministic **text anchors** override OCR/LLM.

## Provenance (attached to every value)

```
Provenance {
  source_type:       credit_agreement | financial_statement | ledger | derived
  source_document:   doc_hash | "ledger" | null
  page:              int | null
  source_span:       str | null          # quoted text or cell reference
  extraction_method: text_anchor | ocr | llm | rule | computed
  validation_method: anchor_reconciled | schema | ab_agreement | range_check | none
}
```

---

## Entities

**Scenario** `{ scenario_id, account_id, borrower_ref, effective_contract_ref }`
— all fields runtime-derived; `scenario_id` from ledger txn prefixes.

**Borrower** `{ name, aliases[], account_ids[], documents[] }`
— identity assembled from documents; aliases capture name variants across docs.

**Account** `{ account_id, scenario_id }`
— 1:1 mapping, built from the ledger (each `account_id` ↔ exactly one `scenario_id`).

**Document** `{ doc_hash, path, type, pages, has_section6, reporting_period, entity_refs[], ocr_ref, provenance }`
— `type` from the classifier; `reporting_period` from surviving date anchors; `entity_refs` are `ACC-id`/name hits.

**Contract** `{ doc_ref, reporting_period, covenants[3], definitions{}, amendments[], is_effective }`
— `is_effective` iff `reporting_period` == ledger year; `definitions` is the resolved term map used by the semantic interpreter.

**Covenant (CovenantRule / DSL)** — full spec in [COVENANT_DSL.md](COVENANT_DSL.md).

**Transaction** `{ txn_id, scenario_id, date, account_id, counterparty, description, amount_raw, currency, amount_usd, category, category_provenance }`
— `scenario_id` from `txn_id` prefix; `amount_usd` = FX-converted; `category` from the taxonomy classifier.

**FinancialMetric** `{ name, kind: ratio|amount, value, components[], provenance }`
— `components` lists contributing `txn_id`s and/or statement figures with their own provenance.

**Evidence** `{ covenant_ref, operation, candidate_txn_id, baseline_verdict, counterfactual_verdict, causal_delta, unique }`
— see the counterfactual model in [COVENANT_DSL.md](COVENANT_DSL.md).

**ValidationResult** `{ cell, checks[], confidence, status: OK|LOW_CONFIDENCE|UNRESOLVED }`
— `checks` records each gate (schema, anchor reconcile, A/B agreement, range).

**AuditRecord** `{ scenario, clause, dsl, in_scope_txn_ids[], categories{}, components[], fx_applied, actual, status, evidence, ab_agreement, validations[] }`
— **verifiable facts only; no chain-of-thought.** Written as one JSON per scenario under `audit/`.

---

## Category taxonomy (extensible, semantics-derived)

Categories are derived from covenant language, transaction descriptions, counterparties, accounting semantics, and document definitions — **not** engineered to fit the 36 public cells (GT only validates coverage). The taxonomy is a small hierarchy, e.g.:

```
operating:  payroll · taxes · utilities · insurance · rent_lease · marketing · professional_services · maintenance
financing:  interest · loan_principal · restricted_payments (dividends/buybacks) · subordinated
investing:  capex (equipment/property) · disposals
adjustments: refunds_credits · corrections · intercompany
revenue:    sales_settlement · rebates_received · deposits_returned
```

Each transaction gets exactly one primary category (with provenance). New categories may be added without touching covenant-evaluation code; the DSL references categories by **semantic name**, never by transaction id.
