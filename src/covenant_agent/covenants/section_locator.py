"""Locates the financial-covenants section within a contract's OCR text.

Feeding the whole document downstream is not an option: a contract runs to
tens of thousands of characters, and clause splitting on that much text
finds boundaries everywhere. This narrows it to the operative section.

**The article number is not assumed.** The submission template states which
clauses are being asked for -- ``5.1`` or ``6.1`` or ``7.1`` -- and that is
authoritative, so the caller passes the article number in rather than the
locator guessing. Hardcoding "article 6" reads whichever section happens to
be numbered 6, which on a contract that numbers covenants differently is
confidently the wrong text rather than an honest miss.

The heading convention itself stays generic (Russian or English wording) so
a contract that words its headings differently still resolves.
"""

from __future__ import annotations

import re

#: Any article/section heading, capturing its number.
_ANY_HEADING = re.compile(r"(?:Статья|Раздел|Section|Article)\s*(\d+)\b", re.IGNORECASE)

# Hard safety cap even when no closing heading is found -- keeps an
# unfamiliar layout from pushing an unbounded slice downstream.
_MAX_SECTION_CHARS = 6000


def extract_covenant_section_text(full_text: str, article: str = "6") -> str:
    """Return the covenants section of ``full_text``.

    Falls back to a bounded leading slice when no heading for ``article`` is
    found, so extraction proceeds best-effort rather than returning nothing.
    """
    # A standalone heading for the article -- deliberately not matching a
    # clause heading like "Section 5.1", which repeats the article number.
    # Treating one as the section start truncates every clause before the
    # last, which reads as "this borrower has one covenant" rather than as
    # a failure.
    heading = re.compile(
        r"(?:Статья|Раздел|Section|Article)\s*"
        + re.escape(article)
        + r"\b(?!\s*[.,]\s*\d)",
        re.IGNORECASE,
    )
    matches = list(heading.finditer(full_text))
    if matches:
        # The last occurrence is the section body; earlier ones are the
        # table of contents.
        start = matches[-1].start()
    else:
        # Some contracts number clauses "Section 5.1" with no "Section 5"
        # heading of their own. Start at the article's first clause.
        first_clause = re.search(
            r"(?:Статья|Раздел|Section|Article|Пункт)\s*"
            + re.escape(article)
            + r"\s*[.,]\s*\d",
            full_text,
            re.IGNORECASE,
        )
        if first_clause is None:
            return full_text[:_MAX_SECTION_CHARS]
        start = first_clause.start()
    end = min(len(full_text), start + _MAX_SECTION_CHARS)

    # The section ends at the next heading that is not this same article --
    # whatever number it carries. Assuming the successor is numbered N+1
    # breaks on a contract that skips or re-numbers sections.
    for following in _ANY_HEADING.finditer(full_text, start + 1):
        if following.group(1) != article:
            end = min(following.start(), end)
            break

    return full_text[start:end].strip()
