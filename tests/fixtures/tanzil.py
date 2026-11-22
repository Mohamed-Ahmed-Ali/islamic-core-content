"""Synthetic Tanzil data fixtures and generators for isolated testing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pipelines.common.lock_inputs import author_lock

SURAH_AYAH_COUNTS: tuple[int, ...] = (7, 118, *([55] * 111), 6)
assert len(SURAH_AYAH_COUNTS) == 114
assert sum(SURAH_AYAH_COUNTS) == 6236

SURAH_STARTS: list[int] = []
_acc = 0
for _c in SURAH_AYAH_COUNTS:
    SURAH_STARTS.append(_acc)
    _acc += _c
assert len(SURAH_STARTS) == 114


def global_id_to_sura_aya(global_id: int) -> tuple[int, int]:
    """Convert 1-based global ayah ID (1..6236) to 1-based (sura, aya)."""
    if not (1 <= global_id <= 6236):
        raise ValueError(f"global_id out of range: {global_id}")
    for idx, (start, count) in enumerate(zip(SURAH_STARTS, SURAH_AYAH_COUNTS), start=1):
        if start < global_id <= start + count:
            return idx, global_id - start
    raise RuntimeError(f"Unreachable for global_id {global_id}")


def build_synthetic_metadata_xml(
    *,
    surah_counts: tuple[int, ...] = SURAH_AYAH_COUNTS,
    juz_starts: list[int] | None = None,
    hizb_starts: list[int] | None = None,
    manzil_starts: list[int] | None = None,
    ruku_starts: list[int] | None = None,
    page_starts: list[int] | None = None,
    sajda_entries: list[tuple[int, str]] | None = None,
    reverse_suras: bool = False,
    override_surah_attrs: dict[int, dict[str, str]] | None = None,
    xml_declaration: str = '<?xml version="1.0" encoding="utf-8" ?>\n',
) -> str:
    """Build synthetic quran-data-Tanzil.xml text."""
    if juz_starts is None:
        juz_starts = [1] + [1 + i * 200 for i in range(1, 30)]
    if hizb_starts is None:
        hizb_starts = [1] + [1 + i * 25 for i in range(1, 240)]
    if manzil_starts is None:
        manzil_starts = [1] + [1 + i * 800 for i in range(1, 7)]
    if ruku_starts is None:
        ruku_starts = [1] + [1 + i * 11 for i in range(1, 556)]
    if page_starts is None:
        page_starts = [1] + [1 + i * 10 for i in range(1, 604)]
    if sajda_entries is None:
        sajda_entries = [
            (100 + i * 200, "recommended" if i % 2 == 0 else "obligatory")
            for i in range(15)
        ]

    lines = [
        xml_declaration,
        '<quran type="metadata" version="1.0" copyright="(C) 2008-2009 Tanzil.info" license="cc-by">\n',
        '\t<suras alias="chapters">\n',
    ]

    starts: list[int] = []
    acc = 0
    for c in surah_counts:
        starts.append(acc)
        acc += c

    sura_indices = list(range(1, len(surah_counts) + 1))
    if reverse_suras:
        sura_indices.reverse()

    for idx in sura_indices:
        count = surah_counts[idx - 1]
        start_offset = starts[idx - 1]
        attrs = {
            "index": str(idx),
            "ayas": str(count),
            "start": str(start_offset),
            "name": f"سورة_{idx}",
            "tname": f"Surah_{idx}",
            "ename": f"Chapter_{idx}",
            "type": "Meccan" if idx % 2 == 1 else "Medinan",
            "order": str(idx),
            "rukus": str(max(1, count // 10)),
        }
        if override_surah_attrs and idx in override_surah_attrs:
            attrs.update(override_surah_attrs[idx])

        attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        lines.append(f"\t\t<sura {attr_str} />\n")

    lines.append("\t</suras>\n")

    def _render_section(tag_plural: str, tag_singular: str, alias: str, start_ids: list[int]) -> None:
        lines.append(f'\t<{tag_plural} alias="{alias}">\n')
        for i, gid in enumerate(start_ids, start=1):
            s, a = global_id_to_sura_aya(gid)
            lines.append(f'\t\t<{tag_singular} index="{i}" sura="{s}" aya="{a}" />\n')
        lines.append(f"\t</{tag_plural}>\n")

    _render_section("juzs", "juz", "parts", juz_starts)
    _render_section("hizbs", "quarter", "groups", hizb_starts)
    _render_section("manzils", "manzil", "stations", manzil_starts)
    _render_section("rukus", "ruku", "sections", ruku_starts)

    # pages has empty/no alias in upstream metadata
    lines.append('\t<pages>\n')
    for i, gid in enumerate(page_starts, start=1):
        s, a = global_id_to_sura_aya(gid)
        lines.append(f'\t\t<page index="{i}" sura="{s}" aya="{a}" />\n')
    lines.append('\t</pages>\n')

    lines.append('\t<sajdas>\n')
    for i, (gid, s_type) in enumerate(sajda_entries, start=1):
        s, a = global_id_to_sura_aya(gid)
        lines.append(f'\t\t<sajda index="{i}" sura="{s}" aya="{a}" type="{s_type}" />\n')
    lines.append('\t</sajdas>\n')

    lines.append("</quran>\n")
    return "".join(lines)


def build_synthetic_scripture_xml(
    script_name: str,
    *,
    reverse_encounter: bool = False,
    duplicate_coordinate: tuple[int, int] | None = None,
    drop_coordinate: tuple[int, int] | None = None,
    blank_coordinate: tuple[int, int] | None = None,
    surah_counts: tuple[int, ...] = SURAH_AYAH_COUNTS,
    xml_declaration: str = '<?xml version="1.0" encoding="utf-8" ?>\n',
    leading_comment: str | None = None,
) -> str:
    """Build synthetic scripture XML (uthmani or simple-clean)."""
    if leading_comment is None:
        leading_comment = (
            f"<!--\n\n# PLEASE DO NOT REMOVE OR CHANGE THIS COPYRIGHT BLOCK\n"
            f"#====================================================================\n"
            f"#\n#  Tanzil Quran Text ({script_name}, Version 1.1)\n"
            f"#  Copyright (C) 2007-2026 Tanzil Project\n"
            f"#  License: Creative Commons Attribution 3.0\n#\n"
            f"#====================================================================\n\t\n-->\n"
        )

    lines = [xml_declaration, leading_comment, "<quran>\n"]

    sura_indices = list(range(1, len(surah_counts) + 1))
    if reverse_encounter:
        sura_indices.reverse()

    for s_idx in sura_indices:
        count = surah_counts[s_idx - 1]
        lines.append(f'\t<sura index="{s_idx}" name="سورة_{s_idx}">\n')
        aya_indices = list(range(1, count + 1))
        if reverse_encounter:
            aya_indices.reverse()

        for a_idx in aya_indices:
            coord = (s_idx, a_idx)
            if coord == drop_coordinate:
                continue

            # Deterministic text distinguishable per script and verse
            if coord == blank_coordinate:
                text_val = "   "
            else:
                text_val = f"نص_{script_name}_{s_idx}_{a_idx}"

            bismillah_attr = ""
            if s_idx > 1 and a_idx == 1:
                bismillah_attr = ' bismillah="بسم الله الرحمن الرحيم"'

            lines.append(f'\t\t<aya index="{a_idx}" text="{text_val}"{bismillah_attr} />\n')

            if coord == duplicate_coordinate:
                lines.append(f'\t\t<aya index="{a_idx}" text="{text_val}_dup"{bismillah_attr} />\n')

        lines.append("\t</sura>\n")

    lines.append("</quran>\n")
    return "".join(lines)


def setup_synthetic_pack_env(
    root: Path,
    *,
    metadata_xml: str | None = None,
    uthmani_xml: str | None = None,
    clean_xml: str | None = None,
) -> tuple[Path, Path]:
    """Create directory structure, files, registry, and lock in root.

    Returns (sources_root, output_dir).
    """
    raw_tanzil = root / "raw-inputs" / "tanzil"
    raw_tanzil.mkdir(parents=True, exist_ok=True)
    sources_dir = root / "sources"
    evidence_dir = sources_dir / "licenses" / "evidence" / "tanzil"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    if metadata_xml is None:
        metadata_xml = build_synthetic_metadata_xml()
    if uthmani_xml is None:
        uthmani_xml = build_synthetic_scripture_xml("Uthmani")
    if clean_xml is None:
        clean_xml = build_synthetic_scripture_xml("Simple Clean")

    meta_path = raw_tanzil / "quran-data-Tanzil.xml"
    uth_path = raw_tanzil / "quran-uthmani.xml"
    clean_path = raw_tanzil / "quran-simple-clean.xml"

    meta_bytes = metadata_xml.encode("utf-8")
    uth_bytes = uthmani_xml.encode("utf-8")
    clean_bytes = clean_xml.encode("utf-8")

    meta_path.write_bytes(meta_bytes)
    uth_path.write_bytes(uth_bytes)
    clean_path.write_bytes(clean_bytes)

    # Extract notice slices
    # metadata: opening <quran ...> tag
    m_tag_start = meta_bytes.find(b"<quran ")
    m_tag_end = meta_bytes.find(b">", m_tag_start) + 1
    meta_notice_bytes = meta_bytes[m_tag_start:m_tag_end]

    # uthmani: leading comment
    u_comm_start = uth_bytes.find(b"<!--")
    u_comm_end = uth_bytes.find(b"-->") + 3
    uth_notice_bytes = uth_bytes[u_comm_start:u_comm_end]

    # clean: leading comment
    c_comm_start = clean_bytes.find(b"<!--")
    c_comm_end = clean_bytes.find(b"-->") + 3
    clean_notice_bytes = clean_bytes[c_comm_start:c_comm_end]

    (evidence_dir / ".gitattributes").write_bytes(b"*.notice.txt -text\n")
    (evidence_dir / "quran-data-Tanzil.xml.notice.txt").write_bytes(meta_notice_bytes)
    (evidence_dir / "quran-uthmani.xml.notice.txt").write_bytes(uth_notice_bytes)
    (evidence_dir / "quran-simple-clean.xml.notice.txt").write_bytes(clean_notice_bytes)

    # Registry
    registry_data = {
        "tanzil/quran-data-Tanzil.xml": {
            "packIds": ["quran-core"],
            "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml",
            "sourceVersion": "1.0",
        },
        "tanzil/quran-simple-clean.xml": {
            "packIds": ["quran-core"],
            "sourceUrl": "https://tanzil.net/pub/download/index.php?quranType=simple-clean&outType=xml&marks=true&sajdah=true&rub=true&alef=true&tatweel=true&agree=true&laa=true&stanween=true",
            "sourceVersion": "1.1",
        },
        "tanzil/quran-uthmani.xml": {
            "packIds": ["quran-core"],
            "sourceUrl": "https://tanzil.net/pub/download/index.php?quranType=uthmani&outType=xml&marks=true&sajdah=true&rub=true&alef=true&tatweel=true&agree=true&laa=true&stanween=true",
            "sourceVersion": "1.1",
        },
    }
    registry_file = sources_dir / "registry.json"
    registry_file.write_text(json.dumps(registry_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Author manifest.lock.json using existing author_lock
    lock_file = sources_dir / "manifest.lock.json"
    author_lock(registry_file, root / "raw-inputs", lock_file)

    # License record
    license_record = {
        "licenseSpdx": "LicenseRef-Tanzil-Quran-Text",
        "packId": "quran-core",
        "primaryPath": "tanzil/quran-uthmani.xml",
        "reviewedBy": None,
        "sources": [
            {
                "attribution": "Tanzil Project (https://tanzil.net/)",
                "name": "Tanzil Quran Metadata",
                "notice": meta_notice_bytes.decode("utf-8"),
                "path": "tanzil/quran-data-Tanzil.xml",
                "permittedUses": [],
                "rightsEvidence": [
                    "https://tanzil.net/docs/quran_metadata",
                    "sources/licenses/evidence/tanzil/quran-data-Tanzil.xml.notice.txt",
                ],
                "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml",
                "sourceVersion": "1.0",
            },
            {
                "attribution": "Tanzil Project (https://tanzil.net/)",
                "name": "Tanzil Quran Text (Simple Clean)",
                "notice": clean_notice_bytes.decode("utf-8"),
                "path": "tanzil/quran-simple-clean.xml",
                "permittedUses": [],
                "rightsEvidence": [
                    "https://tanzil.net/docs/text_license",
                    "sources/licenses/evidence/tanzil/quran-simple-clean.xml.notice.txt",
                ],
                "sourceUrl": "https://tanzil.net/pub/download/index.php?quranType=simple-clean&outType=xml&marks=true&sajdah=true&rub=true&alef=true&tatweel=true&agree=true&laa=true&stanween=true",
                "sourceVersion": "1.1",
            },
            {
                "attribution": "Tanzil Project (https://tanzil.net/)",
                "name": "Tanzil Quran Text (Uthmani)",
                "notice": uth_notice_bytes.decode("utf-8"),
                "path": "tanzil/quran-uthmani.xml",
                "permittedUses": [],
                "rightsEvidence": [
                    "https://tanzil.net/docs/text_license",
                    "sources/licenses/evidence/tanzil/quran-uthmani.xml.notice.txt",
                ],
                "sourceUrl": "https://tanzil.net/pub/download/index.php?quranType=uthmani&outType=xml&marks=true&sajdah=true&rub=true&alef=true&tatweel=true&agree=true&laa=true&stanween=true",
                "sourceVersion": "1.1",
            },
        ],
        "status": "pending",
    }
    lic_file = sources_dir / "licenses" / "quran-core.json"
    lic_file.write_text(json.dumps(license_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return root, out_dir
