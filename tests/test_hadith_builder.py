"""Unit and integration tests for the Hadith SQLite pack builder and search surfaces."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
import pytest

from pipelines.common.lock_inputs import author_lock
from pipelines.builders.build_hadith_book import build_hadith_book, main, ALLOWED_BOOKS

@pytest.fixture(autouse=True)
def _mock_verify_runtime_if_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from pipelines.toolchain.check_runtime import verify_runtime
        verify_runtime()
    except Exception:
        monkeypatch.setattr(
            "pipelines.builders.build_hadith_book.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )


def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def _make_canonical_hadith_data(
    *,
    book_id: int = 1,
    book_name_ar: str = "صحيح البخاري",
    book_name_en: str = "Sahih al-Bukhari",
    hadiths: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if hadiths is None:
        hadiths = [
            {
                "id": 1,
                "bookId": book_id,
                "chapterId": 1,
                "hadithNumber": "1",
                "urn": None,
                "arabicFull": "حَدَّثَنَا الْحُمَيْدِيُّ قَالَ حَدَّثَنَا سُفْيَانُ",
                "englishFull": "I heard Allah's Messenger saying...",
                "englishNarrator": "Narrated 'Umar bin Al-Khattab:",
                "englishBody": "I heard Allah's Messenger saying...",
                "gradeAr": None,
                "gradeEn": None,
                "reference": {"inBook": "Book 1, Hadith 1", "uscMsa": None},
                "textSplitMode": "unsplit_raw",
                "arabicSanad": None,
                "arabicMatn": None,
            },
            {
                "id": 2,
                "bookId": book_id,
                "chapterId": 1,
                "hadithNumber": "2",
                "urn": None,
                "arabicFull": "إسناد رَبِّ ارْحَمْ",
                "englishFull": "Lord have mercy",
                "englishNarrator": None,
                "englishBody": "Lord have mercy",
                "gradeAr": None,
                "gradeEn": None,
                "reference": {"inBook": None, "uscMsa": None},
                "textSplitMode": "source_verified",
                "arabicSanad": "إسناد",
                "arabicMatn": "رَبِّ ارْحَمْ",
            },
            {
                "id": 3,
                "bookId": book_id,
                "chapterId": 1,
                "hadithNumber": "3",
                "urn": None,
                "arabicFull": "حَدَّثَنَا أَبُو نُعَيْمٍ",
                "englishFull": None,
                "englishNarrator": "Narrated Aisha:",
                "englishBody": None,
                "gradeAr": None,
                "gradeEn": None,
                "reference": {"inBook": None, "uscMsa": None},
                "textSplitMode": "unsplit_raw",
                "arabicSanad": None,
                "arabicMatn": None,
            },
        ]

    return {
        "books": [
            {
                "id": book_id,
                "number": str(book_id),
                "nameAr": book_name_ar,
                "nameEn": book_name_en,
            }
        ],
        "chapters": [
            {
                "id": 1,
                "bookId": book_id,
                "number": "1",
                "titleAr": "بدء الوحي",
                "titleEn": "Revelation",
            }
        ],
        "hadiths": hadiths,
    }


def _setup_hadith_pack_env(
    root: Path,
    slug: str = "bukhari",
    *,
    data: dict[str, Any] | None = None,
    splits_data: dict[str, Any] | None = None,
    license_status: str = "pending",
) -> tuple[Path, Path]:
    raw_dir = root / "raw-inputs" / "hadith"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = root / "sources"
    licenses_dir = sources_dir / "licenses"
    licenses_dir.mkdir(parents=True, exist_ok=True)
    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    if data is None:
        data = _make_canonical_hadith_data()

    has_verified = any(h["textSplitMode"] == "source_verified" for h in data["hadiths"])
    if has_verified and splits_data is None:
        splits_data = {
            str(h["id"]): {
                "sourceRef": f"Synthetic evidence {h['id']}",
                "arabicSanad": h["arabicSanad"],
                "arabicMatn": h["arabicMatn"],
            }
            for h in data["hadiths"]
            if h["textSplitMode"] == "source_verified"
        }

    book_file = raw_dir / f"{slug}.json"
    book_file.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    registry_data: dict[str, Any] = {
        f"hadith/{slug}.json": {
            "packIds": [f"hadith-{slug}"],
            "sourceUrl": f"https://example.org/hadith/{slug}.json",
            "sourceVersion": "1.0",
        }
    }

    if splits_data is not None:
        splits_file = raw_dir / f"{slug}.splits.json"
        splits_file.write_text(
            json.dumps(splits_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        registry_data[f"hadith/{slug}.splits.json"] = {
            "packIds": [f"hadith-{slug}"],
            "sourceUrl": f"https://example.org/hadith/{slug}.splits.json",
            "sourceVersion": "1.0",
        }

    reg_file = sources_dir / "registry.json"
    reg_file.write_text(
        json.dumps(registry_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    lock_file = sources_dir / "manifest.lock.json"
    author_lock(reg_file, root / "raw-inputs", lock_file)

    lic_sources = [
        {
            "attribution": "Synthetic Hadith Source",
            "name": f"Synthetic Hadith {slug}",
            "notice": "synthetic notice",
            "path": f"hadith/{slug}.json",
            "permittedUses": [] if license_status == "pending" else ["sqlite_conversion", "search_projection"],
            "rightsEvidence": ["https://example.org/rights"],
            "sourceUrl": f"https://example.org/hadith/{slug}.json",
            "sourceVersion": "1.0",
        }
    ]
    if splits_data is not None:
        lic_sources.append(
            {
                "attribution": "Synthetic Splits Source",
                "name": f"Synthetic Splits {slug}",
                "notice": "synthetic splits notice",
                "path": f"hadith/{slug}.splits.json",
                "permittedUses": [] if license_status == "pending" else ["sqlite_conversion", "search_projection"],
                "rightsEvidence": ["https://example.org/rights"],
                "sourceUrl": f"https://example.org/hadith/{slug}.splits.json",
                "sourceVersion": "1.0",
            }
        )

    lic_data = {
        "licenseSpdx": "CC0-1.0",
        "packId": f"hadith-{slug}",
        "primaryPath": f"hadith/{slug}.json",
        "reviewedBy": None if license_status == "pending" else "reviewer@example.com",
        "status": license_status,
        "sources": lic_sources,
    }

    lic_file = licenses_dir / f"hadith-{slug}.json"
    lic_file.write_text(
        json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return root, out_dir


class TestHadithBuilder:
    def test_synthetic_pack_building_and_structure(self, tmp_path: Path) -> None:
        root, out_dir = _setup_hadith_pack_env(tmp_path, "bukhari")
        build_hadith_book(book="bukhari", sources_root=root, output_dir=out_dir)

        db_path = out_dir / "hadith-bukhari.db"
        assert db_path.is_file()

        conn = _open_ro_db(db_path)
        try:
            # Metadata checks
            meta_rows = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
            assert meta_rows["pack_id"] == "hadith-bukhari"
            assert meta_rows["pack_version"] == "0.2.0"
            assert meta_rows["schema_version"] == "1"
            assert meta_rows["fts5_enabled"] == "true"
            assert meta_rows["matn_split_verified"] == "true"
            assert meta_rows["completeness"] == "partial"  # Hadith 3 has null englishFull

            cov_rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute("SELECT metric, count, notes FROM coverage_report;").fetchall()
            }
            assert cov_rows["expected_total"] == (3, None)
            assert cov_rows["ar_present"] == (3, None)
            assert cov_rows["en_present"] == (2, None)
            assert cov_rows["en_missing"] == (1, None)

            # Table rows checks
            books = conn.execute("SELECT id, number, name_ar, name_en FROM books;").fetchall()
            assert len(books) == 1
            assert books[0] == (1, "1", "صحيح البخاري", "Sahih al-Bukhari")

            chapters = conn.execute(
                "SELECT id, book_id, number, title_ar, title_en FROM chapters;"
            ).fetchall()
            assert len(chapters) == 1
            assert chapters[0] == (1, 1, "1", "بدء الوحي", "Revelation")

            hadiths = conn.execute(
                """SELECT id, book_id, chapter_id, hadith_number, urn,
                          text_split_mode, is_ar_missing, is_en_missing,
                          arabic_matn_clean, arabic_full_clean
                   FROM hadiths ORDER BY id;"""
            ).fetchall()
            assert len(hadiths) == 3

            # Row 1: unsplit
            assert hadiths[0][0] == 1
            assert hadiths[0][5] == "unsplit_raw"
            assert hadiths[0][6] == 0  # is_ar_missing
            assert hadiths[0][7] == 0  # is_en_missing
            assert hadiths[0][8] is None  # arabic_matn_clean
            assert hadiths[0][9] is not None  # arabic_full_clean

            # Row 2: verified
            assert hadiths[1][0] == 2
            assert hadiths[1][5] == "source_verified"
            assert hadiths[1][6] == 0  # is_ar_missing
            assert hadiths[1][7] == 0  # is_en_missing
            assert hadiths[1][8] is not None  # arabic_matn_clean
            assert hadiths[1][9] is None  # arabic_full_clean

            # Row 3: unsplit, missing english
            assert hadiths[2][0] == 3
            assert hadiths[2][5] == "unsplit_raw"
            assert hadiths[2][6] == 0  # is_ar_missing
            assert hadiths[2][7] == 1  # is_en_missing
        finally:
            conn.close()

    def test_determinism_input_ordering(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Feed hadiths in forward vs reverse order in memory; binary DB output must be identical."""
        data = _make_canonical_hadith_data()
        root1 = tmp_path / "run1"
        root2 = tmp_path / "run2"

        _setup_hadith_pack_env(root1, "muslim", data=data)
        _setup_hadith_pack_env(root2, "muslim", data=data)

        out1 = root1 / "dist"
        out2 = root2 / "dist"

        build_hadith_book(book="muslim", sources_root=root1, output_dir=out1)

        import pipelines.builders.build_hadith_book as b_mod
        orig_load = b_mod.load_hadith_input

        def _reversed_load(*args: Any, **kwargs: Any) -> dict[str, Any]:
            loaded = orig_load(*args, **kwargs)
            loaded["hadiths"] = list(reversed(loaded["hadiths"]))
            return loaded

        monkeypatch.setattr(b_mod, "load_hadith_input", _reversed_load)
        build_hadith_book(book="muslim", sources_root=root2, output_dir=out2)

        db1_bytes = (out1 / "hadith-muslim.db").read_bytes()
        db2_bytes = (out2 / "hadith-muslim.db").read_bytes()

        assert hashlib.sha256(db1_bytes).hexdigest() == hashlib.sha256(db2_bytes).hexdigest()
    def test_search_surface_isolation(self, tmp_path: Path) -> None:
        """Verify dual FTS search surface isolation rules and forgiving normalization."""
        from pipelines.common.search_text import arabic_match_literal

        source_phrase = "أَحْـمَدۚ عَلَىٰ صَلَاةٌ رَبِّ"
        data = _make_canonical_hadith_data()
        # Row 1: unsplit raw, set arabicFull to source_phrase
        data["hadiths"][0]["arabicFull"] = source_phrase
        # Row 2: source_verified, prefix arabicFull with existing Sanad and set arabicMatn to source_phrase
        sanad_text = data["hadiths"][1]["arabicSanad"]
        data["hadiths"][1]["arabicFull"] = sanad_text + " " + source_phrase
        data["hadiths"][1]["arabicMatn"] = source_phrase

        splits_data = {
            "2": {
                "sourceRef": "Synthetic verified split 2",
                "arabicSanad": sanad_text,
                "arabicMatn": source_phrase,
            }
        }
        root, out_dir = _setup_hadith_pack_env(tmp_path, "bukhari", data=data, splits_data=splits_data)
        build_hadith_book(book="bukhari", sources_root=root, output_dir=out_dir)

        conn = _open_ro_db(out_dir / "hadith-bukhari.db")
        try:
            cur = conn.cursor()
            # Verify raw source data bytes preserved
            r1 = cur.execute(
                "SELECT CAST(arabic_full AS BLOB), CAST(english_full AS BLOB), CAST(english_body AS BLOB) FROM hadiths WHERE id = 1;"
            ).fetchone()
            assert r1[0] == source_phrase.encode("utf-8")
            assert r1[1] == b"I heard Allah's Messenger saying..."
            assert r1[2] == b"I heard Allah's Messenger saying..."

            r2 = cur.execute(
                "SELECT CAST(arabic_full AS BLOB), CAST(arabic_sanad AS BLOB), CAST(arabic_matn AS BLOB), CAST(english_full AS BLOB), CAST(english_body AS BLOB) FROM hadiths WHERE id = 2;"
            ).fetchone()
            assert r2[0] == (sanad_text + " " + source_phrase).encode("utf-8")
            assert r2[1] == sanad_text.encode("utf-8")
            assert r2[2] == source_phrase.encode("utf-8")
            assert r2[3] == b"Lord have mercy"
            assert r2[4] == b"Lord have mercy"

            # Check tokens in fts5vocab for hadiths_full_fts (Row 1) with explicit column
            cur.execute("CREATE VIRTUAL TABLE temp.vocab_full USING fts5vocab(main, 'hadiths_full_fts', 'instance');")
            for token in ["احمد", "علي", "صلاه", "رب"]:
                cnt = cur.execute(
                    "SELECT count(*) FROM temp.vocab_full WHERE term = ? AND doc = 1 AND col = 'arabic_full_clean';",
                    (token,),
                ).fetchone()[0]
                assert cnt >= 1, f"Token {token} missing from hadiths_full_fts vocab at doc 1"
            cur.execute("DROP TABLE temp.vocab_full;")

            # Check tokens in fts5vocab for hadiths_fts (Row 2, column arabic_matn_clean)
            cur.execute("CREATE VIRTUAL TABLE temp.vocab_matn USING fts5vocab(main, 'hadiths_fts', 'instance');")
            for token in ["احمد", "علي", "صلاه", "رب"]:
                cnt = cur.execute(
                    "SELECT count(*) FROM temp.vocab_matn WHERE term = ? AND doc = 2 AND col = 'arabic_matn_clean';",
                    (token,),
                ).fetchone()[0]
                assert cnt >= 1, f"Token {token} missing from hadiths_fts vocab at doc 2"
            cur.execute("DROP TABLE temp.vocab_matn;")

            # Query row 1 in hadiths_full_fts ONLY
            assert cur.execute("SELECT rowid FROM hadiths_full_fts WHERE hadiths_full_fts MATCH ?;", ('"احمد علي صلاه رب"',)).fetchall() == [(1,)]
            q_lit = arabic_match_literal(source_phrase)
            assert q_lit is not None
            assert cur.execute("SELECT rowid FROM hadiths_full_fts WHERE hadiths_full_fts MATCH ?;", (q_lit,)).fetchall() == [(1,)]
            assert cur.execute("SELECT rowid FROM hadiths_full_fts WHERE hadiths_full_fts MATCH ? AND rowid = 2;", (q_lit,)).fetchall() == []

            # Query row 2 in hadiths_fts ONLY
            assert cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ?;", ('"احمد علي صلاه رب"',)).fetchall() == [(2,)]
            assert cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ?;", (q_lit,)).fetchall() == [(2,)]
            assert cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ? AND rowid = 1;", (q_lit,)).fetchall() == []

            # Unsplit row English narrator vs body (Row 1):
            # Phrase in body ("saying") is searchable in hadiths_fts
            assert cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ?;", ('"saying"',)).fetchall() == [(1,)]
            # Phrase only in narrator ("Khattab") is NOT searchable in hadiths_fts
            assert cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ?;", ('"Khattab"',)).fetchall() == []

            # Negative control
            assert cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ?;", ('"nonexistentxyz"',)).fetchall() == []
            assert cur.execute("SELECT rowid FROM hadiths_full_fts WHERE hadiths_full_fts MATCH ?;", ('"nonexistentxyz"',)).fetchall() == []
        finally:
            conn.close()

    def test_all_bilingual_completeness(self, tmp_path: Path) -> None:
        """When all hadiths have non-null arabicFull and englishFull, completeness is complete."""
        hadiths = [
            {
                "id": 1,
                "bookId": 1,
                "chapterId": 1,
                "hadithNumber": "1",
                "urn": None,
                "arabicFull": "نص عربي كامل",
                "englishFull": "Full English text",
                "englishNarrator": None,
                "englishBody": "Full English text",
                "gradeAr": None,
                "gradeEn": None,
                "reference": {"inBook": None, "uscMsa": None},
                "textSplitMode": "unsplit_raw",
                "arabicSanad": None,
                "arabicMatn": None,
            }
        ]
        data = _make_canonical_hadith_data(hadiths=hadiths)
        root, out_dir = _setup_hadith_pack_env(tmp_path, "nasai", data=data)
        build_hadith_book(book="nasai", sources_root=root, output_dir=out_dir)

        conn = _open_ro_db(out_dir / "hadith-nasai.db")
        try:
            meta = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
            assert meta["completeness"] == "complete"
            assert meta["matn_split_verified"] == "false"
        finally:
            conn.close()

    def test_output_collision_refusal(self, tmp_path: Path) -> None:
        root, out_dir = _setup_hadith_pack_env(tmp_path, "bukhari")
        db_path = out_dir / "hadith-bukhari.db"
        db_path.write_bytes(b"existing content")

        with pytest.raises(FileExistsError, match="already exists"):
            build_hadith_book(book="bukhari", sources_root=root, output_dir=out_dir)

    def test_failure_cleanup_removes_partial_db_and_sidecars(self, tmp_path: Path) -> None:
        root, out_dir = _setup_hadith_pack_env(tmp_path, "bukhari")
        # Corrupt the JSON file so loading fails after opening DB or during input load
        book_file = root / "raw-inputs" / "hadith" / "bukhari.json"
        # Overwrite with invalid json
        book_file.write_text("{bad json", encoding="utf-8")

        with pytest.raises(Exception):
            build_hadith_book(book="bukhari", sources_root=root, output_dir=out_dir)

        db_path = out_dir / "hadith-bukhari.db"
        assert not db_path.exists()
        assert not (out_dir / "hadith-bukhari.db-wal").exists()
        assert not (out_dir / "hadith-bukhari.db-shm").exists()
        assert not (out_dir / "hadith-bukhari.db-journal").exists()

    def test_lock_validation_failure(self, tmp_path: Path) -> None:
        root, out_dir = _setup_hadith_pack_env(tmp_path, "bukhari")
        # Modify the raw input so sha256 mismatch occurs in verify_lock
        book_file = root / "raw-inputs" / "hadith" / "bukhari.json"
        content = book_file.read_text(encoding="utf-8")
        book_file.write_text(content + " ", encoding="utf-8")

        with pytest.raises(ValueError, match="mismatch|Lock verification"):
            build_hadith_book(book="bukhari", sources_root=root, output_dir=out_dir)

    def test_cli_execution_and_slug_validation(self, tmp_path: Path) -> None:
        root, out_dir = _setup_hadith_pack_env(tmp_path, "bukhari")

        # Valid slug
        ret = main(["--book", "bukhari", "--sources-root", str(root), "--output-dir", str(out_dir)])
        assert ret == 0
        assert (out_dir / "hadith-bukhari.db").is_file()

        # Invalid slug
        ret_invalid = main(["--book", "invalid_book", "--sources-root", str(root), "--output-dir", str(out_dir)])
        assert ret_invalid != 0
