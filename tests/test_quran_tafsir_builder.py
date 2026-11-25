"""Contract and behavior tests for the Quran Jalalayn tafsir pack builder."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any

import pytest

from pipelines.common.lock_inputs import author_lock
from pipelines.common.search_text import normalize_arabic
from tests.fixtures.tanzil import (
    SURAH_AYAH_COUNTS,
    build_synthetic_metadata_xml,
    build_synthetic_scripture_xml,
)


@pytest.fixture(autouse=True)
def _mock_verify_runtime_if_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from pipelines.toolchain.check_runtime import verify_runtime
        verify_runtime()
    except Exception:
        monkeypatch.setattr(
            "pipelines.builders.build_quran_tafsir.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )
JALALAYN_NOTICE_DEFAULT = (
    "<!--\n"
    "# --------------------------------------------------------------------\n"
    "#  Quran Translation\n"
    "#  Name: Synthetic Jalalayn\n"
    "#  ID: ar.jalalayn\n"
    "# --------------------------------------------------------------------\n"
    "-->\n"
)


def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def build_synthetic_jalalayn_xml(
    *,
    xml_declaration: str = '<?xml version="1.0" encoding="utf-8" ?>\n',
    leading_comment: str = JALALAYN_NOTICE_DEFAULT,
    surah_counts: tuple[int, ...] = SURAH_AYAH_COUNTS,
    empty_sura_name: bool = True,
    extra_body: str = "",
) -> str:
    """Build synthetic Jalalayn scripture XML reusing shared scripture generator."""
    xml = build_synthetic_scripture_xml(
        "ar.jalalayn",
        xml_declaration=xml_declaration,
        leading_comment=leading_comment,
        surah_counts=surah_counts,
    )
    if empty_sura_name:
        xml = re.sub(r'name="[^"]*"', 'name=""', xml)
    # Set known text on first two ayahs for normalization and FTS verification
    xml = xml.replace('text="نص_ar.jalalayn_1_1"', 'text="  رَبِّ العالمين  "', 1)
    xml = xml.replace('text="نص_ar.jalalayn_1_2"', 'text="الحمد لله رب العالمين"', 1)
    if extra_body:
        xml = xml.replace("</quran>", f"{extra_body}</quran>")
    return xml


def _setup_jalalayn_pack_env(
    root: Path,
    *,
    metadata_xml: str | None = None,
    jalalayn_xml: str | None = None,
) -> tuple[Path, Path]:
    raw_dir = root / "raw-inputs" / "tanzil"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = root / "sources"
    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    meta_content = metadata_xml or build_synthetic_metadata_xml()
    jal_content = jalalayn_xml or build_synthetic_jalalayn_xml()

    meta_path = raw_dir / "quran-data-Tanzil.xml"
    jal_path = raw_dir / "ar.jalalayn.xml"

    meta_path.write_bytes(meta_content.encode("utf-8"))
    jal_path.write_bytes(jal_content.encode("utf-8"))

    registry_data = {
        "tanzil/ar.jalalayn.xml": {
            "packIds": ["quran-tafsir-ar-jalalayn"],
            "sourceUrl": "https://example.org/ar.jalalayn.xml",
            "sourceVersion": "1.0",
        },
        "tanzil/quran-data-Tanzil.xml": {
            "packIds": ["quran-tafsir-ar-jalalayn"],
            "sourceUrl": "https://example.org/quran-data-Tanzil.xml",
            "sourceVersion": "1.0",
        },
    }
    reg_file = sources_dir / "registry.json"
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text(json.dumps(registry_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    author_lock(reg_file, root / "raw-inputs", sources_dir / "manifest.lock.json")

    lic_data = {
        "licenseSpdx": "LicenseRef-Tanzil-Noncommercial-Edition",
        "packId": "quran-tafsir-ar-jalalayn",
        "primaryPath": "tanzil/ar.jalalayn.xml",
        "reviewedBy": None,
        "status": "pending",
        "sources": [
            {
                "attribution": "Synthetic Jalalayn Tafsir",
                "name": "Synthetic Jalalayn",
                "notice": "synthetic notice",
                "path": "tanzil/ar.jalalayn.xml",
                "permittedUses": [],
                "rightsEvidence": [
                    "https://example.org/rights",
                ],
                "sourceUrl": "https://example.org/ar.jalalayn.xml",
                "sourceVersion": "1.0",
            },
            {
                "attribution": "Synthetic Quran Metadata",
                "name": "Synthetic Metadata",
                "notice": "synthetic notice",
                "path": "tanzil/quran-data-Tanzil.xml",
                "permittedUses": [],
                "rightsEvidence": [
                    "https://example.org/rights",
                ],
                "sourceUrl": "https://example.org/quran-data-Tanzil.xml",
                "sourceVersion": "1.0",
            },
        ],
    }
    lic_file = sources_dir / "licenses" / "quran-tafsir-ar-jalalayn.json"
    lic_file.parent.mkdir(parents=True, exist_ok=True)
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return root, out_dir


# =============================================================================
# 1. Grouped synthetic input tests (build_grouped_tafsir_rows)
# =============================================================================


def test_build_grouped_tafsir_rows_oracle() -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    fixture_path = Path("tests/fixtures/inputs/qul/tafsir-ar-fixture.json")
    ayah_ids = {"1:1": 1, "1:2": 2, "1:3": 3}
    entries, map_rows = build_grouped_tafsir_rows(fixture_path, ayah_ids, language="ar")

    assert entries == [
        (1, "1:1", 1, 1, "رَبِّ", "plain", "رب"),
        (2, "1:2-3", 2, 3, "<p>مثال</p>", "html", "مثال"),
    ]
    assert map_rows == [
        (1, "1:1", 1),
        (2, "1:2", 2),
        (3, "1:3", 2),
    ]


def test_build_grouped_tafsir_rows_surah2_group_and_html_comments_script_style(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    json_file = tmp_path / "grouped-s2.json"
    raw_html = (
        "<!-- Leading HTML comment -->"
        "<p>نص <b>واضح</b> ومثال</p>"
        "<script>console.log('exclude me');</script>"
        "<style>.x { color: red; }</style>"
        "<!-- Trailing HTML comment -->"
    )
    payload = [
        {
            "verse_keys": ["2:1", "2:2"],
            "text": raw_html,
            "format": "html",
        }
    ]
    json_file.write_text(json.dumps(payload), encoding="utf-8")
    ayah_ids = {"2:1": 8, "2:2": 9}

    entries, map_rows = build_grouped_tafsir_rows(json_file, ayah_ids, language="ar")

    assert entries == [
        (8, "2:1-2", 8, 9, raw_html, "html", "نص واضح ومثال"),
    ]
    assert map_rows == [
        (8, "2:1", 8),
        (9, "2:2", 8),
    ]


@pytest.mark.parametrize(
    ("desc", "payload_str", "err_match", "ayah_ids_override"),
    [
        ("non_array_root", '{"verse_keys": ["1:1"], "text": "t", "format": "plain"}', "schema|list|array|object", None),
        ("invalid_format", '[{"verse_keys": ["1:1"], "text": "t", "format": "markdown"}]', "schema|format", None),
        ("duplicate_json_key", '[{"verse_keys": ["1:1"], "text": "t", "text": "t2", "format": "plain"}]', "duplicate", None),
        ("ndjson_format", '{"verse_keys": ["1:1"], "text": "t", "format": "plain"}\n{"verse_keys": ["1:2"], "text": "t", "format": "plain"}', "Extra data|json", None),
        ("unknown_keys", '[{"verse_keys": ["1:1", "1:2", "1:4"], "text": "t", "format": "plain"}]', "unknown", None),
        ("missing_keys", '[{"verse_keys": ["1:1", "1:2"], "text": "t", "format": "plain"}]', "missing", None),
        ("interior_gap", '[{"verse_keys": ["1:1", "1:3"], "text": "t", "format": "plain"}, {"verse_keys": ["1:2"], "text": "t", "format": "plain"}]', "consecutive", None),
        ("overlap", '[{"verse_keys": ["1:1", "1:2"], "text": "t", "format": "plain"}, {"verse_keys": ["1:2", "1:3"], "text": "t", "format": "plain"}]', "overlap", None),
        ("descending_keys", '[{"verse_keys": ["1:2", "1:1"], "text": "t", "format": "plain"}, {"verse_keys": ["1:3"], "text": "t", "format": "plain"}]', "consecutive|ascending", None),
        ("descending_groups", '[{"verse_keys": ["1:2", "1:3"], "text": "t", "format": "plain"}, {"verse_keys": ["1:1"], "text": "t", "format": "plain"}]', "first ID", None),
        ("cross_surah_group", '[{"verse_keys": ["1:7", "2:1"], "text": "t", "format": "plain"}]', "cross-surah", {"1:7": 7, "2:1": 8}),
        ("empty_raw_text", '[{"verse_keys": ["1:1", "1:2", "1:3"], "text": "   ", "format": "plain"}]', "blank|empty", None),
        ("empty_projected_text", '[{"verse_keys": ["1:1", "1:2", "1:3"], "text": "<script>var x = 1;</script>", "format": "html"}]', "blank|empty", None),
    ],
)
def test_build_grouped_tafsir_rows_negative_payloads(
    tmp_path: Path,
    desc: str,
    payload_str: str,
    err_match: str,
    ayah_ids_override: dict[str, int] | None,
) -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    p = tmp_path / f"invalid-{desc}.json"
    p.write_text(payload_str, encoding="utf-8")
    ayah_ids = ayah_ids_override if ayah_ids_override is not None else {"1:1": 1, "1:2": 2, "1:3": 3}

    with pytest.raises(ValueError, match=rf"(?i){err_match}"):
        build_grouped_tafsir_rows(p, ayah_ids, language="ar")

@pytest.mark.parametrize(
    ("desc", "ayah_ids", "err_match"),
    [
        ("bool_id", {"1:1": True, "1:2": 2, "1:3": 3}, "bool|integer"),
        ("duplicate_id", {"1:1": 1, "1:2": 1, "1:3": 3}, "unique|duplicate"),
        ("non_consecutive_group_ids", {"1:1": 1, "1:2": 3, "1:3": 4}, "consecutive"),
        ("nonmonotonic_ids", {"1:1": 2, "1:2": 1, "1:3": 3}, "order|monotonic"),
    ],
)
def test_build_grouped_tafsir_rows_inconsistent_ayah_ids(
    desc: str,
    ayah_ids: dict[str, Any],
    err_match: str,
) -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    fixture_path = Path("tests/fixtures/inputs/qul/tafsir-ar-fixture.json")
    with pytest.raises(ValueError, match=rf"(?i){err_match}"):
        build_grouped_tafsir_rows(fixture_path, ayah_ids, language="ar")


def test_build_grouped_tafsir_rows_invalid_utf8(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    p = tmp_path / "bad-utf8.json"
    p.write_bytes(b'[\x80\xff{"verse_keys": ["1:1"]}]')
    with pytest.raises(ValueError, match=r"(?i)utf-8|decode"):
        build_grouped_tafsir_rows(p, {"1:1": 1}, language="ar")


def test_build_grouped_tafsir_rows_ayah_id_out_of_bounds_rejected(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    p = tmp_path / "valid-singleton.json"
    p.write_text('[{"verse_keys": ["2:1"], "text": "تفسير", "format": "plain"}]', encoding="utf-8")
    with pytest.raises(ValueError):
        build_grouped_tafsir_rows(p, {"2:1": 6237}, language="ar")
    with pytest.raises(ValueError):
        build_grouped_tafsir_rows(p, {"2:1": 0}, language="ar")


def test_build_grouped_tafsir_rows_same_surah_gap_invalid_offset_rejected(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    p = tmp_path / "sparse-groups.json"
    p.write_text(
        '[{"verse_keys": ["1:1"], "text": "تفسير 1", "format": "plain"}, '
        '{"verse_keys": ["1:3"], "text": "تفسير 3", "format": "plain"}]',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        build_grouped_tafsir_rows(p, {"1:1": 1, "1:3": 2}, language="ar")

    entries, map_rows = build_grouped_tafsir_rows(p, {"1:1": 1, "1:3": 3}, language="ar")
    assert len(entries) == 2
    assert len(map_rows) == 2
    assert entries[0].id == 1
    assert entries[0].group_key == "1:1"
    assert entries[1].id == 3
    assert entries[1].group_key == "1:3"
    assert map_rows[0] == (1, "1:1", 1)
    assert map_rows[1] == (3, "1:3", 3)


def test_build_grouped_tafsir_rows_non_canonical_verse_key_fullmatch(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    p = tmp_path / "valid-singleton.json"
    p.write_text('[{"verse_keys": ["1:1"], "text": "تفسير", "format": "plain"}]', encoding="utf-8")
    with pytest.raises(ValueError):
        build_grouped_tafsir_rows(p, {"1:1\n": 1}, language="ar")

# =============================================================================
# 2. Production CLI and pack builder tests
# =============================================================================


def test_build_quran_tafsir_success(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import build_quran_tafsir, main

    sources_root, out_dir = _setup_jalalayn_pack_env(tmp_path)
    db_path = out_dir / "quran-tafsir-ar-jalalayn.db"

    ret = main(["--sources-root", str(sources_root), "--output-dir", str(out_dir)])
    assert ret == 0
    assert db_path.is_file()

    conn = _open_ro_db(db_path)
    try:
        # PRAGMA checks
        integrity = conn.execute("PRAGMA integrity_check;").fetchall()
        assert integrity == [("ok",)]
        fk_check = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert fk_check == []
        encoding = conn.execute("PRAGMA encoding;").fetchone()[0]
        assert encoding == "UTF-8"
        page_size = conn.execute("PRAGMA page_size;").fetchone()[0]
        assert page_size == 4096
        user_version = conn.execute("PRAGMA user_version;").fetchone()[0]
        assert user_version == 1

        # Check tafsir_entries rows and ascending PK order
        entries = conn.execute(
            "SELECT id, group_key, start_ayah_id, end_ayah_id, text, text_format, text_search "
            "FROM tafsir_entries ORDER BY id ASC;"
        ).fetchall()
        assert len(entries) == 6236
        assert [r[0] for r in entries] == list(range(1, 6237))

        # Check exact singletons
        for r in entries:
            eid, gkey, start_id, end_id, txt, fmt, txt_search = r
            assert eid == start_id == end_id
            assert fmt == "plain"
            assert txt.strip()
            assert txt_search == normalize_arabic(txt)

        assert entries[0][0] == 1
        assert entries[0][1] == "1:1"
        assert entries[0][4] == "  رَبِّ العالمين  "
        assert entries[0][6] == "  رب العالمين  "

        assert entries[1][0] == 2
        assert entries[1][1] == "1:2"
        assert entries[1][4] == "الحمد لله رب العالمين"
        assert entries[1][6] == "الحمد لله رب العالمين"

        # Check tafsir_ayah_map table
        map_rows = conn.execute(
            "SELECT ayah_id, verse_key, entry_id FROM tafsir_ayah_map ORDER BY ayah_id ASC;"
        ).fetchall()
        assert len(map_rows) == 6236
        assert [r[0] for r in map_rows] == list(range(1, 6237))
        assert map_rows[0] == (1, "1:1", 1)
        assert map_rows[1] == (2, "1:2", 2)
        assert map_rows[-1] == (6236, "114:6", 6236)

        # Check bound FTS queries on Arabic text at rowid=2
        fts_match_norm = conn.execute(
            "SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH ? AND rowid = 2;",
            ('"رب العالمين"',),
        ).fetchall()
        assert fts_match_norm == [(2,)]

        fts_match_diacritics = conn.execute(
            "SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH ? AND rowid = 2;",
            (f'"{normalize_arabic("رَبِّ العالمين")}"',),
        ).fetchall()
        assert fts_match_diacritics == [(2,)]

        fts_none = conn.execute(
            "SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH ? AND rowid = 2;",
            ('"zzzzfixturemissing"',),
        ).fetchall()
        assert fts_none == []

        # FTS integrity check on a temporary copy
        temp_copy = tmp_path / "copy.db"
        temp_copy.write_bytes(db_path.read_bytes())
        copy_conn = sqlite3.connect(temp_copy)
        try:
            copy_conn.execute("INSERT INTO tafsir_fts(tafsir_fts, rank) VALUES('integrity-check', 1);")
        finally:
            copy_conn.close()
            temp_copy.unlink(missing_ok=True)

        # Pack metadata
        meta = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
        assert meta["pack_id"] == "quran-tafsir-ar-jalalayn"
        assert meta["pack_version"] == "0.1.0"
        assert meta["schema_version"] == "1"
        assert meta["fts5_enabled"] == "true"
        assert meta["completeness"] == "complete"
        assert meta["license_spdx"] == "LicenseRef-Tanzil-Noncommercial-Edition"
        assert meta["license_ref"] == "sources/licenses/quran-tafsir-ar-jalalayn.json"
        assert meta["source_name"] == "Synthetic Jalalayn"

        # Coverage report
        cov = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT metric, count, notes FROM coverage_report;").fetchall()}
        assert cov["expected_total"] == (6236, None)
        assert cov["ar_present"] == (6236, None)

    finally:
        conn.close()

    # Refuse overwriting existing output
    ret = main(["--sources-root", str(sources_root), "--output-dir", str(out_dir)])
    assert ret == 1
    assert db_path.is_file()


def test_builder_cli_arg_handling() -> None:
    from pipelines.builders.build_quran_tafsir import main

    assert main([]) == 2
    assert main(["--invalid-arg"]) == 2
    assert main(["--sources-root", "/tmp/nonexistent"]) == 2


def test_build_quran_tafsir_wrong_edition_notice_failure(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import main

    wrong_xml = build_synthetic_jalalayn_xml(
        leading_comment="<!--\n#  ID: wrong.edition\n-->\n"
    )
    sources_root, out_dir = _setup_jalalayn_pack_env(tmp_path, jalalayn_xml=wrong_xml)
    ret = main(["--sources-root", str(sources_root), "--output-dir", str(out_dir)])
    assert ret == 1
    assert not (out_dir / "quran-tafsir-ar-jalalayn.db").exists()


# =============================================================================
# 3. QUL Tafsir pack tests (Task 3.4A)
# =============================================================================


def test_builder_cli_pack_option() -> None:
    from pipelines.builders.build_quran_tafsir import main, SUPPORTED_PACK_IDS

    assert main(["--sources-root", "/tmp/nonexistent", "--output-dir", "/tmp/nonexistent", "--pack", "unknown-pack"]) == 2
    assert main(["--pack", "invalid-pack"]) == 2

    expected_packs = {
        "quran-tafsir-ar-jalalayn",
        "quran-tafsir-ar-ibn-kathir",
        "quran-tafsir-ar-tabari",
        "quran-tafsir-ar-qurtubi",
        "quran-tafsir-ar-wasit",
        "quran-tafsir-ar-razi",
        "quran-tafsir-en-ibn-kathir",
    }
    assert set(SUPPORTED_PACK_IDS) == expected_packs


def test_qul_pointer_grouping_success(tmp_path: Path) -> None:
    import zipfile
    from pipelines.builders.build_quran_tafsir import parse_qul_tafsir_data, build_qul_tafsir_rows

    ayah_ids = {
        "1:1": 1,
        "1:2": 2,
        "1:3": 3,
        "2:1": 4,
        "2:2": 5,
        "2:3": 6,
    }
    data = {
        "1:1": {"text": "<p>Tafsir 1:1</p>"},
        "1:2": {"text": "<p>Tafsir 1:2-3</p>", "ayah_keys": ["1:2", "1:3"]},
        "1:3": "1:2",
        "2:1": {"text": "<p>Tafsir 2:1</p>"},
        "2:2": {"text": "<p>Tafsir 2:2-3</p>"},
        "2:3": "2:2",
    }

    entries, map_rows = parse_qul_tafsir_data(data, ayah_ids, language="ar")
    assert len(entries) == 4
    assert entries[0].id == 1
    assert entries[0].group_key == "1:1"
    assert entries[0].start_ayah_id == 1
    assert entries[0].end_ayah_id == 1
    assert entries[0].text == "<p>Tafsir 1:1</p>"
    assert entries[0].text_format == "html"
    assert entries[0].text_search == normalize_arabic("Tafsir 1:1")

    assert entries[1].id == 2
    assert entries[1].group_key == "1:2-3"
    assert entries[1].start_ayah_id == 2
    assert entries[1].end_ayah_id == 3
    assert entries[1].text == "<p>Tafsir 1:2-3</p>"
    assert entries[1].text_format == "html"

    assert entries[2].id == 4
    assert entries[2].group_key == "2:1"
    assert entries[2].start_ayah_id == 4
    assert entries[2].end_ayah_id == 4

    assert entries[3].id == 5
    assert entries[3].group_key == "2:2-3"
    assert entries[3].start_ayah_id == 5
    assert entries[3].end_ayah_id == 6

    assert len(map_rows) == 6
    assert map_rows == [
        (1, "1:1", 1),
        (2, "1:2", 2),
        (3, "1:3", 2),
        (4, "2:1", 4),
        (5, "2:2", 5),
        (6, "2:3", 5),
    ]

    # Test language="en" preserves search text without arabic normalization
    en_entries, en_map_rows = parse_qul_tafsir_data(data, ayah_ids, language="en")
    assert en_entries[0].text_search == "Tafsir 1:1"
    assert len(en_map_rows) == 6

    # Test reading from single-member zip archive
    zip_file = tmp_path / "test-tafsir.json.zip"
    with zipfile.ZipFile(zip_file, "w") as zf:
        zf.writestr("test-tafsir.json", json.dumps(data).encode("utf-8"))
    zip_entries, zip_map = build_qul_tafsir_rows(zip_file, ayah_ids, language="ar")
    assert zip_entries == entries
    assert zip_map == map_rows


def test_qul_pointer_chain_and_cycle_rejection() -> None:
    from pipelines.builders.build_quran_tafsir import parse_qul_tafsir_data

    ayah_ids = {"1:1": 1, "1:2": 2, "1:3": 3}

    # Pointer chain: 1:3 -> 1:2 -> 1:1
    chain_data = {
        "1:1": {"text": "<p>T1</p>"},
        "1:2": "1:1",
        "1:3": "1:2",
    }
    with pytest.raises(ValueError, match="(?i)pointer"):
        parse_qul_tafsir_data(chain_data, ayah_ids, language="ar")

    # Cycle: 1:1 -> 1:2 -> 1:1
    cycle_data = {
        "1:1": "1:2",
        "1:2": "1:1",
        "1:3": {"text": "<p>T3</p>"},
    }
    with pytest.raises(ValueError, match="(?i)pointer"):
        parse_qul_tafsir_data(cycle_data, ayah_ids, language="ar")

    # Self-pointer: 1:1 -> 1:1
    self_data = {
        "1:1": "1:1",
        "1:2": {"text": "<p>T2</p>"},
        "1:3": {"text": "<p>T3</p>"},
    }
    with pytest.raises(ValueError, match="(?i)pointer|self"):
        parse_qul_tafsir_data(self_data, ayah_ids, language="ar")

    # Pointer to unknown key
    unknown_ptr_data = {
        "1:1": {"text": "<p>T1</p>"},
        "1:2": "9:99",
        "1:3": {"text": "<p>T3</p>"},
    }
    with pytest.raises(ValueError, match="(?i)pointer|unknown"):
        parse_qul_tafsir_data(unknown_ptr_data, ayah_ids, language="ar")


def test_qul_noncontiguous_grouping_rejection() -> None:
    from pipelines.builders.build_quran_tafsir import parse_qul_tafsir_data

    # Gap in same surah: 1:1 anchor, 1:2 anchor, 1:3 points to 1:1
    ayah_ids = {"1:1": 1, "1:2": 2, "1:3": 3}
    gap_data = {
        "1:1": {"text": "<p>T1</p>"},
        "1:2": {"text": "<p>T2</p>"},
        "1:3": "1:1",
    }
    with pytest.raises(ValueError, match="contiguous|consecutive|gap"):
        parse_qul_tafsir_data(gap_data, ayah_ids, language="ar")

    # Cross-surah grouping: 2:1 points to 1:1
    cross_ayah_ids = {"1:1": 1, "2:1": 2}
    cross_data = {
        "1:1": {"text": "<p>T1</p>"},
        "2:1": "1:1",
    }
    with pytest.raises(ValueError, match="cross-surah|surah"):
        parse_qul_tafsir_data(cross_data, cross_ayah_ids, language="ar")

    # Pointer before anchor: 1:1 points to 1:2
    rev_ayah_ids = {"1:1": 1, "1:2": 2}
    rev_data = {
        "1:1": "1:2",
        "1:2": {"text": "<p>T2</p>"},
    }
    with pytest.raises(ValueError, match="first|anchor|contiguous"):
        parse_qul_tafsir_data(rev_data, rev_ayah_ids, language="ar")


def test_qul_shifted_pointer_rejected() -> None:
    from pipelines.builders.build_quran_tafsir import parse_qul_tafsir_data

    ayah_ids = {"1:1": 1, "1:2": 2, "1:3": 3}
    # 1:1 is anchor, 1:2 is independent, 1:3 points back to 1:1 (shifted pointer)
    shifted_data = {
        "1:1": {"text": "<p>T1</p>"},
        "1:2": {"text": "<p>T2</p>"},
        "1:3": "1:1",
    }
    with pytest.raises(ValueError, match="contiguous|consecutive|gap"):
        parse_qul_tafsir_data(shifted_data, ayah_ids, language="ar")


def test_qul_missing_extra_and_malformed_keys_rejection() -> None:
    from pipelines.builders.build_quran_tafsir import parse_qul_tafsir_data

    ayah_ids = {"1:1": 1, "1:2": 2}

    # Missing key from ayah_ids
    with pytest.raises(ValueError, match="missing|canonical"):
        parse_qul_tafsir_data({"1:1": {"text": "<p>T1</p>"}}, ayah_ids, language="ar")

    # Extra key not in ayah_ids
    extra_data = {
        "1:1": {"text": "<p>T1</p>"},
        "1:2": {"text": "<p>T2</p>"},
        "1:3": {"text": "<p>T3</p>"},
    }
    with pytest.raises(ValueError, match="extra|canonical"):
        parse_qul_tafsir_data(extra_data, ayah_ids, language="ar")

    # Blank anchor text
    blank_data = {
        "1:1": {"text": "   "},
        "1:2": {"text": "<p>T2</p>"},
    }
    with pytest.raises(ValueError, match="empty|blank"):
        parse_qul_tafsir_data(blank_data, ayah_ids, language="ar")

    # Invalid value type
    invalid_val_data = {
        "1:1": 12345,
        "1:2": {"text": "<p>T2</p>"},
    }
    with pytest.raises(ValueError, match="type"):
        parse_qul_tafsir_data(invalid_val_data, ayah_ids, language="ar")


def _setup_qul_tafsir_pack_env(
    root: Path,
    *,
    pack_id: str = "quran-tafsir-ar-ibn-kathir",
    language: str = "ar",
    custom_entries: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    import zipfile
    from tests.fixtures.tanzil import build_synthetic_metadata_xml

    raw_dir = root / "raw-inputs"
    sources_dir = root / "sources"
    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    tanzil_dir = raw_dir / "tanzil"
    tanzil_dir.mkdir(parents=True, exist_ok=True)
    meta_path = tanzil_dir / "quran-data-Tanzil.xml"
    meta_path.write_bytes(build_synthetic_metadata_xml().encode("utf-8"))

    # Generate synthetic QUL JSON for 6236 ayahs
    from pipelines.common.tanzil_coordinates import load_tanzil_coordinates
    coords = load_tanzil_coordinates(meta_path)

    # Create QUL tafsir data with some singletons and some 2-ayah groups
    qul_data: dict[str, Any] = {}
    vkeys = list(coords.ayah_ids.keys())
    idx = 0
    while idx < len(vkeys):
        vk = vkeys[idx]
        s_num, a_num = map(int, vk.split(":"))
        # Group with next verse if same surah and even index
        if idx + 1 < len(vkeys):
            next_vk = vkeys[idx + 1]
            next_s, next_a = map(int, next_vk.split(":"))
            if next_s == s_num and next_a == a_num + 1 and idx % 4 == 0:
                qul_data[vk] = {
                    "text": f"<p>Tafsir for {vk}-{next_vk}</p>",
                    "ayah_keys": [vk, next_vk],
                }
                qul_data[next_vk] = vk
                idx += 2
                continue
        qul_data[vk] = {"text": f"<p>Tafsir for {vk}</p>"}
        idx += 1
    if custom_entries:
        qul_data.update(custom_entries)


    from pipelines.builders.build_quran_tafsir import SUPPORTED_TAFSIR_PACKS
    source_rel = SUPPORTED_TAFSIR_PACKS[pack_id]["source_path"]
    zip_dest = raw_dir / source_rel
    zip_dest.parent.mkdir(parents=True, exist_ok=True)

    member_name = Path(source_rel).name.removesuffix(".zip")
    with zipfile.ZipFile(zip_dest, "w") as zf:
        zf.writestr(member_name, json.dumps(qul_data, indent=2).encode("utf-8"))

    registry_data = {
        source_rel: {
            "packIds": [pack_id],
            "sourceUrl": f"https://example.org/{source_rel}",
            "sourceVersion": "1.0",
        },
        "tanzil/quran-data-Tanzil.xml": {
            "packIds": [pack_id],
            "sourceUrl": "https://example.org/quran-data-Tanzil.xml",
            "sourceVersion": "1.0",
        },
    }
    reg_file = sources_dir / "registry.json"
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text(json.dumps(registry_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    author_lock(reg_file, root / "raw-inputs", sources_dir / "manifest.lock.json")

    lic_data = {
        "licenseSpdx": "LicenseRef-QUL-Unreviewed",
        "packId": pack_id,
        "primaryPath": source_rel,
        "reviewedBy": None,
        "status": "pending",
        "sources": [
            {
                "attribution": "Synthetic Tafsir",
                "name": "Synthetic Tafsir",
                "notice": "synthetic notice",
                "path": source_rel,
                "permittedUses": [],
                "rightsEvidence": ["https://example.org/rights"],
                "sourceUrl": f"https://example.org/{source_rel}",
                "sourceVersion": "1.0",
            },
            {
                "attribution": "Synthetic Quran Metadata",
                "name": "Synthetic Metadata",
                "notice": "synthetic notice",
                "path": "tanzil/quran-data-Tanzil.xml",
                "permittedUses": [],
                "rightsEvidence": ["https://example.org/rights"],
                "sourceUrl": "https://example.org/quran-data-Tanzil.xml",
                "sourceVersion": "1.0",
            },
        ],
    }
    lic_file = sources_dir / "licenses" / f"{pack_id}.json"
    lic_file.parent.mkdir(parents=True, exist_ok=True)
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return root, out_dir


def test_build_quran_tafsir_qul_success(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import main

    pack_id = "quran-tafsir-ar-ibn-kathir"
    sources_root, out_dir = _setup_qul_tafsir_pack_env(tmp_path, pack_id=pack_id)
    db_path = out_dir / f"{pack_id}.db"

    ret = main([
        "--sources-root", str(sources_root),
        "--output-dir", str(out_dir),
        "--pack", pack_id,
    ])
    assert ret == 0
    assert db_path.is_file()

    conn = _open_ro_db(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check;").fetchall()
        assert integrity == [("ok",)]
        fk_check = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert fk_check == []

        # Verify entries format and text
        entries = conn.execute(
            "SELECT id, group_key, text_format, text FROM tafsir_entries ORDER BY id ASC;"
        ).fetchall()
        assert len(entries) > 0
        for eid, gkey, fmt, txt in entries:
            assert fmt == "html"
            assert txt.startswith("<p>Tafsir for ")

        # Verify map covers exactly 6236 ayahs
        map_rows = conn.execute(
            "SELECT ayah_id, verse_key, entry_id FROM tafsir_ayah_map ORDER BY ayah_id ASC;"
        ).fetchall()
        assert len(map_rows) == 6236
        assert [r[0] for r in map_rows] == list(range(1, 6237))

        # Verify pack metadata
        meta = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
        assert meta["pack_id"] == pack_id
        assert meta["completeness"] == "complete"

        # Coverage report
        cov = dict(conn.execute("SELECT metric, count FROM coverage_report;").fetchall())
        assert cov["expected_total"] == 6236
        assert cov["ar_present"] == 6236
    finally:
        conn.close()


def test_build_quran_tafsir_qul_en_ibn_kathir_success(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import main

    pack_id = "quran-tafsir-en-ibn-kathir"
    sources_root, out_dir = _setup_qul_tafsir_pack_env(tmp_path, pack_id=pack_id, language="en")
    db_path = out_dir / f"{pack_id}.db"

    ret = main([
        "--sources-root", str(sources_root),
        "--output-dir", str(out_dir),
        "--pack", pack_id,
    ])
    assert ret == 0
    assert db_path.is_file()

    conn = _open_ro_db(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check;").fetchall()
        assert integrity == [("ok",)]
        fk_check = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert fk_check == []

        # Verify entries format and text
        entries = conn.execute(
            "SELECT id, group_key, text_format, text FROM tafsir_entries ORDER BY id ASC;"
        ).fetchall()
        assert len(entries) > 0
        for eid, gkey, fmt, txt in entries:
            assert fmt == "html"
            assert txt.startswith("<p>Tafsir for ")

        # Coverage report for English pack has en_present
        cov = dict(conn.execute("SELECT metric, count FROM coverage_report;").fetchall())
        assert cov["expected_total"] == 6236
        assert cov["en_present"] == 6236
        assert "ar_present" not in cov
    finally:
        conn.close()

def test_build_quran_tafsir_qurtubi_empty_markup_partial_coverage(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import main

    pack_id = "quran-tafsir-ar-qurtubi"
    empty_markup = "<div class=ar lang=ar></div>"
    custom_entries = {
        "34:54": {"text": empty_markup},
        "59:19": {"text": empty_markup},
    }
    sources_root, out_dir = _setup_qul_tafsir_pack_env(
        tmp_path, pack_id=pack_id, custom_entries=custom_entries
    )
    db_path = out_dir / f"{pack_id}.db"

    ret = main([
        "--sources-root", str(sources_root),
        "--output-dir", str(out_dir),
        "--pack", pack_id,
    ])
    assert ret == 0
    assert db_path.is_file()

    conn = _open_ro_db(db_path)
    try:
        integrity = conn.execute("PRAGMA integrity_check;").fetchall()
        assert integrity == [("ok",)]
        fk_check = conn.execute("PRAGMA foreign_key_check;").fetchall()
        assert fk_check == []

        # Check preserved empty markup and empty projection
        rows = dict(
            conn.execute(
                "SELECT group_key, (text || '|||' || text_search) FROM tafsir_entries WHERE group_key IN ('34:54', '59:19');"
            ).fetchall()
        )
        assert len(rows) == 2
        assert rows["34:54"] == f"{empty_markup}|||"
        assert rows["59:19"] == f"{empty_markup}|||"

        # Verify mapping covers all 6236 ayahs
        map_count = conn.execute("SELECT COUNT(*) FROM tafsir_ayah_map;").fetchone()[0]
        assert map_count == 6236

        # Verify mappings for 34:54 and 59:19 exist
        mapped_34_54 = conn.execute(
            "SELECT m.ayah_id, e.group_key, e.text FROM tafsir_ayah_map m "
            "JOIN tafsir_entries e ON m.entry_id = e.id WHERE m.verse_key = '34:54';"
        ).fetchone()
        assert mapped_34_54 is not None
        assert mapped_34_54[1] == "34:54"
        assert mapped_34_54[2] == empty_markup

        mapped_59_19 = conn.execute(
            "SELECT m.ayah_id, e.group_key, e.text FROM tafsir_ayah_map m "
            "JOIN tafsir_entries e ON m.entry_id = e.id WHERE m.verse_key = '59:19';"
        ).fetchone()
        assert mapped_59_19 is not None
        assert mapped_59_19[1] == "59:19"
        assert mapped_59_19[2] == empty_markup

        # Pack metadata must be completeness=partial
        meta = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
        assert meta["pack_id"] == pack_id
        assert meta["completeness"] == "partial"

        # Coverage report must have expected_total=6236 and ar_present=6234
        cov = dict(conn.execute("SELECT metric, count FROM coverage_report;").fetchall())
        assert cov["expected_total"] == 6236
        assert cov["ar_present"] == 6234
    finally:
        conn.close()


def test_build_grouped_tafsir_rows_empty_projection_still_rejected(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_tafsir import build_grouped_tafsir_rows

    p = tmp_path / "empty_markup_grouped.json"
    p.write_text(
        json.dumps([
            {
                "verse_keys": ["1:1"],
                "text": "<div class=ar lang=ar></div>",
                "format": "html",
            }
        ]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Projected search text is empty or blank"):
        build_grouped_tafsir_rows(p, {"1:1": 1}, language="ar")
