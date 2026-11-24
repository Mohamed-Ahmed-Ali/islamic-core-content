"""Shared pack loading and writing context for Quran packs."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys
from typing import Any

from pipelines.common.lock_inputs import verify_lock
from pipelines.common.pack_sources import (
    _VALID_PERMITTED_USES,
    _is_symlink_or_junction,
    _verify_exact_case_regular_file,
    load_sqlite_source_metadata,
)
from pipelines.common.sqlite_utils import (
    finalize_pack,
    open_pack,
    write_pack_metadata,
)

_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")


def load_quran_pack(
    sources_root: Path,
    pack_id: str,
    expected_paths: frozenset[str],
    *,
    fts_enabled: bool,
) -> tuple[Path, dict[str, str]]:
    """Verify pack lock and license, returning (raw_root, pack_metadata)."""
    sources_root = Path(sources_root)
    raw_root = sources_root / "raw-inputs"
    registry_path = sources_root / "sources" / "registry.json"
    lock_path = sources_root / "sources" / "manifest.lock.json"

    entries = verify_lock(registry_path, raw_root, lock_path, pack_id=pack_id)
    actual_paths = frozenset(e.path for e in entries)
    if actual_paths != expected_paths:
        diff_missing = sorted(expected_paths - actual_paths)
        diff_extra = sorted(actual_paths - expected_paths)
        raise ValueError(
            f"Locked paths for pack {pack_id!r} do not match expected paths. "
            f"Missing: {diff_missing}, extra: {diff_extra}"
        )

    metadata = load_sqlite_source_metadata(
        sources_root,
        pack_id,
        entries,
        pack_version="0.1.0",
        completeness="complete",
        fts5_enabled=fts_enabled,
    )
    return raw_root, metadata


@contextmanager
def write_quran_pack(
    output_path: Path,
    schema_path: Path,
    metadata: Mapping[str, str],
    coverage: Mapping[str, tuple[int, str | None]],
    *,
    fts_tables: tuple[str, ...],
) -> Iterator[Any]:
    """Context manager for pack writing with atomicity and cleanup on failure."""
    output_path = Path(output_path)
    schema_path = Path(schema_path)

    # open_pack opens a fresh database, sets PRAGMAs, and applies DDL
    conn = open_pack(output_path, schema_path)
    try:
        conn.execute("BEGIN;")
        yield conn
        write_pack_metadata(conn, metadata, coverage)
        finalize_pack(conn, fts_tables=fts_tables)
        conn.close()
        conn = None
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        # Discard only the owned failed DB and its sidecars
        for p in (output_path, *(output_path.with_name(output_path.name + s) for s in _SIDECAR_SUFFIXES)):
            try:
                if p.is_file() or _is_symlink_or_junction(p):
                    p.unlink(missing_ok=True)
            except Exception:
                pass
        raise
