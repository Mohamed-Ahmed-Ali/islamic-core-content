"""Contract and behavior tests for the Quran core pack builder."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pipelines.common.search_text import normalize_arabic_search
from tests.fixtures.tanzil import (
    SURAH_AYAH_COUNTS,
    SURAH_STARTS,
    build_synthetic_metadata_xml,
    build_synthetic_scripture_xml,
    global_id_to_sura_aya,
    setup_synthetic_pack_env,
)


def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def test_quran_core_builder_happy_path(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    src_root, out_dir = setup_synthetic_pack_env(tmp_path)
    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 0

    db_path = out_dir / "quran-core.db"
    assert db_path.is_file()

    conn = _open_ro_db(db_path)
    try:
        cur = conn.cursor()

        # PRAGMAs
        assert cur.execute("PRAGMA integrity_check;").fetchall() == [("ok",)]
        assert cur.execute("PRAGMA foreign_key_check;").fetchall() == []
        assert cur.execute("PRAGMA encoding;").fetchone()[0] == "UTF-8"
        assert cur.execute("PRAGMA page_size;").fetchone()[0] == 4096
        assert cur.execute("PRAGMA user_version;").fetchone()[0] == 1

        # pack_meta
        meta_rows = dict(cur.execute("SELECT key, value FROM pack_meta;").fetchall())
        expected_keys = {
            "pack_id", "pack_version", "schema_version", "source_name", "source_url",
            "source_version", "license_spdx", "license_ref", "build_input_sha256",
            "completeness", "fts5_enabled",
        }
        assert set(meta_rows.keys()) == expected_keys
        assert meta_rows["pack_id"] == "quran-core"
        assert meta_rows["pack_version"] == "0.1.0"
        assert meta_rows["schema_version"] == "1"
        assert meta_rows["source_name"] == "Tanzil Quran Text (Uthmani)"
        assert meta_rows["license_spdx"] == "LicenseRef-Tanzil-Quran-Text"
        assert meta_rows["license_ref"] == "sources/licenses/quran-core.json"
        assert meta_rows["completeness"] == "complete"
        assert meta_rows["fts5_enabled"] == "true"
        assert len(meta_rows["build_input_sha256"]) == 64

        # coverage_report
        cov_rows = dict(cur.execute("SELECT metric, count FROM coverage_report;").fetchall())
        assert cov_rows == {"expected_total": 6236, "ar_present": 6236}
        notes = cur.execute("SELECT notes FROM coverage_report;").fetchall()
        assert all(n[0] is None for n in notes)

        # surahs table: 114 rows
        surah_rows = cur.execute("SELECT * FROM surahs ORDER BY id ASC;").fetchall()
        assert len(surah_rows) == 114
        # Verify literal values on surah 1
        assert surah_rows[0] == (1, "سورة_1", "Surah_1", "Chapter_1", "Meccan", 1, 7, 1, 1)
        # Verify literal values on surah 2 (count 118, start 7 -> start_ayah_id 8)
        assert surah_rows[1] == (2, "سورة_2", "Surah_2", "Chapter_2", "Medinan", 2, 118, 11, 8)
        # Verify surah 114 (count 6, start 6230 -> start_ayah_id 6231)
        assert surah_rows[113] == (114, "سورة_114", "Surah_114", "Chapter_114", "Medinan", 114, 6, 1, 6231)

        # ayahs table: 6236 rows
        ayah_rows = cur.execute("SELECT * FROM ayahs ORDER BY id ASC;").fetchall()
        assert len(ayah_rows) == 6236

        # Check ayah 1 (1:1)
        # Columns: id, surah_id, ayah_number, verse_key, text_uthmani, text_simple_clean, text_ar_search, juz, hizb_quarter, manzil, ruku, page_madani, sajdah_type
        a1 = ayah_rows[0]
        assert a1[0] == 1
        assert a1[1] == 1
        assert a1[2] == 1
        assert a1[3] == "1:1"
        assert a1[4] == "نص_Uthmani_1_1"
        assert a1[5] == "نص_Simple Clean_1_1"
        assert a1[6] == normalize_arabic_search("نص_Simple Clean_1_1")
        assert a1[7] == 1   # juz
        assert a1[8] == 1   # hizb
        assert a1[9] == 1   # manzil
        assert a1[10] == 1  # ruku
        assert a1[11] == 1  # page

        # Check ayah 7 (1:7)
        a7 = ayah_rows[6]
        assert a7[0] == 7
        assert a7[3] == "1:7"

        # Check ayah 8 (2:1) - verify bismillah was ignored and not part of text
        a8 = ayah_rows[7]
        assert a8[0] == 8
        assert a8[1] == 2
        assert a8[2] == 1
        assert a8[3] == "2:1"
        assert a8[4] == "نص_Uthmani_2_1"

        # Check last ayah 6236 (114:6)
        a_last = ayah_rows[6235]
        assert a_last[0] == 6236
        assert a_last[1] == 114
        assert a_last[2] == 6
        assert a_last[3] == "114:6"

        # Sajdah verification: exact 15 verses have sajdah_type
        sajda_rows = cur.execute("SELECT id, sajdah_type FROM ayahs WHERE sajdah_type IS NOT NULL ORDER BY id ASC;").fetchall()
        assert len(sajda_rows) == 15
        expected_sajda_ids = [100 + i * 200 for i in range(15)]
        assert [r[0] for r in sajda_rows] == expected_sajda_ids
        for i, (aid, stype) in enumerate(sajda_rows):
            assert stype == ("recommended" if i % 2 == 0 else "obligatory")

        # Inclusive boundary assertion on neighboring IDs across juz transition
        b_rows = cur.execute(
            "SELECT id, juz, hizb_quarter, manzil, ruku, page_madani FROM ayahs WHERE id IN (200, 201) ORDER BY id;"
        ).fetchall()
        assert b_rows[0][0] == 200 and b_rows[0][1] == 1
        assert b_rows[1][0] == 201 and b_rows[1][1] == 2

        # Neighboring sajdah null check
        s_adj = cur.execute(
            "SELECT id, sajdah_type FROM ayahs WHERE id IN (99, 100, 101) ORDER BY id;"
        ).fetchall()
        assert s_adj == [(99, None), (100, "recommended"), (101, None)]

        # FTS table verification
        # Positive query
        search_term = normalize_arabic_search("نص_Simple Clean_1_1")
        res = cur.execute(
            "SELECT rowid FROM ayahs_fts WHERE ayahs_fts MATCH ?;",
            (f'"{search_term}"',),
        ).fetchall()
        assert res == [(1,)]

        # Negative query
        res_neg = cur.execute(
            "SELECT rowid FROM ayahs_fts WHERE ayahs_fts MATCH ?;",
            ('"zzzzmissing"',),
        ).fetchall()
        assert res_neg == []

    finally:
        conn.close()


def test_reverse_encounter_order(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    meta_rev = build_synthetic_metadata_xml(reverse_suras=True)
    uth_rev = build_synthetic_scripture_xml("Uthmani", reverse_encounter=True)
    clean_rev = build_synthetic_scripture_xml("Simple Clean", reverse_encounter=True)

    src_root, out_dir = setup_synthetic_pack_env(
        tmp_path,
        metadata_xml=meta_rev,
        uthmani_xml=uth_rev,
        clean_xml=clean_rev,
    )

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 0

    conn = _open_ro_db(out_dir / "quran-core.db")
    try:
        surah_ids = [r[0] for r in conn.execute("SELECT id FROM surahs ORDER BY rowid ASC;").fetchall()]
        assert surah_ids == list(range(1, 115))

        ayah_ids = [r[0] for r in conn.execute("SELECT id FROM ayahs ORDER BY rowid ASC;").fetchall()]
        assert ayah_ids == list(range(1, 6237))

        # Canonical verse keys and text values in reverse encounter order
        ayah_samples = conn.execute(
            "SELECT id, surah_id, ayah_number, verse_key, text_uthmani, text_simple_clean FROM ayahs WHERE id IN (1, 2, 7, 8, 6236) ORDER BY id ASC;"
        ).fetchall()
        assert ayah_samples == [
            (1, 1, 1, "1:1", "نص_Uthmani_1_1", "نص_Simple Clean_1_1"),
            (2, 1, 2, "1:2", "نص_Uthmani_1_2", "نص_Simple Clean_1_2"),
            (7, 1, 7, "1:7", "نص_Uthmani_1_7", "نص_Simple Clean_1_7"),
            (8, 2, 1, "2:1", "نص_Uthmani_2_1", "نص_Simple Clean_2_1"),
            (6236, 114, 6, "114:6", "نص_Uthmani_114_6", "نص_Simple Clean_114_6"),
        ]
    finally:
        conn.close()


def test_repeated_coordinate_replaces_missing(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    # Duplicate (1, 2) and drop (1, 3) in scripture XML
    uth_dup = build_synthetic_scripture_xml(
        "Uthmani",
        duplicate_coordinate=(1, 2),
        drop_coordinate=(1, 3),
    )
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_dup)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_incorrect_prefix_sum(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    # Surah 2 start should be 7, change to 8
    meta_bad = build_synthetic_metadata_xml(override_surah_attrs={2: {"start": "8"}})
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, metadata_xml=meta_bad)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_invalid_starting_boundary(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    # Juz 1 starts at global ID 2 instead of 1
    bad_juz = [2] + [1 + i * 200 for i in range(1, 30)]
    meta_bad = build_synthetic_metadata_xml(juz_starts=bad_juz)
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, metadata_xml=meta_bad)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_invalid_boundary_order(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    # Juz starts not strictly increasing (juz 3 starts at 100, juz 2 starts at 201)
    bad_juz = [1, 201, 100] + [1 + i * 200 for i in range(3, 30)]
    meta_bad = build_synthetic_metadata_xml(juz_starts=bad_juz)
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, metadata_xml=meta_bad)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_invalid_sajdah_type(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    bad_sajda = [(100 + i * 200, "invalid_type") for i in range(15)]
    meta_bad = build_synthetic_metadata_xml(sajda_entries=bad_sajda)
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, metadata_xml=meta_bad)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_dtd_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    dtd_decl = '<?xml version="1.0" encoding="utf-8" ?>\n<!DOCTYPE quran [<!ENTITY test "val">]>\n'
    uth_dtd = build_synthetic_scripture_xml("Uthmani", xml_declaration=dtd_decl)
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_dtd)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_utf8_fffd_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    uth_fffd = build_synthetic_scripture_xml("Uthmani").replace("نص_Uthmani_1_1", "نص_\ufffd_1_1")
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_fffd)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_blank_aya_text_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    uth_blank = build_synthetic_scripture_xml("Uthmani", blank_coordinate=(1, 1))
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_blank)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_output_refusal_preserves_incumbent(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    src_root, out_dir = setup_synthetic_pack_env(tmp_path)
    incumbent_file = out_dir / "quran-core.db"
    incumbent_file.write_text("INCUMBENT_DATA_DO_NOT_DELETE", encoding="utf-8")

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert incumbent_file.is_file()
    assert incumbent_file.read_text(encoding="utf-8") == "INCUMBENT_DATA_DO_NOT_DELETE"


def test_failure_leaves_no_new_db(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    bad_juz = [2] + [1 + i * 200 for i in range(1, 30)]
    meta_bad = build_synthetic_metadata_xml(juz_starts=bad_juz)
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, metadata_xml=meta_bad)

    target_db = out_dir / "quran-core.db"
    assert not target_db.exists()

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not target_db.exists()
    # Also verify no sidecars
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (out_dir / f"quran-core.db{suffix}").exists()


def test_scripture_extra_empty_sura_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    # Insert an empty sura 115 into scripture XML
    uth_base = build_synthetic_scripture_xml("Uthmani")
    uth_extra = uth_base.replace("</quran>", '\t<sura index="115" name="سورة_115">\n\t</sura>\n</quran>')
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_extra)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_scripture_missing_sura_name_attr_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    # Remove 'name' attribute from sura 1
    uth_base = build_synthetic_scripture_xml("Uthmani")
    uth_no_name = uth_base.replace('<sura index="1" name="سورة_1">', '<sura index="1">')
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_no_name)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_scripture_unexpected_child_element_under_aya_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    uth_base = build_synthetic_scripture_xml("Uthmani")
    uth_child = uth_base.replace(
        '<aya index="1" text="نص_Uthmani_1_1" />',
        '<aya index="1" text="نص_Uthmani_1_1"><unexpected_child /></aya>',
    )
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_child)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_scripture_stray_nonwhitespace_content_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    uth_base = build_synthetic_scripture_xml("Uthmani")
    uth_stray = uth_base.replace(
        '<aya index="1" text="نص_Uthmani_1_1" />',
        '<aya index="1" text="نص_Uthmani_1_1">stray text</aya>',
    )
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_stray)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_xml_declaration_competing_encoding_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    uth_base = build_synthetic_scripture_xml("Uthmani")
    uth_iso = uth_base.replace('encoding="utf-8"', 'encoding="iso-8859-1"')
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_iso)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_escaped_fffd_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from pipelines.builders.build_quran_core import main

    uth_base = build_synthetic_scripture_xml("Uthmani")
    # Escaped numeric character reference for U+FFFD
    uth_fffd = uth_base.replace("نص_Uthmani_1_1", "SECRET_VERSE_CONTENT_&#xFFFD;_SUFFIX")
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, uthmani_xml=uth_fffd)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()

    # Verify verse content is NOT included in stderr diagnostics
    captured = capsys.readouterr()
    assert "SECRET_VERSE_CONTENT" not in captured.err


def test_metadata_prefix_sum_overflow_rejection(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_core import main

    meta_bad = build_synthetic_metadata_xml(override_surah_attrs={1: {"ayas": "6237"}})
    src_root, out_dir = setup_synthetic_pack_env(tmp_path, metadata_xml=meta_bad)

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_pending_license_allows_empty_fields(tmp_path: Path) -> None:
    import json
    from pipelines.builders.build_quran_core import main

    src_root, out_dir = setup_synthetic_pack_env(tmp_path)
    lic_path = src_root / "sources" / "licenses" / "quran-core.json"
    lic_data = json.loads(lic_path.read_text(encoding="utf-8"))
    for src in lic_data["sources"]:
        src["notice"] = ""
        src["attribution"] = ""
        src["rightsEvidence"] = []
        src["permittedUses"] = []
    lic_path.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 0
    assert (out_dir / "quran-core.db").is_file()


def test_pending_license_rejects_http_evidence(tmp_path: Path) -> None:
    import json
    from pipelines.builders.build_quran_core import main

    src_root, out_dir = setup_synthetic_pack_env(tmp_path)
    lic_path = src_root / "sources" / "licenses" / "quran-core.json"
    lic_data = json.loads(lic_path.read_text(encoding="utf-8"))
    lic_data["sources"][0]["rightsEvidence"] = ["http://insecure.example.com"]
    lic_path.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


def test_pending_license_rejects_duplicate_unknown_permitted_uses(tmp_path: Path) -> None:
    import json
    from pipelines.builders.build_quran_core import main

    src_root, out_dir = setup_synthetic_pack_env(tmp_path)
    lic_path = src_root / "sources" / "licenses" / "quran-core.json"
    lic_data = json.loads(lic_path.read_text(encoding="utf-8"))
    lic_data["sources"][0]["permittedUses"] = ["unknown_use", "unknown_use"]
    lic_path.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert not (out_dir / "quran-core.db").exists()


@pytest.mark.parametrize("query_variant", ["folded", "vocalized"])
def test_quran_core_forgiving_search_regression(tmp_path: Path, query_variant: str) -> None:
    from pipelines.builders.build_quran_core import main
    from pipelines.common.search_text import arabic_match_literal

    source_phrase = "أَحْـمَدۚ عَلَىٰ صَلَاةٌ رَبِّ"
    uthmani_phrase = "نص_Uthmani_Distinct_1_1"
    clean_xml = build_synthetic_scripture_xml("Simple Clean").replace(
        'text="نص_Simple Clean_1_1"',
        f'text="{source_phrase}"',
    )
    uthmani_xml = build_synthetic_scripture_xml("Uthmani").replace(
        'text="نص_Uthmani_1_1"',
        f'text="{uthmani_phrase}"',
    )

    src_root, out_dir = setup_synthetic_pack_env(
        tmp_path, clean_xml=clean_xml, uthmani_xml=uthmani_xml
    )
    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 0
    db_path = out_dir / "quran-core.db"

    conn = _open_ro_db(db_path)
    try:
        cur = conn.cursor()
        # Verify display values byte equality
        row = cur.execute(
            "SELECT CAST(text_simple_clean AS BLOB), CAST(text_uthmani AS BLOB) FROM ayahs WHERE id = 1;"
        ).fetchone()
        assert row[0] == source_phrase.encode("utf-8")
        assert row[1] == uthmani_phrase.encode("utf-8")

        # Verify all four tokens in temporary fts5vocab
        cur.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, 'ayahs_fts', 'instance');")
        for expected_token in ["احمد", "علي", "صلاه", "رب"]:
            cnt = cur.execute(
                "SELECT count(*) FROM temp.vocab WHERE term = ? AND doc = 1 AND col = 'text_ar_search';",
                (expected_token,),
            ).fetchone()[0]
            assert cnt >= 1, f"Token {expected_token} missing from ayahs_fts vocab at doc 1"
        cur.execute("DROP TABLE temp.vocab;")

        # Query assertion
        if query_variant == "folded":
            q_lit = '"احمد علي صلاه رب"'
        else:
            q_lit = arabic_match_literal(source_phrase)
        assert q_lit is not None

        res = cur.execute("SELECT rowid FROM ayahs_fts WHERE ayahs_fts MATCH ?;", (q_lit,)).fetchall()
        assert res == [(1,)]

        # Negative control
        neg = cur.execute("SELECT rowid FROM ayahs_fts WHERE ayahs_fts MATCH ?;", ('"zzzzabsentphrase"',)).fetchall()
        assert neg == []
    finally:
        conn.close()
