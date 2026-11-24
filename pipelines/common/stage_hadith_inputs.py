"""Staging pipeline for Hadith collections from raw source archives to canonical JSON inputs."""

from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from pipelines.common.hadith_input import load_hadith_input
from pipelines.common.lock_inputs import _is_symlink_or_junction

ALLOWED_SLUGS = (
    "bukhari",
    "muslim",
    "nasai",
    "abudawud",
    "tirmidhi",
    "ibnmajah",
    "malik",
    "darimi",
    "ahmad",
)

SOURCE_HASHES = {
    "abudawud": "da55afe7f372a803f4c484da2982c440c62a8b3d891d3cc50a687606a56b9884",
    "ahmad": "41553fee47fe582a8d0b60fbea1f71b867df44d40adac94dcfe742571a8bce24",
    "bukhari": "9d2e4194786c275f64f627c834711ea0e339a8fe226d5e9569ef962595a562f1",
    "darimi": "45ec3ac92b072287e6c7451084f55f50a2676e0eab2ec165c4ffecfa57f41d2a",
    "ibnmajah": "6c5d2abcabb0c880ae09cb3f03f1f716c166a14b89d5243bc04ad65e1023119d",
    "malik": "61473d46b5a87ca095e4bc300bed3412843844509d09615c99f4d85ab70e0e0c",
    "muslim": "12e3cbe8e2c83acc787b3e1e644877eff0feab11f1b32493386c60703d9076ae",
    "nasai": "d32d122202ab17af5f12b91b2b3737b1e208d9feff3e92effd027f10da56befe",
    "tirmidhi": "9ba6da92af6b0db9768c67f4c562c24bbbdb7be83509a78c40994c6d93366437",
}

MALIK_CANONICAL_SHA256 = "5dab3037718896293bbcc9454983d13f48e8f0fd428642338e488bf323b46f42"
MALIK_CANONICAL_SIZE = 5224529
MALIK_HEADING_IDS = frozenset({35830, 35835, 35838, 35841, 35845, 36014, 34213})


def _clean_str(val: Any) -> str | None:
    if val is None or not isinstance(val, str):
        return None
    if not val.strip():
        return None
    return val


def _no_duplicate_keys_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for k, v in pairs:
        if k in res:
            raise ValueError(f"Duplicate JSON object key: {k!r}")
        res[k] = v
    return res


def _reject_constant(token: str) -> None:
    raise ValueError(f"Nonfinite JSON constant {token!r} not allowed")


def _resolve_source_path(content_root: Path, slug: str) -> Path:
    if slug == "ahmad":
        return (
            content_root
            / "Open-Hadith-Data-master"
            / "Open-Hadith-Data-master"
            / "Musnad_Ahmad_Ibn-Hanbal"
            / "musnad_ahmad_ibn-hanbal_ahadith_mushakkala.utf8.csv"
        )
    return (
        content_root
        / "hadith-json-main"
        / "hadith-json-main"
        / "db"
        / "by_book"
        / "the_9_books"
        / f"{slug}.json"
    )


def stage_hadith_book(
    *,
    content_root: Path | str,
    raw_root: Path | str,
    book: str,
) -> Path:
    slug = book.strip().lower()
    if slug not in ALLOWED_SLUGS:
        raise ValueError(f"Unknown book slug {book!r}. Allowed slugs: {ALLOWED_SLUGS}")

    c_root = Path(content_root)
    r_root = Path(raw_root)

    # Security check 1: Symlinks/junctions
    if _is_symlink_or_junction(c_root):
        raise ValueError(f"Symlink or junction not permitted at content root: {c_root}")
    if _is_symlink_or_junction(r_root):
        raise ValueError(f"Symlink or junction not permitted at raw root: {r_root}")

    # Security check 2: Path containment
    c_res = c_root.resolve()
    r_res = r_root.resolve()
    if c_res == r_res or c_res in r_res.parents or r_res in c_res.parents:
        raise ValueError(
            f"Path containment violation: content_root ({c_root}) and raw_root ({r_root}) cannot contain each other"
        )

    source_path = _resolve_source_path(c_root, slug)
    if _is_symlink_or_junction(source_path):
        raise ValueError(f"Symlink or junction not permitted at source path: {source_path}")
    if not source_path.is_file():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    # Verify source SHA-256
    source_bytes = source_path.read_bytes()
    actual_source_hash = hashlib.sha256(source_bytes).hexdigest()
    expected_source_hash = SOURCE_HASHES[slug]
    if actual_source_hash != expected_source_hash:
        raise ValueError(
            f"Source file {source_path} SHA-256 mismatch: expected {expected_source_hash}, got {actual_source_hash}"
        )

    # Staging logic
    books: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    hadiths: list[dict[str, Any]] = []

    if slug == "ahmad":
        # CSV processing
        try:
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Ahmad CSV {source_path} is not valid UTF-8: {exc}") from exc

        reader = csv.reader(source_text.splitlines(), strict=True)
        idx = 1
        for row in reader:
            if len(row) != 2:
                raise ValueError(f"Ahmad CSV row {idx} has {len(row)} columns, expected exactly 2")
            expected_id_str = str(idx)
            if row[0] != expected_id_str:
                raise ValueError(f"Ahmad CSV row {idx} has record number {row[0]!r}, expected {expected_id_str!r}")
            ar_text = row[1]
            if not ar_text.strip():
                raise ValueError(f"Ahmad CSV row {idx} has empty Arabic text")

            hadiths.append({
                "id": idx,
                "bookId": 8,
                "chapterId": None,
                "hadithNumber": expected_id_str,
                "urn": None,
                "arabicFull": ar_text,
                "englishFull": None,
                "englishNarrator": None,
                "englishBody": None,
                "gradeAr": None,
                "gradeEn": None,
                "reference": {"inBook": None, "uscMsa": None},
                "textSplitMode": "unsplit_raw",
                "arabicSanad": None,
                "arabicMatn": None,
            })
            idx += 1

        if len(hadiths) != 26363:
            raise ValueError(f"Ahmad CSV row count {len(hadiths)} does not match expected 26363")

        books.append({
            "id": 8,
            "number": "8",
            "nameAr": "مسند أحمد بن حنبل",
            "nameEn": "Musnad Ahmad ibn Hanbal",
        })

    else:
        # JSON processing
        try:
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"JSON source {source_path} is not valid UTF-8: {exc}") from exc

        raw_data = json.loads(
            source_text,
            parse_float=Decimal,
            object_pairs_hook=_no_duplicate_keys_pairs_hook,
            parse_constant=_reject_constant,
        )

        declared_length = raw_data.get("metadata", {}).get("length")
        raw_hadiths: list[dict[str, Any]] = raw_data.get("hadiths", [])
        if declared_length != len(raw_hadiths):
            raise ValueError(
                f"{slug}: metadata length ({declared_length}) does not equal hadith count ({len(raw_hadiths)})"
            )

        meta = raw_data.get("metadata", {})
        book_id = int(raw_data["id"])
        books.append({
            "id": book_id,
            "number": str(book_id),
            "nameAr": _clean_str(meta.get("arabic", {}).get("title")),
            "nameEn": _clean_str(meta.get("english", {}).get("title")),
        })

        raw_chapters: list[dict[str, Any]] = raw_data.get("chapters", [])
        pos_ints = [c["id"] for c in raw_chapters if isinstance(c["id"], int) and c["id"] > 0]
        max_pos = max(pos_ints) if pos_ints else 0
        exceptional_ids = sorted([c["id"] for c in raw_chapters if not (isinstance(c["id"], int) and c["id"] > 0)])

        chapter_map: dict[Any, int] = {}
        for cid in pos_ints:
            chapter_map[cid] = cid

        next_id = max_pos + 1
        for exc_id in exceptional_ids:
            chapter_map[exc_id] = next_id
            next_id += 1

        for c in raw_chapters:
            orig_cid = c["id"]
            mapped_cid = chapter_map[orig_cid]
            number_str = str(orig_cid)
            chapters.append({
                "id": mapped_cid,
                "bookId": int(c["bookId"]),
                "number": number_str,
                "titleAr": _clean_str(c.get("arabic")),
                "titleEn": _clean_str(c.get("english")),
            })

        for h in raw_hadiths:
            hid = int(h["id"])
            hid_in_book = str(h["idInBook"])
            h_book_id = int(h["bookId"])
            raw_chid = h.get("chapterId")

            if raw_chid is None:
                mapped_chid = None
            elif raw_chid in chapter_map:
                mapped_chid = chapter_map[raw_chid]
            else:
                mapped_chid = int(raw_chid) if isinstance(raw_chid, int) and raw_chid > 0 else None

            ar_full = _clean_str(h.get("arabic"))
            en_obj = h.get("english", {})
            if isinstance(en_obj, dict):
                en_narrator = _clean_str(en_obj.get("narrator"))
                en_body = _clean_str(en_obj.get("text"))
            else:
                en_narrator = None
                en_body = None

            if en_body is None:
                en_full = None
            elif en_narrator is None:
                en_full = en_body
            else:
                en_full = f"{en_narrator}\n{en_body}"

            hadiths.append({
                "id": hid,
                "bookId": h_book_id,
                "chapterId": mapped_chid,
                "hadithNumber": hid_in_book,
                "urn": None,
                "arabicFull": ar_full,
                "englishFull": en_full,
                "englishNarrator": en_narrator,
                "englishBody": en_body,
                "gradeAr": None,
                "gradeEn": None,
                "reference": {"inBook": None, "uscMsa": None},
                "textSplitMode": "unsplit_raw",
                "arabicSanad": None,
                "arabicMatn": None,
            })

    # Sort arrays by ascending ID
    books.sort(key=lambda b: b["id"])
    chapters.sort(key=lambda c: c["id"])
    hadiths.sort(key=lambda h: h["id"])

    # Specific assertions for Malik
    if slug == "malik":
        if len(hadiths) != 1985 or len({h["id"] for h in hadiths}) != 1985:
            raise ValueError(f"Malik must have exactly 1985 unique hadiths, got {len(hadiths)}")
        if len(chapters) != 61:
            raise ValueError(f"Malik must have exactly 61 chapters, got {len(chapters)}")
        if not all(h["arabicFull"] is not None for h in hadiths):
            raise ValueError("Malik hadiths must all have nonblank arabicFull")
        if not all(h["englishBody"] is not None for h in hadiths):
            raise ValueError("Malik hadiths must all have nonblank englishBody")
        if not all(h["englishFull"] is not None for h in hadiths):
            raise ValueError("Malik hadiths must all have nonblank englishFull")
        # Verify heading rows are nonblank
        for h in hadiths:
            if h["id"] in MALIK_HEADING_IDS:
                if not h["arabicFull"] or not h["englishBody"]:
                    raise ValueError(f"Malik heading row {h['id']} has missing text")

    canonical_data = {
        "books": books,
        "chapters": chapters,
        "hadiths": hadiths,
    }

    raw_output = (
        json.dumps(
            canonical_data,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    output_bytes = raw_output.encode("utf-8")

    if slug == "malik":
        actual_size = len(output_bytes)
        actual_hash = hashlib.sha256(output_bytes).hexdigest()
        if actual_size != MALIK_CANONICAL_SIZE:
            raise ValueError(f"Malik canonical output size {actual_size} != expected {MALIK_CANONICAL_SIZE}")
        if actual_hash != MALIK_CANONICAL_SHA256:
            raise ValueError(f"Malik canonical output hash {actual_hash} != expected {MALIK_CANONICAL_SHA256}")

    out_path = r_root / "hadith" / f"{slug}.json"
    if _is_symlink_or_junction(out_path):
        raise ValueError(f"Symlink or junction not permitted at output path: {out_path}")

    # Idempotency check: if destination exists and equals new bytes, succeed without rewrite
    if out_path.exists():
        if out_path.read_bytes() == output_bytes:
            return out_path
        raise ValueError(f"Destination {out_path} already exists and differs from staged output (no overwrite)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(f".{slug}.tmp.{os.getpid()}")
    try:
        temp_path.write_bytes(output_bytes)
        # Validate complete canonical input
        load_hadith_input(temp_path)
        # Atomic rename
        temp_path.replace(out_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

    return out_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stage canonical Hadith input JSON")
    parser.add_argument("--content-root", type=Path, required=True, help="Path to Contant directory")
    parser.add_argument("--raw-root", type=Path, required=True, help="Path to raw-inputs directory")
    parser.add_argument("--book", type=str, required=True, help="Book slug")
    args = parser.parse_args(argv)

    try:
        out_path = stage_hadith_book(
            content_root=args.content_root,
            raw_root=args.raw_root,
            book=args.book,
        )
        print(f"Successfully staged {args.book} -> {out_path}")
    except Exception as exc:
        print(f"Error staging hadith book {args.book}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
