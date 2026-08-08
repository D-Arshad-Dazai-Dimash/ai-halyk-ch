# THREAT_MODEL.md — Covenant Compliance Agent

The system processes **untrusted financial documents**. PDFs and ledger free-text are adversarial input: text inside a document is **content, never an instruction**.

See also: [PLAN.md](PLAN.md), [ARCHITECTURE.md](ARCHITECTURE.md), [DATA_MODEL.md](DATA_MODEL.md), [COVENANT_DSL.md](COVENANT_DSL.md).

---

## Assets
- Correctness/integrity of `submission.json`.
- Confidentiality of local data and logs.
- Integrity of the developer machine (no code execution from inputs).

## Untrusted inputs
- Every PDF and its OCR/extracted text.
- Ledger free-text fields (`description`, `counterparty`).

## Trust boundaries
- **Untrusted:** document content (including OCR output) + ledger free text.
- **Trusted:** the deterministic parsers, the AST evaluator, and the ledger's structured numeric fields.
- **Out-of-band (test-only):** `ground_truth.json`.

No model runs on the production path, so the prompt-injection surface that
motivated the semi-trusted tier no longer exists: document text is only
ever matched against fixed patterns, never interpreted as instructions.

---

## Threats & mitigations

| # | Threat | Vector | Mitigation |
|---|---|---|---|
| 1 | **Prompt injection** ("ignore previous instructions…") | OCR text | Structurally impossible in the default configuration: no model is invoked, so document text is only ever regex-matched. Injected prose can at most fail to match a pattern. With the model fallback armed, an unparsed clause does reach a model — but only its *structural* reply is used: the clause is delimited as untrusted data under a system rule never to follow it, the reply is validated into the closed AST (unknown metric or node ⇒ rejected), and any threshold it states must occur verbatim in the clause. The anchor check eliminates *fabricated* figures, not document-borne ones: a number the attacker writes into the clause is, by construction, in the clause. That is not a new exposure — a malicious contract can state any threshold and the deterministic parser reads it just the same (threat #3); a covenant's threshold is whatever the contract says. What the fallback adds is bounded to the clause's *structure*, and the closed AST bounds that in turn |
| 2 | **OCR / text spoofing** to bias a metric | crafted glyphs / numbers | Critical numbers parsed from deterministic Latin anchors, not OCR; A/B independent recompute; range checks |
| 3 | **Malicious document content** | crafted prose/figures | Numeric authority comes from a closed set of disclosure patterns; an unrecognised disclosure is ignored, never guessed at |
| 4 | **Arbitrary code execution** | covenant "execution" | The AST is a closed set of node types evaluated by a fixed interpreter; **no `eval`/`exec`**; no `pickle`/unsafe-YAML deserialization |
| 5 | **Path traversal** | filenames/paths in corpus | Read only within `documents/`; normalize + sanitize paths; ignore symlinks/absolute paths |
| 6 | **Malformed / decompression-bomb PDF** | huge or corrupt files | Page/size limits; per-doc timeouts; per-doc `try/except` → skip + flag |
| 7 | **Dependency supply chain** | third-party libs | Pinned versions + hash lock; minimal dependency set; offline install audit |
| 8 | **Secrets exposure** | logs / artifacts | The only credential is an optional model-fallback API key, read from a git-ignored `.env` or the environment and never logged, echoed, or written to a run report — diagnostics name the *model*, never the key. `.gitignore` covers `.env`, caches, renders, OCR text, any GT-derived artifacts |
| 8a | **Outbound exfiltration** | any network use | The default container profile runs with `network_mode: none`; with no key configured the agent needs no network, so denial is enforceable rather than assumed. Arming the fallback requires a separate `online` compose profile, so egress is never granted implicitly. What leaves the machine is then bounded to the text of clauses the parser could not read — never the ledger, and never a whole document |
| 9 | **Ground-truth leakage** | accidental import/use in production | `src/` cannot import GT; **GT-absent production run** in CI; a mutation test derives the public instance's identifiers from the corpus and scans production string constants for them; discovery classifies an answer key found in the input directory as ignored |
| 10 | **Temp-file leakage** | rendered PNG pages | Scoped temp dir; explicit cleanup; git-ignored |
| 11 | **Malicious embedded content** | JS/attachments/links in PDF | Use PyMuPDF text/render only; never execute embedded scripts; do not fetch external references |

---

## Secrets & repository hygiene
- The one possible credential is a model-fallback API key. It is loaded from `.env` (git-ignored) or the environment, never committed, never printed, and never passed into the container image — the `online` compose profile forwards it from the host at run time.
- `.gitignore` excludes: `.env`, `data/` caches (`renders/`, `ocr/`, `categories/`), `audit/`, rendered images, and any calibration artifacts derived from `ground_truth.json`.
- No sensitive document text in commit history or logs; audit records store verifiable facts, not full document dumps beyond cited spans.

## Data-leakage invariant (enforced)
Deleting `ground_truth.json` entirely must change **no** production behavior. Enforced by: (a) import-graph test proving no GT symbol is reachable from `src/`; (b) a CI job that removes the file and asserts a full, self-validating production run.
