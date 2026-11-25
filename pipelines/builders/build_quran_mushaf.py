"""Quran Mushaf layout and Tajweed pack builder CLI and pipeline."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from typing import Any

from pipelines.builders.build_quran_wbw import clean_tajweed_markup, load_qpc_hafs_tajweed_verses
from pipelines.common.qul_input import open_sqlite_from_zip
from pipelines.common.quran_pack import load_quran_pack, write_quran_pack
from pipelines.common.tanzil_coordinates import load_tanzil_coordinates
from pipelines.toolchain.check_runtime import verify_runtime

PACK_VERSION = "0.3.0"

SUPPORTED_MUSHAF_PACKS: dict[str, dict[str, Any]] = {
    "quran-mushaf-madani-v2": {
        "layout_path": "qul/mushaf/qpc-v2-15-lines.db.zip",
        "expected_paths": frozenset({
            "tanzil/quran-data-Tanzil.xml",
            "qul/mushaf/qpc-hafs-tajweed.json.zip",
            "qul/mushaf/qpc-v2-15-lines.db.zip",
        }),
        "page_count": 604,
        "line_count": 9046,
    },
    "quran-mushaf-indopak-15": {
        "layout_path": "qul/mushaf/qudratullah-indopak-15-lines.db.zip",
        "expected_paths": frozenset({
            "tanzil/quran-data-Tanzil.xml",
            "qul/mushaf/qpc-hafs-tajweed.json.zip",
            "qul/mushaf/qudratullah-indopak-15-lines.db.zip",
        }),
        "page_count": 610,
        "line_count": 9131,
    },
}

SUPPORTED_PACK_IDS: tuple[str, ...] = tuple(SUPPORTED_MUSHAF_PACKS.keys())
_VALID_LINE_TYPES: frozenset[str] = frozenset({"ayah", "surah_name", "basmallah"})


def get_expected_lines_on_page(page: int, total_pages: int) -> int:
    """Return expected line count for a given page number in the Mushaf."""
    if page in (1, 2):
        return 8
    if total_pages == 610 and page == 610:
        return 10
    return 15


def validate_layout_info(
    con: sqlite3.Connection,
    *,
    expected_pages: int,
) -> tuple[str, int, int, str]:
    """Validate layout info table and return (name, number_of_pages, lines_per_page, font_name)."""
    cur = con.cursor()
    cur.execute("SELECT name, number_of_pages, lines_per_page, font_name FROM info;")
    rows = cur.fetchall()
    if len(rows) != 1:
        raise ValueError(f"Expected exactly 1 row in layout info table, got {len(rows)}")

    name, number_of_pages, lines_per_page, font_name = rows[0]

    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Expected nonblank string for layout name, got {name!r}")
    if not isinstance(font_name, str) or not font_name.strip():
        raise ValueError(f"Expected nonblank string for font_name, got {font_name!r}")

    if isinstance(number_of_pages, bool) or not isinstance(number_of_pages, int) or number_of_pages != expected_pages:
        raise ValueError(
            f"Layout page count mismatch: expected {expected_pages}, got {number_of_pages!r}"
        )
    if isinstance(lines_per_page, bool) or not isinstance(lines_per_page, int) or lines_per_page != 15:
        raise ValueError(
            f"Layout lines_per_page mismatch: expected 15, got {lines_per_page!r}"
        )

    return (name, number_of_pages, lines_per_page, font_name)


def _normalize_optional_int(val: Any, col_name: str, page_num: int, line_num: int) -> int | None:
    """Convert empty string to None, keep None, validate int, and reject other types."""
    if val is None or val == "":
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(
            f"Invalid type for {col_name} on page {page_num}, line {line_num}: "
            f"expected int or empty/None, got {type(val).__name__} ({val!r})"
        )
    return val


def validate_page_lines(
    con: sqlite3.Connection,
    *,
    expected_pages: int,
    expected_lines: int,
) -> list[tuple[int, int, str, int, int | None, int | None, int | None]]:
    """Validate pages table layout and return list of normalized line tuples."""
    cur = con.cursor()
    cur.execute(
        "SELECT page_number, line_number, line_type, is_centered, "
        "first_word_id, last_word_id, surah_number "
        "FROM pages ORDER BY page_number, line_number;"
    )
    raw_rows = cur.fetchall()

    normalized_rows: list[tuple[int, int, str, int, int | None, int | None, int | None]] = []
    lines_by_page: dict[int, list[int]] = {}
    prev_last_word_id: int | None = None

    for r in raw_rows:
        p_num, l_num, l_type, is_centered, first_w, last_w, surah_num = r

        # Validate page_number and line_number
        if isinstance(p_num, bool) or not isinstance(p_num, int) or p_num <= 0:
            raise ValueError(f"Invalid page_number: {p_num!r}")
        if isinstance(l_num, bool) or not isinstance(l_num, int) or l_num <= 0:
            raise ValueError(f"Invalid line_number on page {p_num}: {l_num!r}")

        # Validate line_type
        if not isinstance(l_type, str) or l_type not in _VALID_LINE_TYPES:
            raise ValueError(f"Invalid line_type on page {p_num}, line {l_num}: {l_type!r}")

        # Validate is_centered
        if isinstance(is_centered, bool) or not isinstance(is_centered, int) or is_centered not in (0, 1):
            raise ValueError(f"Invalid is_centered flag on page {p_num}, line {l_num}: {is_centered!r}")

        # Normalize optional integer fields
        norm_first_w = _normalize_optional_int(first_w, "first_word_id", p_num, l_num)
        norm_last_w = _normalize_optional_int(last_w, "last_word_id", p_num, l_num)
        norm_surah_num = _normalize_optional_int(surah_num, "surah_number", p_num, l_num)

        # Validate rules based on line_type
        if l_type == "ayah":
            if norm_surah_num is not None:
                raise ValueError(
                    f"ayah line requires surah_number to be NULL on page {p_num}, line {l_num}, got {norm_surah_num}"
                )
            if norm_first_w is None or norm_last_w is None:
                raise ValueError(
                    f"ayah line requires word range (first_word_id and last_word_id) on page {p_num}, line {l_num}"
                )
            if norm_first_w <= 0 or norm_last_w <= 0:
                raise ValueError(
                    f"ayah word IDs must be positive integers on page {p_num}, line {l_num}, "
                    f"got first={norm_first_w}, last={norm_last_w}"
                )
            if norm_first_w > norm_last_w:
                raise ValueError(
                    f"ayah line first_word_id must be <= last_word_id on page {p_num}, line {l_num}, "
                    f"got first={norm_first_w}, last={norm_last_w}"
                )
            if prev_last_word_id is not None and norm_first_w <= prev_last_word_id:
                raise ValueError(
                    f"Overlapping or non-increasing word range: first_word_id={norm_first_w} <= "
                    f"previous last_word_id={prev_last_word_id} on page {p_num}, line {l_num}"
                )
            prev_last_word_id = norm_last_w

        elif l_type == "surah_name":
            if norm_first_w is not None or norm_last_w is not None:
                raise ValueError(
                    f"surah_name line requires word IDs to be NULL on page {p_num}, line {l_num}, "
                    f"got first={norm_first_w}, last={norm_last_w}"
                )
            if norm_surah_num is None or not (1 <= norm_surah_num <= 114):
                raise ValueError(
                    f"surah_name line requires surah_number in range 1..114 on page {p_num}, line {l_num}, "
                    f"got {norm_surah_num}"
                )

        elif l_type == "basmallah":
            if norm_first_w is not None or norm_last_w is not None or norm_surah_num is not None:
                raise ValueError(
                    f"basmallah line requires all optional fields to be NULL on page {p_num}, line {l_num}, "
                    f"got first={norm_first_w}, last={norm_last_w}, surah={norm_surah_num}"
                )

        lines_by_page.setdefault(p_num, []).append(l_num)
        normalized_rows.append((
            p_num,
            l_num,
            l_type,
            is_centered,
            norm_first_w,
            norm_last_w,
            norm_surah_num,
        ))

    # Validate total line count
    if expected_lines is not None and len(raw_rows) != expected_lines:
        raise ValueError(
            f"Total line count mismatch: expected {expected_lines}, got {len(raw_rows)}"
        )

    # Validate page inventory: pages must be exactly 1..expected_pages
    actual_pages = sorted(lines_by_page.keys())
    expected_page_list = list(range(1, expected_pages + 1))
    if actual_pages != expected_page_list:
        raise ValueError(
            f"Page inventory mismatch: expected pages 1..{expected_pages}, got {len(actual_pages)} distinct pages"
        )

    # Validate line numbers on each page
    for p in range(1, expected_pages + 1):
        actual_lines = lines_by_page.get(p, [])
        exp_lines_count = get_expected_lines_on_page(p, expected_pages)
        if len(actual_lines) != exp_lines_count:
            raise ValueError(
                f"Page {p} line count mismatch: expected {exp_lines_count}, got {len(actual_lines)}"
            )
        exp_line_nums = list(range(1, exp_lines_count + 1))
        if actual_lines != exp_line_nums:
            raise ValueError(
                f"Page {p} line numbers gap or mismatch: expected {exp_line_nums}, got {actual_lines}"
            )

    return normalized_rows


def validate_tajweed_verses(
    zip_path: Path | str,
    expected_ayah_ids: dict[str, int],
    *,
    expected_count: int = 6236,
) -> list[tuple[int, str, str]]:
    """Load QPC Hafs Tajweed verses, validate balanced rule markup tags, and return in ID order."""
    verses = load_qpc_hafs_tajweed_verses(zip_path, expected_ayah_ids=expected_ayah_ids)

    if len(verses) != expected_count:
        raise ValueError(f"Tajweed verse count mismatch: expected {expected_count}, got {len(verses)}")

    rows: list[tuple[int, str, str]] = []
    for vkey, rec in verses.items():
        aid = rec["id"]
        raw_text = rec["text"]
        # Validate markup tags (depth <= 2, valid rule classes, balanced tags)
        clean_tajweed_markup(raw_text, vkey)
        rows.append((aid, vkey, raw_text))

    rows.sort(key=lambda r: r[0])

    # Validate that ayah_ids are contiguous 1..expected_count
    for idx, (aid, _, _) in enumerate(rows, start=1):
        if aid != idx:
            raise ValueError(f"Non-contiguous or misordered ayah_id at index {idx}: got {aid}")

    return rows


def write_mushaf_pack(
    output_path: Path,
    schema_path: Path,
    metadata: Mapping[str, str],
    layout_info_row: tuple[str, int, int, str],
    page_lines_rows: list[tuple[int, int, str, int, int | None, int | None, int | None]],
    tajweed_rows: list[tuple[int, str, str]],
    *,
    expected_total: int = 6236,
) -> None:
    """Write verified Mushaf layout and Tajweed pack SQLite database."""
    coverage = {
        "expected_total": (expected_total, None),
        "ar_present": (expected_total, None),
    }

    with write_quran_pack(
        output_path,
        schema_path,
        metadata,
        coverage,
        fts_tables=(),
    ) as conn:
        name, page_count, lines_per_page, font_name = layout_info_row
        conn.execute(
            "INSERT INTO layout_info (id, name, page_count, lines_per_page, font_name) "
            "VALUES (1, ?, ?, ?, ?);",
            (name, page_count, lines_per_page, font_name),
        )
        conn.executemany(
            "INSERT INTO page_lines ("
            "page_number, line_number, line_type, is_centered, "
            "first_word_id, last_word_id, surah_number"
            ") VALUES (?, ?, ?, ?, ?, ?, ?);",
            page_lines_rows,
        )
        conn.executemany(
            "INSERT INTO tajweed_ayahs (ayah_id, verse_key, text_markup) "
            "VALUES (?, ?, ?);",
            tajweed_rows,
        )


def build_quran_mushaf(
    sources_root: Path,
    output_dir: Path,
    *,
    pack_id: str,
) -> None:
    """Build selected Mushaf layout SQLite pack deterministically."""
    verify_runtime()

    if pack_id not in SUPPORTED_MUSHAF_PACKS:
        raise ValueError(
            f"Unknown or unsupported mushaf pack ID: {pack_id!r}. Supported: {SUPPORTED_PACK_IDS}"
        )

    cfg = SUPPORTED_MUSHAF_PACKS[pack_id]
    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    raw_root, metadata = load_quran_pack(
        sources_root,
        pack_id,
        cfg["expected_paths"],
        fts_enabled=False,
        pack_version=PACK_VERSION,
        completeness="complete",
    )

    tanzil_xml = raw_root / "tanzil" / "quran-data-Tanzil.xml"
    coords = load_tanzil_coordinates(tanzil_xml)

    layout_zip = raw_root / cfg["layout_path"]
    with closing(open_sqlite_from_zip(layout_zip)) as src_con:
        layout_info_row = validate_layout_info(src_con, expected_pages=cfg["page_count"])
        page_lines_rows = validate_page_lines(
            src_con,
            expected_pages=cfg["page_count"],
            expected_lines=cfg["line_count"],
        )

    tajweed_zip = raw_root / "qul" / "mushaf" / "qpc-hafs-tajweed.json.zip"
    tajweed_rows = validate_tajweed_verses(
        tajweed_zip,
        coords.ayah_ids,
        expected_count=6236,
    )

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "sql" / "quran_mushaf.v1.sql"
    output_path = output_dir / f"{pack_id}.db"

    write_mushaf_pack(
        output_path,
        schema_path,
        metadata,
        layout_info_row,
        page_lines_rows,
        tajweed_rows,
        expected_total=6236,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for Mushaf layout pack builder."""
    parser = argparse.ArgumentParser(description="Build Quran Mushaf layout and Tajweed SQLite pack.")
    parser.add_argument(
        "--sources-root",
        required=True,
        type=Path,
        help="Root directory containing sources/ and raw-inputs/",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Output directory where <pack-id>.db will be written",
    )
    parser.add_argument(
        "--pack",
        required=True,
        choices=SUPPORTED_PACK_IDS,
        help=f"Mushaf pack ID to build: {', '.join(SUPPORTED_PACK_IDS)}",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    if not args.sources_root.is_dir():
        sys.stderr.write(f"Error: Sources root does not exist: {args.sources_root}\n")
        return 2
    if not args.output_dir.is_dir():
        sys.stderr.write(f"Error: Output directory does not exist: {args.output_dir}\n")
        return 2

    try:
        build_quran_mushaf(args.sources_root, args.output_dir, pack_id=args.pack)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building {args.pack}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
