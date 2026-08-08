# COVENANT_DSL.md — Covenant Intermediate Representation

A covenant compiles to a small **closed AST**, produced by a compositional
parser over the contract text and executed by a fixed interpreter (no
`eval`/`exec`). Legal prose never reaches the calculation engine: only AST
nodes do, so every reported figure traces back to a metric, an operator and
a threshold.

See also: [PLAN.md](PLAN.md) · [ARCHITECTURE.md](ARCHITECTURE.md) ·
[DATA_MODEL.md](DATA_MODEL.md) · [THREAT_MODEL.md](THREAT_MODEL.md)

---

## Compilation

```
Article 6 text (OCR, script-canonicalized)
      │  clause splitting — on headings, with positional numbering and
      │  paragraph-break recovery when OCR damages or drops a heading
      ▼
clause text
      │  component extraction — comparator, threshold + unit, period,
      │  every metric the clause names (inflection-tolerant stems),
      │  springing condition
      ▼
structure assembly — using the connective the clause actually uses
      ▼
Covenant (closed AST)
```

Metrics are recognised by accounting vocabulary rather than by whole
phrases, so a clause expressing the same structure in different words still
compiles. A clause whose structure is not recognised compiles to `None` and
its cell is reported unresolved rather than guessed.

### Connectives

| Clause wording | Structure |
|---|---|
| "отношение A к B", "A … 0.08х B" | `Ratio(A, B)` |
| "сумме A и B", "совокупной величины A и B" | `Add(A, B)` |
| "A за вычетом B" | `Subtract(A, B)` |
| "наибольшей из A и B" | `Max(A, B)` |
| "только при условии, что X превышают $N" | `Applicability` |

Both ratio forms are handled: written explicitly the clause names the
relation ("отношение … к …"); written implicitly the multiple itself
separates the two sides. Each side is bounded by its own sentence so a
following definition sentence naming the same captions again cannot add
phantom operands, and repeated mentions of one metric collapse to a single
operand.

## Node types

| Node | Meaning |
|---|---|
| `MetricLeaf(metric, scope, period)` | one statement caption, optionally group-scoped or quarter-scoped |
| `Ebitda(scope, adjusted)` | revenue less operating expenses, optionally with material one-off add-backs |
| `Add` / `Subtract` / `Max` / `Ratio` | the only operators the evaluator can execute |
| `Applicability` | a springing covenant's trigger condition |

Comparators are limited to `<=`, `>=`, `<`, `>`; the unit is `USD` or
`ratio`. Scope is `borrower` or `group`; period is the full covenant
period, a named quarter, or period end.

## Metrics

`revenue`, `operating_expenses`, `capital_expenditures`, `lease_payments`,
`utilities`, `payroll`, `taxes`, `insurance_premiums`, `interest_expense`,
`financing_proceeds`, `professional_services`, plus two **scope metrics** —
`related_party_payments` and `subsidiary_asset_transfers` — whose
membership is decided solely by the KYC dossier and which therefore have no
ledger anchor of their own.

## `actual` semantics

`actual` is the real value of the metric the covenant limits: always
positive, in USD for amounts and as a plain number for ratios, rounded to
two decimals once at the end. It is reported even when a carve-out leaves
the covenant compliant despite exceeding its limit — the metric's value,
never the limit and never `null`.

### Boundary reconciliation

The submitted figure is rounded to cents, so the exact and rounded values
can fall on opposite sides of a threshold. The reported value governs — it
is what the answer states — unless rounding moved the figure by at least
the official 5% tolerance, in which case the rounding is material and the
exact comparison stands. Both comparisons are recorded either way.

## Evidence

`evidence_txn_id` is the single transaction whose corrected treatment
changes the verdict — never the largest contributor, the last before period
end, or a row that merely adds to a total.

Each candidate contributing to a metric the covenant depends on is removed
counterfactually and the whole expression re-evaluated. A transaction
qualifies only when its removal flips compliance.

- Exactly one flipping transaction → that transaction.
- Several flip → the one an authority actually **corrected** (reclassified
  to another caption, or given an amount the ledger export omitted),
  because that correction is what changed the treatment and produced the
  verdict. If no single corrected transaction stands out, the outcome is
  genuinely aggregate.
- None flip, or the metric comes from a disclosed figure with no ledger
  row → `null`.

Removing a transaction that switches a springing covenant off entirely is a
change of *applicability*, not of compliance, and does not qualify.
