#!/usr/bin/env python
"""Covenant compliance agent -- command line entry point.

Consumes only the documents, the transaction ledger, and the submission
template's key structure. It never reads the public answer key: that file,
if present in the input directory, is listed among the ignored inputs and
is only ever opened by ``calibration/score.py`` (see
tests/unit/test_no_gt_leakage.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from covenant_agent.common.config import (  # noqa: E402
    AUDIT_OUTPUT_DIR,
    DATASET_DIR,
    ensure_cache_dirs,
)
from covenant_agent.common.exceptions import CovenantAgentError  # noqa: E402
from covenant_agent.covenants.llm_fallback import FallbackCompiler  # noqa: E402
from covenant_agent.deterministic_pipeline import CellResult, run as run_pipeline  # noqa: E402
from covenant_agent.diagnostics import Diagnostics  # noqa: E402
from covenant_agent.ingestion.dataset import discover_dataset  # noqa: E402
from covenant_agent.submission.generator import validate_submission_schema  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Inputs are found by content, not by filename, so pointing at a\n"
            "directory is normally enough:\n"
            "  run.py --input ./my-dataset\n"
            "Individual paths can still be given explicitly:\n"
            "  run.py --documents ./docs --ledger ./txns.csv --template ./tpl.json\n"
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DATASET_DIR,
        help="dataset root to scan recursively (default: %(default)s)",
    )
    parser.add_argument("--documents", type=Path, help="restrict PDF discovery to this directory")
    parser.add_argument("--ledger", type=Path, help="explicit ledger CSV path")
    parser.add_argument("--template", type=Path, help="explicit submission template JSON path")
    parser.add_argument("--output", type=Path, default=Path("submission.json"))
    parser.add_argument("--audit-dir", type=Path, default=AUDIT_OUTPUT_DIR)
    parser.add_argument("--report", type=Path, help="write a run report with input/output hashes")
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="write a diagnostics report of gaps (unclassified documents, uncompiled clauses)",
    )
    parser.add_argument("--team", default="covenant-agent")
    parser.add_argument("--contact-email", default="team@example.com")
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "name of the AI model used to produce the answers. Leave unset: "
            "it is filled in automatically with whichever model actually "
            "read a clause, and stays null when none did -- covenants are "
            "parsed deterministically and every figure is arithmetic"
        ),
    )
    parser.add_argument(
        "--no-model-fallback",
        action="store_true",
        help=(
            "never call a cloud model, even when an API key is configured. "
            "The fallback is otherwise used only for covenant wordings the "
            "deterministic parser cannot compile"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow replacing an existing output file",
    )
    return parser.parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic(path: Path, payload: str) -> None:
    """Write via a temporary sibling and rename, so an interrupted run
    never leaves a half-written file that looks like a valid submission.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def _build_submission(
    template: dict, results: dict[tuple[str, str], CellResult], args: argparse.Namespace
) -> dict:
    answers: dict[str, dict] = {}
    for scenario_id, clauses in template["answers"].items():
        cells: dict[str, dict] = {}
        for clause in clauses:
            cell = results.get((scenario_id, clause))
            cells[clause] = {
                "status": cell.status if cell else "COMPLIANT",
                # Serialize the Decimal through its own text form so the
                # written number never inherits a binary float's error.
                "actual": float(str(cell.actual)) if cell else 0.0,
                "evidence_txn_id": cell.evidence_txn_id if cell else None,
            }
        answers[scenario_id] = cells
    return {
        "team": args.team,
        "contact_email": args.contact_email,
        "model": args.model,
        "answers": answers,
    }


def _write_audit(results: dict[tuple[str, str], CellResult], output_dir: Path) -> None:
    """One record per scenario: verifiable facts only, no model reasoning."""
    output_dir.mkdir(parents=True, exist_ok=True)
    by_scenario: dict[str, dict] = {}
    for (scenario_id, clause), cell in results.items():
        by_scenario.setdefault(scenario_id, {})[clause] = {
            "status": cell.status,
            "actual": str(cell.actual),
            "evidence_txn_id": cell.evidence_txn_id,
            "resolved": cell.resolved,
            "notes": cell.notes,
        }
    for scenario_id, clauses in by_scenario.items():
        (output_dir / f"{scenario_id}.json").write_text(
            json.dumps(clauses, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    ensure_cache_dirs()

    if args.output.exists() and not args.overwrite:
        print(f"refusing to overwrite existing {args.output} (pass --overwrite)")
        return 1

    try:
        dataset = discover_dataset(
            args.input,
            documents_dir=args.documents,
            ledger=args.ledger,
            template=args.template,
        )
    except CovenantAgentError as exc:
        print(f"dataset error: {exc}")
        return 1

    print(f"Input root: {dataset.root}")
    print(f"  ledger:    {dataset.ledger.relative_to(dataset.root)}")
    print(f"  template:  {dataset.template.relative_to(dataset.root)}")
    print(f"  documents: {len(dataset.documents)} PDF(s)")
    print(f"  ignored:   {len(dataset.ignored)} other file(s)")

    template = json.loads(dataset.template.read_text(encoding="utf-8"))
    diagnostics = Diagnostics()
    fallback = FallbackCompiler.create(enabled=not args.no_model_fallback)
    print(
        "  model fallback: "
        + (
            " then ".join(f"{p.name}/{p.model}" for p in fallback.providers)
            if fallback.available
            else "off (no API key configured)"
        )
    )

    results = run_pipeline(
        dataset.documents, dataset.ledger, template["answers"], diagnostics, fallback
    )

    print()
    print(diagnostics.render())
    print()

    # The rules require naming the model when one is used. Declare exactly
    # the models that actually read a clause: claiming one that never ran
    # would be as false as hiding one that did.
    if args.model is None and diagnostics.models_used:
        args.model = ", ".join(diagnostics.models_used)

    submission = _build_submission(template, results, args)
    errors = validate_submission_schema(submission, template)
    if errors:
        print("SCHEMA VALIDATION ERRORS:")
        for error in errors:
            print(f"  - {error}")
        return 1

    _write_atomic(args.output, json.dumps(submission, indent=2, ensure_ascii=False))
    print(f"\nWrote {args.output}")

    _write_audit(results, args.audit_dir)
    print(f"Wrote audit records to {args.audit_dir}")

    unresolved = sorted(key for key, cell in results.items() if not cell.resolved)
    if unresolved:
        print(f"\n{len(unresolved)} unresolved cell(s): {unresolved}")

    if args.diagnostics:
        diagnostics.write(args.diagnostics)
        print(f"Wrote diagnostics to {args.diagnostics}")

    if args.report:
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "model": args.model,
            "input_root": str(dataset.root),
            "inputs": {
                "ledger": {"path": str(dataset.ledger), "sha256": _sha256(dataset.ledger)},
                "template": {"path": str(dataset.template), "sha256": _sha256(dataset.template)},
                "document_count": len(dataset.documents),
                "ignored_count": len(dataset.ignored),
            },
            "output": {"path": str(args.output), "sha256": _sha256(args.output)},
            "cells": {"total": len(results), "unresolved": len(unresolved)},
        }
        _write_atomic(args.report, json.dumps(report, indent=2, ensure_ascii=False))
        print(f"Wrote run report to {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
