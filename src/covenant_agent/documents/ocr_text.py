"""OCR-tolerant text matching.

Tesseract routinely swaps Cyrillic letters for their Latin lookalikes in
otherwise-Russian words ("не" comes back as "He", "СУММЫ" as "CYMMBI"),
because the glyphs are identical. Matching prose literally therefore fails
on a random subset of documents.

``canon`` folds every confusable pair -- in both scripts and both cases --
onto one representative letter, so a pattern and the text agree regardless
of which script the OCR engine happened to pick. The mapping is strictly
one character to one character, so offsets in the canonical string still
index the original text and extracted spans keep their original spelling.
"""

from __future__ import annotations

# Confusable groups: every character maps to the group's representative.
_CONFUSABLE_GROUPS: tuple[tuple[str, str], ...] = (
    ("a", "аАaA"),
    ("b", "вВB"),
    ("e", "еЕeEёЁ"),
    ("k", "кКkK"),
    ("m", "мМmM"),
    ("h", "нНhH"),
    ("o", "оОoO"),
    ("p", "рРpP"),
    ("c", "сСcC"),
    ("t", "тТtT"),
    ("y", "уУyY"),
    ("x", "хХxX"),
    ("i", "іІiI"),
    ("g", "ГгgG"),
    ("3", "зЗ3"),
    ("0", "0"),
)

_CANON_MAP = {
    ord(char): representative
    for representative, group in _CONFUSABLE_GROUPS
    for char in group
}


def canon(text: str) -> str:
    """Canonical, OCR-script-insensitive form of ``text``.

    Length-preserving: ``len(canon(t)) == len(t)``, so a match offset found
    in the canonical string can be used to slice the original.
    """
    return text.casefold().translate(_CANON_MAP)


def ru(phrase: str) -> str:
    """Canonicalize a Russian literal for embedding in a regex.

    Only letters change; regex metacharacters and ASCII structure are left
    alone, so ``ru`` is safe to interpolate into a pattern that also
    contains character classes and quantifiers.
    """
    return canon(phrase)
