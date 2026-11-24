"""Canonical Hadith input loader, schema validator, and semantic checks."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from pipelines.common.lock_inputs import parse_strict_json

_INT64_MIN = 1
_INT64_MAX = 9223372036854775807


@functools.lru_cache(maxsize=1)
def _load_hadith_schema() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "inputs" / "hadith-input.schema.json"
    if not schema_path.is_file():
        raise FileNotFoundError(f"Hadith schema not found at {schema_path}")
    raw_text = schema_path.read_text(encoding="utf-8")
    schema = parse_strict_json(raw_text)
    Draft202012Validator.check_schema(schema)
    return schema


def load_hadith_input(input_path: Path, *, splits_path: Path | None = None) -> dict[str, Any]:
    """Load canonical Hadith input JSON, validate against schema and semantic rules,
    and validate split evidence if verified rows exist.

    Never repairs or mutates input data.
    """
    input_p = Path(input_path)
    if not input_p.is_file():
        raise FileNotFoundError(f"Hadith input file not found: {input_p}")

    try:
        raw_bytes = input_p.read_bytes()
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Hadith input file {input_p} is not valid UTF-8: {exc}") from exc

    data = parse_strict_json(raw_text)
    if not isinstance(data, dict):
        raise ValueError(f"Hadith input {input_p} must be a JSON object")

    schema = _load_hadith_schema()
    validator = Draft202012Validator(schema)
    try:
        validator.validate(data)
    except ValidationError as exc:
        field_path = ".".join(str(p) for p in exc.absolute_path) or "root"
        raise ValueError(f"Hadith input {input_p} failed schema validation at '{field_path}': {exc.message}") from exc

    # Semantic checks
    books: list[dict[str, Any]] = data["books"]
    chapters: list[dict[str, Any]] = data["chapters"]
    hadiths: list[dict[str, Any]] = data["hadiths"]

    # 1. 64-bit integer bounds and duplicate IDs per array
    book_ids: set[int] = set()
    for b in books:
        bid = b["id"]
        if not isinstance(bid, int) or isinstance(bid, bool) or not (_INT64_MIN <= bid <= _INT64_MAX):
            raise ValueError(f"{input_p}: book id {bid} outside 64-bit positive integer range")
        if bid in book_ids:
            raise ValueError(f"{input_p}: duplicate book id {bid}")
        book_ids.add(bid)

    chapter_ids: set[int] = set()
    chapter_book_map: dict[int, int] = {}
    for c in chapters:
        cid = c["id"]
        if not isinstance(cid, int) or isinstance(cid, bool) or not (_INT64_MIN <= cid <= _INT64_MAX):
            raise ValueError(f"{input_p}: chapter id {cid} outside 64-bit positive integer range")
        if cid in chapter_ids:
            raise ValueError(f"{input_p}: duplicate chapter id {cid}")
        chapter_ids.add(cid)
        chapter_book_map[cid] = c["bookId"]

        # Dangling bookId in chapters
        if c["bookId"] not in book_ids:
            raise ValueError(f"{input_p}: chapter id {cid} has dangling bookId {c['bookId']}")

    hadith_ids: set[int] = set()
    seen_urns: set[str] = set()
    verified_hadiths: list[dict[str, Any]] = []

    for h in hadiths:
        hid = h["id"]
        if not isinstance(hid, int) or isinstance(hid, bool) or not (_INT64_MIN <= hid <= _INT64_MAX):
            raise ValueError(f"{input_p}: hadith id {hid} outside 64-bit positive integer range")
        if hid in hadith_ids:
            raise ValueError(f"{input_p}: duplicate hadith id {hid}")
        hadith_ids.add(hid)

        # Unique non-null URN
        urn = h.get("urn")
        if urn is not None:
            if urn in seen_urns:
                raise ValueError(f"{input_p}: duplicate non-null urn {urn!r} at hadith id {hid}")
            seen_urns.add(urn)

        # Dangling bookId in hadiths
        if h["bookId"] not in book_ids:
            raise ValueError(f"{input_p}: hadith id {hid} has dangling bookId {h['bookId']}")

        # Dangling chapterId & cross-book chapter validation
        chid = h.get("chapterId")
        if chid is not None:
            if chid not in chapter_ids:
                raise ValueError(f"{input_p}: hadith id {hid} has dangling chapterId {chid}")
            if chapter_book_map[chid] != h["bookId"]:
                raise ValueError(
                    f"{input_p}: hadith id {hid} has chapterId {chid} belonging to bookId {chapter_book_map[chid]}, "
                    f"mismatched with hadith bookId {h['bookId']} (cross-book chapter)"
                )

        if h.get("textSplitMode") == "source_verified":
            verified_hadiths.append(h)

    # 2. Split evidence validation
    if verified_hadiths:
        if splits_path is None:
            raise ValueError(
                f"{input_p}: splits evidence file is required when source_verified rows exist "
                f"({len(verified_hadiths)} verified rows present)"
            )
        sp = Path(splits_path)
        if not sp.is_file():
            raise FileNotFoundError(f"Splits evidence file not found: {sp}")

        try:
            splits_raw_bytes = sp.read_bytes()
            splits_raw_text = splits_raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Splits evidence file {sp} is not valid UTF-8: {exc}") from exc

        splits_data = parse_strict_json(splits_raw_text)
        if not isinstance(splits_data, dict) or not splits_data:
            raise ValueError(f"Splits evidence file {sp} must be a non-empty object (reject zero verified rows)")

        expected_keys = {str(h["id"]) for h in verified_hadiths}
        actual_keys = set(splits_data.keys())
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise ValueError(
                f"Splits evidence file {sp} keys mismatch: missing verified IDs {missing}, extra IDs {extra}"
            )

        hadith_lookup = {h["id"]: h for h in verified_hadiths}
        for k_str, entry in splits_data.items():
            hid = int(k_str)
            h = hadith_lookup[hid]
            if not isinstance(entry, dict):
                raise ValueError(f"Splits evidence entry for hadith id {hid} must be an object")

            entry_keys = set(entry.keys())
            expected_entry_keys = {"sourceRef", "arabicSanad", "arabicMatn"}
            if entry_keys != expected_entry_keys:
                raise ValueError(
                    f"Splits evidence entry for hadith id {hid} must contain exactly "
                    f"{expected_entry_keys}, got {entry_keys}"
                )

            for key in ("sourceRef", "arabicSanad", "arabicMatn"):
                val = entry[key]
                if not isinstance(val, str) or not val.strip():
                    raise ValueError(f"Splits evidence entry for hadith id {hid} field {key!r} must be a non-empty string")

            if entry["arabicSanad"] != h["arabicSanad"]:
                raise ValueError(
                    f"Splits evidence entry for hadith id {hid} arabicSanad mismatch with canonical hadith text"
                )
            if entry["arabicMatn"] != h["arabicMatn"]:
                raise ValueError(
                    f"Splits evidence entry for hadith id {hid} arabicMatn mismatch with canonical hadith text"
                )

    else:
        if splits_path is not None:
            raise ValueError(
                f"{input_p}: splits evidence file provided ({splits_path}) but 0 verified rows exist in hadiths"
            )

    return data
