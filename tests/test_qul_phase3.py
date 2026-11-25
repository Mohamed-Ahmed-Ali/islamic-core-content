"""Tests for Phase 3: QUL integration, Task 3.1: source lock, staging, rights, and WBW audit."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import sqlite3
import stat
import zipfile
import pytest

from pipelines.common.stage_qul_inputs import (
    stage_qul_file,
    audit_wbw_sqlite_vs_json,
)


def _try_symlink_dir(src: Path, dst: Path) -> bool:
    """Create a directory symlink; return False if OS denies (Windows without privilege)."""
    try:
        dst.symlink_to(src, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


def test_task_3_1_stage_qul_file_refuses_linked_raw_root_ancestor(tmp_path: Path) -> None:
    """raw_root supplied via symlinked ancestor must be rejected."""
    base = tmp_path / "base"
    base.mkdir()
    link = tmp_path / "link"
    if not _try_symlink_dir(base, link):
        pytest.skip("Symlink creation not permitted on this OS/user")

    # raw_root = link / "raw-inputs" — link is a symlinked ancestor
    raw_root = link / "raw-inputs"
    content_root = tmp_path / "Contant"
    rel_path = "wbw/english-wbw-translation.json.zip"
    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    with pytest.raises(ValueError, match="[Ss]ymlink|[Jj]unction"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )

    # Assert no staged bytes were written
    assert not (raw_root / "qul" / rel_path).exists()


def test_task_3_1_stage_qul_file_refuses_linked_content_root_ancestor(tmp_path: Path) -> None:
    """content_root supplied via symlinked ancestor must be rejected."""
    base = tmp_path / "base"
    base.mkdir()
    link = tmp_path / "link"
    if not _try_symlink_dir(base, link):
        pytest.skip("Symlink creation not permitted on this OS/user")

    # content_root = link / "Contant" — link is a symlinked ancestor
    content_root = link / "Contant"
    actual_contant = base / "Contant"
    rel_path = "wbw/english-wbw-translation.json.zip"
    src_file = actual_contant / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    raw_root = tmp_path / "raw-inputs"

    with pytest.raises(ValueError, match="[Ss]ymlink|[Jj]unction"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )

    # Assert no staged bytes were written
    assert not (raw_root / "qul" / rel_path).exists()


@pytest.mark.parametrize(
    ("target_rel_root", "existing_wrong_comp", "requested_comp"),
    [
        ("raw-inputs", "RAW-INPUTS", "raw-inputs"),
        ("wrapper/raw-inputs", "WRAPPER", "wrapper"),
    ],
    ids=["direct_root", "intermediate_wrapper"],
)
def test_task_3_1_stage_qul_file_refuses_wrong_case_raw_root(
    tmp_path: Path,
    target_rel_root: str,
    existing_wrong_comp: str,
    requested_comp: str,
) -> None:
    """raw_root with wrong case in its own name or ancestor directory must be rejected."""
    wrong_dir = tmp_path / existing_wrong_comp
    wrong_dir.mkdir()

    raw_root = tmp_path / target_rel_root
    content_root = tmp_path / "Contant"
    rel_path = "wbw/english-wbw-translation.json.zip"
    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    with pytest.raises(ValueError, match="[Cc]ase"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )

    # Exact directory names in tmp_path (Windows .exists is case-insensitive, so inspect entries)
    exact_names = {entry.name for entry in tmp_path.iterdir()}
    assert existing_wrong_comp in exact_names
    assert requested_comp not in exact_names
    # Assert no entries were created under the wrong-case directory
    assert list(wrong_dir.iterdir()) == []
    assert not (raw_root / "qul" / rel_path).exists()


def test_task_3_1_stage_qul_file_refuses_case_collision_even_if_exact_name_exists(tmp_path: Path) -> None:
    """Casefold collision must be rejected even if exact name also exists (e.g. on case-sensitive filesystems)."""
    raw_root = tmp_path / "raw-inputs"
    raw_root.mkdir()
    wrong_raw = tmp_path / "RAW-INPUTS"
    try:
        wrong_raw.mkdir()
    except OSError:
        pytest.skip("Filesystem does not support case-sensitive sibling directories")

    content_root = tmp_path / "Contant"
    rel_path = "wbw/english-wbw-translation.json.zip"
    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    with pytest.raises(ValueError, match="[Cc]ase"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )

    assert not (raw_root / "qul" / rel_path).exists()


def test_task_3_1_stage_qul_file_rejects_parent_traversal_in_roots(tmp_path: Path) -> None:
    """Parent directory traversal ('..') in roots must be rejected before walking."""
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "sub" / ".." / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    with pytest.raises(ValueError, match="[Pp]arent|traversal|\\.\\."):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )

def _make_zip_bytes(member_name: str, content: bytes) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(member_name, content)
    return bio.getvalue()

def test_task_3_1_stage_qul_file_refuses_wrong_case_intermediate_directory(tmp_path: Path) -> None:
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    # Pre-create raw_root with wrong-case intermediate directory 'QUL'
    (raw_root / "QUL").mkdir(parents=True)

    with pytest.raises(ValueError, match="[Cc]ase"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=hashlib.sha256(zip_bytes).hexdigest(),
        )

    # Assert no staged bytes were written
    assert not (raw_root / "qul" / rel_path).exists()
    assert not (raw_root / "QUL" / rel_path).exists()


def test_task_3_1_stage_qul_file_refuses_nonregular_zip_member(tmp_path: Path) -> None:
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)

    # Create zip archive with nonregular Unix symlink mode
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        zinfo = zipfile.ZipInfo("english-wbw-translation.json")
        zinfo.external_attr = stat.S_IFLNK << 16
        z.writestr(zinfo, b'{"1:1:1": "test"}')
    zip_bytes = bio.getvalue()
    src_file.write_bytes(zip_bytes)

    with pytest.raises(ValueError, match="nonregular|mode|regular"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=hashlib.sha256(zip_bytes).hexdigest(),
        )

    # Assert no staged bytes on rejection
    assert not (raw_root / "qul" / rel_path).exists()

def test_task_3_1_stage_qul_file_byte_preservation_and_idempotency(tmp_path: Path) -> None:
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    # First staging: copies byte-for-byte
    out_path = stage_qul_file(
        content_root=content_root,
        raw_root=raw_root,
        rel_path=rel_path,
        expected_sha256=None,
    )
    assert out_path == raw_root / "qul" / rel_path
    assert out_path.read_bytes() == zip_bytes

    # Second staging (idempotent): succeeds without error or rewrite
    out_path_2 = stage_qul_file(
        content_root=content_root,
        raw_root=raw_root,
        rel_path=rel_path,
        expected_sha256=None,
    )
    assert out_path_2 == out_path
    assert out_path_2.read_bytes() == zip_bytes


def test_task_3_1_stage_qul_file_refuses_nonidentical_destination(tmp_path: Path) -> None:
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    # Pre-create nonidentical destination
    dest_file = raw_root / "qul" / rel_path
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_bytes(b"corrupted-different-bytes")

    with pytest.raises(ValueError, match="already exists and differs"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )


def test_task_3_1_stage_qul_file_refuses_case_alias(tmp_path: Path) -> None:
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    # Pre-create case alias in destination directory
    dest_file = raw_root / "qul" / "wbw" / "ENGLISH-wbw-translation.json.zip"
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    dest_file.write_bytes(zip_bytes)

    with pytest.raises(ValueError, match="[Cc]ase collision"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )


def test_task_3_1_stage_qul_file_refuses_wrong_member_or_multiple_members(tmp_path: Path) -> None:
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)

    # Multiple members
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as z:
        z.writestr("english-wbw-translation.json", b'{}')
        z.writestr("extra.txt", b'extra')
    src_file.write_bytes(bio.getvalue())

    with pytest.raises(ValueError, match="exactly one member"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )

    # Wrong member name
    bio2 = io.BytesIO()
    with zipfile.ZipFile(bio2, "w") as z:
        z.writestr("wrong-name.json", b'{}')
    src_file.write_bytes(bio2.getvalue())

    with pytest.raises(ValueError, match="member name mismatch"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256=None,
        )


def test_task_3_1_stage_qul_file_refuses_sha256_mismatch(tmp_path: Path) -> None:
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        stage_qul_file(
            content_root=content_root,
            raw_root=raw_root,
            rel_path=rel_path,
            expected_sha256="0" * 64,
        )



def test_task_3_1_wbw_audit_rejects_sqlite_duplicate_key(tmp_path: Path) -> None:
    json_path = tmp_path / "translation.json.zip"
    db_path = tmp_path / "translation.db.zip"

    json_bytes = _make_zip_bytes("translation.json", b'{"1:1:1": "In the name"}')
    json_path.write_bytes(json_bytes)

    # SQLite DB with duplicate key (1, 1, '1')
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE word_translation (surah_number INTEGER, ayah_number INTEGER, word_number INTEGER, text TEXT)")
    con.execute("INSERT INTO word_translation VALUES (1, 1, 1, 'In the name')")
    con.execute("INSERT INTO word_translation VALUES (1, 1, 1, 'In the name')")
    db_bytes = con.serialize()
    con.close()

    db_zip_bytes = _make_zip_bytes("translation.db", db_bytes)
    db_path.write_bytes(db_zip_bytes)

    with pytest.raises(ValueError, match="Duplicate key.*in SQLite"):
        audit_wbw_sqlite_vs_json(
            json_archive_path=json_path,
            db_archive_path=db_path,
            table_name="word_translation",
        )


def test_task_3_1_wbw_audit_rejects_key_or_value_mismatch(tmp_path: Path) -> None:
    json_path = tmp_path / "translation.json.zip"
    db_path = tmp_path / "translation.db.zip"

    json_bytes = _make_zip_bytes("translation.json", b'{"1:1:1": "In the name"}')
    json_path.write_bytes(json_bytes)

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE word_translation (surah_number INTEGER, ayah_number INTEGER, word_number INTEGER, text TEXT)")
    con.execute("INSERT INTO word_translation VALUES (1, 1, 1, 'Different text')")
    db_bytes = con.serialize()
    con.close()

    db_zip_bytes = _make_zip_bytes("translation.db", db_bytes)
    db_path.write_bytes(db_zip_bytes)

    with pytest.raises(ValueError, match="Mismatch between SQLite and JSON"):
        audit_wbw_sqlite_vs_json(
            json_archive_path=json_path,
            db_archive_path=db_path,
            table_name="word_translation",
        )


def test_task_3_1_wbw_audit_rejects_missing_key_in_sqlite(tmp_path: Path) -> None:
    json_path = tmp_path / "translation.json.zip"
    db_path = tmp_path / "translation.db.zip"

    json_bytes = _make_zip_bytes("translation.json", b'{"1:1:1": "In the name", "1:1:2": "of Allah"}')
    json_path.write_bytes(json_bytes)

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE word_translation (surah_number INTEGER, ayah_number INTEGER, word_number INTEGER, text TEXT)")
    con.execute("INSERT INTO word_translation VALUES (1, 1, 1, 'In the name')")
    db_bytes = con.serialize()
    con.close()

    db_zip_bytes = _make_zip_bytes("translation.db", db_bytes)
    db_path.write_bytes(db_zip_bytes)

    with pytest.raises(ValueError, match="Row count mismatch"):
        audit_wbw_sqlite_vs_json(
            json_archive_path=json_path,
            db_archive_path=db_path,
            table_name="word_translation",
        )

def test_task_3_1_wbw_audit_rejects_non_integer_coordinate_types(tmp_path: Path) -> None:
    json_path = tmp_path / "translation.json.zip"
    db_path = tmp_path / "translation.db.zip"

    json_bytes = _make_zip_bytes("translation.json", b'{"1:1:1": "In the name"}')
    json_path.write_bytes(json_bytes)

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE word_translation (surah_number TEXT, ayah_number INTEGER, word_number INTEGER, text TEXT)")
    con.execute("INSERT INTO word_translation VALUES ('1', 1, 1, 'In the name')")
    db_bytes = con.serialize()
    con.close()

    db_zip_bytes = _make_zip_bytes("translation.db", db_bytes)
    db_path.write_bytes(db_zip_bytes)

    with pytest.raises(ValueError, match="[Ii]nteger|[Tt]ype|coordinate"):
        audit_wbw_sqlite_vs_json(
            json_archive_path=json_path,
            db_archive_path=db_path,
            table_name="word_translation",
        )
