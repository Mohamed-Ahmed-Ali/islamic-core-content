"""Tests for unified forgiving Arabic search normalizer and query literal matcher."""

from __future__ import annotations

from pipelines.common.search_text import (
    arabic_match_literal,
    normalize_arabic_search,
    project_tafsir_text,
    strip_arabic_marks,
    tafsir_plain_text,
)


def test_normalize_arabic_search_folds() -> None:
    # أ إ آ ٱ -> ا
    assert normalize_arabic_search("أحمد") == "احمد"
    assert normalize_arabic_search("إبراهيم") == "ابراهيم"
    assert normalize_arabic_search("آدم") == "ادم"
    assert normalize_arabic_search("ٱلله") == "الله"

    # ى -> ي
    assert normalize_arabic_search("هدى") == "هدي"
    assert normalize_arabic_search("على") == "علي"

    # ة -> ه
    assert normalize_arabic_search("رحمة") == "رحمه"
    assert normalize_arabic_search("صلاة") == "صلاه"
    assert normalize_arabic_search("نيّة") == "نيه"


def test_normalize_arabic_search_deletion_boundaries() -> None:
    # U+0610–U+061A
    assert normalize_arabic_search("\u0610") == ""
    assert normalize_arabic_search("\u0615") == ""
    assert normalize_arabic_search("\u061A") == ""
    # Boundaries around U+0610–U+061A
    assert normalize_arabic_search("\u060F") == "\u060F"
    assert normalize_arabic_search("\u061B") == "\u061B"

    # U+0640 (Tatweel)
    assert normalize_arabic_search("تـطويل") == "تطويل"
    assert normalize_arabic_search("\u0640") == ""
    assert normalize_arabic_search("\u063F") == "\u063F"
    assert normalize_arabic_search("\u0641") == "\u0641"

    # U+064B–U+065F (Harakat and diacritics)
    assert normalize_arabic_search("\u064B") == ""
    assert normalize_arabic_search("\u0652") == ""
    assert normalize_arabic_search("\u065F") == ""
    # Boundaries around U+064B–U+065F
    assert normalize_arabic_search("\u064A") == "\u064A"
    assert normalize_arabic_search("\u0660") == "\u0660"

    # U+0670 (Superscript Alef)
    assert normalize_arabic_search("\u0670") == ""
    assert normalize_arabic_search("\u066F") == "\u066F"
    assert normalize_arabic_search("\u0671") == "ا"  # folds to alef
    assert normalize_arabic_search("\u0672") == "\u0672"

    # U+06D6–U+06ED (Quranic annotations)
    assert normalize_arabic_search("\u06D6") == ""
    assert normalize_arabic_search("\u06DC") == ""
    assert normalize_arabic_search("\u06DF") == ""
    assert normalize_arabic_search("\u06E4") == ""
    assert normalize_arabic_search("\u06E7") == ""
    assert normalize_arabic_search("\u06E8") == ""
    assert normalize_arabic_search("\u06EA") == ""
    assert normalize_arabic_search("\u06ED") == ""
    # Boundaries around U+06D6–U+06ED
    assert normalize_arabic_search("\u06D5") == "\u06D5"
    assert normalize_arabic_search("\u06EE") == "\u06EE"

    # U+0897–U+089F
    assert normalize_arabic_search("\u0897") == ""
    assert normalize_arabic_search("\u089B") == ""
    assert normalize_arabic_search("\u089F") == ""
    # Boundaries around U+0897–U+089F
    assert normalize_arabic_search("\u0896") == "\u0896"
    assert normalize_arabic_search("\u08A0") == "\u08A0"

    # U+08CA–U+08FF
    assert normalize_arabic_search("\u08CA") == ""
    assert normalize_arabic_search("\u08E0") == ""
    assert normalize_arabic_search("\u08FF") == ""
    # Boundaries around U+08CA–U+08FF
    assert normalize_arabic_search("\u08C9") == "\u08C9"
    assert normalize_arabic_search("\u0900") == "\u0900"

    # U+10EC5
    assert normalize_arabic_search("\U00010EC5") == ""
    assert normalize_arabic_search("\U00010EC4") == "\U00010EC4"
    assert normalize_arabic_search("\U00010EC6") == "\U00010EC6"

    # U+10EFA–U+10EFF
    assert normalize_arabic_search("\U00010EFA") == ""
    assert normalize_arabic_search("\U00010EFD") == ""
    assert normalize_arabic_search("\U00010EFF") == ""
    # Boundaries around U+10EFA–U+10EFF
    assert normalize_arabic_search("\U00010EF9") == "\U00010EF9"
    assert normalize_arabic_search("\U00010F00") == "\U00010F00"

    # Presentation forms U+FE70–U+FE72, U+FE74, U+FE76–U+FE7F
    assert normalize_arabic_search("\uFE70") == ""
    assert normalize_arabic_search("\uFE71") == ""
    assert normalize_arabic_search("\uFE72") == ""
    assert normalize_arabic_search("\uFE6F") == "\uFE6F"
    assert normalize_arabic_search("\uFE73") == "\uFE73"
    assert normalize_arabic_search("\uFE74") == ""
    assert normalize_arabic_search("\uFE75") == "\uFE75"
    assert normalize_arabic_search("\uFE76") == ""
    assert normalize_arabic_search("\uFE7B") == ""
    assert normalize_arabic_search("\uFE7F") == ""
    assert normalize_arabic_search("\uFE80") == "\uFE80"


def test_normalize_arabic_search_unchanged_characters() -> None:
    # Hamza variants preserved: ء ؤ ئ
    assert normalize_arabic_search("شَيْءٌ") == "شيء"
    assert normalize_arabic_search("مَسْؤُولٌ") == "مسؤول"
    assert normalize_arabic_search("بِرْئٌ") == "برئ"
    assert normalize_arabic_search("قُرْءَان") == "قرءان"

    # Persian Yeh (U+06CC) preserved unchanged (not folded to ي)
    assert normalize_arabic_search("\u06CC") == "\u06CC"
    assert normalize_arabic_search("فارسی") == "فارسی"

    # Whole-word ligatures preserved
    assert normalize_arabic_search("ﷺ") == "ﷺ"
    assert normalize_arabic_search("ﷻ") == "ﷻ"

    # Non-Arabic combining marks preserved
    assert normalize_arabic_search("e\u0301") == "e\u0301"

    # Latin, digits, spaces, and punctuation preserved
    assert normalize_arabic_search("Book 123: Part (A) - Hello!") == "Book 123: Part (A) - Hello!"


def test_normalize_arabic_search_idempotence() -> None:
    cases = [
        "",
        "   ",
        "إِنَّ الأَعْمَالَ عَلَىٰ نِيَّةٍ",
        "شَيْءٌ وَمَسْؤُولٌ وَبِرْئٌ وَرَحْمَةٌ تـطويل",
        "الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ ۞",
        "فارسی \u06CC ﷺ",
    ]
    for text in cases:
        once = normalize_arabic_search(text)
        twice = normalize_arabic_search(once)
        assert twice == once


def test_normalize_arabic_search_empty_and_whitespace() -> None:
    assert normalize_arabic_search("") == ""
    assert normalize_arabic_search("   ") == "   "
    assert normalize_arabic_search("\t\n") == "\t\n"


def test_arabic_match_literal_empty_and_marks_only() -> None:
    assert arabic_match_literal("") is None
    assert arabic_match_literal("   ") is None
    assert arabic_match_literal("\t\n  ") is None
    # Diacritics only
    assert arabic_match_literal("ًٌٍَُِّْٰ") is None
    # Tatweel only
    assert arabic_match_literal("ـ") is None
    # Tatweel-plus-annotation case
    assert arabic_match_literal("ـ\u06D6") is None
    assert arabic_match_literal("  ـ\u06D6  ") is None
    assert arabic_match_literal("\u0640\u06ED\u0897\uFE70") is None


def test_arabic_match_literal_quoting_and_escaping() -> None:
    # Basic word
    assert arabic_match_literal("كِتَابٌ") == '"كتاب"'
    assert arabic_match_literal("رَبِّ") == '"رب"'

    # Folded query
    assert arabic_match_literal("الأعمال عَلَىٰ") == '"الاعمال علي"'
    assert arabic_match_literal("الاعمال علي") == '"الاعمال علي"'
    assert arabic_match_literal("النِّيَّة") == '"النيه"'

    # Preserved internal spaces
    assert arabic_match_literal("الاعمال   علي") == '"الاعمال   علي"'
    assert arabic_match_literal("  الأعمال   عَلَىٰ  ") == '"الاعمال   علي"'

    # Embedded double quotes are doubled
    assert arabic_match_literal('الأعمال "علي"') == '"الاعمال ""علي"""'
    assert arabic_match_literal('قال "نعم" و"لا"') == '"قال ""نعم"" و""لا"""'
    assert arabic_match_literal('"') == '""""'
    assert arabic_match_literal('""') == '""""""'


def test_strip_arabic_marks_legacy_contract() -> None:
    # Deletes diacritics but does NOT fold alefs, yaa, or ta marbuta
    assert strip_arabic_marks("أَحْـمَد") == "أحـمد"
    assert strip_arabic_marks("رَبِّ") == "رب"
    # Does not fold ta marbuta or yaa
    assert strip_arabic_marks("صَلَاةٌ") == "صلاة"
    assert strip_arabic_marks("عَلَىٰ") == "على"


def test_tafsir_plain_text_and_project_tafsir_text() -> None:
    html = "<p>تفسير <!-- comment --> <script>ignore</script> أَحْـمَد</p>"
    plain = tafsir_plain_text(html, text_format="html")
    assert plain == "تفسير أَحْـمَد"

    # ar uses forgiving normalization
    proj_ar = project_tafsir_text(html, text_format="html", language="ar")
    assert proj_ar == "تفسير احمد"

    # non-ar does not normalize Arabic
    proj_en = project_tafsir_text(html, text_format="html", language="en")
    assert proj_en == "تفسير أَحْـمَد"
