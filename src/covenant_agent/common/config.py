"""Central configuration: paths and environment-derived settings.

No scenario IDs, transaction IDs, contract filenames, or covenant answers may
ever appear here or anywhere under ``src/`` -- see THREAT_MODEL.md, threat #9
(ground-truth leakage) and PLAN.md's anti-overfitting rule. Those constants
belong exclusively in ``tests/golden/`` fixtures.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

# Project layout
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
# Derived caches (page renders, OCR text) are regenerable and carry no
# authority, so they live wherever the deployment can write -- the packaged
# container mounts a volume for them and keeps the application tree
# read-only.
DATA_CACHE_DIR = Path(os.environ.get("COVENANT_AGENT_CACHE_DIR", str(PROJECT_ROOT / "data")))
RENDERS_CACHE_DIR = DATA_CACHE_DIR / "renders"
OCR_CACHE_DIR = DATA_CACHE_DIR / "ocr"
CATEGORIES_CACHE_DIR = DATA_CACHE_DIR / "categories"
LLM_CACHE_DIR = DATA_CACHE_DIR / "llm"
AUDIT_OUTPUT_DIR = PROJECT_ROOT / "audit_out"
TESSDATA_DIR = PROJECT_ROOT / "tessdata"

# Dataset location: the challenge dataset lives as a sibling directory to
# covenant-agent/. Overridable via env var for portability / CI.
DATASET_DIR = Path(
    os.environ.get(
        "COVENANT_AGENT_DATASET_DIR",
        str(PROJECT_ROOT.parent / "agentic-bank-public"),
    )
)
DOCUMENTS_DIR = DATASET_DIR / "documents"
LEDGER_CSV = DATASET_DIR / "master_ledger_2025.csv"
SUBMISSION_TEMPLATE = DATASET_DIR / "submission_template.json"

# Ground truth is a TEST/CALIBRATION-ONLY artifact. This path is defined here
# only so calibration/ tooling (outside src/ execution path at import time by
# convention) can locate it -- production code under covenant_agent.* must
# never import or call anything that reads GROUND_TRUTH_JSON. Enforced by
# tests/unit/test_no_gt_leakage.py (import-graph + GT-absent run).
GROUND_TRUTH_JSON = DATASET_DIR / "ground_truth.json"

# OCR
TESSERACT_LANGS = "rus+eng"
OCR_RENDER_DPI = 300

def _load_dotenv() -> None:
    """Populate the environment from a project-root ``.env``, if present.

    API keys must never be committed, so the file is gitignored and read at
    import time rather than baked in. Values already exported win, so a CI
    or container environment always overrides the developer's local file.
    """
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_dotenv()

# Ollama (local, offline)
OLLAMA_MODEL = os.environ.get("COVENANT_AGENT_LLM_MODEL", "qwen2.5:7b-instruct")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Cloud model fallback for covenant wordings the deterministic parser cannot
# compile. Inert unless an API key is present: with no key the pipeline is
# exactly the offline deterministic one, and every figure still comes from
# arithmetic -- a model may only propose a clause's *structure*.
MODEL_FALLBACK_PROVIDER = os.environ.get("COVENANT_AGENT_MODEL_PROVIDER", "auto")
ANTHROPIC_MODEL = os.environ.get("COVENANT_AGENT_ANTHROPIC_MODEL", "claude-opus-5")
GROQ_MODEL = os.environ.get("COVENANT_AGENT_GROQ_MODEL", "llama-3.3-70b-versatile")
# A hung provider must never eat the submission window, so both the per-call
# wait and the number of calls a single run may make are bounded.
MODEL_FALLBACK_TIMEOUT_SECONDS = float(os.environ.get("COVENANT_AGENT_MODEL_TIMEOUT", "120"))
MODEL_FALLBACK_MAX_CALLS = int(os.environ.get("COVENANT_AGENT_MODEL_MAX_CALLS", "24"))

# Tolerances
ANCHOR_RECONCILE_REL_TOL = Decimal("0.01")  # 1% relative tolerance between LLM-read and anchor-parsed numbers
AB_AGREEMENT_REL_TOL = Decimal("0.001")  # Engine A vs Verifier B must agree within 0.1%

# submission_template.json's expected clause keys (structural constant, not
# an answer). Present in the template itself; duplicated here only as a
# sanity-check constant used by schema validation, never to select/skip cells.
EXPECTED_CLAUSES = ("6.1", "6.2", "6.3")


def ensure_cache_dirs() -> None:
    for d in (RENDERS_CACHE_DIR, OCR_CACHE_DIR, CATEGORIES_CACHE_DIR, LLM_CACHE_DIR, AUDIT_OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
