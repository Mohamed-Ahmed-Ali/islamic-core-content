"""Deterministic SQLite connection and pack lifecycle management."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import os
import re
import sqlite3
import sys

_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")

_FTS5_VIRTUAL_TABLE_PATTERN = re.compile(
    r"^\s*CREATE\s+VIRTUAL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z0-9_]+)\.)?(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z0-9_]+)\s+USING\s+fts5\b",
    re.IGNORECASE,
)


def _is_link_or_junction(p: Path | str) -> bool:
    if os.path.islink(p):
        return True
    if sys.platform == "win32" and hasattr(os.path, "isjunction") and os.path.isjunction(p):
        return True
    return False


def _quote_identifier(ident: str) -> str:
    if not isinstance(ident, str) or not ident or "\x00" in ident:
        raise ValueError(f"Invalid identifier: {ident!r}")
    return '"' + ident.replace('"', '""') + '"'

def open_pack(path: Path, schema_path: Path) -> sqlite3.Connection:
    """Open a fresh SQLite database file for pack writing and apply DDL.

    Enforces:
    - Rejects existing database files or sidecar files (-wal, -shm, -journal).
    - PRAGMA encoding = 'UTF-8'
    - PRAGMA page_size = 4096
    - PRAGMA foreign_keys = ON
    - PRAGMA journal_mode = DELETE
    - PRAGMA user_version = 1
    - Applies common_meta.sql then the pack DDL.
    - Returns writer connection in autocommit mode with no transaction open.
    """
    path = Path(path)
    schema_path = Path(schema_path)

    if _is_link_or_junction(path) or os.path.lexists(path) or path.exists():
        raise FileExistsError(f"Database file or link already exists: {path}")

    for suffix in _SIDECAR_SUFFIXES:
        sidecar = path.with_name(path.name + suffix)
        if _is_link_or_junction(sidecar) or os.path.lexists(sidecar) or sidecar.exists():
            raise FileExistsError(f"SQLite sidecar file or link exists: {sidecar}")

    if _is_link_or_junction(schema_path):
        raise ValueError(f"Schema path cannot be a link: {schema_path}")
    if not schema_path.is_file():
        raise FileNotFoundError(f"Pack schema file not found: {schema_path}")

    common_path = Path(__file__).resolve().parents[2] / "specs" / "sql" / "common_meta.sql"
    if not common_path.is_file():
        raise FileNotFoundError(f"Missing checked-in common metadata SQL: {common_path}")

    if schema_path.name == "common_meta.sql" or schema_path.resolve() == common_path.resolve():
        raise ValueError("Pack schema cannot be common_meta.sql (no common-only mode)")

    common_sql = common_path.read_text(encoding="utf-8")
    pack_sql = schema_path.read_text(encoding="utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.close(fd)
    except FileExistsError:
        raise FileExistsError(f"Database file already exists: {path}")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(path, autocommit=True)
        conn.isolation_level = None

        conn.execute("PRAGMA encoding = 'UTF-8';")
        conn.execute("PRAGMA page_size = 4096;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.execute("PRAGMA user_version = 1;")

        conn.executescript(common_sql)
        conn.executescript(pack_sql)

        conn.execute("PRAGMA foreign_keys = ON;")
        if conn.in_transaction:
            conn.execute("COMMIT;")

        return conn
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if path.exists():
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def write_pack_metadata(
    conn: sqlite3.Connection,
    metadata: Mapping[str, str],
    coverage: Mapping[str, tuple[int, str | None]],
) -> None:
    """Insert pack metadata and coverage report records in ascending key order.

    Must be called inside the caller's active data transaction.
    """
    sorted_meta = sorted(metadata.items())
    conn.executemany("INSERT INTO pack_meta (key, value) VALUES (?, ?);", sorted_meta)

    sorted_cov = [
        (metric, count, notes)
        for metric, (count, notes) in sorted(coverage.items())
    ]
    conn.executemany("INSERT INTO coverage_report (metric, count, notes) VALUES (?, ?, ?);", sorted_cov)


def finalize_pack(
    conn: sqlite3.Connection,
    *,
    fts_tables: tuple[str, ...],
) -> None:
    """Finalize an open pack transaction with integrity, FTS, analyze, and vacuum.

    Requirements:
    - Caller must have an open transaction on entry.
    - Foreign keys must remain enabled.
    - foreign_key_check must return no errors.
    - fts_tables must exactly match the declared FTS5 tables in schema metadata.
    - Rebuilds and optimizes each declared FTS5 table in the open transaction.
    - Runs ANALYZE in the transaction.
    - Commits the transaction.
    - Runs VACUUM in autocommit mode outside any open transaction.
    """
    if not conn.in_transaction:
        raise RuntimeError("finalize_pack requires an open transaction on entry")

    fk_status = conn.execute("PRAGMA foreign_keys;").fetchone()
    if not fk_status or fk_status[0] != 1:
        raise RuntimeError("Foreign keys constraint check failed: foreign_keys pragma is not ON")

    fk_errors = conn.execute("PRAGMA foreign_key_check;").fetchall()
    if fk_errors:
        raise RuntimeError(f"Foreign key constraint violation: {fk_errors}")

    # Verify FTS tables against schema native metadata
    schema_rows = conn.execute(
        "SELECT name, sql FROM sqlite_schema WHERE type='table' AND sql IS NOT NULL ORDER BY name;"
    ).fetchall()
    declared_fts = [
        row[0] for row in schema_rows
        if _FTS5_VIRTUAL_TABLE_PATTERN.match(re.sub(r"--[^\r\n]*", "", row[1]).strip())
    ]

    passed_fts = sorted(fts_tables)
    if len(passed_fts) != len(set(passed_fts)):
        raise RuntimeError(f"Duplicate FTS table names in argument: {fts_tables}")

    if declared_fts != passed_fts:
        raise RuntimeError(
            f"FTS tables mismatch schema metadata: declared in schema {declared_fts}, passed {passed_fts}"
        )

    # Perform rebuild and optimize for each declared FTS table
    for fts_tbl in fts_tables:
        quoted = _quote_identifier(fts_tbl)
        conn.execute(f"INSERT INTO {quoted}({quoted}) VALUES('rebuild');")
        conn.execute(f"INSERT INTO {quoted}({quoted}) VALUES('optimize');")

    # Run ANALYZE inside the transaction
    conn.execute("ANALYZE;")

    # Commit transaction
    conn.execute("COMMIT;")

    # Run VACUUM outside transaction in autocommit mode
    conn.execute("VACUUM;")
