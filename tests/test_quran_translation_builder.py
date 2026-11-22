"""Contract and behavior tests for the Quran Pickthall translation pack builder."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import pytest

from pipelines.common.lock_inputs import author_lock
from pipelines.common.tanzil_coordinates import (
    load_tanzil_coordinates,
    load_tanzil_text,
)
from tests.fixtures.tanzil import (
    SURAH_AYAH_COUNTS,
    build_synthetic_metadata_xml,
    build_synthetic_scripture_xml,
)

PICKTHALL_NOTICE_DEFAULT = (
    "<!--\r\n"
    "# ----\n"
    "#  ID: en.pickthall\r\n"
    "# ----\n"
    "-->\r\n"
)


def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def build_synthetic_pickthall_xml(
    *,
    xml_declaration: str = '<?xml version="1.0" encoding="utf-8" ?>\r\n',
    leading_comment: str = PICKTHALL_NOTICE_DEFAULT,
    surah_counts: tuple[int, ...] = SURAH_AYAH_COUNTS,
    empty_sura_name: bool = True,
    extra_body: str = "",
) -> str:
    """Build synthetic Pickthall translation XML reusing shared scripture builder."""
    xml = build_synthetic_scripture_xml(
        "en.pickthall",
        xml_declaration=xml_declaration,
        leading_comment=leading_comment,
        surah_counts=surah_counts,
    )
    if empty_sura_name:
        xml = re.sub(r'name="[^"]*"', 'name=""', xml)
    # Transform first two synthetic attributes for whitespace, "/& and FTS probes
    xml = xml.replace('text="نص_en.pickthall_1_1"', 'text="  &quot;Lord &amp; God&quot;   "', 1)
    xml = xml.replace('text="نص_en.pickthall_1_2"', 'text="Lord of the Worlds"', 1)
    if extra_body:
        xml = xml.replace("</quran>", f"{extra_body}</quran>")
    return xml


def _setup_pickthall_pack_env(
    root: Path,
    *,
    metadata_xml: str | None = None,
    pickthall_xml: str | None = None,
) -> tuple[Path, Path]:
    raw_dir = root / "raw-inputs" / "tanzil"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = root / "sources"
    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    meta_content = metadata_xml or build_synthetic_metadata_xml()
    pick_content = pickthall_xml or build_synthetic_pickthall_xml()

    meta_path = raw_dir / "quran-data-Tanzil.xml"
    pick_path = raw_dir / "en.pickthall.xml"

    meta_path.write_bytes(meta_content.encode("utf-8"))
    pick_path.write_bytes(pick_content.encode("utf-8"))

    registry_data = {
        "tanzil/en.pickthall.xml": {
            "packIds": ["quran-tr-en-pickthall"],
            "sourceUrl": "https://example.org/en.pickthall.xml",
            "sourceVersion": "1.0",
        },
        "tanzil/quran-data-Tanzil.xml": {
            "packIds": ["quran-tr-en-pickthall"],
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
        "packId": "quran-tr-en-pickthall",
        "primaryPath": "tanzil/en.pickthall.xml",
        "reviewedBy": None,
        "status": "pending",
        "sources": [
            {
                "attribution": "Synthetic Pickthall Translation",
                "name": "Synthetic Pickthall",
                "notice": "synthetic notice",
                "path": "tanzil/en.pickthall.xml",
                "permittedUses": [],
                "rightsEvidence": [
                    "https://example.org/rights",
                ],
                "sourceUrl": "https://example.org/en.pickthall.xml",
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
    lic_file = sources_dir / "licenses" / "quran-tr-en-pickthall.json"
    lic_file.parent.mkdir(parents=True, exist_ok=True)
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return root, out_dir


def test_build_quran_translation_success(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_translation import build_quran_translation, main

    sources_root, out_dir = _setup_pickthall_pack_env(tmp_path)
    db_path = out_dir / "quran-tr-en-pickthall.db"

    build_quran_translation(sources_root, out_dir)
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

        # Check translations table rows and ascending PK order
        rows = conn.execute(
            "SELECT ayah_id, surah_id, ayah_number, verse_key, text FROM translations ORDER BY ayah_id ASC;"
        ).fetchall()
        assert len(rows) == 6236
        assert [r[0] for r in rows] == list(range(1, 6237))

        # Check exact preserved tuples without plaintext transformation
        assert rows[0] == (1, 1, 1, "1:1", '  "Lord & God"   ')
        assert rows[1] == (2, 1, 2, "1:2", "Lord of the Worlds")
        assert rows[-1] == (6236, 114, 6, "114:6", "نص_en.pickthall_114_6")

        # Check bound FTS queries using parameterized SQL
        fts_match = conn.execute(
            "SELECT rowid FROM translations_fts WHERE translations_fts MATCH ? AND rowid = 2;",
            ('"Lord of the Worlds"',),
        ).fetchall()
        assert fts_match == [(2,)]

        fts_none = conn.execute(
            "SELECT rowid FROM translations_fts WHERE translations_fts MATCH ? AND rowid = 2;",
            ('"zzzzfixturemissing"',),
        ).fetchall()
        assert fts_none == []

        # FTS integrity check on a temporary copy
        temp_copy = tmp_path / "copy.db"
        temp_copy.write_bytes(db_path.read_bytes())
        copy_conn = sqlite3.connect(temp_copy)
        try:
            copy_conn.execute("INSERT INTO translations_fts(translations_fts, rank) VALUES('integrity-check', 1);")
        finally:
            copy_conn.close()
            temp_copy.unlink(missing_ok=True)

        # Pack metadata
        meta = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
        assert meta["pack_id"] == "quran-tr-en-pickthall"
        assert meta["pack_version"] == "0.1.0"
        assert meta["schema_version"] == "1"
        assert meta["fts5_enabled"] == "true"
        assert meta["completeness"] == "complete"
        assert meta["license_spdx"] == "LicenseRef-Tanzil-Noncommercial-Edition"
        assert meta["license_ref"] == "sources/licenses/quran-tr-en-pickthall.json"
        assert meta["source_name"] == "Synthetic Pickthall"

        # Coverage report
        cov = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT metric, count, notes FROM coverage_report;").fetchall()}
        assert cov["expected_total"] == (6236, None)
        assert cov["en_present"] == (6236, None)
        assert cov["en_missing"] == (0, None)

    finally:
        conn.close()

    # Refuse overwriting existing output
    ret = main(["--sources-root", str(sources_root), "--output-dir", str(out_dir)])
    assert ret == 1
    assert db_path.is_file()


def test_builder_cli_arg_handling() -> None:
    from pipelines.builders.build_quran_translation import main

    assert main([]) == 2
    assert main(["--invalid-arg"]) == 2
    assert main(["--sources-root", "/tmp/nonexistent"]) == 2


_MALFORMED_NOTICE_CASES = [
    ("missing", build_synthetic_pickthall_xml(leading_comment=""), "tanzil/en.pickthall.xml"),
    (
        "wrong_edition",
        build_synthetic_pickthall_xml(
            leading_comment=PICKTHALL_NOTICE_DEFAULT.replace("ID: en.pickthall", "ID: ar.jalalayn")
        ),
        "tanzil/en.pickthall.xml",
    ),
    (
        "nested",
        build_synthetic_pickthall_xml(
            leading_comment=PICKTHALL_NOTICE_DEFAULT.replace("# ----", "# <!-- nested")
        ),
        "tanzil/en.pickthall.xml",
    ),
    (
        "unterminated",
        build_synthetic_pickthall_xml(
            leading_comment=PICKTHALL_NOTICE_DEFAULT.replace("-->\r\n", "")
        ),
        "tanzil/en.pickthall.xml",
    ),
    (
        "shifted_pi",
        build_synthetic_pickthall_xml(
            xml_declaration='<?xml version="1.0" encoding="utf-8" ?>\r\n<?target val?>\r\n'
        ),
        "tanzil/en.pickthall.xml",
    ),
    (
        "shifted_comment",
        build_synthetic_pickthall_xml(
            leading_comment='<!-- ordinary comment -->\r\n' + PICKTHALL_NOTICE_DEFAULT
        ),
        "tanzil/en.pickthall.xml",
    ),
    (
        "decl_lookalike_pi",
        build_synthetic_pickthall_xml(
            xml_declaration='<?xml-stylesheet type="text/xsl" href="s.xsl" ?>\r\n'
        ),
        "tanzil/en.pickthall.xml",
    ),
    (
        "decl_lookalike_upper",
        build_synthetic_pickthall_xml(
            xml_declaration='<?XML version="1.0" encoding="utf-8" ?>\r\n'
        ),
        "tanzil/en.pickthall.xml",
    ),
    ("alias_uthmani", build_synthetic_pickthall_xml(), "tanzil/quran-uthmani.xml"),
    ("alias_suffix", build_synthetic_pickthall_xml(), "tanzil/en.pickthall.xml.alias"),
    ("alias_bare", build_synthetic_pickthall_xml(), "en.pickthall.xml"),
    (
        "non_xml_nul",
        build_synthetic_pickthall_xml(
            leading_comment=PICKTHALL_NOTICE_DEFAULT.replace(
                "#  ID: en.pickthall", "#  ID: en.pickthall\r\n#  Invalid: \x00"
            )
        ),
        "tanzil/en.pickthall.xml",
    ),
    (
        "non_xml_form_feed",
        build_synthetic_pickthall_xml(
            leading_comment=PICKTHALL_NOTICE_DEFAULT.replace(
                "#  ID: en.pickthall", "#  ID: en.pickthall\x0c"
            )
        ),
        "tanzil/en.pickthall.xml",
    ),
]


@pytest.mark.parametrize(
    "case_id, xml_override, locked_path_override",
    _MALFORMED_NOTICE_CASES,
    ids=[c[0] for c in _MALFORMED_NOTICE_CASES],
)
def test_parser_leading_notice_rejections_parametrized(
    tmp_path: Path,
    case_id: str,
    xml_override: str,
    locked_path_override: str,
) -> None:
    meta_path = tmp_path / "metadata.xml"
    meta_path.write_text(build_synthetic_metadata_xml(), encoding="utf-8")
    coords = load_tanzil_coordinates(meta_path)

    script_path = tmp_path / "scripture.xml"
    script_path.write_bytes(xml_override.encode("utf-8"))

    with pytest.raises(ValueError):
        load_tanzil_text(script_path, coords, locked_path=locked_path_override)


def test_parser_second_invalid_comment_rejection(tmp_path: Path) -> None:
    meta_path = tmp_path / "metadata.xml"
    meta_path.write_text(build_synthetic_metadata_xml(), encoding="utf-8")
    coords = load_tanzil_coordinates(meta_path)

    script_path = tmp_path / "scripture.xml"
    xml_with_second_invalid_comment = build_synthetic_pickthall_xml(
        extra_body="\t<!-- invalid -- comment -->\r\n"
    )
    script_path.write_bytes(xml_with_second_invalid_comment.encode("utf-8"))
    with pytest.raises(ValueError):
        load_tanzil_text(script_path, coords, locked_path="tanzil/en.pickthall.xml")


def test_parser_malformed_body_rejection(tmp_path: Path) -> None:
    meta_path = tmp_path / "metadata.xml"
    meta_path.write_text(build_synthetic_metadata_xml(), encoding="utf-8")
    coords = load_tanzil_coordinates(meta_path)

    script_path = tmp_path / "scripture.xml"
    xml_malformed_body = build_synthetic_pickthall_xml(
        extra_body="\t<unclosed_tag>\r\n"
    )
    script_path.write_bytes(xml_malformed_body.encode("utf-8"))
    with pytest.raises(ValueError):
        load_tanzil_text(script_path, coords, locked_path="tanzil/en.pickthall.xml")
