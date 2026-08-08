from __future__ import annotations

from covenant_agent.common.enums import DocumentType
from covenant_agent.documents.classify import classify_from_anchors
from covenant_agent.ingestion.anchors import extract_anchors


def test_classifies_credit_agreement_from_clause_markers():
    text = "Статья 6 — Финансовые ковенанты. Пункт 6.1 ... Пункт 6.2 ... Пункт 6.3 ..."
    assert classify_from_anchors(extract_anchors(text)) == DocumentType.CREDIT_AGREEMENT


def test_classifies_kyc_when_no_full_covenant_section():
    text = "Досье KYC клиента. Проверка бенефициарных владельцев."
    assert classify_from_anchors(extract_anchors(text)) == DocumentType.KYC


def test_credit_agreement_takes_priority_over_kyc_keyword():
    text = "KYC приложение к договору. Пункт 6.1 ... 6.2 ... 6.3 ..."
    assert classify_from_anchors(extract_anchors(text)) == DocumentType.CREDIT_AGREEMENT


def test_classifies_other_when_neither_signal_present():
    text = "Обычная деловая переписка о сроках поставки оборудования."
    assert classify_from_anchors(extract_anchors(text)) == DocumentType.OTHER


def test_partial_clause_markers_do_not_trigger_credit_agreement():
    text = "Пункт 6.1 ... Пункт 6.2 ... (раздел 6.3 отсутствует в этом документе)"
    # only 6.1 and 6.2 present as markers -> not a full covenant section... but
    # note "6.3" literally appears in the sentence above too, so use a case
    # that truly omits one marker:
    text2 = "Пункт 6.1 упомянут. Пункт 6.2 упомянут."
    assert classify_from_anchors(extract_anchors(text2)) != DocumentType.CREDIT_AGREEMENT
