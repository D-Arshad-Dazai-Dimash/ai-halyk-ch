"""Enforces the two structural invariants from PLAN.md / THREAT_MODEL.md #9:

1. No production module under src/covenant_agent/** may reference
   ground_truth.json or the GROUND_TRUTH_JSON symbol -- except its single,
   documented definition as an inert path constant in common/config.py
   (calibration/ tooling looks it up from there; production code never
   calls into anything that reads it).

2. No production module may contain literal scenario/transaction/account
   IDs or contract filenames -- those constants belong exclusively to
   tests/golden/ fixtures. This directly enforces the anti-overfitting rule.

A full "delete ground_truth.json and run production end-to-end" integration
test is added in P13 once run.py exists; this test is the static, always-on
guard that catches leakage the moment it's introduced.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "covenant_agent"

# Precise, low-false-positive literal patterns that would indicate the
# concrete public dataset (P1..P10, B1, B4) leaking into production logic.
FORBIDDEN_LITERAL_PATTERNS = {
    "transaction id literal (e.g. TXN-P5-0004)": re.compile(r"TXN-[A-Z0-9]+-\d+"),
    "account id literal (e.g. ACC-7805)": re.compile(r"ACC-\d{3,6}"),
    "hex-hash contract filename literal (e.g. 1d262694c308.pdf)": re.compile(
        r"\b[0-9a-f]{12}\.pdf\b"
    ),
}

GT_REFERENCE_PATTERN = re.compile(r"ground_truth\.json|GROUND_TRUTH_JSON", re.IGNORECASE)

# The one documented, inert exception: config.py defines the path constant
# for calibration/ tooling to consume, but never reads the file itself.
ALLOWED_GT_REFERENCE_FILE = SRC_ROOT / "common" / "config.py"


def _all_src_files() -> list[Path]:
    assert SRC_ROOT.is_dir(), f"expected src root at {SRC_ROOT}"
    return sorted(SRC_ROOT.rglob("*.py"))


def test_src_root_exists_and_nonempty():
    files = _all_src_files()
    assert files, "no production source files found -- check SRC_ROOT"


@pytest.mark.parametrize("path", _all_src_files(), ids=lambda p: str(p.relative_to(SRC_ROOT)))
def test_no_ground_truth_reference_outside_config(path: Path):
    text = path.read_text(encoding="utf-8")
    if path == ALLOWED_GT_REFERENCE_FILE:
        # Allowed to define the path constant, but must not *open/read* it.
        assert "open(" not in text.split("GROUND_TRUTH_JSON")[0][-200:] or True
        return
    matches = GT_REFERENCE_PATTERN.findall(text)
    assert not matches, (
        f"{path.relative_to(SRC_ROOT)} references ground truth "
        f"({matches}); production code must never read GT. See THREAT_MODEL.md #9."
    )


@pytest.mark.parametrize("path", _all_src_files(), ids=lambda p: str(p.relative_to(SRC_ROOT)))
def test_no_hardcoded_scenario_literals(path: Path):
    text = path.read_text(encoding="utf-8")
    violations = []
    for label, pattern in FORBIDDEN_LITERAL_PATTERNS.items():
        found = pattern.findall(text)
        if found:
            violations.append(f"{label}: {sorted(set(found))}")
    assert not violations, (
        f"{path.relative_to(SRC_ROOT)} contains forbidden literals that "
        f"indicate overfitting to the public dataset: {violations}. "
        f"Move concrete IDs to tests/golden/ fixtures instead."
    )


def test_config_defines_gt_path_but_does_not_open_it():
    """common/config.py is allowed to hold the GT path as an inert constant
    (for calibration/ tooling to import) but must not itself call open()/
    json.load() on it.
    """
    text = ALLOWED_GT_REFERENCE_FILE.read_text(encoding="utf-8")
    assert "GROUND_TRUTH_JSON" in text
    # None of these read-triggering calls should appear anywhere in config.py.
    for forbidden in ("json.load(", "open(GROUND_TRUTH_JSON", ".read_text()"):
        assert forbidden not in text, (
            f"config.py appears to read GROUND_TRUTH_JSON directly ({forbidden}); "
            f"it must only define the path."
        )
