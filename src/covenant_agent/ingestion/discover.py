"""Document discovery: enumerate documents/, keep only PDFs, content-hash
each for caching. Filenames are opaque hash-like identifiers by design
(PLAN.md) -- we index by content hash, never by filename semantics.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiscoveredDocument:
    path: Path
    content_hash: str  # sha256 hex digest of the file bytes


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_documents(paths: Iterable[Path]) -> list[DiscoveredDocument]:
    """Attach a content hash to each given PDF path."""
    return [
        DiscoveredDocument(path=path, content_hash=_hash_file(path))
        for path in sorted(paths)
    ]


def discover_documents(documents_dir: Path) -> list[DiscoveredDocument]:
    """List every PDF under documents_dir, recursively.

    Non-PDF files (decoy CSVs, notes, OS artifacts) are a different format
    entirely and are skipped by extension -- a general file-type filter,
    not a dataset-specific rule, so it holds unchanged for a holdout corpus
    that nests its documents in subdirectories.
    """
    if not documents_dir.is_dir():
        return []
    return hash_documents(
        path
        for path in documents_dir.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".pdf"
    )
