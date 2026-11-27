"""Deterministic SQLite pack builder for Mashrūʿ Taqrīb as-Sunnah."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

from pipelines.common.lock_inputs import (
    parse_strict_json,
    verify_lock,
)
from pipelines.common.pack_sources import (
    _is_symlink_or_junction,
    load_sqlite_source_metadata,
)
from pipelines.common.sqlite_utils import (
    finalize_pack,
    open_pack,
    write_pack_metadata,
)
from pipelines.common.stage_taqreeb_inputs import TAQREEB_SOURCES
from pipelines.common.search_text import normalize_arabic_search
from pipelines.toolchain.check_runtime import verify_runtime

ALLOWED_PACK_IDS: frozenset[str] = frozenset(TAQREEB_SOURCES.keys())

_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")

_MARKER_REGEX: re.Pattern[str] = re.compile(r"\{\[([0-9]+)\]\}")

_EXPECTED_ROOT_KEYS: frozenset[str] = frozenset({
    "book_id",
    "title_ar",
    "title_en",
    "description_ar",
    "total_maqsads",
    "total_hadiths",
    "maqsads",
})
_EXPECTED_MAQSAD_KEYS: frozenset[str] = frozenset({
    "maqsad_id",
    "maqsad_index",
    "name_ar",
    "name_en",
    "kutub",
})
_EXPECTED_KETAB_KEYS: frozenset[str] = frozenset({
    "ketab_id",
    "ketab_index",
    "name_ar",
    "name_en",
    "chapters",
})
_EXPECTED_CHAPTER_KEYS: frozenset[str] = frozenset({
    "chapter_id",
    "chapter_index",
    "name_ar",
    "name_en",
    "hadiths",
})
_EXPECTED_HADITH_KEYS: frozenset[str] = frozenset({
    "id",
    "hadith_no",
    "text_ar",
    "text_ar_simple",
    "takhreej",
    "hukm",
    "page_no",
    "footnotes",
    "atraf",
})
_EXPECTED_FOOTNOTE_KEYS: frozenset[str] = frozenset({"number", "text"})


def _cleanup_output(target: Path) -> None:
    for p in (target, *(target.with_name(target.name + s) for s in _SIDECAR_SUFFIXES)):
        try:
            if p.is_file() or _is_symlink_or_junction(p):
                p.unlink(missing_ok=True)
        except Exception:
            pass


def _is_int(v: Any, min_val: int = 1) -> bool:
    return type(v) is int and min_val <= v <= 9223372036854775807


def _format_marker_notes(discrepancies: list[tuple[int, int]]) -> str | None:
    if not discrepancies:
        return None
    return " | ".join(
        f"hadith_id={hid}; footnote_number={num}"
        for hid, num in sorted(discrepancies)
    )


def build_hadith_taqreeb(pack_id: str, sources_root: Path, output_dir: Path) -> None:
    """Build a Taqreeb Hadith SQLite pack deterministically."""
    verify_runtime()

    if pack_id not in ALLOWED_PACK_IDS:
        raise ValueError(
            f"Unknown or unapproved pack_id: {pack_id!r}. Allowed: {sorted(ALLOWED_PACK_IDS)}"
        )

    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    if _is_symlink_or_junction(sources_root):
        raise ValueError(f"sources_root cannot be a symlink or junction: {sources_root}")
    if _is_symlink_or_junction(output_dir):
        raise ValueError(f"output_dir cannot be a symlink or junction: {output_dir}")
    if not sources_root.is_dir():
        raise NotADirectoryError(f"sources_root is not a directory: {sources_root}")
    if not output_dir.is_dir():
        raise NotADirectoryError(f"output_dir is not a directory: {output_dir}")

    raw_root = sources_root / "raw-inputs"
    registry_path = sources_root / "sources" / "registry.json"
    lock_path = sources_root / "sources" / "manifest.lock.json"

    entries = verify_lock(registry_path, raw_root, lock_path, pack_id=pack_id)
    expected_book_id, basename = TAQREEB_SOURCES[pack_id]
    expected_rel = f"hadith-taqreeb/{basename}"

    entries_by_path = {e.path: e for e in entries}
    if list(entries_by_path.keys()) != [expected_rel]:
        raise ValueError(
            f"Expected sole locked entry {expected_rel!r} for {pack_id}, got: {list(entries_by_path.keys())}"
        )

    raw_file = raw_root / expected_rel
    raw_text = raw_file.read_bytes().decode("utf-8")
    data = parse_strict_json(raw_text)
    if not isinstance(data, dict):
        raise ValueError("Root JSON must be an object")

    if set(data.keys()) != _EXPECTED_ROOT_KEYS:
        raise ValueError(
            f"Root keys mismatch for {pack_id}: expected {sorted(_EXPECTED_ROOT_KEYS)}, got {sorted(data.keys())}"
        )

    book_id = data["book_id"]
    if not _is_int(book_id, 1) or book_id != expected_book_id:
        raise ValueError(f"book_id mismatch for {pack_id}: expected {expected_book_id}, got {book_id}")

    title_ar = data["title_ar"]
    title_en = data["title_en"]
    description_ar = data["description_ar"]
    if not isinstance(title_ar, str) or not isinstance(title_en, str) or not isinstance(description_ar, str):
        raise ValueError("title_ar, title_en, and description_ar must be strings")

    total_maqsads = data["total_maqsads"]
    if not _is_int(total_maqsads, 1):
        raise ValueError("total_maqsads must be a positive integer")

    total_hadiths = data["total_hadiths"]
    if not _is_int(total_hadiths, 1):
        raise ValueError("total_hadiths must be a positive integer")

    maqsads_raw = data["maqsads"]
    if not isinstance(maqsads_raw, list) or len(maqsads_raw) != total_maqsads:
        raise ValueError(
            f"maqsads count mismatch: declared {total_maqsads}, actual list length {len(maqsads_raw) if isinstance(maqsads_raw, list) else type(maqsads_raw)}"
        )

    books_rows = [(book_id, title_ar, title_en, description_ar, total_maqsads, total_hadiths)]
    maqsads_rows: list[tuple[int, int, int, str, str]] = []
    kutub_rows: list[tuple[int, int, int, str, str]] = []
    chapters_rows: list[tuple[int, int, int, str, str]] = []
    hadiths_rows: list[tuple[Any, ...]] = []
    footnotes_rows: list[tuple[int, int, str]] = []
    atraf_rows: list[tuple[int, int, str]] = []

    seen_maqsad_ids: set[int] = set()
    seen_maqsad_indices: set[int] = set()
    seen_ketab_ids: set[int] = set()
    seen_ketab_indices: dict[int, set[int]] = {}
    seen_chapter_ids: set[int] = set()
    seen_chapter_indices: dict[int, set[int]] = {}
    seen_hadith_ids: set[int] = set()

    ar_present = 0
    cross_ref_count = 0
    empty_text_count = 0
    footnote_count = 0
    footnote_missing_marker: list[tuple[int, int]] = []
    marker_missing_footnote: list[tuple[int, int]] = []

    for m in maqsads_raw:
        if not isinstance(m, dict) or set(m.keys()) != _EXPECTED_MAQSAD_KEYS:
            raise ValueError(f"Invalid maqsad object: keys mismatch: {m.keys() if isinstance(m, dict) else type(m)}")

        m_id = m["maqsad_id"]
        m_idx = m["maqsad_index"]
        if not _is_int(m_id, 1) or not _is_int(m_idx, 1):
            raise ValueError(f"Invalid maqsad_id {m_id} or maqsad_index {m_idx}")
        if m_id in seen_maqsad_ids:
            raise ValueError(f"Duplicate maqsad_id: {m_id}")
        seen_maqsad_ids.add(m_id)
        if m_idx in seen_maqsad_indices:
            raise ValueError(f"Duplicate maqsad_index: {m_idx}")
        seen_maqsad_indices.add(m_idx)

        m_name_ar = m["name_ar"]
        m_name_en = m["name_en"]
        if not isinstance(m_name_ar, str) or not isinstance(m_name_en, str):
            raise ValueError("maqsad name_ar and name_en must be strings")

        kutub_raw = m["kutub"]
        if not isinstance(kutub_raw, list):
            raise ValueError(f"maqsad kutub must be a list: {type(kutub_raw)}")

        maqsads_rows.append((m_id, book_id, m_idx, m_name_ar, m_name_en))
        seen_ketab_indices[m_id] = set()

        for k in kutub_raw:
            if not isinstance(k, dict) or set(k.keys()) != _EXPECTED_KETAB_KEYS:
                raise ValueError(f"Invalid ketab object: keys mismatch: {k.keys() if isinstance(k, dict) else type(k)}")

            k_id = k["ketab_id"]
            k_idx = k["ketab_index"]
            if not _is_int(k_id, 1) or not _is_int(k_idx, 1):
                raise ValueError(f"Invalid ketab_id {k_id} or ketab_index {k_idx}")
            if k_id in seen_ketab_ids:
                raise ValueError(f"Duplicate ketab_id: {k_id}")
            seen_ketab_ids.add(k_id)
            if k_idx in seen_ketab_indices[m_id]:
                raise ValueError(f"Duplicate ketab_index {k_idx} in maqsad {m_id}")
            seen_ketab_indices[m_id].add(k_idx)

            k_name_ar = k["name_ar"]
            k_name_en = k["name_en"]
            if not isinstance(k_name_ar, str) or not isinstance(k_name_en, str):
                raise ValueError("ketab name_ar and name_en must be strings")

            chapters_raw = k["chapters"]
            if not isinstance(chapters_raw, list):
                raise ValueError(f"ketab chapters must be a list: {type(chapters_raw)}")

            kutub_rows.append((k_id, m_id, k_idx, k_name_ar, k_name_en))
            seen_chapter_indices[k_id] = set()

            for c in chapters_raw:
                if not isinstance(c, dict) or set(c.keys()) != _EXPECTED_CHAPTER_KEYS:
                    raise ValueError(f"Invalid chapter object: keys mismatch: {c.keys() if isinstance(c, dict) else type(c)}")

                c_id = c["chapter_id"]
                c_idx = c["chapter_index"]
                if not _is_int(c_id, 1) or not _is_int(c_idx, 1):
                    raise ValueError(f"Invalid chapter_id {c_id} or chapter_index {c_idx}")
                if c_id in seen_chapter_ids:
                    raise ValueError(f"Duplicate chapter_id: {c_id}")
                seen_chapter_ids.add(c_id)
                if c_idx in seen_chapter_indices[k_id]:
                    raise ValueError(f"Duplicate chapter_index {c_idx} in ketab {k_id}")
                seen_chapter_indices[k_id].add(c_idx)

                c_name_ar = c["name_ar"]
                c_name_en = c["name_en"]
                if not isinstance(c_name_ar, str) or not isinstance(c_name_en, str):
                    raise ValueError("chapter name_ar and name_en must be strings")

                hadiths_raw = c["hadiths"]
                if not isinstance(hadiths_raw, list):
                    raise ValueError(f"chapter hadiths must be a list: {type(hadiths_raw)}")

                chapters_rows.append((c_id, k_id, c_idx, c_name_ar, c_name_en))

                for h in hadiths_raw:
                    if not isinstance(h, dict) or set(h.keys()) != _EXPECTED_HADITH_KEYS:
                        raise ValueError(f"Invalid hadith object: keys mismatch: {h.keys() if isinstance(h, dict) else type(h)}")

                    h_id = h["id"]
                    h_no = h["hadith_no"]
                    if not _is_int(h_id, 1) or not _is_int(h_no, 1):
                        raise ValueError(f"Invalid hadith id {h_id} or hadith_no {h_no}")
                    if h_id in seen_hadith_ids:
                        raise ValueError(f"Duplicate hadith id: {h_id}")
                    seen_hadith_ids.add(h_id)

                    text_ar = h["text_ar"]
                    if text_ar is not None and not isinstance(text_ar, str):
                        raise ValueError(f"hadith {h_id} text_ar must be string or null")

                    text_ar_simple = h["text_ar_simple"]
                    if not isinstance(text_ar_simple, str):
                        raise ValueError(f"hadith {h_id} text_ar_simple must be string")

                    takhreej = h["takhreej"]
                    if takhreej is not None and not isinstance(takhreej, str):
                        raise ValueError(f"hadith {h_id} takhreej must be string or null")

                    hukm = h["hukm"]
                    if hukm is not None and not isinstance(hukm, str):
                        raise ValueError(f"hadith {h_id} hukm must be string or null")

                    page_no = h["page_no"]
                    if page_no is not None and not _is_int(page_no, 1):
                        raise ValueError(f"hadith {h_id} page_no must be positive int or null")

                    if text_ar is None:
                        if takhreej is None or not takhreej.strip():
                            raise ValueError(
                                f"hadith {h_id}: text_ar is null but takhreej is empty or null"
                            )
                        is_cross_ref = 1
                        cross_ref_count += 1
                    else:
                        is_cross_ref = 0
                        if text_ar.strip():
                            ar_present += 1
                        else:
                            empty_text_count += 1

                    text_split_mode = "unsplit_raw"
                    text_ar_search = normalize_arabic_search(text_ar) if text_ar else ""

                    hadiths_rows.append((
                        h_id,
                        c_id,
                        h_no,
                        text_ar,
                        text_ar_simple,
                        takhreej,
                        hukm,
                        page_no,
                        is_cross_ref,
                        text_split_mode,
                        text_ar_search,
                    ))

                    fn_raw = h["footnotes"]
                    if not isinstance(fn_raw, list):
                        raise ValueError(f"hadith {h_id} footnotes must be a list")

                    seen_fn_nums: set[int] = set()
                    for fn in fn_raw:
                        if not isinstance(fn, dict) or set(fn.keys()) != _EXPECTED_FOOTNOTE_KEYS:
                            raise ValueError(f"hadith {h_id} footnote keys mismatch")
                        fn_num = fn["number"]
                        if not _is_int(fn_num, 0):
                            raise ValueError(f"hadith {h_id} footnote number must be non-negative int")
                        if fn_num in seen_fn_nums:
                            raise ValueError(f"hadith {h_id} duplicate footnote number: {fn_num}")
                        seen_fn_nums.add(fn_num)

                        fn_text = fn["text"]
                        if not isinstance(fn_text, str):
                            raise ValueError(f"hadith {h_id} footnote text must be string")

                        footnotes_rows.append((h_id, fn_num, fn_text))
                        footnote_count += 1

                    if text_ar is not None:
                        marker_nums = {int(m) for m in _MARKER_REGEX.findall(text_ar)}
                    else:
                        marker_nums = set()

                    for missing_m in seen_fn_nums - marker_nums:
                        footnote_missing_marker.append((h_id, missing_m))
                    for missing_fn in marker_nums - seen_fn_nums:
                        marker_missing_footnote.append((h_id, missing_fn))

                    atraf_raw = h["atraf"]
                    if not isinstance(atraf_raw, list):
                        raise ValueError(f"hadith {h_id} atraf must be a list")

                    for pos, atraf_text in enumerate(atraf_raw, start=1):
                        if not isinstance(atraf_text, str):
                            raise ValueError(f"hadith {h_id} atraf entry must be string")
                        atraf_rows.append((h_id, pos, atraf_text))

    if len(seen_hadith_ids) != total_hadiths:
        raise ValueError(
            f"total_hadiths mismatch for {pack_id}: declared {total_hadiths}, actual traversed {len(seen_hadith_ids)}"
        )

    # Sort every table insert by declared full PK
    books_rows.sort(key=lambda r: r[0])
    maqsads_rows.sort(key=lambda r: r[0])
    kutub_rows.sort(key=lambda r: r[0])
    chapters_rows.sort(key=lambda r: r[0])
    hadiths_rows.sort(key=lambda r: r[0])
    footnotes_rows.sort(key=lambda r: (r[0], r[1]))
    atraf_rows.sort(key=lambda r: (r[0], r[1]))

    completeness = "complete" if ar_present == total_hadiths else "partial"

    metadata = load_sqlite_source_metadata(
        sources_root,
        pack_id,
        entries,
        primary_path=expected_rel,
        pack_version="0.4.0",
        completeness=completeness,
        fts5_enabled=True,
    )
    metadata["matn_split_verified"] = "false"

    coverage: dict[str, tuple[int, str | None]] = {
        "expected_total": (total_hadiths, None),
        "ar_present": (ar_present, None),
        "en_present": (0, None),
        "en_missing": (total_hadiths, None),
        "cross_ref_count": (cross_ref_count, None),
        "empty_text_count": (empty_text_count, None),
        "footnote_count": (footnote_count, None),
        "footnote_missing_inline_marker_count": (
            len(footnote_missing_marker),
            _format_marker_notes(footnote_missing_marker),
        ),
        "inline_marker_missing_footnote_count": (
            len(marker_missing_footnote),
            _format_marker_notes(marker_missing_footnote),
        ),
    }

    output_path = output_dir / f"{pack_id}.db"
    if output_path.exists() or _is_symlink_or_junction(output_path):
        raise FileExistsError(f"Output database already exists: {output_path}")

    for s in _SIDECAR_SUFFIXES:
        sidecar = output_path.with_name(output_path.name + s)
        if sidecar.exists() or _is_symlink_or_junction(sidecar):
            raise FileExistsError(f"SQLite sidecar exists: {sidecar}")

    schema_path = Path(__file__).resolve().parents[2] / "specs" / "sql" / "hadith_taqreeb_v1.sql"

    conn: sqlite3.Connection | None = None
    try:
        conn = open_pack(output_path, schema_path)
        conn.execute("BEGIN;")

        conn.executemany(
            "INSERT INTO books (book_id, title_ar, title_en, description_ar, total_maqsads, total_hadiths) VALUES (?, ?, ?, ?, ?, ?);",
            books_rows,
        )
        conn.executemany(
            "INSERT INTO maqsads (maqsad_id, book_id, maqsad_index, name_ar, name_en) VALUES (?, ?, ?, ?, ?);",
            maqsads_rows,
        )
        conn.executemany(
            "INSERT INTO kutub (ketab_id, maqsad_id, ketab_index, name_ar, name_en) VALUES (?, ?, ?, ?, ?);",
            kutub_rows,
        )
        conn.executemany(
            "INSERT INTO chapters (chapter_id, ketab_id, chapter_index, name_ar, name_en) VALUES (?, ?, ?, ?, ?);",
            chapters_rows,
        )
        conn.executemany(
            "INSERT INTO hadiths (id, chapter_id, hadith_no, text_ar, text_ar_simple, takhreej, hukm, page_no, is_cross_ref, text_split_mode, text_ar_search) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            hadiths_rows,
        )
        conn.executemany(
            "INSERT INTO footnotes (hadith_id, number, text) VALUES (?, ?, ?);",
            footnotes_rows,
        )
        conn.executemany(
            "INSERT INTO hadith_atraf (hadith_id, position, text) VALUES (?, ?, ?);",
            atraf_rows,
        )

        write_pack_metadata(conn, metadata, coverage)
        finalize_pack(conn, fts_tables=("hadiths_fts",))
        conn.close()
        conn = None
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            _cleanup_output(output_path)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build Taqreeb Hadith SQLite pack.")
    parser.add_argument(
        "--pack-id",
        required=True,
        choices=sorted(ALLOWED_PACK_IDS),
        help="Taqreeb pack ID",
    )
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

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_hadith_taqreeb(args.pack_id, args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building {args.pack_id}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
