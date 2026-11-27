"""Search text normalization and projection helpers."""

from __future__ import annotations

from html.parser import HTMLParser

# Arabic FTS rule code points to delete (U+0610–U+061A, U+064B–U+065F, U+0670,
# U+06D6–U+06DC, U+06DF–U+06E4, U+06E7–U+06E8, U+06EA–U+06ED).
_ARABIC_DELETE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0610, 0x061A),
    (0x064B, 0x065F),
    (0x0670, 0x0670),
    (0x06D6, 0x06DC),
    (0x06DF, 0x06E4),
    (0x06E7, 0x06E8),
    (0x06EA, 0x06ED),
)

_ARABIC_DELETE_TABLE: dict[int, None] = {
    cp: None
    for start, end in _ARABIC_DELETE_RANGES
    for cp in range(start, end + 1)
}


def normalize_arabic(text: str) -> str:
    """Normalize Arabic text for FTS search projection according to exact deletion rules.

    Deletes code points U+0610–U+061A, U+064B–U+065F, U+0670, U+06D6–U+06DC,
    U+06DF–U+06E4, U+06E7–U+06E8, and U+06EA–U+06ED.
    Does not fold Arabic letters or apply Unicode decomposition.
    """
    return text.translate(_ARABIC_DELETE_TABLE)


_ARABIC_SEARCH_DELETE_RANGES: tuple[tuple[int, int], ...] = (
    (0x0610, 0x061A),
    (0x0640, 0x0640),
    (0x064B, 0x065F),
    (0x0670, 0x0670),
    (0x06D6, 0x06ED),
    (0x0897, 0x089F),
    (0x08CA, 0x08FF),
    (0x10EC5, 0x10EC5),
    (0x10EFA, 0x10EFF),
    (0xFE70, 0xFE72),
    (0xFE74, 0xFE74),
    (0xFE76, 0xFE7F),
)

_ARABIC_SEARCH_FOLD_MAPPINGS: dict[int, str] = {
    0x0623: "ا",
    0x0625: "ا",
    0x0622: "ا",
    0x0671: "ا",
    0x0649: "ي",
    0x0629: "ه",
}

_ARABIC_SEARCH_TABLE: dict[int, str | None] = {
    **{
        cp: None
        for start, end in _ARABIC_SEARCH_DELETE_RANGES
        for cp in range(start, end + 1)
    },
    **_ARABIC_SEARCH_FOLD_MAPPINGS,
}


def normalize_arabic_search(text: str) -> str:
    """Normalize Arabic text for FTS search projection according to forgiving contract.

    Folds:
      أ U+0623, إ U+0625, آ U+0622, ٱ U+0671 -> ا U+0627
      ى U+0649 -> ي U+064A
      ة U+0629 -> ه U+0647
    Deletes:
      U+0610–U+061A, U+0640, U+064B–U+065F, U+0670, U+06D6–U+06ED,
      U+0897–U+089F, U+08CA–U+08FF, U+10EC5, U+10EFA–U+10EFF,
      U+FE70–U+FE72, U+FE74, U+FE76–U+FE7F.
    Preserves unlisted characters unchanged, including ء, ؤ, ئ, Persian ی U+06CC,
    whole-word ligatures, and non-Arabic combining marks.
    Does not apply Unicode normalization, case change, or whitespace change.
    Empty input returns "".
    """
    return text.translate(_ARABIC_SEARCH_TABLE)


def arabic_match_literal(text: str) -> str | None:
    """Convert search query text into an escaped FTS5 literal phrase match string.

    Normalizes via normalize_arabic_search, then trims.
    Returns None when empty.
    Wraps non-empty normalized text in double quotes and doubles embedded double quotes.
    Designed for binding to MATCH ?.
    """
    normalized = normalize_arabic_search(text).strip()
    if not normalized:
        return None
    escaped = normalized.replace('"', '""')
    return f'"{escaped}"'

class _TafsirHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.runs: list[str] = []
        self._ignore_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in ("script", "style"):
            self._ignore_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style") and self._ignore_depth > 0:
            self._ignore_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignore_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self.runs.append(cleaned)

    def handle_comment(self, data: str) -> None:
        pass


def project_tafsir_text(text: str, *, text_format: str, language: str) -> str:
    """Project Tafsir text into plain text search projection.

    Supports 'plain' and 'html' formats. For HTML, collects text-node data
    using HTMLParser(convert_charrefs=True) in source order, omits comments
    and script/style bodies, and joins runs with a single ASCII space.
    Applies Arabic normalization rule for language 'ar'.
    """
    if text_format == "plain":
        projected = text
    elif text_format == "html":
        parser = _TafsirHTMLParser()
        parser.feed(text)
        parser.close()
        projected = " ".join(parser.runs)
    else:
        raise ValueError(f"Unsupported text_format: {text_format!r}")

    if language == "ar":
        return normalize_arabic(projected)
    return projected
