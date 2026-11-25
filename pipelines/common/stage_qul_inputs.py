"""Staging and audit pipeline for Quranic Universal Library (QUL) input archives."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any
import zipfile

from pipelines.common.lock_inputs import (
    _is_symlink_or_junction,
    parse_and_validate_registry,
    validate_safe_relative_path,
)
from pipelines.common.qul_input import (
    open_sqlite_from_zip,
    read_zip_strict_json,
    validate_zip_member,
)



def _validate_path_hierarchy(
    path: Path | str,
    *,
    must_exist: bool,
) -> Path:
    """Validate each existing component of full lexical path from anchor for symlinks, case collisions, and type."""
    path_obj = Path(path)
    if ".." in path_obj.parts:
        raise ValueError(f"Parent directory traversal ('..') not permitted in path: {path}")

    abs_path = path_obj.absolute()
    if ".." in abs_path.parts:
        raise ValueError(f"Parent directory traversal ('..') not permitted in path: {path}")

    anchor = abs_path.anchor
    if not anchor:
        raise ValueError(f"Path has no anchor: {path}")

    current = Path(anchor)
    if _is_symlink_or_junction(current):
        raise ValueError(f"Symlink or junction not permitted at anchor: {current}")

    if not current.is_dir(follow_symlinks=False):
        if must_exist:
            raise FileNotFoundError(f"Anchor directory not found: {current}")
        return abs_path

    components = abs_path.relative_to(anchor).parts
    for idx, comp in enumerate(components):
        is_last = idx == len(components) - 1
        comp_fold = comp.casefold()

        exact_entry: os.DirEntry[str] | None = None
        if current.is_dir(follow_symlinks=False):
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name.casefold() == comp_fold:
                        if entry.name != comp:
                            raise ValueError(
                                f"Case collision or wrong-case component in {current}: {entry.name!r} vs {comp!r}"
                            )
                        exact_entry = entry

        if exact_entry is None:
            if must_exist:
                raise FileNotFoundError(f"Component {comp!r} not found in {current}")
            return abs_path

        if _is_symlink_or_junction(exact_entry.path):
            raise ValueError(f"Symlink or junction not permitted at {exact_entry.path}")

        if is_last:
            if not exact_entry.is_file(follow_symlinks=False):
                raise ValueError(f"Expected regular file at {exact_entry.path}")
            current = Path(exact_entry.path)
        else:
            if not exact_entry.is_dir(follow_symlinks=False):
                raise ValueError(f"Expected directory at {exact_entry.path}")
            current = Path(exact_entry.path)

    return current


_SHA256_HEX_CHARS = frozenset("0123456789abcdef")


def load_qul_registry_inventory(
    registry_path: Path | str = "sources/registry.json",
) -> list[tuple[str, str, str]]:
    """Load QUL inventory from registry.json.

    Returns list of (rel_path, expected_sha256, expected_member) in registry order.
    """
    reg_path = Path(registry_path)
    data = parse_and_validate_registry(reg_path)

    inventory: list[tuple[str, str, str]] = []
    for path_key, entry in data.items():
        if not path_key.startswith("qul/"):
            continue
        rel_path = path_key.removeprefix("qul/")
        if not rel_path.endswith(".zip"):
            raise ValueError(f"QUL registry entry {path_key!r} is not a ZIP file")

        version = entry["sourceVersion"]
        if not version.startswith("sha256:"):
            raise ValueError(f"Entry {path_key} missing sha256: prefix in sourceVersion")
        expected_sha256 = version.removeprefix("sha256:")
        if len(expected_sha256) != 64 or not all(c in _SHA256_HEX_CHARS for c in expected_sha256):
            raise ValueError(
                f"Entry {path_key} has invalid sha256 hash (must be 64 lowercase hex): {expected_sha256!r}"
            )

        file_name = Path(rel_path).name
        if file_name == "ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-957-Muallim.json.zip":
            expected_member = "ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-957.json"
        else:
            expected_member = file_name.removesuffix(".zip")

        inventory.append((rel_path, expected_sha256, expected_member))

    if len(inventory) != 21:
        raise ValueError(f"Expected 21 QUL entries in registry, found {len(inventory)}")

    return inventory


def stage_qul_file(
    *,
    content_root: Path | str,
    raw_root: Path | str,
    rel_path: str,
    expected_sha256: str | None = None,
    expected_member: str | None = None,
) -> Path:
    validate_safe_relative_path(rel_path)
    c_root = Path(content_root)
    r_root = Path(raw_root)
    if ".." in c_root.parts or ".." in r_root.parts:
        raise ValueError("Parent directory traversal ('..') not permitted in root paths")

    # Verify containment
    c_abs = c_root.absolute()
    r_abs = r_root.absolute()
    if c_abs == r_abs or c_abs in r_abs.parents or r_abs in c_abs.parents:
        raise ValueError("content_root and raw_root cannot contain each other")

    # Validate source hierarchy with exact case and no symlinks
    src_path = (c_root / rel_path).absolute()
    _validate_path_hierarchy(src_path, must_exist=True)
    source_bytes = src_path.read_bytes()

    # Verify SHA-256
    actual_sha256 = hashlib.sha256(source_bytes).hexdigest().lower()
    if expected_sha256 is not None and actual_sha256 != expected_sha256.lower():
        raise ValueError(
            f"Source {rel_path} SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )

    # Determine expected member name if not provided
    if expected_member is None:
        file_name = Path(rel_path).name
        if file_name == "ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-957-Muallim.json.zip":
            expected_member = "ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-957.json"
        else:
            expected_member = file_name.removesuffix(".zip")

    # Validate zip member without extracting paths
    with zipfile.ZipFile(io.BytesIO(source_bytes)) as zf:
        validate_zip_member(zf, expected_member)

    # Destination path validation
    out_rel = f"qul/{rel_path}"
    out_path = r_root / out_rel
    out_abs = out_path.absolute()
    _validate_path_hierarchy(out_abs, must_exist=False)

    # Idempotent check if destination file already exists
    if out_abs.exists():
        if _is_symlink_or_junction(out_abs):
            raise ValueError(f"Destination is a symlink or junction: {out_path}")
        if not out_abs.is_file():
            raise ValueError(f"Destination exists but is not a regular file: {out_path}")
        existing_bytes = out_abs.read_bytes()
        if existing_bytes == source_bytes:
            return out_path
        raise ValueError(
            f"Destination {out_path} already exists and differs from source bytes (no overwrite)"
        )

    # Create destination directories component-by-component
    for parent_dir in reversed(out_abs.parents):
        if not parent_dir.exists():
            parent_dir.mkdir(exist_ok=True)

    # Exclusive atomic creation without overwrite
    try:
        with open(out_abs, "xb") as f:
            f.write(source_bytes)
    except FileExistsError:
        raise ValueError(
            f"Destination {out_path} already exists (no overwrite)"
        )

    return out_path


def stage_all_qul_inputs(
    *,
    content_root: Path | str,
    raw_root: Path | str,
    registry_path: Path | str = "sources/registry.json",
) -> list[Path]:
    """Stage all 21 QUL input archives from content_root to raw_root."""
    inventory = load_qul_registry_inventory(registry_path)
    staged: list[Path] = []
    for rel_path, expected_sha256, expected_member in inventory:
        out = stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=expected_sha256,
            expected_member=expected_member,
        )
        staged.append(out)
    return staged


def audit_wbw_sqlite_vs_json(
    *,
    json_archive_path: Path | str,
    db_archive_path: Path | str,
    table_name: str,
) -> None:
    """Audit WBW SQLite table against JSON row-for-row by key and unchanged value."""
    j_path = Path(json_archive_path)
    d_path = Path(db_archive_path)

    if not j_path.is_file():
        raise FileNotFoundError(f"JSON archive not found: {j_path}")
    if not d_path.is_file():
        raise FileNotFoundError(f"DB archive not found: {d_path}")

    json_data = read_zip_strict_json(j_path)
    if not isinstance(json_data, dict):
        raise ValueError(f"JSON data in {j_path} must be an object")

    if table_name not in ("word_translation", "word_transliteration"):
        raise ValueError(f"Invalid table name for WBW audit: {table_name!r}")

    con = open_sqlite_from_zip(d_path)
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT surah_number, ayah_number, word_number, text FROM {table_name}"
        )
        sqlite_rows: dict[str, str] = {}
        for row in cur:
            surah, ayah, word, text = row
            if type(surah) is not int or surah <= 0 or type(ayah) is not int or ayah <= 0:
                raise ValueError(
                    f"Non-positive or non-integer coordinate in SQLite {table_name}: {(surah, ayah, word)!r}"
                )
            if type(word) is int:
                if word <= 0:
                    raise ValueError(f"Non-positive word number in SQLite {table_name}: {word!r}")
                w_str = str(word)
            elif type(word) is str:
                if not word.isdigit() or str(int(word)) != word or int(word) <= 0:
                    raise ValueError(f"Non-canonical word coordinate in SQLite {table_name}: {word!r}")
                w_str = word
            else:
                raise ValueError(f"Invalid word coordinate type in SQLite {table_name}: {type(word)}")

            if type(text) is not str:
                raise ValueError(
                    f"Non-string text in SQLite {table_name} at {(surah, ayah, word)!r}: {type(text)}"
                )

            key = f"{surah}:{ayah}:{w_str}"
            if key in sqlite_rows:
                raise ValueError(
                    f"Duplicate key {key!r} detected in SQLite table {table_name}"
                )
            sqlite_rows[key] = text
    finally:
        con.close()

    if len(sqlite_rows) != len(json_data):
        raise ValueError(
            f"Row count mismatch between SQLite ({len(sqlite_rows)}) and JSON ({len(json_data)}) for {table_name}"
        )

    for k, j_val in json_data.items():
        if type(k) is not str or type(j_val) is not str:
            raise ValueError(f"Non-string key or value in JSON data: {(k, j_val)!r}")
        if k not in sqlite_rows:
            raise ValueError(f"Key {k!r} in JSON missing from SQLite {table_name}")
        if sqlite_rows[k] != j_val:
            raise ValueError(
                f"Mismatch between SQLite and JSON for key {k!r}: SQLite={sqlite_rows[k]!r}, JSON={j_val!r}"
            )


def audit_all_wbw_inputs(raw_root: Path | str) -> None:
    """Audit both English translation and transliteration SQLite vs JSON in raw_root/qul/wbw."""
    r_root = Path(raw_root)
    qul_wbw = r_root / "qul" / "wbw"

    audit_wbw_sqlite_vs_json(
        json_archive_path=qul_wbw / "english-wbw-translation.json.zip",
        db_archive_path=qul_wbw / "english-wbw-translation.db.zip",
        table_name="word_translation",
    )
    audit_wbw_sqlite_vs_json(
        json_archive_path=qul_wbw / "english-wbw-transliteration.json.zip",
        db_archive_path=qul_wbw / "english-wbw-transliteration.db.zip",
        table_name="word_transliteration",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage QUL inputs and audit WBW alternate format")
    parser.add_argument("--content-root", type=Path, required=True, help="Path to Contant directory")
    parser.add_argument("--raw-root", type=Path, required=True, help="Path to raw-inputs directory")
    parser.add_argument("--registry", type=Path, default=Path("sources/registry.json"), help="Path to registry.json")
    parser.add_argument("--audit-wbw", action="store_true", help="Audit staged WBW SQLite vs JSON")
    args = parser.parse_args(argv)

    try:
        staged = stage_all_qul_inputs(
            content_root=args.content_root,
            raw_root=args.raw_root,
            registry_path=args.registry,
        )
        print(f"Successfully staged {len(staged)} QUL input archives to {args.raw_root / 'qul'}")

        if args.audit_wbw:
            audit_all_wbw_inputs(args.raw_root)
            print("Successfully audited WBW SQLite vs JSON row-for-row equivalence")
        return 0
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"Error staging QUL inputs: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
