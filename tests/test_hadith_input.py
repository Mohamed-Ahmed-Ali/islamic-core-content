"""Tests for canonical Hadith input loading, validation, and staging."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import pytest

from pipelines.common.hadith_input import load_hadith_input
from pipelines.common.stage_hadith_inputs import (
    stage_hadith_book,
    ALLOWED_SLUGS,
    SOURCE_HASHES,
)

CONTENT_ROOT = Path(os.environ.get("CONTENT_ROOT", "C:/Users/moham/Downloads/islamic-core-content/Contant"))

_BASE_VALID_INPUT = {
    "books": [
        {
            "id": 1,
            "number": "1",
            "nameAr": "كتاب",
            "nameEn": None,
        }
    ],
    "chapters": [
        {
            "id": 1,
            "bookId": 1,
            "number": "1",
            "titleAr": "باب",
            "titleEn": "Chapter",
        }
    ],
    "hadiths": [
        {
            "id": 1,
            "bookId": 1,
            "chapterId": 1,
            "hadithNumber": "1",
            "urn": None,
            "arabicFull": "نص الحديث",
            "englishFull": "Hadith text",
            "englishNarrator": None,
            "englishBody": "Hadith text",
            "gradeAr": None,
            "gradeEn": None,
            "reference": {
                "inBook": None,
                "uscMsa": None,
            },
            "textSplitMode": "unsplit_raw",
            "arabicSanad": None,
            "arabicMatn": None,
        }
    ],
}


def _write_canonical_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    path.write_bytes(raw.encode("utf-8"))


# --- 1. Schema & Validation Tests (Red-Green) ---

def test_valid_input_passes(tmp_path: Path) -> None:
    target = tmp_path / "valid.json"
    _write_canonical_json(target, _BASE_VALID_INPUT)
    res = load_hadith_input(target)
    assert res["books"][0]["id"] == 1
    assert len(res["hadiths"]) == 1


def test_fractional_canonical_id_fails_schema(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    # Raw string with float literal token 1.5
    raw_str = json.dumps(data, separators=(",", ":")).replace('"id":1', '"id":1.5', 1)
    target = tmp_path / "fractional.json"
    target.write_text(raw_str, encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)floating point|integer"):
        load_hadith_input(target)

def test_id_exceeding_signed_64bit_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["books"][0]["id"] = 9223372036854775808  # 2^63
    target = tmp_path / "overflow.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError):
        load_hadith_input(target)


def test_duplicate_book_id_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["books"].append({
        "id": 1,
        "number": "2",
        "nameAr": "كتاب آخر",
        "nameEn": None,
    })
    target = tmp_path / "dup_book.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)duplicate.*book.*id"):
        load_hadith_input(target)


def test_duplicate_chapter_id_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["chapters"].append({
        "id": 1,
        "bookId": 1,
        "number": "2",
        "titleAr": "باب 2",
        "titleEn": None,
    })
    target = tmp_path / "dup_chapter.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)duplicate.*chapter.*id"):
        load_hadith_input(target)


def test_duplicate_hadith_id_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"].append(copy.deepcopy(data["hadiths"][0]))
    target = tmp_path / "dup_hadith.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)duplicate.*hadith.*id"):
        load_hadith_input(target)


def test_duplicate_nonnull_urn_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"][0]["urn"] = "urn:test:1"
    h2 = copy.deepcopy(data["hadiths"][0])
    h2["id"] = 2
    data["hadiths"].append(h2)
    target = tmp_path / "dup_urn.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)duplicate.*urn"):
        load_hadith_input(target)


def test_cross_book_chapter_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["books"].append({
        "id": 2,
        "number": "2",
        "nameAr": "كتاب 2",
        "nameEn": None,
    })
    data["chapters"].append({
        "id": 2,
        "bookId": 2,
        "number": "1",
        "titleAr": "باب",
        "titleEn": None,
    })
    # Hadith has bookId=1 but chapterId=2 (which belongs to bookId=2)
    data["hadiths"][0]["chapterId"] = 2
    target = tmp_path / "cross_book.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)mismatch|cross-book"):
        load_hadith_input(target)


def test_dangling_book_id_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["chapters"][0]["bookId"] = 999
    target = tmp_path / "dangling_book.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)dangling|not found"):
        load_hadith_input(target)


def test_dangling_chapter_id_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"][0]["chapterId"] = 999
    target = tmp_path / "dangling_chapter.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)dangling|not found"):
        load_hadith_input(target)


def test_invalid_utf8_fails(tmp_path: Path) -> None:
    target = tmp_path / "invalid_utf8.json"
    target.write_bytes(b'{"books": [\xff\xfe]}')
    with pytest.raises((ValueError, UnicodeDecodeError)):
        load_hadith_input(target)


def test_replacement_char_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"][0]["arabicFull"] = "test \ufffd text"
    target = tmp_path / "replacement.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)replacement"):
        load_hadith_input(target)


# --- 2. Split Evidence Tests ---

def test_verified_row_without_splits_file_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"][0]["textSplitMode"] = "source_verified"
    data["hadiths"][0]["arabicSanad"] = "إسناد"
    data["hadiths"][0]["arabicMatn"] = "متن"
    target = tmp_path / "verified_no_splits.json"
    _write_canonical_json(target, data)
    with pytest.raises(ValueError, match="(?i)splits.*required"):
        load_hadith_input(target, splits_path=None)


def test_splits_file_with_zero_verified_rows_fails(tmp_path: Path) -> None:
    target = tmp_path / "unsplit.json"
    _write_canonical_json(target, _BASE_VALID_INPUT)
    splits_file = tmp_path / "unsplit.splits.json"
    splits_file.write_text('{"1": {"sourceRef": "ref", "arabicSanad": "s", "arabicMatn": "m"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)zero verified rows|0 verified"):
        load_hadith_input(target, splits_path=splits_file)


def test_empty_splits_file_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"][0]["textSplitMode"] = "source_verified"
    data["hadiths"][0]["arabicSanad"] = "إسناد"
    data["hadiths"][0]["arabicMatn"] = "متن"
    target = tmp_path / "verified.json"
    _write_canonical_json(target, data)
    splits_file = tmp_path / "empty.splits.json"
    splits_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)empty|zero verified"):
        load_hadith_input(target, splits_path=splits_file)


def test_splits_file_missing_or_extra_ids_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"][0]["textSplitMode"] = "source_verified"
    data["hadiths"][0]["arabicSanad"] = "إسناد"
    data["hadiths"][0]["arabicMatn"] = "متن"
    target = tmp_path / "verified.json"
    _write_canonical_json(target, data)

    # Extra ID 999
    splits_file = tmp_path / "extra.splits.json"
    splits_file.write_text(
        json.dumps({
            "1": {"sourceRef": "ref", "arabicSanad": "إسناد", "arabicMatn": "متن"},
            "999": {"sourceRef": "ref2", "arabicSanad": "s2", "arabicMatn": "m2"},
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="(?i)mismatch|extra|missing"):
        load_hadith_input(target, splits_path=splits_file)


def test_splits_text_mismatch_fails(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"][0]["textSplitMode"] = "source_verified"
    data["hadiths"][0]["arabicSanad"] = "إسناد"
    data["hadiths"][0]["arabicMatn"] = "متن"
    target = tmp_path / "verified.json"
    _write_canonical_json(target, data)

    splits_file = tmp_path / "mismatch.splits.json"
    splits_file.write_text(
        json.dumps({
            "1": {"sourceRef": "ref", "arabicSanad": "إسناد مختلف", "arabicMatn": "متن"},
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="(?i)mismatch|equality"):
        load_hadith_input(target, splits_path=splits_file)


def test_verified_row_with_valid_splits_succeeds(tmp_path: Path) -> None:
    data = copy.deepcopy(_BASE_VALID_INPUT)
    data["hadiths"][0]["textSplitMode"] = "source_verified"
    data["hadiths"][0]["arabicSanad"] = "إسناد"
    data["hadiths"][0]["arabicMatn"] = "متن"
    target = tmp_path / "verified.json"
    _write_canonical_json(target, data)

    splits_file = tmp_path / "valid.splits.json"
    splits_file.write_text(
        json.dumps({
            "1": {"sourceRef": "ref 1", "arabicSanad": "إسناد", "arabicMatn": "متن"},
        }),
        encoding="utf-8",
    )
    res = load_hadith_input(target, splits_path=splits_file)
    assert res["hadiths"][0]["textSplitMode"] == "source_verified"


# --- 3. Staging Tests ---

def test_staging_narrator_only_and_body_formatting(tmp_path: Path) -> None:
    """Narrator-only -> englishFull=None; Narrator+body -> narrator\nbody."""
    # Create synthetic source JSON for bukhari-like book
    content_dir = tmp_path / "content"
    book_file = content_dir / "hadith-json-main" / "hadith-json-main" / "db" / "by_book" / "the_9_books" / "bukhari.json"
    book_file.parent.mkdir(parents=True, exist_ok=True)

    source_data = {
        "id": 1,
        "metadata": {
            "length": 2,
            "arabic": {"title": "صحيح البخاري"},
            "english": {"title": "Sahih al-Bukhari"},
        },
        "chapters": [
            {"id": 1, "bookId": 1, "arabic": "كتاب", "english": "Book"}
        ],
        "hadiths": [
            {
                "id": 1,
                "idInBook": 1,
                "chapterId": 1,
                "bookId": 1,
                "arabic": "حديث 1",
                "english": {"narrator": "Narrator only", "text": ""},
            },
            {
                "id": 2,
                "idInBook": 2,
                "chapterId": 1,
                "bookId": 1,
                "arabic": "حديث 2",
                "english": {"narrator": "Narrator", "text": "Body"},
            },
        ],
    }
    raw = json.dumps(source_data).encode("utf-8")
    book_file.write_bytes(raw)
    computed_hash = hashlib.sha256(raw).hexdigest()

    raw_dir = tmp_path / "raw-inputs"
    # Overriding hash mapping for test
    saved_hash = SOURCE_HASHES["bukhari"]
    try:
        SOURCE_HASHES["bukhari"] = computed_hash
        out_file = stage_hadith_book(content_root=content_dir, raw_root=raw_dir, book="bukhari")
        res = load_hadith_input(out_file)
        assert res["hadiths"][0]["englishNarrator"] == "Narrator only"
        assert res["hadiths"][0]["englishBody"] is None
        assert res["hadiths"][0]["englishFull"] is None

        assert res["hadiths"][1]["englishNarrator"] == "Narrator"
        assert res["hadiths"][1]["englishBody"] == "Body"
        assert res["hadiths"][1]["englishFull"] == "Narrator\nBody"
    finally:
        SOURCE_HASHES["bukhari"] = saved_hash


def test_nasai_chapter_remapping(tmp_path: Path) -> None:
    """Chapter 35.2 is remapped to 52 across chapters and hadith chapterId."""
    content_dir = tmp_path / "content"
    book_file = content_dir / "hadith-json-main" / "hadith-json-main" / "db" / "by_book" / "the_9_books" / "nasai.json"
    book_file.parent.mkdir(parents=True, exist_ok=True)

    # 51 regular chapters + 1 exceptional chapter 35.2
    chapters = [{"id": i, "bookId": 3, "arabic": f"باب {i}", "english": f"Ch {i}"} for i in range(1, 52)]
    # Use exact string "35.2" in JSON which parses as float/Decimal
    source_json_str = json.dumps({
        "id": 3,
        "metadata": {
            "length": 1,
            "arabic": {"title": "سنن النسائي"},
            "english": {"title": "Sunan an-Nasa'i"},
        },
        "chapters": chapters,
        "hadiths": [
            {
                "id": 1,
                "idInBook": 1,
                "chapterId": 35.2,
                "bookId": 3,
                "arabic": "حديث",
                "english": {"narrator": "", "text": "Text"},
            }
        ],
    })
    # Inject chapter 35.2 into chapters array
    source_json_str = source_json_str.replace(
        '"chapters":[',
        '{"id":35.2,"bookId":3,"arabic":"باب استثنائي","english":"Special Ch"},',
    )
    # Wait, replace properly
    data = json.loads(source_json_str)
    # Put 35.2 as float in chapters
    data["chapters"].append({"id": 35.2, "bookId": 3, "arabic": "باب استثنائي", "english": "Special Ch"})
    raw = json.dumps(data).encode("utf-8")
    book_file.write_bytes(raw)
    computed_hash = hashlib.sha256(raw).hexdigest()

    raw_dir = tmp_path / "raw-inputs"
    saved_hash = SOURCE_HASHES["nasai"]
    try:
        SOURCE_HASHES["nasai"] = computed_hash
        out_file = stage_hadith_book(content_root=content_dir, raw_root=raw_dir, book="nasai")
        res = load_hadith_input(out_file)
        # Check remapped chapter ID 52
        ch52 = [c for c in res["chapters"] if c["id"] == 52]
        assert len(ch52) == 1
        assert ch52[0]["number"] == "35.2"
        assert res["hadiths"][0]["chapterId"] == 52
    finally:
        SOURCE_HASHES["nasai"] = saved_hash


def test_staging_idempotency_and_no_overwrite(tmp_path: Path) -> None:
    content_dir = tmp_path / "content"
    book_file = content_dir / "hadith-json-main" / "hadith-json-main" / "db" / "by_book" / "the_9_books" / "bukhari.json"
    book_file.parent.mkdir(parents=True, exist_ok=True)
    source_data = {
        "id": 1,
        "metadata": {"length": 1, "arabic": {"title": "صحيح"}, "english": {"title": "Sahih"}},
        "chapters": [{"id": 1, "bookId": 1, "arabic": "باب", "english": "Ch"}],
        "hadiths": [{"id": 1, "idInBook": 1, "chapterId": 1, "bookId": 1, "arabic": "حديث", "english": {"narrator": "", "text": "body"}}],
    }
    raw = json.dumps(source_data).encode("utf-8")
    book_file.write_bytes(raw)
    computed_hash = hashlib.sha256(raw).hexdigest()

    raw_dir = tmp_path / "raw-inputs"
    saved_hash = SOURCE_HASHES["bukhari"]
    try:
        SOURCE_HASHES["bukhari"] = computed_hash
        # First run succeeds
        out1 = stage_hadith_book(content_root=content_dir, raw_root=raw_dir, book="bukhari")
        # Second run succeeds idempotently
        out2 = stage_hadith_book(content_root=content_dir, raw_root=raw_dir, book="bukhari")
        assert out1 == out2

        # Corrupt the output file with different content
        out1.write_bytes(b'{"different": true}\n')
        # Re-running must fail without overwrite
        with pytest.raises(ValueError, match="(?i)already exists|nonidentical"):
            stage_hadith_book(content_root=content_dir, raw_root=raw_dir, book="bukhari")
    finally:
        SOURCE_HASHES["bukhari"] = saved_hash


# --- 4. Integration Tests against Real Sources (if available) ---

@pytest.mark.skipif(not CONTENT_ROOT.exists(), reason="Contant root not found")
def test_real_malik_staging_and_pins(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw-inputs"
    out_file = stage_hadith_book(content_root=CONTENT_ROOT, raw_root=raw_dir, book="malik")
    out_bytes = out_file.read_bytes()
    assert len(out_bytes) == 5224529
    assert hashlib.sha256(out_bytes).hexdigest() == "5dab3037718896293bbcc9454983d13f48e8f0fd428642338e488bf323b46f42"

    res = load_hadith_input(out_file)
    assert len(res["hadiths"]) == 1985
    assert len(res["chapters"]) == 61

    # Check preserved heading rows and 34213
    heading_ids = [35830, 35835, 35838, 35841, 35845, 36014, 34213]
    h_map = {h["id"]: h for h in res["hadiths"] if h["id"] in heading_ids}
    assert len(h_map) == 7
    for hid in heading_ids:
        assert h_map[hid]["arabicFull"] is not None
        assert h_map[hid]["englishBody"] is not None
        assert h_map[hid]["englishFull"] is not None


@pytest.mark.skipif(not CONTENT_ROOT.exists(), reason="Contant root not found")
def test_real_ahmad_staging(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw-inputs"
    out_file = stage_hadith_book(content_root=CONTENT_ROOT, raw_root=raw_dir, book="ahmad")
    res = load_hadith_input(out_file)
    assert len(res["hadiths"]) == 26363
    assert len(res["books"]) == 1
    assert len(res["chapters"]) == 0
    # First row check
    h1 = res["hadiths"][0]
    assert h1["id"] == 1
    assert h1["hadithNumber"] == "1"
    assert "\u200f" in h1["arabicFull"]  # bidi marks preserved
    assert h1["englishFull"] is None
    assert h1["englishBody"] is None
    assert h1["englishNarrator"] is None


@pytest.mark.skipif(not CONTENT_ROOT.exists(), reason="Contant root not found")
def test_stage_all_nine_books_oracle_counts(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw-inputs"
    expected_oracles = {
        "bukhari": (7277, 7277, 7276, 1, 7276),
        "muslim": (7459, 7459, 7458, 1, 7458),
        "nasai": (5768, 5768, 5768, 0, 5768),
        "abudawud": (5276, 5276, 5276, 0, 5276),
        "tirmidhi": (4053, 4053, 4052, 1, 4052),
        "ibnmajah": (4345, 4345, 4345, 0, 4345),
        "malik": (1985, 1985, 1985, 0, 1985),
        "darimi": (3406, 3406, 0, 3406, 0),
        "ahmad": (26363, 26363, 0, 26363, 0),
    }
    for slug, (exp_rows, exp_ar, exp_en, exp_en_missing, exp_both) in expected_oracles.items():
        out_file = stage_hadith_book(content_root=CONTENT_ROOT, raw_root=raw_dir, book=slug)
        res = load_hadith_input(out_file)
        rows = len(res["hadiths"])
        ar_present = sum(1 for h in res["hadiths"] if h["arabicFull"] is not None)
        en_present = sum(1 for h in res["hadiths"] if h["englishFull"] is not None)
        en_missing = sum(1 for h in res["hadiths"] if h["englishFull"] is None)
        both = sum(1 for h in res["hadiths"] if h["arabicFull"] is not None and h["englishFull"] is not None)

        assert (rows, ar_present, en_present, en_missing, both) == (exp_rows, exp_ar, exp_en, exp_en_missing, exp_both), f"Mismatch in {slug}"
