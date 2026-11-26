"""Common utilities for reading Quranic Universal Library (QUL) input archives."""

from __future__ import annotations

import io
from pathlib import Path
import sqlite3
import stat
from typing import Any
import zipfile

from pipelines.common.lock_inputs import parse_strict_json


def validate_zip_member(zf: zipfile.ZipFile, expected_name: str) -> zipfile.ZipInfo:
    """Validate that zip contains exactly one regular member matching expected_name."""
    infolist = zf.infolist()
    if len(infolist) != 1:
        raise ValueError(f"Archive must have exactly one member, found {len(infolist)}")
    member = infolist[0]

    if member.is_dir() or (member.external_attr & 0x10) != 0 or member.filename.endswith(("/", "\\")):
        raise ValueError(f"Archive member must be a regular file, got directory: {member.filename!r}")

    orig_name = getattr(member, "orig_filename", member.filename)
    if orig_name != member.filename:
        raise ValueError(f"Archive member original filename mismatch: {orig_name!r} vs {member.filename!r}")

    unix_mode = member.external_attr >> 16
    if unix_mode != 0:
        file_type = stat.S_IFMT(unix_mode)
        if file_type != 0 and file_type != stat.S_IFREG:
            raise ValueError(
                f"Archive member has nonregular Unix mode {oct(unix_mode)} (file type {oct(file_type)}): {member.filename!r}"
            )

    if member.filename != expected_name:
        raise ValueError(
            f"Archive member name mismatch: expected {expected_name!r}, got {member.filename!r}"
        )

    return member


def read_zip_member_bytes(zip_path: Path | str, expected_name: str | None = None) -> bytes:
    """Read verified member bytes from a single-member zip archive."""
    p = Path(zip_path)
    if not p.is_file():
        raise FileNotFoundError(f"Archive not found: {p}")
    target_name = expected_name if expected_name is not None else p.name.removesuffix(".zip")
    with zipfile.ZipFile(p) as zf:
        member = validate_zip_member(zf, target_name)
        return zf.read(member)


def read_zip_strict_json(
    zip_path: Path | str,
    expected_name: str | None = None,
    *,
    allow_decimal: bool = False,
) -> Any:
    """Read and parse strict UTF-8 JSON from a verified single-member zip archive."""
    raw_bytes = read_zip_member_bytes(zip_path, expected_name=expected_name)
    try:
        decoded_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"Invalid UTF-8 in archive {zip_path}: {e}") from e
    try:
        return parse_strict_json(decoded_text, allow_decimal=allow_decimal)
    except Exception as e:
        raise ValueError(f"Invalid JSON in archive {zip_path}: {e}") from e


def open_sqlite_from_zip(
    zip_path: Path | str, expected_name: str | None = None
) -> sqlite3.Connection:
    """Read verified SQLite DB from zip into memory with trusted_schema off and query_only on."""
    db_bytes = read_zip_member_bytes(zip_path, expected_name=expected_name)
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA trusted_schema = OFF")
        con.execute("PRAGMA query_only = ON")
        con.deserialize(db_bytes)
        return con
    except Exception:
        con.close()
        raise
