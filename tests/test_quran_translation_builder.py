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

@pytest.fixture(autouse=True)
def _mock_verify_runtime_if_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from pipelines.toolchain.check_runtime import verify_runtime
        verify_runtime()
    except Exception:
        monkeypatch.setattr(
            "pipelines.builders.build_quran_translation.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
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


def _build_synthetic_metadata_xml_with_7_143() -> str:
    import tests.fixtures.tanzil as tanzil_mod
    orig_counts = tanzil_mod.SURAH_AYAH_COUNTS
    orig_starts = tanzil_mod.SURAH_STARTS
    counts = list(orig_counts)
    counts[6] += 88  # surah 7: 55 + 88 = 143
    counts[7] -= 44  # surah 8: 55 - 44 = 11
    counts[8] -= 44  # surah 9: 55 - 44 = 11
    try:
        tanzil_mod.SURAH_AYAH_COUNTS = tuple(counts)
        tanzil_mod.SURAH_STARTS = []
        acc = 0
        for c in counts:
            tanzil_mod.SURAH_STARTS.append(acc)
            acc += c
        return build_synthetic_metadata_xml(surah_counts=tuple(counts))
    finally:
        tanzil_mod.SURAH_AYAH_COUNTS = orig_counts
        tanzil_mod.SURAH_STARTS = orig_starts


def _setup_sahih_pack_env(
    root: Path,
    *,
    sahih_json: dict | None = None,
    metadata_xml: str | None = None,
) -> tuple[Path, Path]:
    import zipfile

    raw_dir = root / "raw-inputs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = root / "sources"
    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    meta_content = metadata_xml or _build_synthetic_metadata_xml_with_7_143()
    meta_path = raw_dir / "tanzil" / "quran-data-Tanzil.xml"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_bytes(meta_content.encode("utf-8"))
    if sahih_json is None:
        coords = load_tanzil_coordinates(meta_path)
        sahih_json = {}
        fn_counter = 1
        for s in coords.surahs:
            for a in range(1, s.ayah_count + 1):
                vkey = f"{s.id}:{a}"
                if vkey == "7:143":
                    sahih_json[vkey] = {
                        "t": f'Translation of {vkey} with tag <sup foot_note="226798">1</sup>.',
                        "f": {"226798": ""},
                    }
                elif vkey == "1:1":
                    sahih_json[vkey] = {
                        "t": 'Translation of 1:1 with marker <sup foot_note="1">888</sup>.',
                        "f": {"1": "Footnote note 1"},
                    }
                else:
                    sahih_json[vkey] = {
                        "t": f'Translation of {vkey} with tag <sup foot_note="{fn_counter}">{fn_counter}</sup>.',
                        "f": {str(fn_counter): f"Footnote note {fn_counter}"},
                    }
                fn_counter += 1
    zip_dest = raw_dir / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    zip_dest.parent.mkdir(parents=True, exist_ok=True)
    member_name = "en-sahih-international-with-footnote-tags.json"
    with zipfile.ZipFile(zip_dest, "w") as zf:
        zf.writestr(member_name, json.dumps(sahih_json, indent=2).encode("utf-8"))

    source_rel = "qul/translation/en-sahih-international-with-footnote-tags.json.zip"
    registry_data = {
        source_rel: {
            "packIds": ["quran-tr-en-sahih"],
            "sourceUrl": f"https://example.org/{source_rel}",
            "sourceVersion": "1.0",
        },
        "tanzil/quran-data-Tanzil.xml": {
            "packIds": ["quran-tr-en-sahih"],
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
        "packId": "quran-tr-en-sahih",
        "primaryPath": source_rel,
        "reviewedBy": None,
        "status": "pending",
        "sources": [
            {
                "attribution": "Synthetic Saheeh Translation",
                "name": "Synthetic Saheeh",
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
    lic_file = sources_dir / "licenses" / "quran-tr-en-sahih.json"
    lic_file.parent.mkdir(parents=True, exist_ok=True)
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return root, out_dir


def test_builder_cli_pack_arg(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_translation import main

    assert main(["--pack", "unknown"]) == 2
    assert main(["--sources-root", str(tmp_path), "--output-dir", str(tmp_path), "--pack", "unknown"]) == 2


def test_build_quran_translation_sahih_success(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_translation import build_quran_translation, main

    sources_root, out_dir = _setup_sahih_pack_env(tmp_path)
    db_path = out_dir / "quran-tr-en-sahih.db"

    build_quran_translation(sources_root, out_dir, pack_id="quran-tr-en-sahih")
    assert db_path.is_file()
    assert not (out_dir / "quran-tr-en-pickthall.db").exists()

    conn = _open_ro_db(db_path)
    try:
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

        rows = conn.execute(
            "SELECT ayah_id, surah_id, ayah_number, verse_key, text, text_search FROM translations ORDER BY ayah_id ASC;"
        ).fetchall()
        assert len(rows) == 6236
        assert [r[0] for r in rows] == list(range(1, 6237))

        # Preserved text containing footnote tags
        assert rows[0][:5] == (1, 1, 1, "1:1", 'Translation of 1:1 with marker <sup foot_note="1">888</sup>.')
        assert rows[-1][:5] == (6236, 114, 6, "114:6", 'Translation of 114:6 with tag <sup foot_note="6236">6236</sup>.')

        # text_search projection omits <sup> marker numbers
        assert rows[0][5] == "Translation of 1:1 with marker ."
        assert rows[-1][5] == "Translation of 114:6 with tag ."

        # translation_footnotes table check
        fn_rows = conn.execute(
            "SELECT ayah_id, footnote_id, text FROM translation_footnotes ORDER BY ayah_id ASC, footnote_id ASC;"
        ).fetchall()
        assert len(fn_rows) == 6236
        # Verse 7:143 footnote 226798 is preserved as empty string
        aid_7_143 = conn.execute("SELECT ayah_id FROM translations WHERE verse_key = '7:143';").fetchone()[0]
        fn_7_143 = conn.execute(
            "SELECT footnote_id, text FROM translation_footnotes WHERE ayah_id = ?;", (aid_7_143,)
        ).fetchall()
        assert fn_7_143 == [(226798, "")]

        # FTS query matches text_search and does not rely on superscript marker numbers
        fts_match = conn.execute(
            "SELECT rowid FROM translations_fts WHERE translations_fts MATCH ? AND rowid = 1;",
            ('"Translation of 1:1"',),
        ).fetchall()
        assert fts_match == [(1,)]

        # Superscript marker number is not in translations_fts text_search
        fts_marker_match = conn.execute(
            "SELECT rowid FROM translations_fts WHERE translations_fts MATCH '888';",
        ).fetchall()
        assert fts_marker_match == []
        # Pack metadata
        meta = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
        assert meta["pack_id"] == "quran-tr-en-sahih"
        assert meta["pack_version"] == "0.1.0"
        assert meta["schema_version"] == "1"
        assert meta["fts5_enabled"] == "true"
        assert meta["completeness"] == "complete"
        assert meta["license_spdx"] == "LicenseRef-QUL-Unreviewed"
        assert meta["license_ref"] == "sources/licenses/quran-tr-en-sahih.json"

        # Coverage report
        cov = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT metric, count, notes FROM coverage_report;").fetchall()}
        assert cov["expected_total"] == (6236, None)
        assert cov["en_present"] == (6236, None)
        assert cov["en_missing"] == (0, None)
        assert cov["source_blank_footnotes"] == (1, "7:143:226798")
    finally:
        conn.close()

    # CLI refusal to overwrite
    ret = main([
        "--sources-root", str(sources_root),
        "--output-dir", str(out_dir),
        "--pack", "quran-tr-en-sahih",
    ])
    assert ret == 1


@pytest.mark.parametrize(
    "mutation_type",
    [
        "missing_key",
        "extra_key",
        "bad_value_type",
        "missing_t",
        "missing_f",
        "extra_field",
        "empty_t",
        "whitespace_t",
        "non_string_t",
        "non_dict_f",
        "bad_f_key_zero",
        "bad_f_key_leading_zero",
        "bad_f_key_negative",
        "bad_f_key_alpha",
        "bad_f_val_type",
    ],
)
def test_build_quran_translation_sahih_malformed_payload(tmp_path: Path, mutation_type: str) -> None:
    from pipelines.builders.build_quran_translation import build_quran_translation

    meta_content = _build_synthetic_metadata_xml_with_7_143()
    meta_path = tmp_path / "metadata.xml"
    meta_path.write_bytes(meta_content.encode("utf-8"))
    coords = load_tanzil_coordinates(meta_path)

    sahih_json: dict = {}
    for s in coords.surahs:
        for a in range(1, s.ayah_count + 1):
            vkey = f"{s.id}:{a}"
            sahih_json[vkey] = {
                "t": f'Translation of {vkey} <sup foot_note="1">1</sup>',
                "f": {"1": "note 1"},
            }
    if mutation_type == "missing_key":
        del sahih_json["1:1"]
    elif mutation_type == "extra_key":
        sahih_json["999:1"] = {"t": "extra", "f": {}}
    elif mutation_type == "bad_value_type":
        sahih_json["1:1"] = "not a dict"
    elif mutation_type == "missing_t":
        sahih_json["1:1"] = {"f": {}}
    elif mutation_type == "missing_f":
        sahih_json["1:1"] = {"t": "text"}
    elif mutation_type == "extra_field":
        sahih_json["1:1"] = {"t": "text", "f": {}, "extra": 1}
    elif mutation_type == "empty_t":
        sahih_json["1:1"] = {"t": "", "f": {}}
    elif mutation_type == "whitespace_t":
        sahih_json["1:1"] = {"t": "   ", "f": {}}
    elif mutation_type == "non_string_t":
        sahih_json["1:1"] = {"t": 12345, "f": {}}
    elif mutation_type == "non_dict_f":
        sahih_json["1:1"] = {"t": "text", "f": ["not", "dict"]}
    elif mutation_type == "bad_f_key_zero":
        sahih_json["1:1"] = {"t": "text", "f": {"0": "note"}}
    elif mutation_type == "bad_f_key_leading_zero":
        sahih_json["1:1"] = {"t": "text", "f": {"01": "note"}}
    elif mutation_type == "bad_f_key_negative":
        sahih_json["1:1"] = {"t": "text", "f": {"-1": "note"}}
    elif mutation_type == "bad_f_key_alpha":
        sahih_json["1:1"] = {"t": "text", "f": {"footnote_one": "note"}}
    elif mutation_type == "bad_f_val_type":
        sahih_json["1:1"] = {"t": "text", "f": {"1": 123}}

    env_root = tmp_path / f"env_{mutation_type}"
    sources_root, out_dir = _setup_sahih_pack_env(env_root, sahih_json=sahih_json)

    with pytest.raises(ValueError):
        build_quran_translation(sources_root, out_dir, pack_id="quran-tr-en-sahih")


def test_build_quran_translation_sahih_blank_footnote_rejections(tmp_path: Path) -> None:
    import zipfile
    from pipelines.builders.build_quran_translation import build_quran_translation

    # 1. Blank footnote at 1:1 (non-exempt)
    env1 = tmp_path / "env_blank_1_1"
    s1, o1 = _setup_sahih_pack_env(env1)
    z1 = s1 / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z1, "r") as zf:
        d1 = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))
    d1["1:1"]["f"]["1"] = ""
    with zipfile.ZipFile(z1, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(d1, indent=2).encode("utf-8"))
    author_lock(s1 / "sources" / "registry.json", s1 / "raw-inputs", s1 / "sources" / "manifest.lock.json")
    with pytest.raises(ValueError, match="blank"):
        build_quran_translation(s1, o1, pack_id="quran-tr-en-sahih")

    # 2. Blank footnote at 7:143 but with different footnote id (non-exempt)
    env2 = tmp_path / "env_blank_7_143_wrong_id"
    s2, o2 = _setup_sahih_pack_env(env2)
    z2 = s2 / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z2, "r") as zf:
        d2 = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))
    d2["7:143"] = {
        "t": 'Translation of 7:143 with tag <sup foot_note="999">1</sup>.',
        "f": {"999": ""},
    }
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(d2, indent=2).encode("utf-8"))
    author_lock(s2 / "sources" / "registry.json", s2 / "raw-inputs", s2 / "sources" / "manifest.lock.json")
    with pytest.raises(ValueError, match="blank"):
        build_quran_translation(s2, o2, pack_id="quran-tr-en-sahih")


def test_build_quran_translation_sahih_orphan_and_lost_footnotes_rejected(tmp_path: Path) -> None:
    import zipfile
    from pipelines.builders.build_quran_translation import build_quran_translation

    # 1. Orphan reference in t (missing in f)
    env_orphan = tmp_path / "env_orphan"
    s1, o1 = _setup_sahih_pack_env(env_orphan)
    z1 = s1 / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z1, "r") as zf:
        d1 = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))
    d1["1:1"]["t"] = 'Translation of 1:1 with tag <sup foot_note="1">1</sup> and <sup foot_note="999">2</sup>.'
    with zipfile.ZipFile(z1, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(d1, indent=2).encode("utf-8"))
    author_lock(s1 / "sources" / "registry.json", s1 / "raw-inputs", s1 / "sources" / "manifest.lock.json")
    with pytest.raises(ValueError, match="mismatch|orphan|reference"):
        build_quran_translation(s1, o1, pack_id="quran-tr-en-sahih")

    # 2. Lost footnote in f (missing in t)
    env_lost = tmp_path / "env_lost"
    s2, o2 = _setup_sahih_pack_env(env_lost)
    z2 = s2 / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z2, "r") as zf:
        d2 = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))
    d2["1:1"]["f"]["999"] = "lost footnote body"
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(d2, indent=2).encode("utf-8"))
    author_lock(s2 / "sources" / "registry.json", s2 / "raw-inputs", s2 / "sources" / "manifest.lock.json")
    with pytest.raises(ValueError, match="mismatch|orphan|reference"):
        build_quran_translation(s2, o2, pack_id="quran-tr-en-sahih")

    # 3. Duplicate footnote reference in t
    env_dup = tmp_path / "env_dup"
    s3, o3 = _setup_sahih_pack_env(env_dup)
    z3 = s3 / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z3, "r") as zf:
        d3 = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))
    d3["1:1"]["t"] = 'Translation of 1:1 with tag <sup foot_note="1">1</sup> and <sup foot_note="1">2</sup>.'
    with zipfile.ZipFile(z3, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(d3, indent=2).encode("utf-8"))
    author_lock(s3 / "sources" / "registry.json", s3 / "raw-inputs", s3 / "sources" / "manifest.lock.json")
    with pytest.raises(ValueError, match="duplicate|reference"):
        build_quran_translation(s3, o3, pack_id="quran-tr-en-sahih")

def test_build_quran_translation_sahih_footnote_pk_order_and_truthful_coverage(tmp_path: Path) -> None:
    import zipfile
    from pipelines.builders.build_quran_translation import build_quran_translation

    env = tmp_path / "env_pk_order"
    sources_root, out_dir = _setup_sahih_pack_env(env)
    z_path = sources_root / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z_path, "r") as zf:
        data = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))

    # Verse 1:1 has footnote 20 before footnote 10 in verse marker display order
    data["1:1"]["t"] = 'Translation of 1:1 <sup foot_note="20">2</sup> and <sup foot_note="10">1</sup>.'
    data["1:1"]["f"] = {"20": "Note twenty", "10": "Note ten"}

    # Verse 7:143 has non-blank body (sole pinned blank absent)
    data["7:143"]["t"] = 'Translation of 7:143 <sup foot_note="226798">1</sup>.'
    data["7:143"]["f"] = {"226798": "Non-blank body"}

    with zipfile.ZipFile(z_path, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(data, indent=2).encode("utf-8"))
    author_lock(sources_root / "sources" / "registry.json", sources_root / "raw-inputs", sources_root / "sources" / "manifest.lock.json")

    build_quran_translation(sources_root, out_dir, pack_id="quran-tr-en-sahih")
    db_path = out_dir / "quran-tr-en-sahih.db"
    conn = _open_ro_db(db_path)
    try:
        # 1. Verse display marker order is unchanged in translations.text
        t_row = conn.execute("SELECT text FROM translations WHERE ayah_id=1;").fetchone()
        assert t_row[0] == 'Translation of 1:1 <sup foot_note="20">2</sup> and <sup foot_note="10">1</sup>.'

        # 2. Footnotes are inserted in primary-key integer order (ayah_id, footnote_id)
        fn_rows = conn.execute("SELECT footnote_id, text FROM translation_footnotes WHERE ayah_id=1;").fetchall()
        assert fn_rows == [(10, "Note ten"), (20, "Note twenty")]

        # 3. Truthful coverage: source_blank_footnotes is absent when sole blank is absent
        cov = dict(conn.execute("SELECT metric, count FROM coverage_report;").fetchall())
        assert "source_blank_footnotes" not in cov
    finally:
        conn.close()


def test_build_quran_translation_sahih_whitespace_footnote_rejected(tmp_path: Path) -> None:
    import zipfile
    from pipelines.builders.build_quran_translation import build_quran_translation

    env = tmp_path / "env_whitespace_fn"
    sources_root, out_dir = _setup_sahih_pack_env(env)
    z_path = sources_root / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z_path, "r") as zf:
        data = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))
    data["7:143"]["f"]["226798"] = "   "
    with zipfile.ZipFile(z_path, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(data, indent=2).encode("utf-8"))
    author_lock(sources_root / "sources" / "registry.json", sources_root / "raw-inputs", sources_root / "sources" / "manifest.lock.json")

    with pytest.raises(ValueError, match="whitespace"):
        build_quran_translation(sources_root, out_dir, pack_id="quran-tr-en-sahih")


def test_build_quran_translation_sahih_newline_footnote_id_rejected(tmp_path: Path) -> None:
    import zipfile
    from pipelines.builders.build_quran_translation import build_quran_translation

    # 1. Newline in <sup> tag foot_note attribute
    env1 = tmp_path / "env_newline_tag"
    s1, o1 = _setup_sahih_pack_env(env1)
    z1 = s1 / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z1, "r") as zf:
        d1 = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))
    d1["1:1"]["t"] = 'Translation of 1:1 <sup foot_note="1\n">1</sup>.'
    d1["1:1"]["f"] = {"1\n": "Note 1"}
    with zipfile.ZipFile(z1, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(d1, indent=2).encode("utf-8"))
    author_lock(s1 / "sources" / "registry.json", s1 / "raw-inputs", s1 / "sources" / "manifest.lock.json")

    with pytest.raises(ValueError, match="Invalid footnote ID"):
        build_quran_translation(s1, o1, pack_id="quran-tr-en-sahih")

    # 2. Newline in footnote dictionary key
    env2 = tmp_path / "env_newline_key"
    s2, o2 = _setup_sahih_pack_env(env2)
    z2 = s2 / "raw-inputs" / "qul" / "translation" / "en-sahih-international-with-footnote-tags.json.zip"
    with zipfile.ZipFile(z2, "r") as zf:
        d2 = json.loads(zf.read("en-sahih-international-with-footnote-tags.json"))
    d2["1:1"]["t"] = 'Translation of 1:1 <sup foot_note="1">1</sup>.'
    d2["1:1"]["f"] = {"1\n": "Note 1"}
    with zipfile.ZipFile(z2, "w") as zf:
        zf.writestr("en-sahih-international-with-footnote-tags.json", json.dumps(d2, indent=2).encode("utf-8"))
    author_lock(s2 / "sources" / "registry.json", s2 / "raw-inputs", s2 / "sources" / "manifest.lock.json")

    with pytest.raises(ValueError, match="positive canonical decimal"):
        build_quran_translation(s2, o2, pack_id="quran-tr-en-sahih")
