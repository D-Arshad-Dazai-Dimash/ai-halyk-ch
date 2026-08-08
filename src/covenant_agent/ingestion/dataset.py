"""Content-based dataset discovery.

Inputs are located by what they contain, not by what they are called: the
ledger is the CSV carrying the transaction columns, the template is the
JSON matching the submission schema, and the documents are every PDF found
recursively. Filenames in this corpus are deliberately opaque, and a
private dataset may nest or rename anything, so name-based selection would
be fragile.

Ambiguity is a hard error. Two files that both look like the ledger, or
none at all, stop the run with an actionable message instead of silently
picking one.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from covenant_agent.common.exceptions import CovenantAgentError

#: Columns every transaction ledger must provide.
LEDGER_COLUMNS = frozenset(
    {"txn_id", "date", "account_id", "counterparty", "description", "amount", "currency"}
)
TEMPLATE_TOP_LEVEL = frozenset({"team", "contact_email", "model", "answers"})
TEMPLATE_CELL_KEYS = frozenset({"status", "actual", "evidence_txn_id"})


@dataclass(frozen=True)
class Dataset:
    root: Path
    ledger: Path
    template: Path
    documents: tuple[Path, ...]
    ignored: tuple[Path, ...]


def looks_like_ledger(path: Path) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle), None)
    except (OSError, UnicodeError, csv.Error):
        return False
    return header is not None and LEDGER_COLUMNS.issubset(set(header))


def looks_like_template(path: Path) -> bool:
    """A submission template: the exact top-level shape, with every answer
    cell present and empty. An already-filled submission is rejected, so a
    previous run's output cannot be mistaken for the template.
    """
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or set(data) != set(TEMPLATE_TOP_LEVEL):
        return False
    answers = data.get("answers")
    if not isinstance(answers, dict) or not answers:
        return False
    for clauses in answers.values():
        if not isinstance(clauses, dict) or not clauses:
            return False
        for cell in clauses.values():
            if not isinstance(cell, dict) or set(cell) != set(TEMPLATE_CELL_KEYS):
                return False
            if any(value is not None for value in cell.values()):
                return False
    return True


def _one(candidates: list[Path], what: str, root: Path) -> Path:
    if len(candidates) == 1:
        return candidates[0]
    listing = ", ".join(str(p.relative_to(root)) for p in candidates) or "none"
    raise CovenantAgentError(
        f"expected exactly one {what} under {root}, found {len(candidates)}: {listing}"
    )


def discover_dataset(
    root: Path,
    *,
    documents_dir: Path | None = None,
    ledger: Path | None = None,
    template: Path | None = None,
) -> Dataset:
    """Locate the inputs under ``root``; explicit paths override discovery.

    Anything not selected -- including a public answer key -- is reported as
    ignored and never opened by the pipeline.
    """
    if not root.is_dir():
        raise CovenantAgentError(f"input directory not found: {root}")

    files = [p for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()]

    pdfs = tuple(
        p
        for p in files
        if p.suffix.lower() == ".pdf"
        and (documents_dir is None or documents_dir in p.parents or p.parent == documents_dir)
    )
    if not pdfs:
        raise CovenantAgentError(f"no PDF documents found under {documents_dir or root}")

    resolved_ledger = ledger or _one(
        [p for p in files if looks_like_ledger(p)], "transaction ledger CSV", root
    )
    resolved_template = template or _one(
        [p for p in files if looks_like_template(p)], "submission template JSON", root
    )

    selected = {resolved_ledger, resolved_template, *pdfs}
    ignored = tuple(p for p in files if p not in selected)

    return Dataset(
        root=root,
        ledger=resolved_ledger,
        template=resolved_template,
        documents=pdfs,
        ignored=ignored,
    )
