"""Shared vocabulary (DATA_MODEL.md / COVENANT_DSL.md). No business data here."""

from __future__ import annotations

from enum import Enum


class CovenantStatus(str, Enum):
    COMPLIANT = "COMPLIANT"
    BREACH = "BREACH"


class MetricKind(str, Enum):
    RATIO = "ratio"
    AMOUNT = "amount"


class Operator(str, Enum):
    LE = "<="
    GE = ">="
    LT = "<"
    GT = ">"
    EQ = "=="


class Aggregation(str, Enum):
    SUM = "sum"
    ABS_SUM = "abs_sum"
    MAX_ABS = "max_abs"
    COUNT = "count"
    RATIO = "ratio"


class Sign(str, Enum):
    OUTFLOW = "outflow"
    INFLOW = "inflow"
    ANY = "any"


class Scope(str, Enum):
    AGGREGATE = "aggregate"
    SINGLE_TXN = "single_txn"


class SourceType(str, Enum):
    CREDIT_AGREEMENT = "credit_agreement"
    FINANCIAL_STATEMENT = "financial_statement"
    LEDGER = "ledger"
    DERIVED = "derived"


class ExtractionMethod(str, Enum):
    TEXT_ANCHOR = "text_anchor"
    OCR = "ocr"
    LLM = "llm"
    RULE = "rule"
    COMPUTED = "computed"


class ValidationMethod(str, Enum):
    ANCHOR_RECONCILED = "anchor_reconciled"
    SCHEMA = "schema"
    AB_AGREEMENT = "ab_agreement"
    RANGE_CHECK = "range_check"
    NONE = "none"


class ValidationStatus(str, Enum):
    OK = "OK"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNRESOLVED = "UNRESOLVED"


class EvidenceOperation(str, Enum):
    EXCLUDE = "exclude"
    INCLUDE = "include"
    RECLASSIFY = "reclassify"
    CORRECT = "correct"


class DocumentType(str, Enum):
    CREDIT_AGREEMENT = "credit_agreement"
    KYC = "kyc"
    FINANCIAL_STATEMENT = "financial_statement"
    AUDIT = "audit"
    COMPLIANCE = "compliance"
    CORRESPONDENCE = "correspondence"
    POLICY = "policy"
    OTHER = "other"
