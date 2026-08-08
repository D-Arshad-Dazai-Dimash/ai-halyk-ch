"""Exception hierarchy. Failures degrade to LOW_CONFIDENCE/UNRESOLVED results
(see ARCHITECTURE.md, Failure boundaries) -- they are caught at stage
boundaries and recorded, never silently swallowed into a guessed answer.
"""

from __future__ import annotations


class CovenantAgentError(Exception):
    """Base class for all covenant-agent errors."""


class UnresolvedError(CovenantAgentError):
    """Raised when a stage cannot produce a result and no fallback applies
    (e.g. no ACC-id and no name match). Caller records ValidationStatus.UNRESOLVED.
    """


class LowConfidenceError(CovenantAgentError):
    """Raised when a stage produces a result that fails a validation gate
    (schema, anchor reconciliation, A/B agreement). Caller records
    ValidationStatus.LOW_CONFIDENCE rather than propagating a guess.
    """


class DocumentExtractionError(CovenantAgentError):
    """PDF could not be opened, rendered, or OCR'd within limits."""


class SchemaValidationError(CovenantAgentError):
    """A DSL or output object failed Pydantic/schema validation."""


class UntrustedContentError(CovenantAgentError):
    """Raised when document/ledger content attempts to act as an instruction
    (e.g. prompt-injection pattern) rather than data. See THREAT_MODEL.md #1.
    """
