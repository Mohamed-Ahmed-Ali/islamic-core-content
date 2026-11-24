"""Deterministic SQLite pack builder for bilingual Hadith books."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

from pipelines.common.hadith_input import load_hadith_input
from pipelines.common.lock_inputs import verify_lock
from pipelines.common.pack_sources import (
    _is_symlink_or_junction,
    load_sqlite_source_metadata,
)
from pipelines.common.search_text import normalize_arabic
from pipelines.common.sqlite_utils import (
    finalize_pack,
    open_pack,
    write_pack_metadata,
)
from pipelines.toolchain.check_runtime import verify_runtime

ALLOWED_BOOKS: frozenset[str] = frozenset({
    "bukhari",
    "muslim",
    "nasai",
    "abudawud",
    "tirmidhi",
    "ibnmajah",
    "malik",
    "darimi",
    "ahmad",
})

_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")

HADITH_COLUMNS: tuple[str, ...] = (
    "id",
    "book_id",
    "chapter_id",
    "hadith_number",
    "urn",
    "arabic_full",
    "english_full",
    "text_split_mode",
    "arabic_sanad",
    "arabic_matn",
    "arabic_matn_clean",
    "arabic_full_clean",
    "english_narrator",
    "english_body",
    "grade_ar",
    "grade_en",
    "reference_json",
    "is_ar_missing",
    "is_en_missing",
)


def _cleanup_output(target: Path) -> None:
    for p in (target, *(target.with_name(target.name + s) for s in _SIDECAR_SUFFIXES)):
        try:
            if p.is_file() or _is_symlink_or_junction(p):
                p.unlink(missing_ok=True)
        except Exception:
            pass


def build_hadith_book(book: str, sources_root: Path, output_dir: Path) -> None:
    """Build a Hadith book SQLite pack deterministically."""
    # 1. Pinned runtime verification before touching any files
    verify_runtime()

    if book not in ALLOWED_BOOKS:
        raise ValueError(
            f"Unknown or unapproved Hadith book slug: {book!r}. Allowed: {sorted(ALLOWED_BOOKS)}"
        )

    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    if _is_symlink_or_junction(sources_root):
        raise ValueError(f"sources_root cannot be a symlink or junction: {sources_root}")
    if _is_symlink_or_junction(output_dir):
        raise ValueError(f"output_dir cannot be a symlink or junction: {output_dir}")

    raw_root = sources_root / "raw-inputs"
    registry_path = sources_root / "sources" / "registry.json"
    lock_path = sources_root / "sources" / "manifest.lock.json"
    pack_id = f"hadith-{book}"

    # 2. Lock verification for this pack
    entries = verify_lock(registry_path, raw_root, lock_path, pack_id=pack_id)
    entries_by_path = {e.path: e for e in entries}

    primary_rel = f"hadith/{book}.json"
    splits_rel = f"hadith/{book}.splits.json"

    if primary_rel not in entries_by_path:
        raise ValueError(f"Primary file {primary_rel!r} not found in locked entries for {pack_id}")

    allowed_entries = {primary_rel, splits_rel}
    for e_path in entries_by_path:
        if e_path not in allowed_entries:
            raise ValueError(f"Unexpected locked entry for {pack_id}: {e_path}")

    # 3. Canonical input validation
    primary_file = raw_root / primary_rel
    splits_file = (raw_root / splits_rel) if splits_rel in entries_by_path else None
    data = load_hadith_input(primary_file, splits_path=splits_file)

    hadiths: list[dict[str, Any]] = data.get("hadiths", [])
    books: list[dict[str, Any]] = data.get("books", [])
    chapters: list[dict[str, Any]] = data.get("chapters", [])

    is_complete = all(
        h.get("arabicFull") is not None and h.get("englishFull") is not None
        for h in hadiths
    )
    completeness = "complete" if is_complete else "partial"
    any_verified = any(h.get("textSplitMode") == "source_verified" for h in hadiths)

    # 4. Extract source metadata
    metadata = load_sqlite_source_metadata(
        sources_root,
        pack_id,
        entries,
        primary_path=primary_rel,
        pack_version="0.2.0",
        completeness=completeness,
        fts5_enabled=True,
    )
    metadata["matn_split_verified"] = "true" if any_verified else "false"

    # 5. Output file target and collision check
    output_path = output_dir / f"{pack_id}.db"
    if output_path.exists() or _is_symlink_or_junction(output_path):
        raise FileExistsError(f"Output database already exists: {output_path}")

    schema_path = (
        Path(__file__).resolve().parents[2] / "specs" / "sql" / "hadith_book.v1.sql"
    )

    # 6. Build SQLite DB
    conn: sqlite3.Connection | None = None
    try:
        conn = open_pack(output_path, schema_path)
        conn.execute("BEGIN;")

        # Populate books sorted by id ascending
        sorted_books = sorted(books, key=lambda b: b["id"])
        conn.executemany(
            "INSERT INTO books (id, number, name_ar, name_en) VALUES (?, ?, ?, ?);",
            [
                (b["id"], str(b["number"]), b.get("nameAr"), b.get("nameEn"))
                for b in sorted_books
            ],
        )

        # Populate chapters sorted by id ascending
        sorted_chapters = sorted(chapters, key=lambda c: c["id"])
        conn.executemany(
            "INSERT INTO chapters (id, book_id, number, title_ar, title_en) VALUES (?, ?, ?, ?, ?);",
            [
                (
                    c["id"],
                    c["bookId"],
                    str(c["number"]) if c.get("number") is not None else None,
                    c.get("titleAr"),
                    c.get("titleEn"),
                )
                for c in sorted_chapters
            ],
        )

        # Populate hadiths sorted by id ascending
        sorted_hadiths = sorted(hadiths, key=lambda h: h["id"])
        hadith_rows: list[tuple[Any, ...]] = []
        for h in sorted_hadiths:
            split_mode = h["textSplitMode"]
            ar_matn = h.get("arabicMatn")
            ar_full = h.get("arabicFull")

            ar_matn_clean = (
                normalize_arabic(ar_matn)
                if (split_mode == "source_verified" and ar_matn is not None)
                else None
            )
            ar_full_clean = (
                normalize_arabic(ar_full)
                if (split_mode == "unsplit_raw" and ar_full is not None)
                else None
            )

            is_ar_missing = 1 if ar_full is None else 0
            is_en_missing = 1 if h.get("englishFull") is None else 0

            ref = h.get("reference")
            ref_json = (
                json.dumps(
                    ref,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                if ref is not None
                else None
            )

            hadith_rows.append((
                h["id"],
                h["bookId"],
                h.get("chapterId"),
                str(h["hadithNumber"]),
                h.get("urn"),
                ar_full,
                h.get("englishFull"),
                split_mode,
                h.get("arabicSanad"),
                ar_matn,
                ar_matn_clean,
                ar_full_clean,
                h.get("englishNarrator"),
                h.get("englishBody"),
                h.get("gradeAr"),
                h.get("gradeEn"),
                ref_json,
                is_ar_missing,
                is_en_missing,
            ))

        placeholders = ", ".join("?" for _ in HADITH_COLUMNS)
        columns_sql = ", ".join(HADITH_COLUMNS)
        conn.executemany(
            f"INSERT INTO hadiths ({columns_sql}) VALUES ({placeholders});",
            hadith_rows,
        )

        # Coverage report
        expected_total = len(sorted_hadiths)
        ar_present = sum(1 for h in sorted_hadiths if h.get("arabicFull") is not None)
        en_present = sum(1 for h in sorted_hadiths if h.get("englishFull") is not None)
        en_missing = expected_total - en_present

        coverage: dict[str, tuple[int, str | None]] = {
            "expected_total": (expected_total, None),
            "ar_present": (ar_present, None),
            "en_present": (en_present, None),
            "en_missing": (en_missing, None),
        }

        write_pack_metadata(conn, metadata, coverage)

        # Rebuild, optimize both FTS indexes, ANALYZE, COMMIT, VACUUM
        finalize_pack(conn, fts_tables=("hadiths_fts", "hadiths_full_fts"))
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
    parser = argparse.ArgumentParser(description="Build Hadith SQLite pack.")
    parser.add_argument(
        "--book",
        required=True,
        choices=sorted(ALLOWED_BOOKS),
        help="Hadith book slug",
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
        help="Output directory where hadith-{book}.db will be written",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_hadith_book(args.book, args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building hadith-{args.book}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
