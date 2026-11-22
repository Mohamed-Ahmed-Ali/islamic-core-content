"""Quran Jalalayn tafsir pack builder CLI and pipeline."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
import re
import sys
from typing import Any, NamedTuple

from jsonschema import Draft202012Validator, ValidationError

from pipelines.common.lock_inputs import parse_strict_json
from pipelines.common.quran_pack import load_quran_pack, write_quran_pack
from pipelines.common.search_text import project_tafsir_text
from pipelines.common.tanzil_coordinates import (
    load_tanzil_coordinates,
    load_tanzil_text,
)
from pipelines.toolchain.check_runtime import verify_runtime

PACK_ID = "quran-tafsir-ar-jalalayn"
EXPECTED_PATHS = frozenset({
    "tanzil/quran-data-Tanzil.xml",
    "tanzil/ar.jalalayn.xml",
})

_VERSE_KEY_RE = re.compile(r"^([1-9][0-9]*):([1-9][0-9]*)$")


class TafsirEntryRow(NamedTuple):
    id: int
    group_key: str
    start_ayah_id: int
    end_ayah_id: int
    text: str
    text_format: str
    text_search: str


class TafsirMapRow(NamedTuple):
    ayah_id: int
    verse_key: str
    entry_id: int


def _make_tafsir_entry_row(
    *,
    group_key: str,
    text: str,
    text_format: str,
    language: str,
    members: list[tuple[int, str]],
) -> TafsirEntryRow:
    """Construct TafsirEntryRow deriving start/id/end from members."""
    text_search = project_tafsir_text(text, text_format=text_format, language=language)
    if not text_search.strip():
        raise ValueError(f"Projected search text is empty or blank for group {group_key!r}")

    entry_id = members[0][0]
    return TafsirEntryRow(
        id=entry_id,
        group_key=group_key,
        start_ayah_id=entry_id,
        end_ayah_id=members[-1][0],
        text=text,
        text_format=text_format,
        text_search=text_search,
    )


def _validate_ayah_ids_profile(ayah_ids: Mapping[str, int]) -> None:
    """Validate that ayah_ids is a non-empty mapping with canonical keys and consistent IDs."""
    if not isinstance(ayah_ids, Mapping) or not ayah_ids:
        raise ValueError("ayah_ids must be a non-empty mapping of canonical verse keys to global IDs")

    parsed_keys: list[tuple[int, int, str]] = []
    seen_ids: set[int] = set()

    for vkey, aid in ayah_ids.items():
        if not isinstance(vkey, str):
            raise ValueError(f"Invalid verse_key type in ayah_ids: {type(vkey).__name__}")
        m = _VERSE_KEY_RE.fullmatch(vkey)
        if not m:
            raise ValueError(f"Non-canonical verse_key in ayah_ids: {vkey!r}")
        s_num, a_num = int(m.group(1)), int(m.group(2))
        parsed_keys.append((s_num, a_num, vkey))

        if isinstance(aid, bool) or not isinstance(aid, int) or aid < 1 or aid > 6236:
            raise ValueError(f"Invalid global ayah_id for {vkey}: {aid!r} (must be integer between 1 and 6236)")
        if aid in seen_ids:
            raise ValueError(f"Duplicate global ayah_id {aid} in ayah_ids for key {vkey}")
        seen_ids.add(aid)

    # Sort keys by canonical verse order (surah, ayah)
    parsed_keys.sort(key=lambda t: (t[0], t[1]))

    for idx in range(len(parsed_keys) - 1):
        s1, a1, k1 = parsed_keys[idx]
        s2, a2, k2 = parsed_keys[idx + 1]
        id1 = ayah_ids[k1]
        id2 = ayah_ids[k2]
        if id2 <= id1:
            raise ValueError(
                f"nonmonotonic order inconsistent with canonical verse order: {k1}={id1} >= {k2}={id2}"
            )
        if s1 == s2 and (id2 - a2) != (id1 - a1):
            raise ValueError(
                f"inconsistent global ID offset or non-consecutive IDs for {k1}={id1}, {k2}={id2}"
            )

def build_grouped_tafsir_rows(
    path: Path,
    ayah_ids: Mapping[str, int],
    *,
    language: str,
) -> tuple[list[TafsirEntryRow], list[TafsirMapRow]]:
    """Parse and validate synthetic grouped Tafsir JSON, returning entries and map rows."""
    path = Path(path)
    raw_bytes = path.read_bytes()
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Tafsir input file {path} is not valid UTF-8: {exc}") from exc

    data = parse_strict_json(raw_text)

    _validate_ayah_ids_profile(ayah_ids)

    # Validate shape using pinned jsonschema Draft202012Validator
    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "inputs" / "tafsir-input.schema.json"
    schema = parse_strict_json(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    try:
        validator.validate(data)
    except ValidationError as exc:
        raise ValueError(f"Tafsir input {path} failed schema validation: {exc.message}") from exc

    entries: list[TafsirEntryRow] = []
    map_rows: list[TafsirMapRow] = []
    seen_keys: set[str] = set()
    last_first_aid = 0

    for group_idx, group in enumerate(data):
        vkeys: list[str] = group["verse_keys"]
        k0 = vkeys[0]
        if k0 not in ayah_ids:
            raise ValueError(f"unknown key in group: {k0!r}")
        if k0 in seen_keys:
            raise ValueError(f"overlap: key {k0!r} already appeared in a previous group")
        seen_keys.add(k0)

        first_aid = ayah_ids[k0]
        if first_aid <= last_first_aid:
            raise ValueError(
                f"groups not strictly ordered by global first ID: {first_aid} <= {last_first_aid}"
            )
        last_first_aid = first_aid

        s_str, a_str = k0.split(":")
        surah_id = int(s_str)
        start_ayah = int(a_str)
        members: list[tuple[int, str]] = [(first_aid, k0)]

        for idx in range(1, len(vkeys)):
            k = vkeys[idx]
            if k not in ayah_ids:
                raise ValueError(f"unknown key in group: {k!r}")
            if k in seen_keys:
                raise ValueError(f"overlap: key {k!r} already appeared in a previous group")
            seen_keys.add(k)

            s_k, a_k = k.split(":")
            if int(s_k) != surah_id:
                raise ValueError(f"cross-surah group not permitted in group {group_idx}: {vkeys}")
            if int(a_k) != start_ayah + idx:
                raise ValueError(
                    f"keys not strictly ascending numerically consecutive in group {group_idx}: {vkeys}"
                )
            members.append((ayah_ids[k], k))

        group_key = k0 if len(vkeys) == 1 else f"{surah_id}:{start_ayah}-{start_ayah + len(vkeys) - 1}"
        entry = _make_tafsir_entry_row(
            group_key=group_key,
            text=group["text"],
            text_format=group["format"],
            language=language,
            members=members,
        )
        entries.append(entry)
        for aid, vkey in members:
            map_rows.append(TafsirMapRow(ayah_id=aid, verse_key=vkey, entry_id=entry.id))

    expected_keys = set(ayah_ids.keys())
    if seen_keys != expected_keys:
        missing = sorted(expected_keys - seen_keys)
        extra = sorted(seen_keys - expected_keys)
        raise ValueError(
            f"Tafsir groups do not match expected ayah_ids union. Missing: {missing}, Extra: {extra}"
        )

    return entries, map_rows


def build_quran_tafsir(sources_root: Path, output_dir: Path) -> None:
    """Build quran-tafsir-ar-jalalayn.db deterministically."""
    verify_runtime()

    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    raw_root, metadata = load_quran_pack(
        sources_root,
        PACK_ID,
        EXPECTED_PATHS,
        fts_enabled=True,
    )

    meta_file = raw_root / "tanzil" / "quran-data-Tanzil.xml"
    jalalayn_file = raw_root / "tanzil" / "ar.jalalayn.xml"

    coords = load_tanzil_coordinates(meta_file)
    jalalayn_text = load_tanzil_text(
        jalalayn_file,
        coords,
        locked_path="tanzil/ar.jalalayn.xml",
    )

    coverage: dict[str, tuple[int, str | None]] = {
        "expected_total": (6236, None),
        "ar_present": (6236, None),
    }

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "sql" / "quran_tafsir.v1.sql"
    output_path = output_dir / "quran-tafsir-ar-jalalayn.db"

    with write_quran_pack(
        output_path,
        schema_path,
        metadata,
        coverage,
        fts_tables=("tafsir_fts",),
    ) as conn:
        conn.executemany(
            "INSERT INTO tafsir_entries ("
            "id, group_key, start_ayah_id, end_ayah_id, text, text_format, text_search"
            ") VALUES (?, ?, ?, ?, ?, ?, ?);",
            (
                _make_tafsir_entry_row(
                    group_key=vkey,
                    text=jalalayn_text[vkey],
                    text_format="plain",
                    language="ar",
                    members=[(aid, vkey)],
                )
                for vkey, aid in coords.ayah_ids.items()
            ),
        )
        conn.executemany(
            "INSERT INTO tafsir_ayah_map ("
            "ayah_id, verse_key, entry_id"
            ") VALUES (?, ?, ?);",
            (
                TafsirMapRow(ayah_id=aid, verse_key=vkey, entry_id=aid)
                for vkey, aid in coords.ayah_ids.items()
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build quran-tafsir-ar-jalalayn SQLite pack.")
    parser.add_argument("--sources-root", required=True, type=Path, help="Root directory containing sources/ and raw-inputs/")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory where quran-tafsir-ar-jalalayn.db will be written")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_quran_tafsir(args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building quran-tafsir-ar-jalalayn: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
