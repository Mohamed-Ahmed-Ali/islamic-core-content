"""Staging pipeline for Mashrūʿ Taqrīb as-Sunnah raw JSON exports."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

from pipelines.common.lock_inputs import (
    _is_symlink_or_junction,
    parse_and_validate_lock,
)
from pipelines.common.stage_qul_inputs import _validate_path_hierarchy

TAQREEB_SOURCES: dict[str, tuple[int, str]] = {
    "hadith-taqreeb-jami": (1, "01_jame3_al_usul_al_tisah.json"),
    "hadith-taqreeb-maalim": (2, "02_maalem_al_sunnah_al_nabawiyyah.json"),
    "hadith-taqreeb-wajeez": (3, "03_al_wajeez.json"),
    "hadith-taqreeb-kulliyyah": (4, "04_al_ahadith_al_kulliyyah.json"),
}


def _hash_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size_bytes = 0
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
            size_bytes += len(chunk)
    return size_bytes, hasher.hexdigest().lower()


def stage_taqreeb_inputs(content_root: Path, raw_root: Path, lock_path: Path) -> list[Path]:
    """Stage four Taqreeb JSON exports from content_root to raw_root based on lockfile."""
    c_path = Path(content_root)
    r_path = Path(raw_root)
    l_path = Path(lock_path)

    if not c_path.exists() or not c_path.is_dir():
        raise FileNotFoundError(f"Content root does not exist or is not a directory: {c_path}")

    if _is_symlink_or_junction(c_path):
        raise ValueError(f"Symlink or junction not permitted at content root: {c_path}")
    if r_path.exists() and _is_symlink_or_junction(r_path):
        raise ValueError(f"Symlink or junction not permitted at raw root: {r_path}")

    c_resolved = c_path.resolve()
    r_resolved = r_path.resolve()
    if c_resolved == r_resolved:
        raise ValueError(f"Content root and raw root cannot be the same or nested: {c_path}")

    try:
        r_resolved.relative_to(c_resolved)
        raise ValueError(f"Content root and raw root cannot be the same or nested: {r_path} inside {c_path}")
    except ValueError as err:
        if "cannot be the same or nested" in str(err):
            raise

    try:
        c_resolved.relative_to(r_resolved)
        raise ValueError(f"Content root and raw root cannot be the same or nested: {c_path} inside {r_path}")
    except ValueError as err:
        if "cannot be the same or nested" in str(err):
            raise

    lock_entries = parse_and_validate_lock(l_path)
    lock_map = {e.path: e for e in lock_entries}

    source_dir = c_path / "hadith" / "Hadith Saleh al-Shami"
    dest_dir = r_path / "hadith-taqreeb"

    preflight_plan: list[tuple[Path, Path, int, str, bool]] = []

    for _pack_id, (_book_id, basename) in TAQREEB_SOURCES.items():
        rel_lock_path = f"hadith-taqreeb/{basename}"
        lock_entry = lock_map.get(rel_lock_path)
        if lock_entry is None:
            raise ValueError(f"Lock entry missing for {rel_lock_path}")

        src_raw = source_dir / basename
        src_validated = _validate_path_hierarchy(src_raw, must_exist=True)

        src_size, src_sha256 = _hash_file(src_validated)
        if src_size != lock_entry.size_bytes:
            raise ValueError(
                f"Source size mismatch for {basename}: got {src_size}, expected {lock_entry.size_bytes}"
            )
        if src_sha256 != lock_entry.sha256.lower():
            raise ValueError(
                f"Source SHA-256 mismatch for {basename}: got {src_sha256}, expected {lock_entry.sha256}"
            )

        dest_raw = dest_dir / basename
        dest_validated = _validate_path_hierarchy(dest_raw, must_exist=False)

        skip_write = False
        if dest_validated.exists():
            dest_existing = _validate_path_hierarchy(dest_validated, must_exist=True)
            if not dest_existing.is_file(follow_symlinks=False) or _is_symlink_or_junction(dest_existing):
                raise ValueError(f"Pre-existing destination is not a regular file: {dest_existing}")
            dest_size, dest_sha256 = _hash_file(dest_existing)
            if dest_size != lock_entry.size_bytes or dest_sha256 != lock_entry.sha256.lower():
                raise ValueError(
                    f"Destination conflict at {dest_existing}: differing bytes/hash from canonical lock"
                )
            skip_write = True

        preflight_plan.append((src_validated, dest_validated, src_size, src_sha256, skip_write))

    staged_paths: list[Path] = []
    dest_dir.mkdir(parents=True, exist_ok=True)

    for src, dest, size, _sha256, skip_write in preflight_plan:
        if not skip_write:
            with open(src, "rb") as in_f, open(dest, "xb") as out_f:
                while chunk := in_f.read(65536):
                    out_f.write(chunk)
        staged_paths.append(dest)

    return staged_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage canonical Taqreeb JSON inputs")
    parser.add_argument("--content-root", required=True, type=Path, help="Base content directory")
    parser.add_argument("--raw-root", required=True, type=Path, help="Raw inputs destination directory")
    parser.add_argument("--lock", required=True, type=Path, help="Path to sources/manifest.lock.json")

    args = parser.parse_args(argv)

    try:
        stage_taqreeb_inputs(args.content_root, args.raw_root, args.lock)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error staging Taqreeb inputs: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
