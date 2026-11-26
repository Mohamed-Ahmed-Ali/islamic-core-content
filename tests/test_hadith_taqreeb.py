"""Unit and integration tests for Mashrūʿ Taqrīb as-Sunnah schema, search, and builder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
import pytest

from pipelines.common.lock_inputs import (
    LockEntry,
    canonical_lock_json,
    parse_strict_json,
    parse_and_validate_lock,
)
from pipelines.common.pack_sources import load_sqlite_source_metadata
from pipelines.common.stage_taqreeb_inputs import TAQREEB_SOURCES
from pipelines.common.taqreeb_search import (
    normalize_taqreeb_arabic,
    taqreeb_match_literal,
)
from pipelines.builders.hadith_taqreeb_builder import (
    build_hadith_taqreeb,
    main,
)

@pytest.fixture(autouse=True)
def _mock_verify_runtime_if_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from pipelines.toolchain.check_runtime import verify_runtime
        verify_runtime()
    except Exception:
        monkeypatch.setattr(
            "pipelines.builders.hadith_taqreeb_builder.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )



def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def _make_synthetic_taqreeb_json() -> dict[str, Any]:
    return {
        "book_id": 1,
        "title_ar": "جامع الأصول التسعة",
        "title_en": "Jami al-Usul al-Tisah",
        "description_ar": "وصف جامع الأصول",
        "total_maqsads": 1,
        "total_hadiths": 5,
        "maqsads": [
            {
                "maqsad_id": 1,
                "maqsad_index": 1,
                "name_ar": "المقصد الأول: العقيدة",
                "name_en": "First Maqsad: Creed",
                "kutub": [
                    {
                        "ketab_id": 1,
                        "ketab_index": 1,
                        "name_ar": "كتاب الإيمان",
                        "name_en": "Book of Faith",
                        "chapters": [
                            {
                                "chapter_id": 1,
                                "chapter_index": 1,
                                "name_ar": "باب فارغ",
                                "name_en": "Empty Chapter",
                                "hadiths": [],
                            },
                            {
                                "chapter_id": 2,
                                "chapter_index": 2,
                                "name_ar": "باب الأعمال والنيات",
                                "name_en": "Chapter of Deeds and Intentions",
                                "hadiths": [
                                    {
                                        "id": 1,
                                        "hadith_no": 1,
                                        "text_ar": "إِنَّ الأَعْمَالَ عَلَىٰ نِيَّةٍ {[1]} {[1]}",
                                        "text_ar_simple": "إن الأعمال على نية",
                                        "takhreej": "أخرجه البخاري ومسلم.",
                                        "hukm": "صحيح",
                                        "page_no": 10,
                                        "footnotes": [
                                            {"number": 1, "text": "حاشية عن النية ومحلها القلب"}
                                        ],
                                        "atraf": ["طرف الحديث الأول"],
                                    },
                                    {
                                        "id": 2,
                                        "hadith_no": 1,
                                        "text_ar": "إنما الأعمال بالنيات",
                                        "text_ar_simple": "إنما الأعمال بالنيات",
                                        "takhreej": None,
                                        "hukm": "صحيح",
                                        "page_no": 11,
                                        "footnotes": [],
                                        "atraf": [],
                                    },
                                    {
                                        "id": 3,
                                        "hadith_no": 2,
                                        "text_ar": None,
                                        "text_ar_simple": "",
                                        "takhreej": "وأخرجه البخاري برقم (1).",
                                        "hukm": None,
                                        "page_no": None,
                                        "footnotes": [],
                                        "atraf": [],
                                    },
                                    {
                                        "id": 4,
                                        "hadith_no": 3,
                                        "text_ar": "",
                                        "text_ar_simple": "",
                                        "takhreej": None,
                                        "hukm": None,
                                        "page_no": 12,
                                        "footnotes": [],
                                        "atraf": ["[انظر الحديث: 7734]."],
                                    },
                                    {
                                        "id": 5,
                                        "hadith_no": 4,
                                        "text_ar": "لا هجرة بعد الفتح ولكن جهاد ونية",
                                        "text_ar_simple": "لا هجرة بعد الفتح ولكن جهاد ونية",
                                        "takhreej": None,
                                        "hukm": "صحيح",
                                        "page_no": 13,
                                        "footnotes": [
                                            {
                                                "number": 0,
                                                "text": " English footnote for translation of first hadith",
                                            }
                                        ],
                                        "atraf": [],
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }
        ],
    }


def _setup_synthetic_taqreeb_env(
    tmp_path: Path,
    pack_id: str = "hadith-taqreeb-jami",
    doc: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    sources_root = tmp_path / "sources_root"
    output_dir = tmp_path / "output_dir"
    sources_root.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    if doc is None:
        doc = _make_synthetic_taqreeb_json()

    raw_bytes = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    book_id, basename = TAQREEB_SOURCES[pack_id]
    rel_path = f"hadith-taqreeb/{basename}"

    raw_file = sources_root / "raw-inputs" / rel_path
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_bytes(raw_bytes)

    source_url = "https://sunnahenc.com/about"
    source_ver = f"android-apk-3.0.4-realm-export+sha256:{hashlib.sha256(raw_bytes).hexdigest().lower()}"

    # Registry
    reg_dir = sources_root / "sources"
    reg_dir.mkdir(parents=True, exist_ok=True)
    registry_data = {
        rel_path: {
            "packIds": [pack_id],
            "sourceUrl": source_url,
            "sourceVersion": source_ver,
        }
    }
    (reg_dir / "registry.json").write_text(json.dumps(registry_data, indent=2), encoding="utf-8")

    # Lock
    lock_entry = LockEntry(
        path=rel_path,
        source_url=source_url,
        source_version=source_ver,
        size_bytes=len(raw_bytes),
        sha256=hashlib.sha256(raw_bytes).hexdigest().lower(),
    )
    (reg_dir / "manifest.lock.json").write_bytes(canonical_lock_json([lock_entry]))

    # License (pending)
    lic_dir = reg_dir / "licenses"
    lic_dir.mkdir(parents=True, exist_ok=True)
    lic_data = {
        "packId": pack_id,
        "licenseSpdx": "LicenseRef-Taqreeb-Al-Sunnah-Unreviewed",
        "primaryPath": rel_path,
        "status": "pending",
        "reviewedBy": None,
        "sources": [
            {
                "name": f"Taqreeb {pack_id} - Test",
                "path": rel_path,
                "sourceUrl": source_url,
                "sourceVersion": source_ver,
                "attribution": "Saleh Ahmad ash-Shami",
                "notice": "Local export pinned by SHA-256.",
                "rightsEvidence": [source_url],
                "permittedUses": [],
            }
        ],
    }
    (lic_dir / f"{pack_id}.json").write_text(json.dumps(lic_data, indent=2), encoding="utf-8")

    return sources_root, output_dir


def test_taqreeb_search_normalization_and_match_literal() -> None:
    # Basic normalization with foldings: أ إ آ ٱ -> ا and ى ی -> ي
    raw = "إِنَّ الأَعْمَالَ عَلَىٰ نِيَّةٍ"
    norm = normalize_taqreeb_arabic(raw)
    assert norm == "ان الاعمال علي نية"

    # Persian Yeh folding
    assert normalize_taqreeb_arabic("ی") == "ي"
    # Alef wasla
    assert normalize_taqreeb_arabic("ٱ") == "ا"
    # Non-folded characters: ئ, ء, ؤ, ة
    assert normalize_taqreeb_arabic("شَيْءٌ وَمَسْؤُولٌ وَبِرْئٌ وَرَحْمَةٌ") == "شيء ومسؤول وبرئ ورحمة"

    # Literal matching:
    # Diacritics only returns None
    assert taqreeb_match_literal("ًٌٍَُِّْٰ") is None
    assert taqreeb_match_literal("   ") is None
    assert taqreeb_match_literal("") is None

    # Quoting and escaping
    matched = taqreeb_match_literal('الأعمال "علي"')
    assert matched == '"الاعمال ""علي"""'

    # Single word query
    assert taqreeb_match_literal("الأعمال عَلَىٰ") == '"الاعمال علي"'
    assert taqreeb_match_literal("الاعمال علي") == '"الاعمال علي"'


def test_build_hadith_taqreeb_deterministic_and_query(tmp_path: Path) -> None:
    sources_root, output_dir = _setup_synthetic_taqreeb_env(tmp_path)
    pack_id = "hadith-taqreeb-jami"

    build_hadith_taqreeb(pack_id, sources_root, output_dir)
    db_path = output_dir / f"{pack_id}.db"
    assert db_path.is_file()

    conn = _open_ro_db(db_path)
    try:
        # Check books table
        book = conn.execute("SELECT book_id, title_ar, title_en, total_maqsads, total_hadiths FROM books;").fetchone()
        assert book == (1, "جامع الأصول التسعة", "Jami al-Usul al-Tisah", 1, 5)

        # Check empty chapter preservation
        empty_ch = conn.execute("SELECT chapter_id, chapter_index, name_ar FROM chapters WHERE chapter_id = 1;").fetchone()
        assert empty_ch == (1, 1, "باب فارغ")

        # Check hadiths rows
        rows = conn.execute(
            "SELECT id, chapter_id, hadith_no, text_ar, text_ar_simple, takhreej, hukm, page_no, is_cross_ref, text_split_mode, text_ar_search FROM hadiths ORDER BY id;"
        ).fetchall()
        assert len(rows) == 5

        # Row 1: exact display and original text_ar_simple
        r1 = rows[0]
        assert r1[0] == 1
        assert r1[2] == 1  # hadith_no
        assert r1[3] == "إِنَّ الأَعْمَالَ عَلَىٰ نِيَّةٍ {[1]} {[1]}"
        assert r1[4] == "إن الأعمال على نية"
        assert r1[8] == 0  # is_cross_ref
        assert r1[9] == "unsplit_raw"
        assert r1[10] == "ان الاعمال علي نية {[1]} {[1]}"

        # Row 2: shares hadith_no 1
        r2 = rows[1]
        assert r2[0] == 2
        assert r2[2] == 1  # hadith_no repeats

        # Row 3: cross ref row (text_ar is NULL, takhreej nonblank, is_cross_ref=1)
        r3 = rows[2]
        assert r3[0] == 3
        assert r3[3] is None
        assert r3[8] == 1
        assert r3[10] == ""

        # Row 4: empty text row with reference in atraf (text_ar is "", is_cross_ref=0)
        r4 = rows[3]
        assert r4[0] == 4
        assert r4[3] == ""
        assert r4[8] == 0
        assert r4[10] == ""
        atraf_row = conn.execute("SELECT position, text FROM hadith_atraf WHERE hadith_id = 4;").fetchone()
        assert atraf_row == (1, "[انظر الحديث: 7734].")

        # Row 5: footnote numbered 0
        fn0 = conn.execute("SELECT number, text FROM footnotes WHERE hadith_id = 5;").fetchone()
        assert fn0 == (0, " English footnote for translation of first hadith")

        # Check pack_meta
        meta = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
        assert meta["pack_id"] == pack_id
        assert meta["pack_version"] == "0.4.0"
        assert meta["schema_version"] == "1"
        assert meta["completeness"] == "partial"
        assert meta["fts5_enabled"] == "true"
        assert meta["matn_split_verified"] == "false"

        # Check coverage report
        cov = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT metric, count, notes FROM coverage_report;").fetchall()}
        assert cov["expected_total"] == (5, None)
        assert cov["ar_present"] == (3, None)
        assert cov["en_present"] == (0, None)
        assert cov["en_missing"] == (5, None)
        assert cov["cross_ref_count"] == (1, None)
        assert cov["empty_text_count"] == (1, None)
        assert cov["footnote_count"] == (2, None)
        assert cov["footnote_missing_inline_marker_count"] == (1, "hadith_id=5; footnote_number=0")
        assert cov["inline_marker_missing_footnote_count"] == (0, None)

        # FTS query verification:
        # Both "الأعمال عَلَىٰ" and "الاعمال علي" hit exactly rowid=1
        query1 = taqreeb_match_literal("الأعمال عَلَىٰ")
        query2 = taqreeb_match_literal("الاعمال علي")
        assert query1 is not None and query2 is not None

        res1 = conn.execute("SELECT rowid FROM hadiths_fts WHERE text_ar_search MATCH ?;", (query1,)).fetchall()
        res2 = conn.execute("SELECT rowid FROM hadiths_fts WHERE text_ar_search MATCH ?;", (query2,)).fetchall()
        assert res1 == [(1,)]
        assert res2 == [(1,)]

        # No citation hit (takhreej not indexed)
        query_takhreej = taqreeb_match_literal("البخاري")
        assert query_takhreej is not None
        res_tak = conn.execute("SELECT rowid FROM hadiths_fts WHERE text_ar_search MATCH ?;", (query_takhreej,)).fetchall()
        assert res_tak == []

        # Injection-shaped queries do not execute as FTS operators
        for injection in ['" OR 1=1 --', "NOT rowid", "* AND *", "NEAR()"]:
            lit = taqreeb_match_literal(injection)
            if lit is not None:
                res_inj = conn.execute("SELECT rowid FROM hadiths_fts WHERE text_ar_search MATCH ?;", (lit,)).fetchall()
                assert res_inj == []

    finally:
        conn.close()


def test_build_refuses_preexisting_output(tmp_path: Path) -> None:
    sources_root, output_dir = _setup_synthetic_taqreeb_env(tmp_path)
    pack_id = "hadith-taqreeb-jami"
    target = output_dir / f"{pack_id}.db"
    target.write_bytes(b"existing content")

    with pytest.raises(FileExistsError):
        build_hadith_taqreeb(pack_id, sources_root, output_dir)

    assert target.read_bytes() == b"existing content"


def test_malformed_input_duplicate_id_and_orphan(tmp_path: Path) -> None:
    # Duplicate hadith id
    doc = _make_synthetic_taqreeb_json()
    doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][1]["id"] = 1  # duplicate of id 1
    s_root, o_dir = _setup_synthetic_taqreeb_env(tmp_path / "dup_id", doc=doc)
    with pytest.raises(Exception):
        build_hadith_taqreeb("hadith-taqreeb-jami", s_root, o_dir)

    # Sibling index duplicate in chapters
    doc2 = _make_synthetic_taqreeb_json()
    doc2["maqsads"][0]["kutub"][0]["chapters"][1]["chapter_index"] = 1  # duplicate index 1
    s_root2, o_dir2 = _setup_synthetic_taqreeb_env(tmp_path / "dup_idx", doc=doc2)
    with pytest.raises(Exception):
        build_hadith_taqreeb("hadith-taqreeb-jami", s_root2, o_dir2)

    # Book ID mismatch
    doc3 = _make_synthetic_taqreeb_json()
    doc3["book_id"] = 99
    s_root3, o_dir3 = _setup_synthetic_taqreeb_env(tmp_path / "book_mismatch", doc=doc3)
    with pytest.raises(Exception):
        build_hadith_taqreeb("hadith-taqreeb-jami", s_root3, o_dir3)


def test_cli_argument_parsing(tmp_path: Path) -> None:
    sources_root, output_dir = _setup_synthetic_taqreeb_env(tmp_path)
    pack_id = "hadith-taqreeb-jami"

    # Missing args -> exit 2
    assert main([]) == 2
    assert main(["--pack-id", pack_id]) == 2
    assert main(["--pack-id", "invalid-pack", "--sources-root", str(sources_root), "--output-dir", str(output_dir)]) == 2

    # Valid invocation -> exit 0
    assert main(["--pack-id", pack_id, "--sources-root", str(sources_root), "--output-dir", str(output_dir)]) == 0

    # Build failure (already exists) -> exit 1
    assert main(["--pack-id", pack_id, "--sources-root", str(sources_root), "--output-dir", str(output_dir)]) == 1


def test_taqreeb_pending_provenance(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    lock_entries = parse_and_validate_lock(repo_root / "sources" / "manifest.lock.json")
    entries_by_path = {e.path: e for e in lock_entries}

    pack_suffixes = ["jami", "maalim", "wajeez", "kulliyyah"]
    for suffix in pack_suffixes:
        pack_id = f"hadith-taqreeb-{suffix}"
        book_id, basename = TAQREEB_SOURCES[pack_id]
        rel_path = f"hadith-taqreeb/{basename}"
        assert rel_path in entries_by_path, f"Lock entry missing for {rel_path}"
        entry = entries_by_path[rel_path]

        lic_file = repo_root / "sources" / "licenses" / f"{pack_id}.json"
        lic_data = parse_strict_json(lic_file.read_bytes().decode("utf-8"))

        assert lic_data["status"] == "pending"
        assert lic_data["reviewedBy"] is None
        assert lic_data["licenseSpdx"] == "LicenseRef-Taqreeb-Al-Sunnah-Unreviewed"
        assert lic_data["primaryPath"] == rel_path

        metadata = load_sqlite_source_metadata(
            repo_root,
            pack_id,
            (entry,),
            primary_path=rel_path,
            pack_version="0.4.0",
        )
        assert metadata["license_spdx"] == "LicenseRef-Taqreeb-Al-Sunnah-Unreviewed"
        assert metadata["source_version"] == entry.source_version

    # Mismatched source version using a temporary copy in tmp_path
    tmp_sources = tmp_path / "modified_sources"
    tmp_lic_dir = tmp_sources / "sources" / "licenses"
    tmp_lic_dir.mkdir(parents=True)

    test_pack = "hadith-taqreeb-jami"
    test_entry = entries_by_path[f"hadith-taqreeb/{TAQREEB_SOURCES[test_pack][1]}"]
    orig_lic = (repo_root / "sources" / "licenses" / f"{test_pack}.json").read_bytes().decode("utf-8")
    mod_data = parse_strict_json(orig_lic)
    mod_data["sources"][0]["sourceVersion"] = "wrong-version"
    (tmp_lic_dir / f"{test_pack}.json").write_text(json.dumps(mod_data, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="sourceVersion mismatch"):
        load_sqlite_source_metadata(
            tmp_sources,
            test_pack,
            (test_entry,),
            primary_path=test_entry.path,
            pack_version="0.4.0",
        )
