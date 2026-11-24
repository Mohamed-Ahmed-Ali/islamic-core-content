"""Shared license and source metadata validation for SQLite packs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
from typing import Any

from pipelines.common.lock_inputs import (
    LockEntry,
    canonical_entries_json,
    parse_strict_json,
    validate_https_url,
    validate_safe_relative_path,
)

_VALID_PERMITTED_USES: frozenset[str] = frozenset({
    "public_redistribution",
    "commercial_use",
    "sqlite_conversion",
    "search_projection",
    "transcode",
    "clip",
    "fade",
    "loudness_normalization",
})


def _is_symlink_or_junction(p: Path | str) -> bool:
    if os.path.islink(p):
        return True
    if sys.platform == "win32" and hasattr(os.path, "isjunction") and os.path.isjunction(p):
        return True
    return False


def _verify_exact_case_regular_file(base_dir: Path, rel_path: str) -> Path:
    """Verify that rel_path exists under base_dir with exact case and is a regular file."""
    current = base_dir.resolve()
    components = rel_path.split("/")
    for idx, comp in enumerate(components):
        is_last = (idx == len(components) - 1)
        found_entry = None
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name == comp:
                        found_entry = entry
                        break
        except FileNotFoundError:
            raise FileNotFoundError(f"Directory not found: {current}")

        if found_entry is None:
            raise FileNotFoundError(
                f"Component {comp!r} of {rel_path!r} not found with exact case in {current}"
            )
        if _is_symlink_or_junction(found_entry.path):
            raise ValueError(f"Symlink or junction not permitted at {found_entry.path}")

        if is_last:
            if not found_entry.is_file(follow_symlinks=False):
                raise ValueError(f"Expected regular file at {found_entry.path}")
            current = Path(found_entry.path)
        else:
            if not found_entry.is_dir(follow_symlinks=False):
                raise ValueError(f"Expected directory at {found_entry.path}")
            current = Path(found_entry.path)

    return current


def load_sqlite_source_metadata(
    sources_root: Path,
    pack_id: str,
    entries: tuple[LockEntry, ...],
    *,
    primary_path: str | None = None,
    pack_version: str = "0.1.0",
    completeness: str = "complete",
    fts5_enabled: bool = False,
) -> dict[str, str]:
    """Verify pack license and metadata against locked entries, returning pack_meta dictionary."""
    sources_root = Path(sources_root)
    rel_lic_path = f"sources/licenses/{pack_id}.json"
    abs_lic_path = _verify_exact_case_regular_file(sources_root, rel_lic_path)

    raw_lic_bytes = abs_lic_path.read_bytes()
    try:
        lic_text = raw_lic_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"License file {rel_lic_path} is not valid UTF-8: {exc}") from exc

    lic_data: dict[str, Any] = parse_strict_json(lic_text)
    if not isinstance(lic_data, dict):
        raise ValueError(f"License root must be a JSON object: {rel_lic_path}")

    expected_lic_keys = {"packId", "licenseSpdx", "primaryPath", "status", "reviewedBy", "sources"}
    if set(lic_data.keys()) != expected_lic_keys:
        raise ValueError(
            f"License file {rel_lic_path} keys mismatch: expected {sorted(expected_lic_keys)}, got {sorted(lic_data.keys())}"
        )

    if lic_data["packId"] != pack_id:
        raise ValueError(f"packId mismatch in {rel_lic_path}: expected {pack_id!r}, got {lic_data['packId']!r}")

    lic_spdx = lic_data["licenseSpdx"]
    if not isinstance(lic_spdx, str) or not lic_spdx.strip():
        raise ValueError(f"licenseSpdx must be non-empty string in {rel_lic_path}")

    expected_paths = sorted(e.path for e in entries)
    target_primary = primary_path if primary_path is not None else lic_data["primaryPath"]
    if target_primary not in expected_paths:
        raise ValueError(f"primaryPath {target_primary!r} not in expected paths: {expected_paths}")

    status = lic_data["status"]
    if status not in ("approved", "pending"):
        raise ValueError(
            f"Production build rejects status {status!r} in {rel_lic_path} (must be 'approved' or 'pending')"
        )

    reviewed_by = lic_data["reviewedBy"]
    if status == "pending":
        if reviewed_by is not None:
            raise ValueError(f"reviewedBy must be null when status is 'pending' in {rel_lic_path}")
    elif status == "approved":
        if not isinstance(reviewed_by, str) or not reviewed_by.strip():
            raise ValueError(f"reviewedBy must be non-empty string when status is 'approved' in {rel_lic_path}")

    sources_list = lic_data["sources"]
    if not isinstance(sources_list, list):
        raise ValueError(f"sources must be a list in {rel_lic_path}")

    src_paths = [s.get("path") for s in sources_list if isinstance(s, dict)]
    if src_paths != expected_paths:
        raise ValueError(
            f"License sources list must be sorted by path and match expected paths: expected {expected_paths}, got {src_paths}"
        )

    expected_src_keys = {
        "attribution",
        "name",
        "notice",
        "path",
        "permittedUses",
        "rightsEvidence",
        "sourceUrl",
        "sourceVersion",
    }
    entries_by_path = {e.path: e for e in entries}
    primary_source: dict[str, Any] | None = None

    for src in sources_list:
        if not isinstance(src, dict):
            raise ValueError(f"Source entry must be an object in {rel_lic_path}")
        if set(src.keys()) != expected_src_keys:
            raise ValueError(f"Source keys mismatch in {rel_lic_path} for {src.get('path')}: got {sorted(src.keys())}")

        p = src["path"]
        entry = entries_by_path[p]

        if src["sourceUrl"] != entry.source_url:
            raise ValueError(f"sourceUrl mismatch in {rel_lic_path} for {p}: {src['sourceUrl']} != {entry.source_url}")
        if src["sourceVersion"] != entry.source_version:
            raise ValueError(f"sourceVersion mismatch in {rel_lic_path} for {p}: {src['sourceVersion']} != {entry.source_version}")

        if not isinstance(src["name"], str) or not src["name"].strip():
            raise ValueError(f"Empty source name in {rel_lic_path} for {p}")

        if not isinstance(src["notice"], str):
            raise ValueError(f"notice must be a string in {rel_lic_path} for {p}")
        if not isinstance(src["attribution"], str):
            raise ValueError(f"attribution must be a string in {rel_lic_path} for {p}")

        if not isinstance(src["rightsEvidence"], list):
            raise ValueError(f"rightsEvidence must be a list in {rel_lic_path} for {p}")
        for ev in src["rightsEvidence"]:
            if not isinstance(ev, str) or not ev.strip():
                raise ValueError(f"Invalid rightsEvidence entry in {rel_lic_path} for {p}")
            if ev.startswith("https://"):
                validate_https_url(ev)
            elif ev.startswith("sources/licenses/evidence/"):
                validate_safe_relative_path(ev)
                _verify_exact_case_regular_file(sources_root, ev)
            else:
                raise ValueError(
                    f"rightsEvidence entry must be HTTPS URL or exact-case path under sources/licenses/evidence/: {ev!r}"
                )

        if not isinstance(src["permittedUses"], list):
            raise ValueError(f"permittedUses must be a list in {rel_lic_path} for {p}")
        if len(src["permittedUses"]) != len(set(src["permittedUses"])):
            raise ValueError(f"permittedUses contains duplicates in {rel_lic_path} for {p}: {src['permittedUses']}")
        for u in src["permittedUses"]:
            if not isinstance(u, str) or u not in _VALID_PERMITTED_USES:
                raise ValueError(f"Invalid permittedUses entry {u!r} in {rel_lic_path} for {p}")

        if status == "approved":
            if not src["notice"].strip():
                raise ValueError(f"Empty source notice in approved {rel_lic_path} for {p}")
            if not src["attribution"].strip():
                raise ValueError(f"Empty source attribution in approved {rel_lic_path} for {p}")
            if not src["rightsEvidence"]:
                raise ValueError(f"Empty rightsEvidence in approved {rel_lic_path} for {p}")
            if not src["permittedUses"]:
                raise ValueError(f"Empty permittedUses in approved {rel_lic_path} for {p}")

        if p == target_primary:
            primary_source = src

    if primary_source is None:
        raise ValueError(f"Primary source {target_primary!r} not found in sources list: {rel_lic_path}")

    input_sha256 = hashlib.sha256(canonical_entries_json(entries)).hexdigest()

    metadata: dict[str, str] = {
        "pack_id": pack_id,
        "pack_version": pack_version,
        "schema_version": "1",
        "source_name": primary_source["name"],
        "source_url": primary_source["sourceUrl"],
        "source_version": primary_source["sourceVersion"],
        "license_spdx": lic_spdx,
        "license_ref": rel_lic_path,
        "build_input_sha256": input_sha256,
        "completeness": completeness,
        "fts5_enabled": "true" if fts5_enabled else "false",
    }

    return metadata
