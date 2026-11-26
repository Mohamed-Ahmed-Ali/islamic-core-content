"""Search normalization and literal match helpers for Mashrūʿ Taqrīb as-Sunnah."""

from __future__ import annotations

from pipelines.common.search_text import normalize_arabic

_TAQREEB_FOLD_TABLE: dict[int, str] = {
    ord("أ"): "ا",
    ord("إ"): "ا",
    ord("آ"): "ا",
    ord("ٱ"): "ا",
    ord("ى"): "ي",
    ord("ی"): "ي",
}


def normalize_taqreeb_arabic(text: str) -> str:
    """Normalize Arabic text for Taqreeb search projection.

    Strips diacritics via normalize_arabic, then folds:
    أ إ آ ٱ -> ا
    ى ی -> ي
    Does not fold ئ, ء, ؤ, or ة.
    """
    cleaned = normalize_arabic(text)
    return cleaned.translate(_TAQREEB_FOLD_TABLE)


def taqreeb_match_literal(text: str) -> str | None:
    """Convert search query text into an escaped FTS5 literal phrase match string.

    Normalizes and trims; returns None if empty.
    Wraps in double quotes and doubles embedded double quotes for MATCH ? binding.
    """
    normalized = normalize_taqreeb_arabic(text).strip()
    if not normalized:
        return None
    escaped = normalized.replace('"', '""')
    return f'"{escaped}"'
