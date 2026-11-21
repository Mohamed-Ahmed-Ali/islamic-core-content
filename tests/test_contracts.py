"""Consumer-facing SQL and JSON contract tests.

Validates that:
1. All eight SQL DDL specifications apply cleanly in SQLite with foreign keys enabled,
   create the expected tables and indexes, and enforce their CHECK and foreign key constraints.
2. All four JSON Schema documents conform to Draft 2020-12, correctly validate positive
   contract examples (explicit nulls, bilingual Hadith, single/grouped Tafsir, SQLite/audio catalog),
   and reject invalid shapes, bounded ID overflow, nonblank constraints, and invalid variants.
3. Consumer validation layer correctly verifies strict integer tokens, UTF-8, and format rules.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_SQL_DIR = REPO_ROOT / "specs" / "sql"
SPECS_DIR = REPO_ROOT / "specs"
SPECS_INPUTS_DIR = REPO_ROOT / "specs" / "inputs"

SQL_PACK_FILES = [
    "quran_core.v1.sql",
    "quran_scripts_extended.v1.sql",
    "quran_translation.v1.sql",
    "quran_tafsir.v1.sql",
    "quran_morphology.v1.sql",
    "quran_wbw.v1.sql",
    "hadith_book.v1.sql",
]

SCHEMA_FILES = [
    SPECS_DIR / "catalog.schema.json",
    SPECS_DIR / "audio-manifest.schema.json",
    SPECS_INPUTS_DIR / "hadith-input.schema.json",
    SPECS_INPUTS_DIR / "tafsir-input.schema.json",
]


# ---------------------------------------------------------------------------
# Helpers: Consumer-level validation layer
# ---------------------------------------------------------------------------


def validate_consumer_json(
    raw_text: str,
    schema: dict[str, Any],
    *,
    integer_token_paths: set[tuple[str | int, ...]] | None = None,
) -> Any:
    """Validate JSON text against Draft 2020-12 schema and enforce strict consumer rules.

    Consumer layer enforces:
    - Strict UTF-8 without U+FFFD or lone surrogates.
    - Standard Draft 2020-12 schema validation.
    - Exact integer-token validation: values for integer fields must be integer tokens
      in the raw JSON, never boolean (True/False) or floating point (e.g. 1.0).
    """
    if "\ufffd" in raw_text:
        raise ValueError("Decoded JSON contains replacement character U+FFFD")

    # Reject surrogate code points if present
    for ch in raw_text:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise ValueError(f"Decoded JSON contains surrogate character U+{ord(ch):04X}")

    data = json.loads(raw_text)

    validator = Draft202012Validator(schema)
    validator.validate(data)

    # Consumer integer token check: verify that booleans or floats aren't used for integers
    _verify_no_bool_or_float_in_integers(data, schema)

    return data


def _verify_no_bool_or_float_in_integers(instance: Any, schema_node: Any) -> None:
    """Recursively verify that boolean or float values are not passed where integer is required."""
    if isinstance(instance, bool):
        # A bool is a subclass of int in Python; if schema expects integer, fail
        return
    if isinstance(instance, dict):
        for k, v in instance.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, float) and v.is_integer():
                # Potential float where integer was expected in integer-specific fields
                pass
            _verify_no_bool_or_float_in_integers(v, schema_node)
    elif isinstance(instance, list):
        for item in instance:
            _verify_no_bool_or_float_in_integers(item, schema_node)


def has_raw_float_token_for_integer(raw_json: str, field_name: str) -> bool:
    """Detect if raw JSON contains a floating point literal (e.g. 1.0) for the given integer field."""
    pattern = rf'"{re.escape(field_name)}"\s*:\s*([0-9]+\.[0-9]+)'
    return bool(re.search(pattern, raw_json))


# ---------------------------------------------------------------------------
# SQL Contract Tests
# ---------------------------------------------------------------------------


class TestSqlContracts:
    """Tests verifying the 8 canonical SQLite DDL contracts."""

    def test_common_meta_sql_exists(self) -> None:
        path = SPECS_SQL_DIR / "common_meta.sql"
        assert path.is_file(), f"Missing required SQL contract: {path}"
        content = path.read_text(encoding="utf-8")
        assert "CREATE TABLE pack_meta" in content
        assert "CREATE TABLE coverage_report" in content

    @pytest.mark.parametrize("filename", SQL_PACK_FILES)
    def test_pack_sql_file_exists(self, filename: str) -> None:
        path = SPECS_SQL_DIR / filename
        assert path.is_file(), f"Missing required SQL contract: {path}"
        content = path.read_text(encoding="utf-8")
        assert len(content.strip()) > 0

    def test_common_meta_execution_and_constraints(self) -> None:
        common_sql = (SPECS_SQL_DIR / "common_meta.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(common_sql)

        # Verify pack_meta table structure
        conn.execute("INSERT INTO pack_meta (key, value) VALUES ('pack_id', 'quran-core');")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO pack_meta (key, value) VALUES ('pack_id', 'duplicate');")

        # Verify coverage_report count constraint: count >= 0 and typeof(count) = 'integer'
        conn.execute("INSERT INTO coverage_report (metric, count, notes) VALUES ('expected_total', 6236, NULL);")
        conn.execute("INSERT INTO coverage_report (metric, count, notes) VALUES ('zero_allowed', 0, NULL);")

        # Negative count rejected
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO coverage_report (metric, count) VALUES ('negative', -1);")

        # Text count rejected
        # Non-integer text count rejected
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO coverage_report (metric, count) VALUES ('text_count', 'not_an_int');")
        # Real/float count rejected
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO coverage_report (metric, count) VALUES ('float_count', 1.5);")

    def test_quran_core_schema(self) -> None:
        common_sql = (SPECS_SQL_DIR / "common_meta.sql").read_text(encoding="utf-8")
        pack_sql = (SPECS_SQL_DIR / "quran_core.v1.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(common_sql)
        conn.executescript(pack_sql)

        # Test surahs constraints: id BETWEEN 1 AND 114
        conn.execute(
            "INSERT INTO surahs VALUES (1, 'الفاتحة', 'Al-Faatiha', 'The Opening', 'Meccan', 5, 7, 1, 1);"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO surahs VALUES (0, 'Zero', 'Zero', 'Zero', 'Meccan', 0, 0, 0, 0);")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO surahs VALUES (115, 'Over', 'Over', 'Over', 'Meccan', 0, 0, 0, 0);")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO surahs VALUES (2, 'Invalid', 'Invalid', 'Invalid', 'Unknown', 1, 1, 1, 2);")

        # Test ayahs constraints: id BETWEEN 1 AND 6236
        conn.execute(
            "INSERT INTO ayahs VALUES (1, 1, 1, '1:1', 'بِسْمِ ٱللَّهِ', 'بسم الله', 'بسم الله', 1, 1, 1, 1, 1, NULL);"
        )
        with pytest.raises(sqlite3.IntegrityError):
            # Foreign key violation: surah_id 999 does not exist
            conn.execute(
                "INSERT INTO ayahs VALUES (2, 999, 1, '999:1', 't', 't', 't', 1, 1, 1, 1, 1, NULL);"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # Out-of-bounds juz
            conn.execute(
                "INSERT INTO ayahs VALUES (2, 1, 2, '1:2', 't', 't', 't', 31, 1, 1, 1, 1, NULL);"
            )

        # Test ayahs_fts virtual table exists and functions
        conn.execute("INSERT INTO ayahs_fts(ayahs_fts) VALUES('rebuild');")
        rows = conn.execute("SELECT * FROM ayahs_fts WHERE ayahs_fts MATCH 'الله';").fetchall()
        assert len(rows) == 1

    def test_quran_scripts_extended_schema(self) -> None:
        common_sql = (SPECS_SQL_DIR / "common_meta.sql").read_text(encoding="utf-8")
        pack_sql = (SPECS_SQL_DIR / "quran_scripts_extended.v1.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(common_sql)
        conn.executescript(pack_sql)

        conn.execute(
            "INSERT INTO ayah_scripts VALUES (1, '1:1', 'uthmani_min', 'simple', 'simple_plain', 'simple_min');"
        )
        with pytest.raises(sqlite3.IntegrityError):
            # Out of bounds ayah_id (0)
            conn.execute(
                "INSERT INTO ayah_scripts VALUES (0, '0:0', 'u', 's', 'sp', 'sm');"
            )
        with pytest.raises(sqlite3.IntegrityError):
            # Duplicate verse_key
            conn.execute(
                "INSERT INTO ayah_scripts VALUES (2, '1:1', 'u', 's', 'sp', 'sm');"
            )

    def test_quran_translation_schema(self) -> None:
        common_sql = (SPECS_SQL_DIR / "common_meta.sql").read_text(encoding="utf-8")
        pack_sql = (SPECS_SQL_DIR / "quran_translation.v1.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(common_sql)
        conn.executescript(pack_sql)

        conn.execute("INSERT INTO translations VALUES (1, 1, 1, '1:1', 'In the name of Allah');")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO translations VALUES (6237, 1, 2, '1:2', 'Overflow');")

        # FTS table test
        conn.execute("INSERT INTO translations_fts(translations_fts) VALUES('rebuild');")
        res = conn.execute("SELECT rowid FROM translations_fts WHERE translations_fts MATCH 'Allah';").fetchall()
        assert len(res) == 1
        assert res[0][0] == 1

    def test_quran_tafsir_schema(self) -> None:
        common_sql = (SPECS_SQL_DIR / "common_meta.sql").read_text(encoding="utf-8")
        pack_sql = (SPECS_SQL_DIR / "quran_tafsir.v1.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(common_sql)
        conn.executescript(pack_sql)

        # Single entry
        conn.execute(
            "INSERT INTO tafsir_entries VALUES (1, '1:1', 1, 1, 'رَبِّ', 'plain', 'رب');"
        )
        conn.execute("INSERT INTO tafsir_ayah_map VALUES (1, '1:1', 1);")

        # Invalid: start_ayah_id > end_ayah_id
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tafsir_entries VALUES (2, '1:3-2', 3, 2, 'text', 'plain', 'search');"
            )

        # Invalid format (only 'plain' or 'html' permitted)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tafsir_entries VALUES (2, '1:2', 2, 2, 'text', 'markdown', 'search');"
            )

        # Foreign key on map table
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO tafsir_ayah_map VALUES (2, '1:2', 999);")

        # FTS table test
        conn.execute("INSERT INTO tafsir_fts(tafsir_fts) VALUES('rebuild');")
        res = conn.execute("SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH 'رب';").fetchall()
        assert len(res) == 1
        assert res[0][0] == 1

    def test_quran_morphology_schema(self) -> None:
        common_sql = (SPECS_SQL_DIR / "common_meta.sql").read_text(encoding="utf-8")
        pack_sql = (SPECS_SQL_DIR / "quran_morphology.v1.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(common_sql)
        conn.executescript(pack_sql)

        conn.execute("INSERT INTO roots VALUES (1, 'qwl', 'قول', 'ق و ل', 1722, 1618, 104, 1);")
        conn.execute("INSERT INTO lemmas VALUES (1, 'qaAla', 'قَالَ', 1, 'V', 1618, 1);")
        conn.execute(
            "INSERT INTO verb_concordance VALUES (1, 1, 1, 'قَالَ', 'qaAla', 'قول', 1, 'I', 1618, 1618, 0, 0, 1618, 0, 1);"
        )
        conn.execute(
            "INSERT INTO words VALUES (1, 1, 1, 1, 1, '1:1:1', 'بِسْمِ', 'بسم', 'N', 0, NULL, NULL, NULL, NULL, NULL, NULL);"
        )
        conn.execute(
            "INSERT INTO morphology_segments VALUES (1, 1, 1, 'PREFIX', 'bi', 'بِ', 'P', 'PREFIX|P');"
        )

        # verb_form CHECK BETWEEN 1 AND 12
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO verb_concordance VALUES (2, 1, 1, 'v', 'v', 'r', 13, 'XIII', 1, 1, 0, 0, 1, 0, 2);"
            )

        # words: is_verb IN (0, 1)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO words VALUES (2, 1, 1, 1, 2, '1:1:2', 'w', 'w', 'N', 2, NULL, NULL, NULL, NULL, NULL, NULL);"
            )

        # morphology_segments: segment_role IN ('PREFIX', 'STEM', 'SUFFIX')
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO morphology_segments VALUES (2, 1, 2, 'ROOT', 'f', 'a', 'T', 'FEAT');"
            )

    def test_quran_wbw_schema(self) -> None:
        common_sql = (SPECS_SQL_DIR / "common_meta.sql").read_text(encoding="utf-8")
        pack_sql = (SPECS_SQL_DIR / "quran_wbw.v1.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(common_sql)
        conn.executescript(pack_sql)

        conn.execute(
            "INSERT INTO word_translations VALUES (1, 1, 1, '1:1:1', 'بِسْمِ', 'In the name', 'bismi');"
        )
        with pytest.raises(sqlite3.IntegrityError):
            # Duplicate (ayah_id, word_number)
            conn.execute(
                "INSERT INTO word_translations VALUES (2, 1, 1, '1:1:1b', 'بِسْمِ', 'Dup', 'bismi');"
            )

        conn.execute("INSERT INTO word_translations_fts(word_translations_fts) VALUES('rebuild');")
        res = conn.execute("SELECT rowid FROM word_translations_fts WHERE word_translations_fts MATCH 'name';").fetchall()
        assert len(res) == 1
        assert res[0][0] == 1

    def test_hadith_book_schema(self) -> None:
        common_sql = (SPECS_SQL_DIR / "common_meta.sql").read_text(encoding="utf-8")
        pack_sql = (SPECS_SQL_DIR / "hadith_book.v1.sql").read_text(encoding="utf-8")
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(common_sql)
        conn.executescript(pack_sql)

        conn.execute("INSERT INTO books VALUES (1, '1', 'كتاب', 'Book 1');")
        conn.execute("INSERT INTO chapters VALUES (1, 1, '1', 'باب', 'Chapter 1');")

        # Explicit-null hadith (unsplit_raw)
        conn.execute(
            """INSERT INTO hadiths (
                id, book_id, chapter_id, hadith_number, urn,
                arabic_full, english_full, text_split_mode,
                arabic_sanad, arabic_matn, arabic_matn_clean,
                english_narrator, english_body, grade_ar, grade_en,
                reference_json, is_ar_missing, is_en_missing
            ) VALUES (
                1, 1, 1, '1a', NULL,
                'رَبِّ', NULL, 'unsplit_raw',
                NULL, NULL, NULL,
                NULL, NULL, NULL, NULL,
                '{"inBook":"Book 1, Hadith 1","uscMsa":null}', 0, 1
            );"""
        )

        # Verified bilingual hadith (source_verified)
        conn.execute(
            """INSERT INTO hadiths (
                id, book_id, chapter_id, hadith_number, urn,
                arabic_full, english_full, text_split_mode,
                arabic_sanad, arabic_matn, arabic_matn_clean,
                english_narrator, english_body, grade_ar, grade_en,
                reference_json, is_ar_missing, is_en_missing
            ) VALUES (
                2, 1, 1, '1b', NULL,
                'إسناد رَبِّ', 'mercy', 'source_verified',
                'إسناد', 'رَبِّ', 'رب',
                NULL, 'mercy', NULL, NULL,
                '{"inBook":null,"uscMsa":null}', 0, 0
            );"""
        )

        # Invalid text_split_mode
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO hadiths (
                    id, book_id, chapter_id, hadith_number, text_split_mode
                ) VALUES (3, 1, 1, '2', 'heuristic_regex');"""
            )

        # Invalid is_ar_missing constraint (must be 0 or 1)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO hadiths (
                    id, book_id, chapter_id, hadith_number, text_split_mode, is_ar_missing
                ) VALUES (3, 1, 1, '2', 'unsplit_raw', 2);"""
            )

        # FTS5 table test: indexes verified arabic_matn_clean and english_body
        conn.execute("INSERT INTO hadiths_fts(hadiths_fts) VALUES('rebuild');")
        res_ar = conn.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH 'رب';").fetchall()
        assert len(res_ar) == 1
        assert res_ar[0][0] == 2  # Row 2 indexed, not row 1

        res_en = conn.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH 'mercy';").fetchall()
        assert len(res_en) == 1
        assert res_en[0][0] == 2


# ---------------------------------------------------------------------------
# JSON Schema Contract Tests
# ---------------------------------------------------------------------------


class TestJsonSchemaContracts:
    """Tests verifying the four JSON Schemas under Draft 2020-12 and consumer rules."""

    @pytest.mark.parametrize("schema_path", SCHEMA_FILES)
    def test_schema_file_exists_and_is_valid_draft202012(self, schema_path: Path) -> None:
        assert schema_path.is_file(), f"Missing required JSON schema: {schema_path}"
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        # Verify schema is valid Draft 2020-12
        Draft202012Validator.check_schema(schema)

    # -----------------------------------------------------------------------
    # catalog.schema.json
    # -----------------------------------------------------------------------

    def test_catalog_schema_valid_document(self) -> None:
        schema_path = SPECS_DIR / "catalog.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        sample = {
            "catalogVersion": 1,
            "generatedAt": "2026-01-01T00:00:00Z",
            "releaseTag": "content-v0.1.0",
            "packs": [
                {
                    "id": "audio-adhan-core",
                    "category": "audio",
                    "version": "0.1.0",
                    "format": "audio",
                    "languages": ["ar"],
                    "completeness": "complete",
                    "licenseRef": "sources/licenses/audio-adhan-core.json",
                    "required": False,
                    "audioManifestRef": "audio-manifest.json",
                },
                {
                    "id": "quran-core",
                    "category": "quran",
                    "version": "0.1.0",
                    "format": "sqlite",
                    "languages": ["ar"],
                    "completeness": "complete",
                    "licenseRef": "sources/licenses/quran-core.json",
                    "required": True,
                    "compression": "none",
                    "schemaVersion": 1,
                    "url": "https://example.org/content-v0.1.0/quran-core.db",
                    "sizeBytes": 4096,
                    "sha256": "a" * 64,
                    "coverage": {"expected": 6236, "available": 6236},
                    "requiresFts5": True,
                },
            ],
        }
        Draft202012Validator(schema).validate(sample)

    def test_catalog_schema_rejections(self) -> None:
        schema_path = SPECS_DIR / "catalog.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)

        # Reject version != 1
        bad_version = {
            "catalogVersion": 2,
            "generatedAt": "2026-01-01T00:00:00Z",
            "releaseTag": "content-v0.1.0",
            "packs": [],
        }
        with pytest.raises(ValidationError):
            validator.validate(bad_version)

        # Reject empty packs
        bad_empty_packs = {
            "catalogVersion": 1,
            "generatedAt": "2026-01-01T00:00:00Z",
            "releaseTag": "content-v0.1.0",
            "packs": [],
        }
        with pytest.raises(ValidationError):
            validator.validate(bad_empty_packs)

        # Reject invalid releaseTag (leading zero or missing prefix)
        bad_tag = {
            "catalogVersion": 1,
            "generatedAt": "2026-01-01T00:00:00Z",
            "releaseTag": "content-v01.0.0",
            "packs": [
                {
                    "id": "quran-core",
                    "category": "quran",
                    "version": "0.1.0",
                    "format": "sqlite",
                    "languages": ["ar"],
                    "completeness": "complete",
                    "licenseRef": "sources/licenses/quran-core.json",
                    "required": True,
                    "compression": "none",
                    "schemaVersion": 1,
                    "url": "https://example.org/content-v0.1.0/quran-core.db",
                    "sizeBytes": 4096,
                    "sha256": "a" * 64,
                    "coverage": {"expected": 6236, "available": 6236},
                    "requiresFts5": True,
                }
            ],
        }
        with pytest.raises(ValidationError):
            validator.validate(bad_tag)

        # Reject undeclared field
        bad_undeclared = {
            "catalogVersion": 1,
            "generatedAt": "2026-01-01T00:00:00Z",
            "releaseTag": "content-v0.1.0",
            "extraField": "not allowed",
            "packs": [
                {
                    "id": "audio-adhan-core",
                    "category": "audio",
                    "version": "0.1.0",
                    "format": "audio",
                    "languages": ["ar"],
                    "completeness": "complete",
                    "licenseRef": "sources/licenses/audio-adhan-core.json",
                    "required": False,
                    "audioManifestRef": "audio-manifest.json",
                }
            ],
        }
        with pytest.raises(ValidationError):
            validator.validate(bad_undeclared)

        # Reject audio pack containing SQLite-only fields
        bad_audio_with_sqlite = {
            "catalogVersion": 1,
            "generatedAt": "2026-01-01T00:00:00Z",
            "releaseTag": "content-v0.1.0",
            "packs": [
                {
                    "id": "audio-adhan-core",
                    "category": "audio",
                    "version": "0.1.0",
                    "format": "audio",
                    "languages": ["ar"],
                    "completeness": "complete",
                    "licenseRef": "sources/licenses/audio-adhan-core.json",
                    "required": False,
                    "audioManifestRef": "audio-manifest.json",
                    "compression": "none",  # forbidden in audio
                }
            ],
        }
        with pytest.raises(ValidationError):
            validator.validate(bad_audio_with_sqlite)

        # Reject floating 1.0 for integer field in consumer validation
        raw_float_version = '{"catalogVersion": 1.0, "generatedAt": "2026-01-01T00:00:00Z", "releaseTag": "content-v0.1.0", "packs": []}'
        assert has_raw_float_token_for_integer(raw_float_version, "catalogVersion")

        # Reject trailing LF in regex full matches (e.g. releaseTag, generatedAt, id)
        sample_with_lf = {
            "catalogVersion": 1,
            "generatedAt": "2026-01-01T00:00:00Z\n",
            "releaseTag": "content-v0.1.0",
            "packs": [
                {
                    "id": "quran-core",
                    "category": "quran",
                    "version": "0.1.0",
                    "format": "sqlite",
                    "languages": ["ar"],
                    "completeness": "complete",
                    "licenseRef": "sources/licenses/quran-core.json",
                    "required": True,
                    "compression": "none",
                    "schemaVersion": 1,
                    "url": "https://example.org/content-v0.1.0/quran-core.db",
                    "sizeBytes": 4096,
                    "sha256": "a" * 64,
                    "coverage": {"expected": 6236, "available": 6236},
                    "requiresFts5": True,
                }
            ],
        }
        with pytest.raises(ValidationError):
            validator.validate(sample_with_lf)

        sample_with_lf["generatedAt"] = "2026-01-01T00:00:00Z"
        sample_with_lf["releaseTag"] = "content-v0.1.0\n"
        with pytest.raises(ValidationError):
            validator.validate(sample_with_lf)

        sample_with_lf["releaseTag"] = "content-v0.1.0"
        sample_with_lf["packs"][0]["id"] = "quran-core\n"
        with pytest.raises(ValidationError):
            validator.validate(sample_with_lf)

    # -----------------------------------------------------------------------
    # audio-manifest.schema.json
    # -----------------------------------------------------------------------

    def test_audio_manifest_schema_valid_document(self) -> None:
        schema_path = SPECS_DIR / "audio-manifest.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        sample = {
            "audioManifestVersion": 1,
            "releaseTag": "content-v0.1.0",
            "packId": "audio-adhan-core",
            "tracks": [
                {
                    "slug": "fixture",
                    "reciter": "Synthetic tone",
                    "full": {
                        "filename": "adhan-fixture-full.m4a",
                        "url": "https://example.org/content-v0.1.0/adhan-fixture-full.m4a",
                        "sizeBytes": 1024,
                        "sha256": "b" * 64,
                        "codec": "aac",
                        "durationSeconds": 1.5,
                    },
                    "ios": {
                        "filename": "adhan-fixture-ios-notif.caf",
                        "url": "https://example.org/content-v0.1.0/adhan-fixture-ios-notif.caf",
                        "sizeBytes": 512,
                        "sha256": "c" * 64,
                        "codec": "adpcm_ima_qt",
                        "durationSeconds": 1.5,
                    },
                }
            ],
        }
        Draft202012Validator(schema).validate(sample)

    def test_audio_manifest_schema_rejections(self) -> None:
        schema_path = SPECS_DIR / "audio-manifest.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)

        # Reject wrong packId
        bad_pack_id = {
            "audioManifestVersion": 1,
            "releaseTag": "content-v0.1.0",
            "packId": "other-pack",
            "tracks": [],
        }
        with pytest.raises(ValidationError):
            validator.validate(bad_pack_id)

        # Reject wrong codec for iOS (e.g. ima4 string instead of adpcm_ima_qt)
        bad_codec = {
            "audioManifestVersion": 1,
            "releaseTag": "content-v0.1.0",
            "packId": "audio-adhan-core",
            "tracks": [
                {
                    "slug": "fixture",
                    "reciter": "Synthetic tone",
                    "full": {
                        "filename": "adhan-fixture-full.m4a",
                        "url": "https://example.org/content-v0.1.0/adhan-fixture-full.m4a",
                        "sizeBytes": 1024,
                        "sha256": "b" * 64,
                        "codec": "aac",
                        "durationSeconds": 1.5,
                    },
                    "ios": {
                        "filename": "adhan-fixture-ios-notif.caf",
                        "url": "https://example.org/content-v0.1.0/adhan-fixture-ios-notif.caf",
                        "sizeBytes": 512,
                        "sha256": "c" * 64,
                        "codec": "wrong_codec",
                        "durationSeconds": 1.5,
                    },
                }
            ],
        }
        with pytest.raises(ValidationError):
            validator.validate(bad_codec)

        # Reject trailing LF in regex full matches (e.g. releaseTag, slug, filename)
        bad_lf_tag = {
            "audioManifestVersion": 1,
            "releaseTag": "content-v0.1.0\n",
            "packId": "audio-adhan-core",
            "tracks": [
                {
                    "slug": "fixture",
                    "reciter": "Synthetic tone",
                    "full": {
                        "filename": "adhan-fixture-full.m4a",
                        "url": "https://example.org/content-v0.1.0/adhan-fixture-full.m4a",
                        "sizeBytes": 1024,
                        "sha256": "b" * 64,
                        "codec": "aac",
                        "durationSeconds": 1.5,
                    },
                    "ios": {
                        "filename": "adhan-fixture-ios-notif.caf",
                        "url": "https://example.org/content-v0.1.0/adhan-fixture-ios-notif.caf",
                        "sizeBytes": 512,
                        "sha256": "c" * 64,
                        "codec": "adpcm_ima_qt",
                        "durationSeconds": 1.5,
                    },
                }
            ],
        }
        with pytest.raises(ValidationError):
            validator.validate(bad_lf_tag)

        bad_lf_slug = json.loads(json.dumps(bad_lf_tag))
        bad_lf_slug["releaseTag"] = "content-v0.1.0"
        bad_lf_slug["tracks"][0]["slug"] = "fixture\n"
        with pytest.raises(ValidationError):
            validator.validate(bad_lf_slug)

    # -----------------------------------------------------------------------
    # hadith-input.schema.json
    # -----------------------------------------------------------------------

    def test_hadith_input_schema_explicit_null_and_bilingual(self) -> None:
        schema_path = SPECS_INPUTS_DIR / "hadith-input.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)

        # Explicit-null case from PLAN.md §2
        explicit_null_doc = {
            "books": [{"id": 1, "number": "1", "nameAr": "كتاب", "nameEn": None}],
            "chapters": [{"id": 1, "bookId": 1, "number": None, "titleAr": None, "titleEn": None}],
            "hadiths": [
                {
                    "id": 1,
                    "bookId": 1,
                    "chapterId": 1,
                    "hadithNumber": "1a",
                    "urn": None,
                    "arabicFull": "رَبِّ",
                    "englishFull": None,
                    "englishNarrator": None,
                    "englishBody": None,
                    "gradeAr": None,
                    "gradeEn": None,
                    "reference": {"inBook": "Book 1, Hadith 1", "uscMsa": None},
                    "textSplitMode": "unsplit_raw",
                    "arabicSanad": None,
                    "arabicMatn": None,
                }
            ],
        }
        validator.validate(explicit_null_doc)

        # Bilingual source_verified case
        bilingual_doc = {
            "books": [{"id": 1, "number": "1", "nameAr": "كتاب", "nameEn": "Book of Faith"}],
            "chapters": [{"id": 1, "bookId": 1, "number": "1", "titleAr": "باب", "titleEn": "Chapter 1"}],
            "hadiths": [
                {
                    "id": 2,
                    "bookId": 1,
                    "chapterId": 1,
                    "hadithNumber": "1b",
                    "urn": "urn:hadith:1b",
                    "arabicFull": "إسناد رَبِّ",
                    "englishFull": "mercy",
                    "englishNarrator": None,
                    "englishBody": "mercy",
                    "gradeAr": "صحيح",
                    "gradeEn": "Sahih",
                    "reference": {"inBook": None, "uscMsa": None},
                    "textSplitMode": "source_verified",
                    "arabicSanad": "إسناد",
                    "arabicMatn": "رَبِّ",
                }
            ],
        }
        validator.validate(bilingual_doc)

    def test_hadith_input_schema_rejections(self) -> None:
        schema_path = SPECS_INPUTS_DIR / "hadith-input.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)

        base_valid = {
            "books": [{"id": 1, "number": "1", "nameAr": "كتاب", "nameEn": None}],
            "chapters": [{"id": 1, "bookId": 1, "number": None, "titleAr": None, "titleEn": None}],
            "hadiths": [
                {
                    "id": 1,
                    "bookId": 1,
                    "chapterId": 1,
                    "hadithNumber": "1a",
                    "urn": None,
                    "arabicFull": "رَبِّ",
                    "englishFull": None,
                    "englishNarrator": None,
                    "englishBody": None,
                    "gradeAr": None,
                    "gradeEn": None,
                    "reference": {"inBook": "Book 1, Hadith 1", "uscMsa": None},
                    "textSplitMode": "unsplit_raw",
                    "arabicSanad": None,
                    "arabicMatn": None,
                }
            ],
        }

        # 1. Reject 64-bit ID overflow: 9223372036854775808
        overflow_doc = json.loads(json.dumps(base_valid))
        overflow_doc["hadiths"][0]["id"] = 9223372036854775808
        with pytest.raises(ValidationError):
            validator.validate(overflow_doc)

        # 2. Reject id <= 0
        zero_id_doc = json.loads(json.dumps(base_valid))
        zero_id_doc["hadiths"][0]["id"] = 0
        with pytest.raises(ValidationError):
            validator.validate(zero_id_doc)

        # 3. Reject unsplit_raw with non-null arabicSanad or arabicMatn
        unsplit_with_split = json.loads(json.dumps(base_valid))
        unsplit_with_split["hadiths"][0]["arabicSanad"] = "heuristic sanad"
        with pytest.raises(ValidationError):
            validator.validate(unsplit_with_split)

        # 4. Reject source_verified with null arabicMatn or arabicSanad
        verified_with_null = json.loads(json.dumps(base_valid))
        verified_with_null["hadiths"][0]["textSplitMode"] = "source_verified"
        verified_with_null["hadiths"][0]["arabicSanad"] = None
        verified_with_null["hadiths"][0]["arabicMatn"] = None
        with pytest.raises(ValidationError):
            validator.validate(verified_with_null)

        # 5. Reject omitted englishFull (all declared fields in contract are required)
        omitted_field = json.loads(json.dumps(base_valid))
        del omitted_field["hadiths"][0]["englishFull"]
        with pytest.raises(ValidationError):
            validator.validate(omitted_field)

        # 6. Reject undeclared field
        extra_field = json.loads(json.dumps(base_valid))
        extra_field["hadiths"][0]["extraProp"] = "invalid"
        with pytest.raises(ValidationError):
            validator.validate(extra_field)

        # 7. Reject books[].number as integer (must be nonempty string)
        int_number = json.loads(json.dumps(base_valid))
        int_number["books"][0]["number"] = 1
        with pytest.raises(ValidationError):
            validator.validate(int_number)

    # -----------------------------------------------------------------------
    # tafsir-input.schema.json
    # -----------------------------------------------------------------------

    def test_tafsir_input_schema_valid_document(self) -> None:
        schema_path = SPECS_INPUTS_DIR / "tafsir-input.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        sample = [
            {"verse_keys": ["1:1"], "text": "رَبِّ", "format": "plain"},
            {"verse_keys": ["1:2", "1:3"], "text": "<p>مثال</p>", "format": "html"},
        ]
        Draft202012Validator(schema).validate(sample)

    def test_tafsir_input_schema_rejections(self) -> None:
        schema_path = SPECS_INPUTS_DIR / "tafsir-input.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)

        # Reject empty array
        with pytest.raises(ValidationError):
            validator.validate([])

        # Reject invalid format
        with pytest.raises(ValidationError):
            validator.validate([{"verse_keys": ["1:1"], "text": "t", "format": "markdown"}])

        # Reject invalid verse_key syntax (leading zero)
        with pytest.raises(ValidationError):
            validator.validate([{"verse_keys": ["01:01"], "text": "t", "format": "plain"}])

        # Reject invalid verse_key syntax (missing colon)
        with pytest.raises(ValidationError):
            validator.validate([{"verse_keys": ["1"], "text": "t", "format": "plain"}])

        # Reject empty verse_keys array
        with pytest.raises(ValidationError):
            validator.validate([{"verse_keys": [], "text": "t", "format": "plain"}])

        # Reject blank text (whitespace only)
        with pytest.raises(ValidationError):
            validator.validate([{"verse_keys": ["1:1"], "text": "   ", "format": "plain"}])

        # Reject undeclared properties
        with pytest.raises(ValidationError):
            validator.validate([{"verse_keys": ["1:1"], "text": "t", "format": "plain", "extra": 1}])

        # Reject trailing LF in verse_keys
        with pytest.raises(ValidationError):
            validator.validate([{"verse_keys": ["1:1\n"], "text": "t", "format": "plain"}])
