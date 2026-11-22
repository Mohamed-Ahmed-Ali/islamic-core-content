"""Contract and behavior tests for the Quran scripts extended pack builder."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET

from pipelines.common.lock_inputs import author_lock
from tests.fixtures.tanzil import (
    build_synthetic_metadata_xml,
    build_synthetic_scripture_xml,
    global_id_to_sura_aya,
)


def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def _setup_extended_pack_env(
    root: Path,
    *,
    metadata_xml: str | None = None,
    uthmani_min_xml: str | None = None,
    simple_xml: str | None = None,
    simple_plain_xml: str | None = None,
    simple_min_xml: str | None = None,
) -> tuple[Path, Path]:
    raw_dir = root / "raw-inputs" / "tanzil"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = root / "sources"
    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "tanzil/quran-data-Tanzil.xml": metadata_xml or build_synthetic_metadata_xml(),
        "tanzil/quran-uthmani-min.xml": uthmani_min_xml or build_synthetic_scripture_xml("uthmani-min"),
        "tanzil/quran-simple.xml": simple_xml or build_synthetic_scripture_xml("simple"),
        "tanzil/quran-simple-plain.xml": simple_plain_xml or build_synthetic_scripture_xml("simple-plain"),
        "tanzil/quran-simple-min.xml": simple_min_xml or build_synthetic_scripture_xml("simple-min"),
    }
    for rel_path, content in files.items():
        (root / "raw-inputs" / rel_path).write_bytes(content.encode("utf-8"))

    registry_data = {
        p: {
            "packIds": ["quran-scripts-extended"],
            "sourceUrl": "https://example.org",
            "sourceVersion": "1.0",
        }
        for p in sorted(files)
    }
    reg_file = sources_dir / "registry.json"
    reg_file.parent.mkdir(parents=True, exist_ok=True)
    reg_file.write_text(json.dumps(registry_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    author_lock(reg_file, root / "raw-inputs", sources_dir / "manifest.lock.json")

    lic_data = {
        "licenseSpdx": "LicenseRef-Tanzil-Quran-Text",
        "packId": "quran-scripts-extended",
        "primaryPath": "tanzil/quran-uthmani-min.xml",
        "reviewedBy": None,
        "status": "pending",
        "sources": [
            {
                "attribution": "",
                "name": f"Source {p}",
                "notice": "",
                "path": p,
                "permittedUses": [],
                "rightsEvidence": [],
                "sourceUrl": "https://example.org",
                "sourceVersion": "1.0",
            }
            for p in sorted(files)
        ],
    }
    lic_file = sources_dir / "licenses" / "quran-scripts-extended.json"
    lic_file.parent.mkdir(parents=True, exist_ok=True)
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return root, out_dir


def test_quran_scripts_extended_builder_reordered_and_distinct_text(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_scripts_extended import main

    # Independently reorder each script source's XML encounter order
    # 1. uthmani-min: normal suras, normal ayas
    uth_min_xml = build_synthetic_scripture_xml("uthmani-min")
    # 2. simple: reverse suras, reverse ayas
    simple_xml = build_synthetic_scripture_xml("simple", reverse_encounter=True)
    # 3. simple-plain: odd suras then even suras, normal ayas (via stdlib ET)
    root_plain = ET.fromstring(build_synthetic_scripture_xml("simple-plain"))
    suras_plain = list(root_plain)
    root_plain[:] = [s for s in suras_plain if int(s.attrib["index"]) % 2 != 0] + [
        s for s in suras_plain if int(s.attrib["index"]) % 2 == 0
    ]
    simple_plain_xml = '<?xml version="1.0" encoding="utf-8" ?>\n' + ET.tostring(root_plain, encoding="unicode")
    # 4. simple-min: reverse suras, normal ayas (via stdlib ET)
    root_min = ET.fromstring(build_synthetic_scripture_xml("simple-min"))
    root_min[:] = list(root_min)[::-1]
    simple_min_xml = '<?xml version="1.0" encoding="utf-8" ?>\n' + ET.tostring(root_min, encoding="unicode")

    src_root, out_dir = _setup_extended_pack_env(
        tmp_path,
        uthmani_min_xml=uth_min_xml,
        simple_xml=simple_xml,
        simple_plain_xml=simple_plain_xml,
        simple_min_xml=simple_min_xml,
    )

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 0

    db_path = out_dir / "quran-scripts-extended.db"
    assert db_path.is_file()
    assert [p.name for p in out_dir.iterdir()] == ["quran-scripts-extended.db"]

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
        assert meta_rows["pack_id"] == "quran-scripts-extended"
        assert meta_rows["pack_version"] == "0.1.0"
        assert meta_rows["schema_version"] == "1"
        assert meta_rows["source_name"] == "Source tanzil/quran-uthmani-min.xml"
        assert meta_rows["source_url"] == "https://example.org"
        assert meta_rows["source_version"] == "1.0"
        assert meta_rows["license_spdx"] == "LicenseRef-Tanzil-Quran-Text"
        assert meta_rows["license_ref"] == "sources/licenses/quran-scripts-extended.json"
        assert meta_rows["completeness"] == "complete"
        assert meta_rows["fts5_enabled"] == "false"
        assert len(meta_rows["build_input_sha256"]) == 64

        # coverage_report
        cov_rows = dict(cur.execute("SELECT metric, count FROM coverage_report;").fetchall())
        assert cov_rows == {"expected_total": 6236, "ar_present": 6236}
        notes = cur.execute("SELECT notes FROM coverage_report;").fetchall()
        assert all(n[0] is None for n in notes)

        # No FTS tables
        fts_tables = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%';"
        ).fetchall()
        assert fts_tables == []

        # ayah_scripts: exactly 6236 rows in strictly ascending ayah_id order
        rows = cur.execute(
            "SELECT ayah_id, verse_key, text_uthmani_min, text_simple, text_simple_plain, text_simple_min "
            "FROM ayah_scripts ORDER BY ayah_id ASC;"
        ).fetchall()
        assert len(rows) == 6236

        # Check all 6236 rows: canonical IDs, keys, and distinctly preserved script texts
        for idx, (aid, vkey, t_uth_min, t_simp, t_plain, t_simp_min) in enumerate(rows, start=1):
            assert aid == idx
            s, a = global_id_to_sura_aya(aid)
            assert vkey == f"{s}:{a}"
            assert t_uth_min == f"نص_uthmani-min_{s}_{a}"
            assert t_simp == f"نص_simple_{s}_{a}"
            assert t_plain == f"نص_simple-plain_{s}_{a}"
            assert t_simp_min == f"نص_simple-min_{s}_{a}"
    finally:
        conn.close()


def test_duplicate_ayah_in_single_script_fails_cleanly(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_scripts_extended import main

    # Substitute duplicate for one ayah in only one script while dropping another coordinate (1:3)
    # so row count remains 6236, proving semantic duplicate validation rejects the file
    simple_with_dup = build_synthetic_scripture_xml(
        "simple",
        duplicate_coordinate=(1, 2),
        drop_coordinate=(1, 3),
    )

    src_root, out_dir = _setup_extended_pack_env(
        tmp_path,
        simple_xml=simple_with_dup,
    )

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1

    # Verify no DB file was created or left behind
    db_path = out_dir / "quran-scripts-extended.db"
    assert not db_path.exists()
    assert list(out_dir.iterdir()) == []


def test_cli_misuse_returns_code_2() -> None:
    from pipelines.builders.build_quran_scripts_extended import main

    assert main([]) == 2
    assert main(["--sources-root", "only_one"]) == 2
    assert main(["--output-dir", "only_one"]) == 2


def test_output_refusal_when_db_already_exists(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_scripts_extended import main

    src_root, out_dir = _setup_extended_pack_env(tmp_path)
    existing_db = out_dir / "quran-scripts-extended.db"
    existing_db.write_bytes(b"EXISTING_DATA")

    rc = main(["--sources-root", str(src_root), "--output-dir", str(out_dir)])
    assert rc == 1
    assert existing_db.read_bytes() == b"EXISTING_DATA"
