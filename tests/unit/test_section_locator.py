from __future__ import annotations

from covenant_agent.covenants.section_locator import extract_covenant_section_text


def test_extracts_body_not_table_of_contents():
    text = (
        "Статья 5 Случаи неисполнения\n"
        "Статья 6 Финансовые ковенанты\n"  # TOC entry -- must be skipped
        "Статья 7 Ограничительные обязательства\n"
        "\n... 200 chars of unrelated body text ...\n"
        "Статья 6 — Финансовые ковенанты\n"
        "Пункт 6.1 текст ковенанта про EBITDA.\n"
        "Статья 7 — Ограничительные обязательства\n"
        "unrelated trailing content that should not be included\n"
    )
    section = extract_covenant_section_text(text)
    assert "Пункт 6.1" in section
    assert "unrelated trailing content" not in section
    assert section.startswith("Статья 6")


def test_falls_back_to_bounded_slice_when_no_heading_found():
    text = "completely unstructured text with no section headings at all " * 200
    section = extract_covenant_section_text(text)
    assert len(section) <= 6000


def test_falls_back_to_capped_slice_when_no_closing_heading():
    text = "Статья 6 Финансовые ковенанты\n" + ("x" * 10000)
    section = extract_covenant_section_text(text)
    assert len(section) <= 6000
    assert section.startswith("Статья 6")


def test_english_section_heading_convention_supported():
    text = "Section 6 Financial Covenants\nsome covenant text here\nSection 7 Other\ntrailing"
    section = extract_covenant_section_text(text)
    assert "some covenant text here" in section
    assert "trailing" not in section
