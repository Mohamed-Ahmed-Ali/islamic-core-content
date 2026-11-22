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
