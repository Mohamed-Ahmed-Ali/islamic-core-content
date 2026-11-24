"""Synthetic fixture builder for test and quality gate verification.

Creates candidate, rebuild, and source directory trees under a specified root,
populates synthetic inputs, authors registry, lock, and clearance records,
and deterministically builds all synthetic SQLite packs and audio assets twice.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from typing import Any
import wave

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SPECS_SQL_DIR = REPO_ROOT / "specs" / "sql"
FIXTURE_INPUTS_DIR = REPO_ROOT / "tests" / "fixtures" / "inputs"

RELEASE_TAG = "content-v0.0.0"
PACK_VERSION = "0.0.0"
TIMESTAMP = "2026-01-01T00:00:00Z"
BASE_URL = f"https://example.org/{RELEASE_TAG}/"


@dataclass(frozen=True)
class SqlitePackSpec:
    id: str
    category: str
    schema_sql: str
    filename: str
    languages: tuple[str, ...]
    completeness: str
    required: bool
    requires_fts5: bool
    fts_tables: tuple[str, ...]
    source_rel_path: str
    coverage_report: dict[str, tuple[int, str | None]]
    catalog_coverage: dict[str, int]
    extra_meta: dict[str, str] = field(default_factory=dict)


FIXED_SQLITE_PACKS: tuple[SqlitePackSpec, ...] = (
    SqlitePackSpec(
        id="quran-core",
        category="quran",
        schema_sql="quran_core.v1.sql",
        filename="quran-core.db",
        languages=("ar",),
        completeness="complete",
        required=True,
        requires_fts5=True,
        fts_tables=("ayahs_fts",),
        source_rel_path="sqlite/quran-core.json",
        coverage_report={"expected_total": (3, None), "ar_present": (3, None)},
        catalog_coverage={"expected": 3, "available": 3},
    ),
    SqlitePackSpec(
        id="quran-scripts-extended",
        category="quran",
        schema_sql="quran_scripts_extended.v1.sql",
        filename="quran-scripts-extended.db",
        languages=("ar",),
        completeness="complete",
        required=False,
        requires_fts5=False,
        fts_tables=(),
        source_rel_path="sqlite/quran-scripts-extended.json",
        coverage_report={"expected_total": (3, None), "ar_present": (3, None)},
        catalog_coverage={"expected": 3, "available": 3},
    ),
    SqlitePackSpec(
        id="quran-tr-en-fixture",
        category="quran",
        schema_sql="quran_translation.v1.sql",
        filename="quran-tr-en-fixture.db",
        languages=("en",),
        completeness="complete",
        required=False,
        requires_fts5=True,
        fts_tables=("translations_fts",),
        source_rel_path="sqlite/quran-tr-en-fixture.json",
        coverage_report={"expected_total": (3, None), "en_present": (3, None), "en_missing": (0, None)},
        catalog_coverage={"expected": 3, "available": 3},
    ),
    SqlitePackSpec(
        id="quran-tafsir-ar-fixture",
        category="quran",
        schema_sql="quran_tafsir.v1.sql",
        filename="quran-tafsir-ar-fixture.db",
        languages=("ar",),
        completeness="complete",
        required=False,
        requires_fts5=True,
        fts_tables=("tafsir_fts",),
        source_rel_path="qul/tafsir-ar-fixture.json",
        coverage_report={"expected_total": (3, None), "ar_present": (3, None)},
        catalog_coverage={"expected": 3, "available": 3},
    ),
    SqlitePackSpec(
        id="quran-morphology",
        category="quran",
        schema_sql="quran_morphology.v1.sql",
        filename="quran-morphology.db",
        languages=("ar",),
        completeness="complete",
        required=False,
        requires_fts5=False,
        fts_tables=(),
        source_rel_path="sqlite/quran-morphology.json",
        coverage_report={"expected_total": (2, None), "ar_present": (2, None)},
        catalog_coverage={"expected": 2, "available": 2},
    ),
    SqlitePackSpec(
        id="quran-wbw-en",
        category="quran",
        schema_sql="quran_wbw.v1.sql",
        filename="quran-wbw-en.db",
        languages=("ar", "en"),
        completeness="complete",
        required=False,
        requires_fts5=True,
        fts_tables=("word_translations_fts",),
        source_rel_path="sqlite/quran-wbw-en.json",
        coverage_report={"expected_total": (2, None), "ar_present": (2, None), "en_present": (2, None), "en_missing": (0, None)},
        catalog_coverage={"expected": 2, "available": 2},
    ),
    SqlitePackSpec(
        id="hadith-fixture",
        category="hadith",
        schema_sql="hadith_book.v1.sql",
        filename="hadith-fixture.db",
        languages=("ar", "en"),
        completeness="partial",
        required=False,
        requires_fts5=True,
        fts_tables=("hadiths_fts", "hadiths_full_fts"),
        source_rel_path="hadith/fixture.json",
        coverage_report={"expected_total": (2, None), "ar_present": (2, None), "en_present": (1, None), "en_missing": (1, None)},
        catalog_coverage={"expected": 2, "available": 1},
        extra_meta={"matn_split_verified": "true"},
    ),
)

AUDIO_PACK_DEF: dict[str, Any] = {
    "id": "audio-adhan-core",
    "category": "audio",
    "version": PACK_VERSION,
    "format": "audio",
    "languages": ["ar"],
    "completeness": "complete",
    "licenseRef": "sources/licenses/audio-adhan-core.json",
    "required": False,
    "audioManifestRef": "audio-manifest.json",
}

# Fixed column definitions for schema tables (fixed literals, never JSON keys)
SURAH_COLUMNS = (
    "id", "name_ar", "name_tname", "name_en", "revelation_type",
    "revelation_order", "ayah_count", "ruku_count", "start_ayah_id",
)
AYAH_COLUMNS = (
    "id", "surah_id", "ayah_number", "verse_key", "text_uthmani",
    "text_simple_clean", "text_ar_search", "juz", "hizb_quarter",
    "manzil", "ruku", "page_madani", "sajdah_type",
)
AYAH_SCRIPT_COLUMNS = (
    "ayah_id", "verse_key", "text_uthmani_min", "text_simple",
    "text_simple_plain", "text_simple_min",
)
TRANSLATION_COLUMNS = ("ayah_id", "surah_id", "ayah_number", "verse_key", "text")
TAFSIR_ENTRY_COLUMNS = ("id", "group_key", "start_ayah_id", "end_ayah_id", "text", "text_format", "text_search")
TAFSIR_MAP_COLUMNS = ("ayah_id", "verse_key", "entry_id")
ROOT_COLUMNS = (
    "id", "root_buckwalter", "root_ar", "root_ar_spaced", "total_occurrences",
    "verb_occurrences", "noun_occurrences", "frequency_rank",
)
LEMMA_COLUMNS = (
    "id", "lemma_buckwalter", "lemma_ar", "root_id", "primary_pos",
    "occurrence_count", "frequency_rank",
)
VERB_CONCORDANCE_COLUMNS = (
    "id", "root_id", "lemma_id", "verb_ar", "verb_buckwalter", "root_ar",
    "verb_form", "verb_form_roman", "total_count", "perf_count", "impf_count",
    "impv_count", "active_count", "passive_count", "frequency_rank",
)
WORD_COLUMNS = (
    "id", "ayah_id", "surah_id", "ayah_number", "word_number", "word_key",
    "word_ar_uthmani", "word_ar_clean", "primary_pos", "is_verb", "root_id",
    "lemma_id", "verb_concordance_id", "verb_form", "verb_tense", "verb_voice",
)
MORPHOLOGY_SEGMENT_COLUMNS = (
    "id", "word_id", "segment_number", "segment_role", "form_buckwalter",
    "form_ar", "pos_tag", "features_raw",
)
WORD_TRANSLATION_COLUMNS = (
    "id", "ayah_id", "word_number", "word_key", "text_uthmani", "translation", "transliteration",
)
BOOK_COLUMNS = ("id", "number", "name_ar", "name_en")
CHAPTER_COLUMNS = ("id", "book_id", "number", "title_ar", "title_en")
HADITH_COLUMNS = (
    "id", "book_id", "chapter_id", "hadith_number", "urn", "arabic_full",
    "english_full", "text_split_mode", "arabic_sanad", "arabic_matn",
    "arabic_matn_clean", "arabic_full_clean", "english_narrator", "english_body", "grade_ar",
    "grade_en", "reference_json", "is_ar_missing", "is_en_missing",
)


def insert_table_rows(
    conn: Any,
    table_name: str,
    columns: tuple[str, ...],
    rows: list[tuple[Any, ...]],
) -> None:
    """Insert rows using fixed literal table/column names and parameterized values.

    Never interpolate identifiers from JSON keys.
    """
    placeholders = ", ".join("?" for _ in columns)
    col_str = ", ".join(columns)
    sql = f"INSERT INTO {table_name} ({col_str}) VALUES ({placeholders});"
    conn.executemany(sql, rows)


def generate_synthetic_audio_wav(dest_path: Path) -> None:
    """Generate 2 s, 16-bit LE PCM, mono, 32 kHz tone (1 kHz square wave)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 32000
    duration_s = 2
    total_samples = sample_rate * duration_s  # 64,000 samples
    period_samples = 32  # 1 kHz at 32 kHz = 32 samples per period (16 high, 16 low)
    cycles = total_samples // period_samples  # 2,000 cycles

    high_chunk = struct.pack("<16h", *([4096] * 16))
    low_chunk = struct.pack("<16h", *([-4096] * 16))
    period = high_chunk + low_chunk

    with wave.open(str(dest_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(period * cycles)


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def format_duration_seconds(duration: Fraction) -> float:
    """Round rational duration to milliseconds half-up, returning float seconds (e.g. 2.0 or 1.5).

    Uses integer-rational half-up arithmetic without Decimal context.
    """
    num = duration.numerator
    den = duration.denominator
    if den <= 0 or num < 0:
        raise ValueError(f"Invalid duration fraction: {duration}")
    ms = (2 * num * 1000 + den) // (2 * den)
    if ms <= 0:
        raise ValueError(f"Duration rounded to nonpositive {ms} ms")
    return ms / 1000.0


def setup_synthetic_source(source_dir: Path) -> tuple[Path, Path, Path]:
    """Copy synthetic inputs into source_dir/raw-inputs and generate registry, lock and licenses."""
    raw_root = source_dir / "raw-inputs"
    sources_dir = source_dir / "sources"
    licenses_dir = sources_dir / "licenses"

    raw_root.mkdir(parents=True, exist_ok=True)
    licenses_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy SQLite input JSONs
    sqlite_inputs = [
        "quran-core.json",
        "quran-scripts-extended.json",
        "quran-tr-en-fixture.json",
        "quran-morphology.json",
        "quran-wbw-en.json",
    ]
    (raw_root / "sqlite").mkdir(parents=True, exist_ok=True)
    for name in sqlite_inputs:
        src = FIXTURE_INPUTS_DIR / "sqlite" / name
        dst = raw_root / "sqlite" / name
        shutil.copy2(src, dst)

    # 2. Copy Hadith inputs
    (raw_root / "hadith").mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURE_INPUTS_DIR / "hadith" / "fixture.json", raw_root / "hadith" / "fixture.json")
    shutil.copy2(FIXTURE_INPUTS_DIR / "hadith" / "fixture.splits.json", raw_root / "hadith" / "fixture.splits.json")

    # 3. Copy Tafsir input
    (raw_root / "qul").mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURE_INPUTS_DIR / "qul" / "tafsir-ar-fixture.json", raw_root / "qul" / "tafsir-ar-fixture.json")

    # 4. Generate audio wav master
    wav_path = raw_root / "audio" / "adhan" / "fixture.wav"
    generate_synthetic_audio_wav(wav_path)

    # 5. Build registry mapping
    registry_map: dict[str, dict[str, Any]] = {
        "sqlite/quran-core.json": {
            "sourceUrl": "https://example.org/fixture/sqlite/quran-core.json",
            "sourceVersion": "1",
            "packIds": ["quran-core"],
        },
        "sqlite/quran-scripts-extended.json": {
            "sourceUrl": "https://example.org/fixture/sqlite/quran-scripts-extended.json",
            "sourceVersion": "1",
            "packIds": ["quran-scripts-extended"],
        },
        "sqlite/quran-tr-en-fixture.json": {
            "sourceUrl": "https://example.org/fixture/sqlite/quran-tr-en-fixture.json",
            "sourceVersion": "1",
            "packIds": ["quran-tr-en-fixture"],
        },
        "sqlite/quran-morphology.json": {
            "sourceUrl": "https://example.org/fixture/sqlite/quran-morphology.json",
            "sourceVersion": "1",
            "packIds": ["quran-morphology"],
        },
        "sqlite/quran-wbw-en.json": {
            "sourceUrl": "https://example.org/fixture/sqlite/quran-wbw-en.json",
            "sourceVersion": "1",
            "packIds": ["quran-wbw-en"],
        },
        "hadith/fixture.json": {
            "sourceUrl": "https://example.org/fixture/hadith/fixture.json",
            "sourceVersion": "1",
            "packIds": ["hadith-fixture"],
        },
        "hadith/fixture.splits.json": {
            "sourceUrl": "https://example.org/fixture/hadith/fixture.splits.json",
            "sourceVersion": "1",
            "packIds": ["hadith-fixture"],
        },
        "qul/tafsir-ar-fixture.json": {
            "sourceUrl": "https://example.org/fixture/qul/tafsir-ar-fixture.json",
            "sourceVersion": "1",
            "packIds": ["quran-tafsir-ar-fixture"],
        },
        "audio/adhan/fixture.wav": {
            "sourceUrl": "https://example.org/fixture/audio/adhan/fixture.wav",
            "sourceVersion": "1",
            "packIds": ["audio-adhan-core"],
        },
    }

    registry_path = sources_dir / "registry.json"
    registry_bytes = (
        json.dumps(registry_map, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
        + b"\n"
    )
    registry_path.write_bytes(registry_bytes)

    # 6. Author manifest.lock.json via documented lock author CLI
    lock_path = sources_dir / "manifest.lock.json"
    lock_cmd = [
        sys.executable,
        "-m",
        "pipelines.common.lock_inputs",
        "--registry",
        str(registry_path),
        "--raw-root",
        str(raw_root),
        "--output",
        str(lock_path),
    ]
    subprocess.run(lock_cmd, check=True, shell=False)

    # 7. Author clearance records under sources/licenses/{pack-id}.json
    packs_to_sources: dict[str, list[str]] = {}
    for rel_path, reg_entry in registry_map.items():
        for pid in reg_entry["packIds"]:
            packs_to_sources.setdefault(pid, []).append(rel_path)

    for pid, paths in packs_to_sources.items():
        primary = paths[0]
        is_audio = pid == "audio-adhan-core"
        sources_list = []
        for p in sorted(paths):
            src_item: dict[str, Any] = {
                "path": p,
                "name": "Synthetic fixture",
                "sourceUrl": registry_map[p]["sourceUrl"],
                "sourceVersion": registry_map[p]["sourceVersion"],
                "rightsEvidence": ["https://example.org/fixture/synthetic"],
                "notice": "Synthetic fixture",
                "attribution": "Synthetic fixture",
                "permittedUses": [],
            }
            if is_audio:
                src_item["slug"] = "fixture"
                src_item["reciter"] = "Synthetic tone"
                src_item["clipStartMs"] = 0
            sources_list.append(src_item)

        rec = {
            "packId": pid,
            "licenseSpdx": "CC0-1.0",
            "primaryPath": primary,
            "status": "fixture",
            "reviewedBy": None,
            "sources": sources_list,
        }
        lic_file = licenses_dir / f"{pid}.json"
        lic_file.write_bytes(
            json.dumps(rec, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        )

    return registry_path, lock_path, raw_root


def populate_pack_tables(
    pack_id: str,
    conn: Any,
    raw_root: Path,
    *,
    reverse_order: bool = False,
) -> None:
    """Populate database tables for a specific pack using fixed table writers.

    Applies row transformation only for core, morph, Tafsir, and Hadith.
    Sorts all rows by primary key in ascending order (after reversing if reverse_order=True).
    """
    from pipelines.common.search_text import normalize_arabic, project_tafsir_text

    def _order_and_sort(rows: list[dict[str, Any]], pk_key: str = "id") -> list[dict[str, Any]]:
        copied = list(rows)
        if reverse_order:
            copied.reverse()
        copied.sort(key=lambda r: r[pk_key])
        return copied

    if pack_id == "quran-core":
        with open(raw_root / "sqlite" / "quran-core.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        surahs = _order_and_sort(raw["surahs"], "id")
        ayahs = _order_and_sort(raw["ayahs"], "id")
        surah_rows = [
            (
                s["id"], s["name_ar"], s["name_tname"], s["name_en"],
                s["revelation_type"], s["revelation_order"], s["ayah_count"],
                s["ruku_count"], s["start_ayah_id"],
            )
            for s in surahs
        ]
        ayah_rows = [
            (
                a["id"], a["surah_id"], a["ayah_number"], a["verse_key"],
                a["text_uthmani"], a["text_simple_clean"],
                normalize_arabic(a["text_simple_clean"]),
                a["juz"], a["hizb_quarter"], a["manzil"], a["ruku"],
                a["page_madani"], a["sajdah_type"],
            )
            for a in ayahs
        ]
        insert_table_rows(conn, "surahs", SURAH_COLUMNS, surah_rows)
        insert_table_rows(conn, "ayahs", AYAH_COLUMNS, ayah_rows)

    elif pack_id == "quran-scripts-extended":
        with open(raw_root / "sqlite" / "quran-scripts-extended.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        ext = _order_and_sort(raw["ayah_scripts"], "ayah_id")
        rows = [
            (
                r["ayah_id"], r["verse_key"], r["text_uthmani_min"],
                r["text_simple"], r["text_simple_plain"], r["text_simple_min"],
            )
            for r in ext
        ]
        insert_table_rows(conn, "ayah_scripts", AYAH_SCRIPT_COLUMNS, rows)

    elif pack_id == "quran-tr-en-fixture":
        with open(raw_root / "sqlite" / "quran-tr-en-fixture.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        tr = _order_and_sort(raw["translations"], "ayah_id")
        rows = [
            (r["ayah_id"], r["surah_id"], r["ayah_number"], r["verse_key"], r["text"])
            for r in tr
        ]
        insert_table_rows(conn, "translations", TRANSLATION_COLUMNS, rows)

    elif pack_id == "quran-tafsir-ar-fixture":
        with open(raw_root / "qul" / "tafsir-ar-fixture.json", "r", encoding="utf-8") as f:
            entries_data = list(json.load(f))
        if reverse_order:
            entries_data.reverse()

        prepared_entries = []
        map_rows = []
        for entry in entries_data:
            vkeys = entry["verse_keys"]
            first_key = vkeys[0]
            s_str, a_str = first_key.split(":")
            start_id = int(a_str)
            if len(vkeys) == 1:
                group_key = first_key
                end_id = start_id
            else:
                last_key = vkeys[-1]
                end_id = int(last_key.split(":")[1])
                group_key = f"{s_str}:{start_id}-{end_id}"

            text = entry["text"]
            fmt = entry["format"]
            text_search = project_tafsir_text(text, text_format=fmt, language="ar")
            prepared_entries.append((start_id, group_key, start_id, end_id, text, fmt, text_search))
            for vk in vkeys:
                ayah_id = int(vk.split(":")[1])
                map_rows.append((ayah_id, vk, start_id))

        prepared_entries.sort(key=lambda r: r[0])
        map_rows.sort(key=lambda r: r[0])
        insert_table_rows(conn, "tafsir_entries", TAFSIR_ENTRY_COLUMNS, prepared_entries)
        insert_table_rows(conn, "tafsir_ayah_map", TAFSIR_MAP_COLUMNS, map_rows)

    elif pack_id == "quran-morphology":
        with open(raw_root / "sqlite" / "quran-morphology.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        roots = _order_and_sort(raw["roots"], "id")
        lemmas = _order_and_sort(raw["lemmas"], "id")
        verbs = _order_and_sort(raw["verb_concordance"], "id")
        words = _order_and_sort(raw["words"], "id")
        segs = _order_and_sort(raw["morphology_segments"], "id")

        root_rows = [
            (
                r["id"], r["root_buckwalter"], r["root_ar"], r["root_ar_spaced"],
                r["total_occurrences"], r["verb_occurrences"], r["noun_occurrences"],
                r["frequency_rank"],
            )
            for r in roots
        ]
        lemma_rows = [
            (
                r["id"], r["lemma_buckwalter"], r["lemma_ar"], r["root_id"],
                r["primary_pos"], r["occurrence_count"], r["frequency_rank"],
            )
            for r in lemmas
        ]
        verb_rows = [
            (
                r["id"], r["root_id"], r["lemma_id"], r["verb_ar"],
                r["verb_buckwalter"], r["root_ar"], r["verb_form"],
                r["verb_form_roman"], r["total_count"], r["perf_count"],
                r["impf_count"], r["impv_count"], r["active_count"],
                r["passive_count"], r["frequency_rank"],
            )
            for r in verbs
        ]
        word_rows = [
            (
                r["id"], r["ayah_id"], r["surah_id"], r["ayah_number"],
                r["word_number"], r["word_key"], r["word_ar_uthmani"],
                normalize_arabic(r["word_ar_uthmani"]),
                r["primary_pos"], r["is_verb"], r["root_id"], r["lemma_id"],
                r["verb_concordance_id"], r["verb_form"], r["verb_tense"],
                r["verb_voice"],
            )
            for r in words
        ]
        seg_rows = [
            (
                r["id"], r["word_id"], r["segment_number"], r["segment_role"],
                r["form_buckwalter"], r["form_ar"], r["pos_tag"], r["features_raw"],
            )
            for r in segs
        ]
        insert_table_rows(conn, "roots", ROOT_COLUMNS, root_rows)
        insert_table_rows(conn, "lemmas", LEMMA_COLUMNS, lemma_rows)
        insert_table_rows(conn, "verb_concordance", VERB_CONCORDANCE_COLUMNS, verb_rows)
        insert_table_rows(conn, "words", WORD_COLUMNS, word_rows)
        insert_table_rows(conn, "morphology_segments", MORPHOLOGY_SEGMENT_COLUMNS, seg_rows)

    elif pack_id == "quran-wbw-en":
        with open(raw_root / "sqlite" / "quran-wbw-en.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        wbw = _order_and_sort(raw["word_translations"], "id")
        rows = [
            (
                r["id"], r["ayah_id"], r["word_number"], r["word_key"],
                r["text_uthmani"], r["translation"], r["transliteration"],
            )
            for r in wbw
        ]
        insert_table_rows(conn, "word_translations", WORD_TRANSLATION_COLUMNS, rows)

    elif pack_id == "hadith-fixture":
        with open(raw_root / "hadith" / "fixture.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        books = _order_and_sort(raw["books"], "id")
        chapters = _order_and_sort(raw["chapters"], "id")
        hadiths = _order_and_sort(raw["hadiths"], "id")

        book_rows = [(b["id"], b["number"], b["nameAr"], b["nameEn"]) for b in books]
        chapter_rows = [(c["id"], c["bookId"], c["number"], c["titleAr"], c["titleEn"]) for c in chapters]
        hadith_rows = []
        for h in hadiths:
            ref_json = json.dumps(
                h["reference"], separators=(",", ":"), sort_keys=True, ensure_ascii=False, allow_nan=False
            )
            is_ar_missing = 1 if h["arabicFull"] is None else 0
            is_en_missing = 1 if h["englishFull"] is None else 0
            split_mode = h["textSplitMode"]
            ar_sanad = h["arabicSanad"]
            ar_matn = h["arabicMatn"]
            ar_matn_clean = normalize_arabic(ar_matn) if (split_mode == "source_verified" and ar_matn) else None
            ar_full_clean = normalize_arabic(h["arabicFull"]) if (split_mode == "unsplit_raw" and h["arabicFull"]) else None
            hadith_rows.append((
                h["id"], h["bookId"], h["chapterId"], h["hadithNumber"], h["urn"],
                h["arabicFull"], h["englishFull"], split_mode, ar_sanad, ar_matn,
                ar_matn_clean, ar_full_clean, h["englishNarrator"], h["englishBody"], h["gradeAr"],
                h["gradeEn"], ref_json, is_ar_missing, is_en_missing,
            ))
        insert_table_rows(conn, "books", BOOK_COLUMNS, book_rows)
        insert_table_rows(conn, "chapters", CHAPTER_COLUMNS, chapter_rows)
        insert_table_rows(conn, "hadiths", HADITH_COLUMNS, hadith_rows)


def build_single_target(
    out_dir: Path,
    registry_path: Path,
    lock_path: Path,
    raw_root: Path,
    *,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    reverse_order: bool = False,
) -> None:
    """Build all packs into out_dir."""
    from pipelines.common.lock_inputs import verify_lock
    from pipelines.common.sqlite_utils import open_pack, write_pack_metadata, finalize_pack
    from pipelines.common.audio_probe import probe_asset

    # Helper: get locked inputs sha256 for a specific pack via documented canonical subset
    def get_pack_lock_sha256(pack_id: str) -> str:
        pack_entries = verify_lock(registry_path, raw_root, lock_path, pack_id=pack_id)
        data = [
            {
                "path": e.path,
                "sourceUrl": e.source_url,
                "sourceVersion": e.source_version,
                "sizeBytes": e.size_bytes,
                "sha256": e.sha256,
            }
            for e in pack_entries
        ]
        canon = json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        return hashlib.sha256(canon).hexdigest().lower()

    # Build all SQLite packs via the fixed inventory
    for spec in FIXED_SQLITE_PACKS:
        db_path = out_dir / spec.filename
        conn = open_pack(db_path, SPECS_SQL_DIR / spec.schema_sql)
        conn.execute("BEGIN;")
        populate_pack_tables(spec.id, conn, raw_root, reverse_order=reverse_order)
        meta = {
            "pack_id": spec.id,
            "pack_version": PACK_VERSION,
            "schema_version": "1",
            "source_name": "Synthetic fixture",
            "source_url": f"https://example.org/fixture/{spec.source_rel_path}",
            "source_version": "1",
            "license_spdx": "CC0-1.0",
            "license_ref": f"sources/licenses/{spec.id}.json",
            "build_input_sha256": get_pack_lock_sha256(spec.id),
            "completeness": spec.completeness,
            "fts5_enabled": "true" if spec.requires_fts5 else "false",
        }
        meta.update(spec.extra_meta)
        write_pack_metadata(conn, meta, spec.coverage_report)
        finalize_pack(conn, fts_tables=spec.fts_tables)
        conn.close()

    # Encode Audio Assets (.m4a and .caf)
    wav_file = raw_root / "audio" / "adhan" / "fixture.wav"
    full_m4a = out_dir / "adhan-fixture-full.m4a"
    ios_caf = out_dir / "adhan-fixture-ios-notif.caf"

    # Full M4A: native AAC-LC, 44.1 kHz mono, 128 kbps, threads=1, bitexact
    cmd_m4a = [
        str(ffmpeg_path),
        "-y",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        str(wav_file),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        str(full_m4a),
    ]
    subprocess.run(cmd_m4a, check=True, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # iOS CAF: adpcm_ima_qt, 32 kHz mono, fade last 48000 samples, threads=1, bitexact
    cmd_caf = [
        str(ffmpeg_path),
        "-y",
        "-nostdin",
        "-threads",
        "1",
        "-i",
        str(wav_file),
        "-map",
        "0:a:0",
        "-ac",
        "1",
        "-c:a",
        "adpcm_ima_qt",
        "-ar",
        "32000",
        "-af",
        "afade=t=out:ss=16000:ns=48000",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        str(ios_caf),
    ]
    subprocess.run(cmd_caf, check=True, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Probe both assets using documented production probe_asset API
    full_codec, full_duration = probe_asset(full_m4a, variant="full")
    ios_codec, ios_duration = probe_asset(ios_caf, variant="ios")

    # Strict CAF duration cap: must be strictly < 29.0s
    if ios_duration >= Fraction(29, 1):
        raise ValueError(f"iOS notification audio duration must be strictly < 29.0s, got {ios_duration}s")

    # Write catalog/audio-manifest.json and catalog/catalog.json
    audio_manifest = {
        "audioManifestVersion": 1,
        "releaseTag": RELEASE_TAG,
        "packId": "audio-adhan-core",
        "tracks": [
            {
                "slug": "fixture",
                "reciter": "Synthetic tone",
                "full": {
                    "filename": "adhan-fixture-full.m4a",
                    "url": f"{BASE_URL}adhan-fixture-full.m4a",
                    "sizeBytes": full_m4a.stat().st_size,
                    "sha256": compute_file_sha256(full_m4a),
                    "codec": full_codec,
                    "durationSeconds": format_duration_seconds(full_duration),
                },
                "ios": {
                    "filename": "adhan-fixture-ios-notif.caf",
                    "url": f"{BASE_URL}adhan-fixture-ios-notif.caf",
                    "sizeBytes": ios_caf.stat().st_size,
                    "sha256": compute_file_sha256(ios_caf),
                    "codec": ios_codec,
                    "durationSeconds": format_duration_seconds(ios_duration),
                },
            }
        ],
    }
    audio_man_path = out_dir / "audio-manifest.json"
    audio_man_path.write_bytes(
        json.dumps(
            audio_manifest, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )

    catalog_packs: list[dict[str, Any]] = [AUDIO_PACK_DEF.copy()]
    for spec in FIXED_SQLITE_PACKS:
        db_path = out_dir / spec.filename
        catalog_packs.append({
            "id": spec.id,
            "category": spec.category,
            "version": PACK_VERSION,
            "format": "sqlite",
            "languages": list(spec.languages),
            "completeness": spec.completeness,
            "licenseRef": f"sources/licenses/{spec.id}.json",
            "required": spec.required,
            "compression": "none",
            "schemaVersion": 1,
            "url": f"{BASE_URL}{spec.filename}",
            "sizeBytes": db_path.stat().st_size,
            "sha256": compute_file_sha256(db_path),
            "coverage": spec.catalog_coverage,
            "requiresFts5": spec.requires_fts5,
        })
    catalog_packs.sort(key=lambda p: p["id"])

    catalog_doc = {
        "catalogVersion": 1,
        "generatedAt": TIMESTAMP,
        "releaseTag": RELEASE_TAG,
        "packs": catalog_packs,
    }
    catalog_path = out_dir / "catalog.json"
    catalog_path.write_bytes(
        json.dumps(
            catalog_doc, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )


def build_fixtures(root_path: Path | str) -> dict[str, Path]:
    """Execute full fixture recipe: setup sources, build candidate and rebuild."""
    from pipelines.toolchain.check_runtime import verify_runtime

    # Preflight once before writes
    ffmpeg_path, ffprobe_path = verify_runtime()

    root = Path(root_path).resolve()
    candidate_dir = root / "candidate"
    rebuild_dir = root / "rebuild"
    source_dir = root / "source"

    targets = (candidate_dir, rebuild_dir, source_dir)
    # Precheck all three paths, including dangling links, before creating any
    for d in targets:
        if d.exists() or d.is_symlink() or os.path.lexists(d):
            raise FileExistsError(f"Target directory already exists or is a link: {d}")

    for d in targets:
        d.mkdir(parents=True, exist_ok=False)

    reg_path, lock_path, raw_root = setup_synthetic_source(source_dir)

    # First build: candidate (forward order)
    build_single_target(
        candidate_dir,
        reg_path,
        lock_path,
        raw_root,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        reverse_order=False,
    )

    # Second build: rebuild (reverse order before primary-key sort)
    build_single_target(
        rebuild_dir,
        reg_path,
        lock_path,
        raw_root,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        reverse_order=True,
    )

    return {
        "candidate": candidate_dir,
        "rebuild": rebuild_dir,
        "source": source_dir,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build deterministic test fixtures.")
    parser.add_argument("--root", "-r", required=True, help="Root directory for candidate, rebuild, and source")
    args = parser.parse_args(argv)

    build_fixtures(args.root)
    print(f"Fixtures successfully built under {args.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
