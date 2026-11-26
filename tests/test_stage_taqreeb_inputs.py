"""Tests for pipelines.common.stage_taqreeb_inputs and Taqreeb source audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import pytest

from pipelines.common.lock_inputs import (
    LockEntry,
    canonical_lock_json,
    parse_strict_json,
)
from pipelines.common.stage_taqreeb_inputs import (
    TAQREEB_SOURCES,
    main,
    stage_taqreeb_inputs,
)

INVENTORY_EXPECTATIONS = {
    "hadith-taqreeb-jami": {
        "book_id": 1,
        "basename": "01_jame3_al_usul_al_tisah.json",
        "bytes": 35520125,
        "sha256": "6158e62d02556a7828a49938c090ec93d1874e2e0f3cb718e7413f066273a4fd",
        "maqsads": 10,
        "kutub": 45,
        "chapters": 2740,
        "hadiths": 22564,
        "footnotes": 7472,
    },
    "hadith-taqreeb-maalim": {
        "book_id": 2,
        "basename": "02_maalem_al_sunnah_al_nabawiyyah.json",
        "bytes": 7187636,
        "sha256": "8c114bb63c2f25f6bd214b55198b5c99e06d0f5217a1b62dc5882ccf3d0102e9",
        "maqsads": 10,
        "kutub": 51,
        "chapters": 2018,
        "hadiths": 4303,
        "footnotes": 1920,
    },
    "hadith-taqreeb-wajeez": {
        "book_id": 3,
        "basename": "03_al_wajeez.json",
        "bytes": 2235929,
        "sha256": "3b3e5bfb4f8060c13957944f3d250c2dcd09b9776ef214b9ecb15d61d67618d7",
        "maqsads": 9,
        "kutub": 43,
        "chapters": 916,
        "hadiths": 1402,
        "footnotes": 658,
    },
    "hadith-taqreeb-kulliyyah": {
        "book_id": 4,
        "basename": "04_al_ahadith_al_kulliyyah.json",
        "bytes": 297525,
        "sha256": "e7990e9336315a97f7ca975e734a7ad97feebab93268de9aff8904efb25aee7c",
        "maqsads": 6,
        "kutub": 20,
        "chapters": 40,
        "hadiths": 151,
        "footnotes": 157,
    },
}


def _create_synthetic_environment(tmp_path: Path):
    content_root = tmp_path / "Contant"
    source_dir = content_root / "hadith" / "Hadith Saleh al-Shami"
    source_dir.mkdir(parents=True)
    raw_root = tmp_path / "raw-inputs"

    entries = []
    for pack_id, (book_id, basename) in TAQREEB_SOURCES.items():
        payload = json.dumps({"book_id": book_id, "name": basename}).encode("utf-8")
        src_file = source_dir / basename
        src_file.write_bytes(payload)
        sha256 = hashlib.sha256(payload).hexdigest()
        entries.append(
            LockEntry(
                path=f"hadith-taqreeb/{basename}",
                source_url="https://sunnahenc.com/about",
                source_version="test-version",
                size_bytes=len(payload),
                sha256=sha256,
            )
        )

    lock_file = tmp_path / "manifest.lock.json"
    lock_file.write_bytes(canonical_lock_json(entries))
    return content_root, raw_root, lock_file


def test_stage_taqreeb_inputs_success_and_idempotent(tmp_path: Path):
    content_root, raw_root, lock_file = _create_synthetic_environment(tmp_path)

    staged = stage_taqreeb_inputs(content_root, raw_root, lock_file)
    assert len(staged) == 4
    for dest in staged:
        assert dest.is_file()
        rel_name = dest.name
        src = content_root / "hadith" / "Hadith Saleh al-Shami" / rel_name
        assert dest.read_bytes() == src.read_bytes()

    staged_again = stage_taqreeb_inputs(content_root, raw_root, lock_file)
    assert staged_again == staged
    for dest in staged:
        assert dest.is_file()


def test_stage_taqreeb_inputs_corrupt_original_fails_preflight(tmp_path: Path):
    content_root, raw_root, lock_file = _create_synthetic_environment(tmp_path)
    first_basename = list(TAQREEB_SOURCES.values())[0][1]
    (content_root / "hadith" / "Hadith Saleh al-Shami" / first_basename).write_bytes(b"corrupted")

    with pytest.raises(ValueError, match="mismatch"):
        stage_taqreeb_inputs(content_root, raw_root, lock_file)

    dest_dir = raw_root / "hadith-taqreeb"
    if dest_dir.exists():
        assert list(dest_dir.iterdir()) == []


def test_stage_taqreeb_inputs_missing_original_fails(tmp_path: Path):
    content_root, raw_root, lock_file = _create_synthetic_environment(tmp_path)
    first_basename = list(TAQREEB_SOURCES.values())[0][1]
    (content_root / "hadith" / "Hadith Saleh al-Shami" / first_basename).unlink()

    with pytest.raises(FileNotFoundError):
        stage_taqreeb_inputs(content_root, raw_root, lock_file)

    dest_dir = raw_root / "hadith-taqreeb"
    if dest_dir.exists():
        assert list(dest_dir.iterdir()) == []


def test_stage_taqreeb_inputs_preexisting_conflict_fails(tmp_path: Path):
    content_root, raw_root, lock_file = _create_synthetic_environment(tmp_path)
    first_basename = list(TAQREEB_SOURCES.values())[0][1]
    dest_file = raw_root / "hadith-taqreeb" / first_basename
    dest_file.parent.mkdir(parents=True)
    dest_file.write_bytes(b"differing-bytes")

    with pytest.raises(ValueError, match="conflict|mismatch"):
        stage_taqreeb_inputs(content_root, raw_root, lock_file)

    assert dest_file.read_bytes() == b"differing-bytes"


def test_stage_taqreeb_inputs_nested_or_same_roots_rejected(tmp_path: Path):
    content_root, raw_root, lock_file = _create_synthetic_environment(tmp_path)

    with pytest.raises(ValueError, match="same or nested"):
        stage_taqreeb_inputs(content_root, content_root, lock_file)

    nested_raw = content_root / "raw"
    with pytest.raises(ValueError, match="same or nested"):
        stage_taqreeb_inputs(content_root, nested_raw, lock_file)

    nested_content = raw_root / "content"
    nested_content.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="same or nested"):
        stage_taqreeb_inputs(nested_content, raw_root, lock_file)


def test_stage_taqreeb_inputs_wrong_case_source_rejected(tmp_path: Path):
    content_root, raw_root, lock_file = _create_synthetic_environment(tmp_path)
    # Create directory with different casing on filesystem or query wrong casing
    bad_content_root = Path(str(content_root).swapcase())
    # On Windows or case-insensitive FS, _validate_path_hierarchy catches case mismatch
    with pytest.raises((ValueError, FileNotFoundError)):
        stage_taqreeb_inputs(bad_content_root, raw_root, lock_file)


def test_stage_taqreeb_cli(tmp_path: Path):
    content_root, raw_root, lock_file = _create_synthetic_environment(tmp_path)
    res = main([
        "--content-root",
        str(content_root),
        "--raw-root",
        str(raw_root),
        "--lock",
        str(lock_file),
    ])
    assert res == 0

    bad_res = main([
        "--content-root",
        str(content_root / "nonexistent"),
        "--raw-root",
        str(raw_root),
        "--lock",
        str(lock_file),
    ])
    assert bad_res != 0


def _locate_real_content_root() -> Path | None:
    candidates = [
        Path("Contant"),
        Path("/original"),
        Path("C:/Users/moham/Downloads/islamic-core-content/Contant"),
    ]
    for c in candidates:
        if (c / "hadith" / "Hadith Saleh al-Shami").is_dir():
            return c
    return None


def test_real_taqreeb_source_audit():
    content_root = _locate_real_content_root()
    if content_root is None:
        pytest.skip("Real Contant directory not found for audit")

    source_dir = content_root / "hadith" / "Hadith Saleh al-Shami"
    total_hadiths = 0
    total_footnotes = 0
    h7427 = None
    h8199 = None
    h16588 = None
    fn1 = None

    for pack_id, exp in INVENTORY_EXPECTATIONS.items():
        file_path = source_dir / exp["basename"]
        assert file_path.is_file(), f"Missing expected file: {file_path}"

        raw_bytes = file_path.read_bytes()
        assert len(raw_bytes) == exp["bytes"], f"Byte size mismatch for {exp['basename']}"
        assert hashlib.sha256(raw_bytes).hexdigest() == exp["sha256"], f"SHA256 mismatch for {exp['basename']}"

        doc = parse_strict_json(raw_bytes.decode("utf-8"))
        assert doc["book_id"] == exp["book_id"]

        maqsads = doc.get("maqsads", [])
        assert len(maqsads) == exp["maqsads"]

        kutub_count = sum(len(m.get("kutub", [])) for m in maqsads)
        assert kutub_count == exp["kutub"]

        chapters_count = sum(
            len(k.get("chapters", []))
            for m in maqsads
            for k in m.get("kutub", [])
        )
        assert chapters_count == exp["chapters"]

        hadiths = [
            h
            for m in maqsads
            for k in m.get("kutub", [])
            for c in k.get("chapters", [])
            for h in c.get("hadiths", [])
        ]
        assert len(hadiths) == exp["hadiths"]
        assert doc["total_hadiths"] == exp["hadiths"]
        total_hadiths += len(hadiths)

        footnotes = [
            fn
            for h in hadiths
            for fn in h.get("footnotes", [])
        ]
        assert len(footnotes) == exp["footnotes"]
        total_footnotes += len(footnotes)

        # Pack-specific anomalies check
        if exp["book_id"] == 1:
            h7427 = next(h for h in hadiths if h["id"] == 7427)
            assert h7427["text_ar"] is None
            assert h7427["text_ar_simple"] == ""
            assert h7427["takhreej"] == "وأخرجه/ حم(2254) (2380) (2381)."

            h8199 = next(h for h in hadiths if h["id"] == 8199)
            assert h8199["text_ar"] == ""

            h16588 = next(h for h in hadiths if h["id"] == 16588)
            assert h16588["text_ar"] == ""
            assert h16588["atraf"] == ["[انظر الحديث: 7734]."]

        elif exp["book_id"] == 2:
            h1 = next(h for h in hadiths if h["id"] == 1)
            fn1 = next(fn for fn in h1.get("footnotes", []) if fn["number"] == 1)
            assert fn1["text"] == " English footnote for translation of first hadith"
            assert "{[1]}" not in (h1["text_ar"] or "")

    assert total_hadiths == 28420
    assert total_footnotes == 10207

    # Audit DB if present
    db_path = source_dir / "taqreeb_al_sunnah.db"
    if db_path.is_file():
        db_sha256 = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert db_sha256 == "c300810bc9f76540cc92ea226f5e6f4e6d9482ec1b29186a8d39a34e2f4e839f"
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro&immutable=1", uri=True)
        try:
            h_count = conn.execute("SELECT count(*) FROM hadiths").fetchone()[0]
            assert h_count == 28420
            f_count = conn.execute("SELECT count(*) FROM footnotes").fetchone()[0]
            assert f_count == 10207
            # Verify sample values by hadith ID and footnote key against taqreeb_al_sunnah.db
            assert h7427 is not None and h8199 is not None and h16588 is not None and fn1 is not None

            # Jāmiʿ ID 7427: text_ar is NULL (None), takhreej matches
            row_7427 = conn.execute(
                "SELECT text_ar, takhreej FROM hadiths WHERE id = 7427"
            ).fetchone()
            assert row_7427 is not None
            assert row_7427[0] is None
            assert row_7427[1] == h7427["takhreej"]

            # Jāmiʿ ID 8199 and 16588: text_ar is empty string ''
            row_8199 = conn.execute(
                "SELECT text_ar FROM hadiths WHERE id = 8199"
            ).fetchone()
            assert row_8199 is not None
            assert row_8199[0] == ""
            assert row_8199[0] == h8199["text_ar"]

            row_16588 = conn.execute(
                "SELECT text_ar FROM hadiths WHERE id = 16588"
            ).fetchone()
            assert row_16588 is not None
            assert row_16588[0] == ""
            assert row_16588[0] == h16588["text_ar"]

            # Maʿālim hadith ID 1 footnote key 1: text matches source footnote
            row_fn1 = conn.execute(
                "SELECT footnote_text FROM footnotes WHERE hadith_id = 1 AND footnote_number = 1"
            ).fetchone()
            assert row_fn1 is not None
            assert row_fn1[0] == fn1["text"]
        finally:
            conn.close()
