"""Run diagnostics.

On an unfamiliar corpus the useful question is not "what is the answer" but
"where did the pipeline lose its footing" -- which borrowers have no
authority for a purpose, which clauses failed to compile, which cells could
not be computed. Under a submission deadline that has to be visible in
seconds, so the pipeline reports it rather than leaving it to be dug out of
audit records.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from covenant_agent.documents.doctype import DocType, Lifecycle
from covenant_agent.resolve.authority import AuthorityBundle, CatalogedDocument


@dataclass
class Diagnostics:
    document_types: Counter[str] = field(default_factory=Counter)
    lifecycles: Counter[str] = field(default_factory=Counter)
    authority_warnings: list[str] = field(default_factory=list)
    uncompiled_clauses: list[str] = field(default_factory=list)
    unresolved_cells: list[str] = field(default_factory=list)
    scenarios_without_account: list[str] = field(default_factory=list)
    model_assisted_clauses: list[str] = field(default_factory=list)
    model_rejections: list[str] = field(default_factory=list)
    models_used: list[str] = field(default_factory=list)

    def note_catalog(self, catalog: list[CatalogedDocument]) -> None:
        for document in catalog:
            self.document_types[document.doc_type.value] += 1
            self.lifecycles[document.lifecycle.value] += 1

    def note_authorities(self, scenario_id: str, bundle: AuthorityBundle) -> None:
        self.authority_warnings.extend(
            f"{scenario_id}/{bundle.account_id}: {warning}" for warning in bundle.warnings
        )

    def note_fallback(self, fallback: object) -> None:
        """Record what the model fallback contributed, if anything.

        ``models_used`` is what the submission's ``model`` field must name:
        a model that actually produced part of an answer has to be
        declared, and one that never ran must not be.
        """
        self.models_used = sorted(getattr(fallback, "models_used", ()) or ())
        self.model_rejections.extend(getattr(fallback, "rejections", ()) or ())

    @property
    def is_clean(self) -> bool:
        return not (
            self.authority_warnings
            or self.uncompiled_clauses
            or self.unresolved_cells
            or self.scenarios_without_account
        )

    def render(self) -> str:
        lines: list[str] = []
        relevant = {
            kind.value
            for kind in DocType
            if kind is not DocType.OTHER
        }
        classified = {k: v for k, v in self.document_types.items() if k in relevant}
        lines.append(
            "classified documents: "
            + (", ".join(f"{k}={v}" for k, v in sorted(classified.items())) or "none")
            + f"  (other={self.document_types.get(DocType.OTHER.value, 0)})"
        )
        interesting = {
            k: v
            for k, v in self.lifecycles.items()
            if k != Lifecycle.UNKNOWN.value
        }
        lines.append(
            "lifecycles: " + (", ".join(f"{k}={v}" for k, v in sorted(interesting.items())) or "none")
        )

        if self.models_used:
            lines.append("models consulted: " + ", ".join(self.models_used))

        for label, items in (
            ("scenarios with no ledger account", self.scenarios_without_account),
            ("authority warnings", self.authority_warnings),
            ("clauses that did not compile", self.uncompiled_clauses),
            ("clauses compiled by a model", self.model_assisted_clauses),
            ("model proposals rejected", self.model_rejections),
            ("cells left unresolved", self.unresolved_cells),
        ):
            if items:
                lines.append(f"{label} ({len(items)}):")
                lines.extend(f"    {item}" for item in items[:20])
                if len(items) > 20:
                    lines.append(f"    ... and {len(items) - 20} more")

        if self.is_clean:
            lines.append("no gaps: every scenario resolved, every clause compiled")
        return "\n".join(lines)

    def write(self, path: Path) -> None:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "document_types": dict(self.document_types),
                    "lifecycles": dict(self.lifecycles),
                    "scenarios_without_account": self.scenarios_without_account,
                    "authority_warnings": self.authority_warnings,
                    "uncompiled_clauses": self.uncompiled_clauses,
                    "model_assisted_clauses": self.model_assisted_clauses,
                    "model_rejections": self.model_rejections,
                    "models_used": self.models_used,
                    "unresolved_cells": self.unresolved_cells,
                    "clean": self.is_clean,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
