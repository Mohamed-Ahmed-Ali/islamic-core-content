"""Tests for Phase 3: QUL integration, Task 3.1: source lock, staging, rights, and WBW audit."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sqlite3
import stat
from typing import Any
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


# ---------------------------------------------------------------------------
# Task 3.2: English QUL word meanings pack builder and QUL input helpers
# ---------------------------------------------------------------------------

def test_task_3_2_qul_input_helpers(tmp_path: Path) -> None:
    from pipelines.common.qul_input import (
        validate_zip_member,
        read_zip_strict_json,
        open_sqlite_from_zip,
    )

    # 1. Test validate_zip_member and read_zip_strict_json
    bad_json_zip = tmp_path / "bad.json.zip"
    bad_json_zip.write_bytes(_make_zip_bytes("bad.json", b"{invalid json}"))
    with pytest.raises(ValueError, match="JSON"):
        read_zip_strict_json(bad_json_zip)

    valid_json_zip = tmp_path / "valid.json.zip"
    valid_json_zip.write_bytes(_make_zip_bytes("valid.json", b'{"key": "value"}'))
    data = read_zip_strict_json(valid_json_zip)
    assert data == {"key": "value"}

    # 2. Test open_sqlite_from_zip
    con0 = sqlite3.connect(":memory:")
    con0.execute("CREATE TABLE t (id INTEGER, val TEXT);")
    con0.execute("INSERT INTO t VALUES (1, 'hello');")
    db_bytes = con0.serialize()
    con0.close()

    db_zip = tmp_path / "sample.db.zip"
    db_zip.write_bytes(_make_zip_bytes("sample.db", db_bytes))
    con = open_sqlite_from_zip(db_zip)
    try:
        cur = con.cursor()
        cur.execute("SELECT id, val FROM t;")
        rows = cur.fetchall()
        assert rows == [(1, "hello")]
        # verify query_only pragma
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO t VALUES (2, 'fail');")
    finally:
        con.close()


def test_task_3_2_wbw_loader_rejects_unknown_coordinate_gap(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import (
        load_qul_wbw_meanings,
        load_qpc_hafs_tajweed_words,
    )

    # Missing coordinate gap in normal verse (e.g. 1:1:1 and 1:1:3 present, but 1:1:2 missing)
    gap_meanings = {
        "1:1:1": "In",
        "1:1:3": "the Most Gracious",
        "1:1:4": "(1)",
    }
    gap_zip = tmp_path / "gap_meanings.json.zip"
    gap_zip.write_bytes(_make_zip_bytes("gap_meanings.json", json.dumps(gap_meanings).encode("utf-8")))

    with pytest.raises(ValueError, match="contiguous|positions|gap"):
        load_qul_wbw_meanings(gap_zip, ayah_keys=["1:1"])

    # In 2:181, word 4 is the only allowed omitted position. If word 2 is omitted instead, reject.
    bad_2_181 = {
        "2:181:1": "Then whoever",
        "2:181:3": "after what",
        "2:181:4": "he heard",
        "2:181:15": "(181)",
    }
    bad_zip = tmp_path / "bad_2_181.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("bad_2_181.json", json.dumps(bad_2_181).encode("utf-8")))
    with pytest.raises(ValueError, match="positions|2:181"):
        load_qul_wbw_meanings(bad_zip, ayah_keys=["2:181"])

def test_task_3_2_wbw_meanings_rejects_noncanonical_coordinate(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qul_wbw_meanings

    bad_meanings = {
        "01:1:1": "In",
        "1:1:2": "(1)",
    }
    bad_zip = tmp_path / "bad_coord.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("bad_coord.json", json.dumps(bad_meanings).encode("utf-8")))

    with pytest.raises(ValueError, match="[Cc]oordinate|[Ff]ormat|[Cc]anonical"):
        load_qul_wbw_meanings(bad_zip)


def test_task_3_2_wbw_meanings_rejects_blank_text(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qul_wbw_meanings

    bad_meanings = {
        "1:1:1": "   ",
        "1:1:2": "(1)",
    }
    bad_zip = tmp_path / "blank_gloss.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("blank_gloss.json", json.dumps(bad_meanings).encode("utf-8")))

    with pytest.raises(ValueError, match="[Bb]lank|[Ee]mpty|gloss"):
        load_qul_wbw_meanings(bad_zip)


def test_task_3_2_wbw_meanings_rejects_marker_looking_lexical_row(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qul_wbw_meanings

    bad_meanings = {
        "1:1:1": "(1)",
        "1:1:2": "word",
        "1:1:3": "(1)",
    }
    bad_zip = tmp_path / "marker_lexical.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("marker_lexical.json", json.dumps(bad_meanings).encode("utf-8")))

    with pytest.raises(ValueError, match="[Mm]arker|lexical"):
        load_qul_wbw_meanings(bad_zip)


def test_task_3_2_wbw_meanings_rejects_extra_verse(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qul_wbw_meanings

    meanings_with_extra = {
        "1:1:1": "In",
        "1:1:2": "(1)",
        "999:1:1": "extra",
        "999:1:2": "(1)",
    }
    bad_zip = tmp_path / "extra_verse.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("extra_verse.json", json.dumps(meanings_with_extra).encode("utf-8")))

    with pytest.raises(ValueError, match="[Ee]xtra|[Mm]issing|verse"):
        load_qul_wbw_meanings(bad_zip, ayah_keys=["1:1"])


def test_task_3_2_qpc_loader_rejects_rule_without_class_and_accepts_valid_nesting(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qpc_hafs_tajweed_words

    # <rule> without class MUST be rejected
    bad_qpc = {
        "1:1": {
            "id": 1,
            "verse_key": "1:1",
            "surah": 1,
            "ayah": 1,
            "text": "بِسۡمِ <rule>ٱ</rule>للَّهِ ١",
        }
    }
    bad_zip = tmp_path / "no_class.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("no_class.json", json.dumps(bad_qpc).encode("utf-8")))
    with pytest.raises(ValueError, match="[Cc]lass|[Rr]ule|[Ii]nvalid tag"):
        load_qpc_hafs_tajweed_words(bad_zip, ayah_keys=["1:1"])

    # Valid nested rule (depth 2): <rule class="outer">a<rule class="inner">b</rule>c</rule>
    nested_qpc = {
        "1:1": {
            "id": 1,
            "verse_key": "1:1",
            "surah": 1,
            "ayah": 1,
            "text": "بِسۡمِ <rule class=\"outer\">ٱ<rule class=\"inner\">ل</rule></rule> ١",
        }
    }
    nested_zip = tmp_path / "nested.json.zip"
    nested_zip.write_bytes(_make_zip_bytes("nested.json", json.dumps(nested_qpc).encode("utf-8")))
    words = load_qpc_hafs_tajweed_words(nested_zip, ayah_keys=["1:1"])["1:1"]
    assert words == ["بِسۡمِ", "ٱل"]


def test_task_3_2_qpc_loader_validates_exact_fields_and_types(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qpc_hafs_tajweed_words

    # 1. Extra field in record
    bad_qpc = {
        "1:1": {
            "id": 1, "verse_key": "1:1", "surah": 1, "ayah": 1, "text": "بِسۡمِ ١",
            "unexpected": "field",
        }
    }
    bad_zip = tmp_path / "extra_field.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("extra_field.json", json.dumps(bad_qpc).encode("utf-8")))
    with pytest.raises(ValueError, match="[Ff]ield|[Kk]ey"):
        load_qpc_hafs_tajweed_words(bad_zip, ayah_keys=["1:1"])

    # 2. Bool for surah/ayah
    bad_qpc2 = {
        "1:1": {
            "id": 1, "verse_key": "1:1", "surah": True, "ayah": 1, "text": "بِسۡمِ ١",
        }
    }
    bad_zip2 = tmp_path / "bool_surah.json.zip"
    bad_zip2.write_bytes(_make_zip_bytes("bool_surah.json", json.dumps(bad_qpc2).encode("utf-8")))
    with pytest.raises(ValueError, match="[Ii]nt|bool|type"):
        load_qpc_hafs_tajweed_words(bad_zip2, ayah_keys=["1:1"])

    # 3. Mismatched terminal decimal
    bad_qpc3 = {
        "1:1": {
            "id": 1, "verse_key": "1:1", "surah": 1, "ayah": 1, "text": "بِسۡمِ ٢",
        }
    }
    bad_zip3 = tmp_path / "mismatched_terminal.json.zip"
    bad_zip3.write_bytes(_make_zip_bytes("mismatched_terminal.json", json.dumps(bad_qpc3).encode("utf-8")))
    with pytest.raises(ValueError, match="[Tt]erminal|[Mm]ismatch|decimal"):
        load_qpc_hafs_tajweed_words(bad_zip3, ayah_keys=["1:1"])

    # 4. Decimal lexical leak
    bad_qpc4 = {
        "1:1": {
            "id": 1, "verse_key": "1:1", "surah": 1, "ayah": 1, "text": "بِسۡمِ ١٢٣ ١",
        }
    }
    bad_zip4 = tmp_path / "decimal_leak.json.zip"
    bad_zip4.write_bytes(_make_zip_bytes("decimal_leak.json", json.dumps(bad_qpc4).encode("utf-8")))
    with pytest.raises(ValueError, match="[Dd]ecimal|leak|digit"):
        load_qpc_hafs_tajweed_words(bad_zip4, ayah_keys=["1:1"])

    # 5. Blank text
    bad_qpc5 = {
        "1:1": {
            "id": 1, "verse_key": "1:1", "surah": 1, "ayah": 1, "text": "   ",
        }
    }
    bad_zip5 = tmp_path / "blank_qpc.json.zip"
    bad_zip5.write_bytes(_make_zip_bytes("blank_qpc.json", json.dumps(bad_qpc5).encode("utf-8")))
    with pytest.raises(ValueError, match="[Bb]lank|[Ee]mpty|text"):
        load_qpc_hafs_tajweed_words(bad_zip5, ayah_keys=["1:1"])


def test_task_3_2_qpc_loader_rejects_extra_verse(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qpc_hafs_tajweed_words

    qpc_with_extra = {
        "1:1": {"id": 1, "verse_key": "1:1", "surah": 1, "ayah": 1, "text": "بِسۡمِ ١"},
        "999:1": {"id": 999, "verse_key": "999:1", "surah": 999, "ayah": 1, "text": "extra ١"},
    }
    bad_zip = tmp_path / "extra_qpc_verse.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("extra_qpc_verse.json", json.dumps(qpc_with_extra).encode("utf-8")))
    with pytest.raises(ValueError, match="[Ee]xtra|[Mm]issing|verse"):
        load_qpc_hafs_tajweed_words(bad_zip, ayah_keys=["1:1"])


def test_task_3_2_pronunciations_rejects_extra_verse_or_keys(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qul_wbw_pronunciations

    # Extra verse in transliteration
    pron_extra = {
        "1:1:1": "bismi",
        "999:1:1": "extra",
    }
    bad_zip = tmp_path / "pron_extra_verse.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("pron_extra_verse.json", json.dumps(pron_extra).encode("utf-8")))
    with pytest.raises(ValueError, match="[Ee]xtra|[Mm]issing|verse"):
        load_qul_wbw_pronunciations(bad_zip, ayah_keys=["1:1"])

    # Duplicate slot mismatch in 2:181
    pron_dup_mismatch = {
        "2:181:1": "faman", "2:181:2": "baddalahu", "2:181:3": "baʿdamā",
        "2:181:5": "DIFFERENT", # slot 5 != slot 3
        "2:181:7": "fa-innamā", "2:181:8": "ith'muhu", "2:181:9": "ʿalā",
        "2:181:10": "alladhīna", "2:181:11": "yubaddilūnahu", "2:181:12": "inna",
        "2:181:13": "l-laha", "2:181:14": "samīʿun", "2:181:15": "ʿalīmun",
    }
    bad_zip2 = tmp_path / "dup_mismatch.json.zip"
    bad_zip2.write_bytes(_make_zip_bytes("dup_mismatch.json", json.dumps(pron_dup_mismatch).encode("utf-8")))
    with pytest.raises(ValueError, match="duplicate"):
        load_qul_wbw_pronunciations(bad_zip2, ayah_keys=["2:181"])

def test_task_3_2_qpc_loader_rule_markup_and_nesting(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qpc_hafs_tajweed_words

    # Valid nested rules: max depth 2
    valid_qpc = {
        "1:1": {
            "id": 1,
            "verse_key": "1:1",
            "surah": 1,
            "ayah": 1,
            "text": "بِسۡمِ <rule class=ham_wasl>ٱ</rule>للَّهِ <rule class=ham_wasl>ٱ</rule><rule class=laam_shamsiyah>ل</rule>رَّحۡمَ<rule class=madda_normal>ـٰ</rule>نِ ١",
        },
        "7:206": {
            "id": 206,
            "verse_key": "7:206",
            "surah": 7,
            "ayah": 206,
            "text": "وَلَهُۥ يَسۡجُدُونَ ۩ ٢٠٦",
        },
    }
    qpc_zip = tmp_path / "qpc.json.zip"
    qpc_zip.write_bytes(_make_zip_bytes("qpc.json", json.dumps(valid_qpc).encode("utf-8")))

    words = load_qpc_hafs_tajweed_words(qpc_zip, ayah_keys=["1:1", "7:206"])
    # In 1:1, terminal numeral omitted, leaving 3 words: بِسۡمِ, ٱللَّهِ, ٱلرَّحۡمَـٰنِ
    assert len(words["1:1"]) == 3
    # In 7:206, standalone ۩ omitted, terminal numeral omitted, leaving 2 words: وَلَهُۥ, يَسۡجُدُونَ
    assert words["7:206"] == ["وَلَهُۥ", "يَسۡجُدُونَ"]

    # Unmatched closing tag rejected
    unmatched_qpc = {
        "1:1": {
            "id": 1, "verse_key": "1:1", "surah": 1, "ayah": 1,
            "text": "بِسۡمِ </rule> ١",
        }
    }
    unm_zip = tmp_path / "unmatched.json.zip"
    unm_zip.write_bytes(_make_zip_bytes("unmatched.json", json.dumps(unmatched_qpc).encode("utf-8")))
    with pytest.raises(ValueError, match="[Uu]nbalanced|[Tt]ag"):
        load_qpc_hafs_tajweed_words(unm_zip, ayah_keys=["1:1"])


def test_task_3_2_wbw_loader_shifted_2_181_and_pronunciations(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import (
        load_qpc_hafs_tajweed_words,
        load_qul_wbw_meanings,
        load_qul_wbw_pronunciations,
    )

    # 1. 2:181 fixture
    qpc_data = {
        "2:181": {
            "id": 181,
            "verse_key": "2:181",
            "surah": 2,
            "ayah": 181,
            "text": "فَمَنۢ بَدَّلَهُۥ بَعۡدَ مَا سَمِعَهُۥ فَإِنَّمَآ إِثۡمُهُۥ عَلَى ٱلَّذِينَ يُبَدِّلُونَهُۥٓ‌ۚ إِنَّ ٱللَّهَ سَمِيعٌ عَلِيمٌ ١٨١",
        }
    }
    qpc_zip = tmp_path / "qpc.json.zip"
    qpc_zip.write_bytes(_make_zip_bytes("qpc.json", json.dumps(qpc_data).encode("utf-8")))

    meanings_data = {
        "2:181:1": "Then whoever",
        "2:181:2": "changes it",
        "2:181:3": "after what",
        "2:181:5": "he (has) heard [it]",
        "2:181:6": "so only",
        "2:181:7": "its sin",
        "2:181:8": "(would be) on",
        "2:181:9": "those who",
        "2:181:10": "alter it",
        "2:181:11": "Indeed",
        "2:181:12": "Allah",
        "2:181:13": "(is) All-Hearing",
        "2:181:14": "All-Knowing",
        "2:181:15": "(181)",
    }
    meanings_zip = tmp_path / "meanings.json.zip"
    meanings_zip.write_bytes(_make_zip_bytes("meanings.json", json.dumps(meanings_data).encode("utf-8")))

    pron_data = {
        "2:181:1": "faman",
        "2:181:2": "baddalahu",
        "2:181:3": "baʿdamā",
        "2:181:5": "baʿdamā",
        "2:181:7": "fa-innamā",
        "2:181:8": "ith'muhu",
        "2:181:9": "ʿalā",
        "2:181:10": "alladhīna",
        "2:181:11": "yubaddilūnahu",
        "2:181:12": "inna",
        "2:181:13": "l-laha",
        "2:181:14": "samīʿun",
        "2:181:15": "ʿalīmun",
    }
    pron_zip = tmp_path / "pron.json.zip"
    pron_zip.write_bytes(_make_zip_bytes("pron.json", json.dumps(pron_data).encode("utf-8")))

    words = load_qpc_hafs_tajweed_words(qpc_zip, ayah_keys=["2:181"])["2:181"]
    meanings = load_qul_wbw_meanings(meanings_zip, ayah_keys=["2:181"])["2:181"]
    prons = load_qul_wbw_pronunciations(pron_zip, ayah_keys=["2:181"])["2:181"]

    # In 2:181, 14 tokens minus terminal marker = 13 words; tokens 3 and 4 merged to 'بَعۡدَ مَا'
    assert len(words) == 13
    assert words[2] == "بَعۡدَ مَا"

    # Meanings compacted from 1..3, 5..14 -> 1..13
    assert len(meanings) == 13
    # Word 4 shifted from raw 5: "he (has) heard [it]"
    assert meanings[3] == (4, "he (has) heard [it]")
    # Word 13 shifted from raw 14: "All-Knowing"
    assert meanings[12] == (13, "All-Knowing")

    # Pronunciations: word 4 is NULL, word 13 is "ʿalīmun" (from raw 15)
    assert prons[4] is None
    assert prons[13] == "ʿalīmun"
    assert prons[1] == "faman"


def test_task_3_2_wbw_pack_writer_and_fts(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import (
        PACK_ID,
        PACK_VERSION,
        load_qpc_hafs_tajweed_words,
        load_qul_wbw_meanings,
        load_qul_wbw_pronunciations,
        write_wbw_pack,
    )

    # Create minimal 1:1 and 2:181 fixture archives
    qpc_data = {
        "1:1": {
            "id": 1,
            "verse_key": "1:1",
            "surah": 1,
            "ayah": 1,
            "text": "بِسۡمِ <rule class=ham_wasl>ٱ</rule>للَّهِ <rule class=ham_wasl>ٱ</rule><rule class=laam_shamsiyah>ل</rule>رَّحۡمَ<rule class=madda_normal>ـٰ</rule>نِ <rule class=ham_wasl>ٱ</rule><rule class=laam_shamsiyah>ل</rule>رَّح<rule class=madda_permissible>ِي</rule>مِ ١",
        },
        "2:181": {
            "id": 188,
            "verse_key": "2:181",
            "surah": 2,
            "ayah": 181,
            "text": "فَمَنۢ بَدَّلَهُۥ بَعۡدَ مَا سَمِعَهُۥ فَإِنَّمَآ إِثۡمُهُۥ عَلَى ٱلَّذِينَ يُبَدِّلُونَهُۥٓ‌ۚ إِنَّ ٱللَّهَ سَمِيعٌ عَلِيمٌ ١٨١",
        },
    }
    qpc_zip = tmp_path / "qpc.json.zip"
    qpc_zip.write_bytes(_make_zip_bytes("qpc.json", json.dumps(qpc_data).encode("utf-8")))

    meanings_data = {
        "1:1:1": "In (the) name",
        "1:1:2": "(of) Allah",
        "1:1:3": "the Most Gracious",
        "1:1:4": "the Most Merciful",
        "1:1:5": "(1)",
        "2:181:1": "Then whoever",
        "2:181:2": "changes it",
        "2:181:3": "after what",
        "2:181:5": "he (has) heard [it]",
        "2:181:6": "so only",
        "2:181:7": "its sin",
        "2:181:8": "(would be) on",
        "2:181:9": "those who",
        "2:181:10": "alter it",
        "2:181:11": "Indeed",
        "2:181:12": "Allah",
        "2:181:13": "(is) All-Hearing",
        "2:181:14": "All-Knowing",
        "2:181:15": "(181)",
    }
    meanings_zip = tmp_path / "meanings.json.zip"
    meanings_zip.write_bytes(_make_zip_bytes("meanings.json", json.dumps(meanings_data).encode("utf-8")))

    pron_data = {
        "1:1:1": "bis'mi",
        "1:1:2": "l-lahi",
        "1:1:3": "r-raḥmāni",
        "1:1:4": "r-raḥīmi",
        "2:181:1": "faman",
        "2:181:2": "baddalahu",
        "2:181:3": "baʿdamā",
        "2:181:5": "baʿdamā",
        "2:181:7": "fa-innamā",
        "2:181:8": "ith'muhu",
        "2:181:9": "ʿalā",
        "2:181:10": "alladhīna",
        "2:181:11": "yubaddilūnahu",
        "2:181:12": "inna",
        "2:181:13": "l-laha",
        "2:181:14": "samīʿun",
        "2:181:15": "ʿalīmun",
    }
    pron_zip = tmp_path / "pron.json.zip"
    pron_zip.write_bytes(_make_zip_bytes("pron.json", json.dumps(pron_data).encode("utf-8")))

    ayah_ids = {"1:1": 1, "2:181": 188}
    target_keys = ["1:1", "2:181"]
    ar_words = load_qpc_hafs_tajweed_words(qpc_zip, ayah_ids)
    meanings = load_qul_wbw_meanings(meanings_zip, target_keys)
    gloss_counts = {k: len(meanings[k]) for k in target_keys}
    prons = load_qul_wbw_pronunciations(pron_zip, target_keys, expected_counts=gloss_counts)

    repo_root = Path(__file__).resolve().parents[1]
    schema_path = repo_root / "specs" / "sql" / "quran_wbw.v1.sql"
    out_db = tmp_path / "quran-wbw-en.db"

    metadata = {
        "category": "quran",
        "completeness": "complete",
        "fts5_enabled": "1",
        "id": PACK_ID,
        "languages": "ar,en",
        "license": "LicenseRef-QUL-Unreviewed",
        "pack_version": PACK_VERSION,
        "provenance": "QUL",
        "schema_version": "1",
    }
    surah_ayah_sequence = [(1, 1, 1), (2, 181, 188)]
    expected_words = len(ar_words["1:1"]) + len(ar_words["2:181"])
    assert expected_words == 17

    write_wbw_pack(
        out_db,
        schema_path,
        metadata,
        surah_ayah_sequence,
        ar_words,
        meanings,
        prons,
        expected_total_words=17,
    )

    assert out_db.is_file()
    con = sqlite3.connect(f"{out_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("PRAGMA integrity_check;")
        assert cur.fetchall() == [("ok",)]
        # Verify pack metadata
        cur.execute("SELECT key, value FROM pack_meta ORDER BY key;")
        meta_rows = dict(cur.fetchall())
        assert meta_rows["pack_version"] == "0.3.0"
        assert meta_rows["languages"] == "ar,en"

        # Verify coverage report
        cur.execute("SELECT metric, count, notes FROM coverage_report ORDER BY metric;")
        cov_rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        assert cov_rows["expected_total"] == (17, None)
        assert cov_rows["ar_present"] == (17, None)
        assert cov_rows["en_present"] == (17, None)
        assert cov_rows["en_missing"] == (0, None)
        assert cov_rows["alignment_markers_removed"] == (2, None)
        assert cov_rows["alignment_rekeyed_ayahs"] == (1, "2:181")
        assert cov_rows["alignment_raw_delta:1:1"] == (1, "QUL raw=5; normalized=4; delta=-1")
        assert cov_rows["alignment_raw_delta:2:181"] == (1, "QUL raw=14; normalized=13; delta=-1")

        # Verify row counts and Arabic storage fidelity
        cur.execute("SELECT COUNT(*) FROM word_translations;")
        assert cur.fetchone() == (17,)

        cur.execute("SELECT text_uthmani, translation, transliteration FROM word_translations WHERE word_key = '1:1:1';")
        assert cur.fetchall() == [("بِسۡمِ", "In (the) name", "bis'mi")]

        # Verify shifted 2:181:4 (gloss: 'he (has) heard [it]', pronunciation NULL)
        cur.execute("SELECT text_uthmani, translation, transliteration FROM word_translations WHERE word_key = '2:181:4';")
        assert cur.fetchall() == [("سَمِعَهُۥ", "he (has) heard [it]", None)]

        # Verify shifted 2:181:13 (gloss: 'All-Knowing', transliteration: 'ʿalīmun')
        cur.execute("SELECT text_uthmani, translation, transliteration FROM word_translations WHERE word_key = '2:181:13';")
        assert cur.fetchall() == [("عَلِيمٌ", "All-Knowing", "ʿalīmun")]

        # Verify FTS search matches
        cur.execute(
            "SELECT w.word_key FROM word_translations AS w "
            "JOIN word_translations_fts AS fts ON fts.rowid = w.id "
            "WHERE word_translations_fts MATCH 'heard';"
        )
        assert cur.fetchall() == [("2:181:4",)]

        cur.execute(
            "SELECT w.word_key FROM word_translations AS w "
            "JOIN word_translations_fts AS fts ON fts.rowid = w.id "
            "WHERE word_translations_fts MATCH '\"All-Knowing\"';"
        )
        assert cur.fetchall() == [("2:181:13",)]

        cur.execute(
            "SELECT w.word_key FROM word_translations AS w "
            "JOIN word_translations_fts AS fts ON fts.rowid = w.id "
            "WHERE word_translations_fts MATCH ?;",
            ("ʿalīmun",),
        )
        assert cur.fetchall() == [("2:181:13",)]
    finally:
        con.close()


def test_task_3_2_builder_cli_arg_handling() -> None:
    from pipelines.builders.build_quran_wbw import main

    assert main(["--sources-root", "/tmp/nonexistent_root_dir", "--output-dir", "/tmp/nonexistent_out_dir"]) == 2


def test_task_3_2_qpc_loader_rejects_noncanonical_verse_key(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import load_qpc_hafs_tajweed_verses

    bad_qpc = {
        "01:1": {
            "id": 1,
            "verse_key": "01:1",
            "surah": 1,
            "ayah": 1,
            "text": "بِسۡمِ ١",
        }
    }
    bad_zip = tmp_path / "bad_qpc_leading_zero.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("bad_qpc_leading_zero.json", json.dumps(bad_qpc).encode("utf-8")))

    with pytest.raises(ValueError, match="[Nn]on-canonical|[Cc]anonical"):
        load_qpc_hafs_tajweed_verses(bad_zip, expected_ayah_ids=None)


def test_task_3_2_stage_qul_file_directory_creation_race_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = tmp_path / "Contant"
    raw_root = tmp_path / "raw-inputs"
    rel_path = "wbw/english-wbw-translation.json.zip"

    src_file = content_root / rel_path
    src_file.parent.mkdir(parents=True, exist_ok=True)
    zip_bytes = _make_zip_bytes("english-wbw-translation.json", b'{"1:1:1": "test"}')
    src_file.write_bytes(zip_bytes)

    dest_parent = (raw_root / "qul" / rel_path).parent
    dest_parent.mkdir(parents=True, exist_ok=True)

    orig_exists = Path.exists

    def race_exists(self: Path) -> bool:
        if self == dest_parent:
            return False
        return orig_exists(self)

    monkeypatch.setattr(Path, "exists", race_exists)

    out_path = stage_qul_file(
        content_root=content_root,
        raw_root=raw_root,
        rel_path=rel_path,
        expected_sha256=None,
    )
    assert out_path.exists()


def test_task_3_2_loaders_mismatch_diagnostics_include_missing_and_extra(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_wbw import (
        load_qpc_hafs_tajweed_verses,
        load_qul_wbw_meanings,
        load_qul_wbw_pronunciations,
    )

    # 1. QPC verse set mismatch
    qpc_data = {
        "1:1": {"id": 1, "verse_key": "1:1", "surah": 1, "ayah": 1, "text": "text 1"},
        "1:3": {"id": 3, "verse_key": "1:3", "surah": 1, "ayah": 3, "text": "text 3"},
    }
    qpc_zip = tmp_path / "qpc_mismatch.json.zip"
    qpc_zip.write_bytes(_make_zip_bytes("qpc_mismatch.json", json.dumps(qpc_data).encode("utf-8")))

    with pytest.raises(ValueError) as exc_qpc:
        load_qpc_hafs_tajweed_verses(qpc_zip, expected_ayah_ids={"1:1": 1, "1:2": 2})
    msg_qpc = str(exc_qpc.value)
    assert "missing=" in msg_qpc and "extra=" in msg_qpc
    assert "1:2" in msg_qpc and "1:3" in msg_qpc

    # 2. WBW meanings verse set mismatch
    meanings_data = {
        "1:1:1": "In", "1:1:2": "(1)",
        "1:3:1": "The", "1:3:2": "(3)",
    }
    meanings_zip = tmp_path / "meanings_mismatch.json.zip"
    meanings_zip.write_bytes(_make_zip_bytes("meanings_mismatch.json", json.dumps(meanings_data).encode("utf-8")))

    with pytest.raises(ValueError) as exc_meanings:
        load_qul_wbw_meanings(meanings_zip, ayah_keys=["1:1", "1:2"])
    msg_meanings = str(exc_meanings.value)
    assert "missing=" in msg_meanings and "extra=" in msg_meanings
    assert "1:2" in msg_meanings and "1:3" in msg_meanings

    # 3. WBW transliteration verse set mismatch
    pron_data = {
        "1:1:1": "bismi",
        "1:3:1": "al-rahmani",
    }
    pron_zip = tmp_path / "pron_mismatch.json.zip"
    pron_zip.write_bytes(_make_zip_bytes("pron_mismatch.json", json.dumps(pron_data).encode("utf-8")))

    with pytest.raises(ValueError) as exc_pron:
        load_qul_wbw_pronunciations(pron_zip, ayah_keys=["1:1", "1:2"])
    msg_pron = str(exc_pron.value)
    assert "missing=" in msg_pron and "extra=" in msg_pron
    assert "1:2" in msg_pron and "1:3" in msg_pron


# ---------------------------------------------------------------------------
# Task 3.3: Phase 3 concordance and word alignment proof query
# ---------------------------------------------------------------------------

def _create_concordance_test_dbs(
    tmp_path: Path,
    *,
    lemma: str = "Eabada",
    verb_count: int = 1,
    translation: str = "we worship",
    text_uthmani: str = "إِيَّاكَ نَعْبُدُ وَإِيَّاكَ نَسْتَعِينُ",
    special_glosses: dict[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    core_path = tmp_path / "quran-core.db"
    morph_path = tmp_path / "quran-morphology.db"
    wbw_path = tmp_path / "quran-wbw-en.db"

    with sqlite3.connect(core_path) as conn:
        conn.execute(
            "CREATE TABLE ayahs ("
            "id INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 6236), "
            "surah_id INTEGER NOT NULL, "
            "ayah_number INTEGER NOT NULL, "
            "verse_key TEXT NOT NULL UNIQUE, "
            "text_uthmani TEXT NOT NULL);"
        )
        conn.execute(
            "INSERT INTO ayahs (id, surah_id, ayah_number, verse_key, text_uthmani) "
            "VALUES (1, 1, 5, '1:5', ?);",
            (text_uthmani,),
        )

    with sqlite3.connect(morph_path) as conn:
        conn.execute(
            "CREATE TABLE verb_concordance ("
            "id INTEGER PRIMARY KEY, "
            "verb_buckwalter TEXT NOT NULL, "
            "total_count INTEGER NOT NULL);"
        )
        conn.execute(
            "CREATE TABLE words ("
            "id INTEGER PRIMARY KEY, "
            "ayah_id INTEGER NOT NULL, "
            "word_number INTEGER NOT NULL, "
            "word_key TEXT NOT NULL UNIQUE, "
            "verb_concordance_id INTEGER REFERENCES verb_concordance(id));"
        )
        conn.execute(
            "INSERT INTO verb_concordance (id, verb_buckwalter, total_count) "
            "VALUES (1, ?, ?);",
            (lemma, verb_count),
        )
        conn.execute(
            "INSERT INTO words (id, ayah_id, word_number, word_key, verb_concordance_id) "
            "VALUES (1, 1, 2, '1:5:2', 1);"
        )

    if special_glosses is None:
        special_glosses = {
            "2:181:4": "he (has) heard [it]",
            "8:6:5": "was made clear",
            "13:37:9": "came to you",
        }

    with sqlite3.connect(wbw_path) as conn:
        conn.execute(
            "CREATE TABLE word_translations ("
            "id INTEGER PRIMARY KEY, "
            "ayah_id INTEGER NOT NULL, "
            "word_number INTEGER NOT NULL, "
            "word_key TEXT NOT NULL UNIQUE, "
            "text_uthmani TEXT NOT NULL, "
            "translation TEXT NOT NULL, "
            "transliteration TEXT);"
        )
        conn.execute(
            "INSERT INTO word_translations (id, ayah_id, word_number, word_key, text_uthmani, translation, transliteration) "
            "VALUES (1, 1, 2, '1:5:2', 'نَعْبُدُ', ?, 'naʿbudu');",
            (translation,),
        )
        next_id = 2
        for w_key, gloss in special_glosses.items():
            conn.execute(
                "INSERT INTO word_translations (id, ayah_id, word_number, word_key, text_uthmani, translation, transliteration) "
                "VALUES (?, 188, 1, ?, 'dummy', ?, NULL);",
                (next_id, w_key, gloss),
            )
            next_id += 1

    return core_path, morph_path, wbw_path


def test_task_3_3_concordance_cli_arg_handling() -> None:
    from pipelines.validators.phase3_concordance import main

    assert main([]) == 2
    assert main(["/nonexistent/directory/path/for/concordance"]) == 1


def test_task_3_3_concordance_missing_files(tmp_path: Path) -> None:
    from pipelines.validators.phase3_concordance import validate_phase3_concordance

    # Empty dir
    with pytest.raises(FileNotFoundError, match="quran-core.db"):
        validate_phase3_concordance(tmp_path)

    # Missing morphology
    (tmp_path / "quran-core.db").touch()
    with pytest.raises(FileNotFoundError, match="quran-morphology.db"):
        validate_phase3_concordance(tmp_path)

    # Missing WBW
    (tmp_path / "quran-morphology.db").touch()
    with pytest.raises(FileNotFoundError, match="quran-wbw-en.db"):
        validate_phase3_concordance(tmp_path)


@pytest.fixture
def concordance_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep semantic fixtures independent of scheduler delays; measure pinned packs separately."""
    from itertools import count
    from types import SimpleNamespace

    from pipelines.validators import phase3_concordance

    clock = SimpleNamespace(perf_counter_ns=count(step=1000).__next__)
    monkeypatch.setattr(phase3_concordance, "time", clock)


def test_task_3_3_concordance_happy_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], concordance_clock: None,
) -> None:
    from pipelines.validators.phase3_concordance import validate_phase3_concordance

    _create_concordance_test_dbs(tmp_path)
    validate_phase3_concordance(tmp_path)
    captured = capsys.readouterr()
    assert "20 samples" in captured.out


def test_task_3_3_concordance_mismatched_lemma_or_gloss(
    tmp_path: Path, concordance_clock: None,
) -> None:
    from pipelines.validators.phase3_concordance import validate_phase3_concordance

    # Mismatched lemma
    dir_lemma = tmp_path / "bad_lemma"
    dir_lemma.mkdir()
    _create_concordance_test_dbs(dir_lemma, lemma="wrong_lemma")
    with pytest.raises(ValueError, match="Expected verb_buckwalter 'Eabada'"):
        validate_phase3_concordance(dir_lemma)

    # Mismatched translation
    dir_trans = tmp_path / "bad_trans"
    dir_trans.mkdir()
    _create_concordance_test_dbs(dir_trans, translation="wrong translation")
    with pytest.raises(ValueError, match="Expected translation 'we worship'"):
        validate_phase3_concordance(dir_trans)

    # Mismatched special gloss
    dir_gloss = tmp_path / "bad_gloss"
    dir_gloss.mkdir()
    _create_concordance_test_dbs(
        dir_gloss,
        special_glosses={
            "2:181:4": "wrong gloss",
            "8:6:5": "was made clear",
            "13:37:9": "came to you",
        },
    )
    with pytest.raises(ValueError, match="Special gloss mismatch for 2:181:4"):
        validate_phase3_concordance(dir_gloss)

# ---------------------------------------------------------------------------
# Task 3.3: Word alignment validator
# ---------------------------------------------------------------------------

def _create_alignment_test_dbs(
    tmp_path: Path,
    ayahs: list[tuple[int, str]],
    morph_words: list[tuple[int, int, str]],
    wbw_words: list[tuple[int, int, str]],
) -> tuple[Path, Path, Path]:
    core_path = tmp_path / "quran-core.db"
    morph_path = tmp_path / "quran-morphology.db"
    wbw_path = tmp_path / "quran-wbw-en.db"

    with sqlite3.connect(core_path) as conn:
        conn.execute("CREATE TABLE ayahs (id INTEGER PRIMARY KEY, verse_key TEXT NOT NULL UNIQUE);")
        conn.executemany("INSERT INTO ayahs (id, verse_key) VALUES (?, ?);", ayahs)

    with sqlite3.connect(morph_path) as conn:
        conn.execute(
            "CREATE TABLE words (id INTEGER PRIMARY KEY, ayah_id INTEGER NOT NULL, word_number INTEGER NOT NULL, word_key TEXT NOT NULL UNIQUE);"
        )
        conn.executemany(
            "INSERT INTO words (ayah_id, word_number, word_key) VALUES (?, ?, ?);",
            morph_words,
        )

    with sqlite3.connect(wbw_path) as conn:
        conn.execute(
            "CREATE TABLE word_translations (id INTEGER PRIMARY KEY, ayah_id INTEGER NOT NULL, word_number INTEGER NOT NULL, word_key TEXT NOT NULL UNIQUE);"
        )
        conn.executemany(
            "INSERT INTO word_translations (ayah_id, word_number, word_key) VALUES (?, ?, ?);",
            wbw_words,
        )

    return core_path, morph_path, wbw_path


def test_task_3_3_word_alignment_aligned_happy_path(tmp_path: Path) -> None:
    from pipelines.validators.word_alignment import validate_word_alignment

    ayahs = [(1, "1:1"), (2, "1:2")]
    morph_words = [
        (1, 1, "1:1:1"), (1, 2, "1:1:2"), (1, 3, "1:1:3"), (1, 4, "1:1:4"),
        (2, 1, "1:2:1"), (2, 2, "1:2:2"),
    ]
    wbw_words = list(morph_words)

    core_p, morph_p, wbw_p = _create_alignment_test_dbs(tmp_path, ayahs, morph_words, wbw_words)
    res = validate_word_alignment(core_p, morph_p, wbw_p)
    assert res is None


def test_task_3_3_word_alignment_equal_total_displaced_key_mismatch(tmp_path: Path) -> None:
    from pipelines.validators.word_alignment import validate_word_alignment

    ayahs = [(1, "1:1")]
    morph_words = [(1, 1, "1:1:1"), (1, 2, "1:1:2")]
    wbw_words = [(1, 1, "1:1:1"), (1, 2, "1:1:3")]  # Equal count (2), but displaced key at ordinal 2

    core_p, morph_p, wbw_p = _create_alignment_test_dbs(tmp_path, ayahs, morph_words, wbw_words)
    with pytest.raises(ValueError) as exc:
        validate_word_alignment(core_p, morph_p, wbw_p)

    err_msg = str(exc.value)
    assert "1:1" in err_msg
    assert "QAC=2 QUL=2 delta=0" in err_msg
    assert "missing ordinals:" in err_msg
    assert "extra ordinals:" in err_msg
    assert "2" in err_msg


def test_task_3_3_word_alignment_multi_ayah_mismatch_numeric_order(tmp_path: Path) -> None:
    from pipelines.validators.word_alignment import validate_word_alignment

    ayahs = [(1, "1:1"), (2, "1:2"), (3, "1:3")]
    # Ayah 1: QAC has 4 words, QUL has 3 words (missing word 4, delta = -1)
    # Ayah 2: Aligned (2 words each)
    # Ayah 3: QAC has 2 words, QUL has 3 words (extra word 3, delta = 1)
    morph_words = [
        (1, 1, "1:1:1"), (1, 2, "1:1:2"), (1, 3, "1:1:3"), (1, 4, "1:1:4"),
        (2, 1, "1:2:1"), (2, 2, "1:2:2"),
        (3, 1, "1:3:1"), (3, 2, "1:3:2"),
    ]
    wbw_words = [
        (1, 1, "1:1:1"), (1, 2, "1:1:2"), (1, 3, "1:1:3"),
        (2, 1, "1:2:1"), (2, 2, "1:2:2"),
        (3, 1, "1:3:1"), (3, 2, "1:3:2"), (3, 3, "1:3:3"),
    ]

    core_p, morph_p, wbw_p = _create_alignment_test_dbs(tmp_path, ayahs, morph_words, wbw_words)
    with pytest.raises(ValueError) as exc:
        validate_word_alignment(core_p, morph_p, wbw_p)

    err_msg = str(exc.value)
    # Ayah 2 should not be in the mismatch report
    assert "1:2" not in err_msg

    # Both Ayah 1 and Ayah 3 must be in error report
    assert "1:1" in err_msg
    assert "1:3" in err_msg

    # Per-ayah diagnostics in numeric order: Ayah 1 precedes Ayah 3
    pos_1_1 = err_msg.find("1:1")
    pos_1_3 = err_msg.find("1:3")
    assert 0 <= pos_1_1 < pos_1_3

    # Check diagnostics contents
    assert "QAC=4 QUL=3 delta=-1" in err_msg
    assert "QAC=2 QUL=3 delta=1" in err_msg
    assert "missing ordinals:" in err_msg
    assert "extra ordinals:" in err_msg


def test_task_3_3_word_alignment_missing_file_and_empty_core(tmp_path: Path) -> None:
    from pipelines.validators.word_alignment import validate_word_alignment

    non_existent = tmp_path / "does_not_exist.db"
    core_p, morph_p, wbw_p = _create_alignment_test_dbs(tmp_path, [(1, "1:1")], [(1, 1, "1:1:1")], [(1, 1, "1:1:1")])

    with pytest.raises(FileNotFoundError):
        validate_word_alignment(non_existent, morph_p, wbw_p)

    # Empty core
    empty_core = tmp_path / "empty_core.db"
    with sqlite3.connect(empty_core) as conn:
        conn.execute("CREATE TABLE ayahs (id INTEGER PRIMARY KEY, verse_key TEXT NOT NULL UNIQUE);")
    with pytest.raises(ValueError, match="contains no ayahs"):
        validate_word_alignment(empty_core, morph_p, wbw_p)



# ---------------------------------------------------------------------------
# Task 3.5 tests: Page layouts and Tajweed pack builder
# ---------------------------------------------------------------------------

def test_task_3_5_mushaf_builder_cli_arg_handling() -> None:
    from pipelines.builders.build_quran_mushaf import main

    # Missing required --pack
    assert main(["--sources-root", "/tmp/nonexistent", "--output-dir", "/tmp/nonexistent"]) == 2

    # Invalid choice for --pack
    assert main(["--sources-root", "/tmp/nonexistent", "--output-dir", "/tmp/nonexistent", "--pack", "unknown-pack"]) == 2

    # Nonexistent sources-root
    assert main(["--sources-root", "/tmp/nonexistent_sources_root_dir", "--output-dir", "/tmp/out", "--pack", "quran-mushaf-madani-v2"]) == 2

def test_task_3_5_builder_rejects_unsupported_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipelines.builders.build_quran_mushaf import build_quran_mushaf
    monkeypatch.setattr("pipelines.builders.build_quran_mushaf.verify_runtime", lambda: (Path("ffmpeg"), Path("ffprobe")))
    with pytest.raises(ValueError, match="Unknown or unsupported mushaf pack ID"):
        build_quran_mushaf(tmp_path, tmp_path, pack_id="invalid-pack")


def test_task_3_5_layout_info_validation() -> None:
    from pipelines.builders.build_quran_mushaf import validate_layout_info

    # Empty info table
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE info (name TEXT, number_of_pages INT, lines_per_page INT, font_name TEXT);")
    with pytest.raises(ValueError, match="Expected exactly 1 row"):
        validate_layout_info(con, expected_pages=604)

    # Multiple rows in info table
    con.execute("INSERT INTO info VALUES ('A', 604, 15, 'f1');")
    con.execute("INSERT INTO info VALUES ('B', 604, 15, 'f2');")
    with pytest.raises(ValueError, match="Expected exactly 1 row"):
        validate_layout_info(con, expected_pages=604)

    # Blank name
    con.execute("DELETE FROM info;")
    con.execute("INSERT INTO info VALUES ('   ', 604, 15, 'font_v2');")
    with pytest.raises(ValueError, match="nonblank"):
        validate_layout_info(con, expected_pages=604)

    # Blank font_name
    con.execute("DELETE FROM info;")
    con.execute("INSERT INTO info VALUES ('QPC V2', 604, 15, '');")
    with pytest.raises(ValueError, match="nonblank"):
        validate_layout_info(con, expected_pages=604)

    # Wrong page count
    con.execute("DELETE FROM info;")
    con.execute("INSERT INTO info VALUES ('QPC V2', 605, 15, 'font_v2');")
    with pytest.raises(ValueError, match="page count mismatch"):
        validate_layout_info(con, expected_pages=604)

    # Wrong lines per page
    con.execute("DELETE FROM info;")
    con.execute("INSERT INTO info VALUES ('QPC V2', 604, 16, 'font_v2');")
    with pytest.raises(ValueError, match="lines_per_page mismatch"):
        validate_layout_info(con, expected_pages=604)

    # Valid info row returns exact fields
    con.execute("DELETE FROM info;")
    con.execute("INSERT INTO info VALUES ('QPC V2 15 Lines', 604, 15, 'qpc_v2');")
    row = validate_layout_info(con, expected_pages=604)
    assert row == ("QPC V2 15 Lines", 604, 15, "qpc_v2")
    con.close()


def test_task_3_5_page_lines_validation_and_null_conversion() -> None:
    from pipelines.builders.build_quran_mushaf import validate_page_lines

    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE pages ("
        "page_number INT, line_number INT, line_type TEXT, is_centered INT, "
        "first_word_id INT, last_word_id INT, surah_number INT);"
    )

    # Empty string in optional numeric column converted to NULL / None
    con.execute("INSERT INTO pages VALUES (1, 1, 'surah_name', 1, '', '', 1);")
    con.execute("INSERT INTO pages VALUES (1, 2, 'basmallah', 1, '', '', '');")
    for l in range(3, 9):
        con.execute(f"INSERT INTO pages VALUES (1, {l}, 'ayah', 0, {(l-3)*3 + 1}, {(l-3)*3 + 3}, '');")
    for l in range(1, 9):
        con.execute(f"INSERT INTO pages VALUES (2, {l}, 'ayah', 0, {18 + (l-1)*3 + 1}, {18 + (l-1)*3 + 3}, '');")

    rows = validate_page_lines(con, expected_pages=2, expected_lines=16)
    assert rows[0] == (1, 1, "surah_name", 1, None, None, 1)
    assert rows[1] == (1, 2, "basmallah", 1, None, None, None)
    assert rows[2] == (1, 3, "ayah", 0, 1, 3, None)

    # Wrong type: string representing integer in optional field
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'surah_name', 1, NULL, NULL, 'bad_int');")
    with pytest.raises(ValueError, match="[Ii]nvalid type|[Ww]rong type"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Wrong type: boolean in is_centered
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'surah_name', 2, NULL, NULL, 1);")
    with pytest.raises(ValueError, match="is_centered"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Wrong line type
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'footer', 0, NULL, NULL, NULL);")
    with pytest.raises(ValueError, match="line_type"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Ayah line with surah_number not None
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'ayah', 0, 1, 3, 1);")
    with pytest.raises(ValueError, match="ayah.*surah_number"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Ayah line with missing word range
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'ayah', 0, NULL, 3, NULL);")
    with pytest.raises(ValueError, match="ayah.*word range"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Ayah line with non-positive word ID
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'ayah', 0, 0, 3, NULL);")
    with pytest.raises(ValueError, match="positive"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Ayah line with first > last
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'ayah', 0, 5, 3, NULL);")
    with pytest.raises(ValueError, match="first.*last"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Surah name with word IDs
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'surah_name', 1, 1, NULL, 1);")
    with pytest.raises(ValueError, match="surah_name.*word"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Surah name with out of range surah number (115)
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'surah_name', 1, NULL, NULL, 115);")
    with pytest.raises(ValueError, match="surah_number.*1..114"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Basmallah with non-null optional field
    con.execute("DELETE FROM pages;")
    con.execute("INSERT INTO pages VALUES (1, 1, 'basmallah', 1, NULL, NULL, 1);")
    with pytest.raises(ValueError, match="basmallah"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)
    con.close()


def test_task_3_5_page_lines_rejects_overlapping_or_nonincreasing_word_range() -> None:
    from pipelines.builders.build_quran_mushaf import validate_page_lines

    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE pages ("
        "page_number INT, line_number INT, line_type TEXT, is_centered INT, "
        "first_word_id INT, last_word_id INT, surah_number INT);"
    )
    # Page 1: 8 lines. Line 3 has word range 1..5. Line 4 has overlapping word range 5..8 (5 <= 5)
    con.execute("INSERT INTO pages VALUES (1, 1, 'surah_name', 1, NULL, NULL, 1);")
    con.execute("INSERT INTO pages VALUES (1, 2, 'basmallah', 1, NULL, NULL, NULL);")
    con.execute("INSERT INTO pages VALUES (1, 3, 'ayah', 0, 1, 5, NULL);")
    con.execute("INSERT INTO pages VALUES (1, 4, 'ayah', 0, 5, 8, NULL);")
    for l in range(5, 9):
        con.execute(f"INSERT INTO pages VALUES (1, {l}, 'ayah', 0, {(l-4)*4 + 9}, {(l-4)*4 + 12}, NULL);")
    for l in range(1, 9):
        con.execute(f"INSERT INTO pages VALUES (2, {l}, 'ayah', 0, {30 + (l-1)*3 + 1}, {30 + (l-1)*3 + 3}, NULL);")
    with pytest.raises(ValueError, match="[Oo]verlapp|[Nn]on-increasing"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)
    con.close()


def test_task_3_5_page_lines_structure_and_counts() -> None:
    from pipelines.builders.build_quran_mushaf import validate_page_lines

    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE pages ("
        "page_number INT, line_number INT, line_type TEXT, is_centered INT, "
        "first_word_id INT, last_word_id INT, surah_number INT);"
    )

    # Page 1 has 7 lines instead of 8
    for l in range(1, 8):
        con.execute(f"INSERT INTO pages VALUES (1, {l}, 'ayah', 0, {(l-1)*3 + 1}, {(l-1)*3 + 3}, NULL);")
    for l in range(1, 9):
        con.execute(f"INSERT INTO pages VALUES (2, {l}, 'ayah', 0, {25 + (l-1)*3 + 1}, {25 + (l-1)*3 + 3}, NULL);")
    with pytest.raises(ValueError, match="line count mismatch"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Page 1 has line numbers with a gap: 1, 2, 4..9 (total 8 lines, but gap)
    con.execute("DELETE FROM pages;")
    for idx, l in enumerate([1, 2, 4, 5, 6, 7, 8, 9], start=1):
        con.execute(f"INSERT INTO pages VALUES (1, {l}, 'ayah', 0, {(idx-1)*3 + 1}, {(idx-1)*3 + 3}, NULL);")
    for l in range(1, 9):
        con.execute(f"INSERT INTO pages VALUES (2, {l}, 'ayah', 0, {30 + (l-1)*3 + 1}, {30 + (l-1)*3 + 3}, NULL);")
    with pytest.raises(ValueError, match="[Gg]ap|[Ll]ine number"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)

    # Page missing: page 1 then page 3
    con.execute("DELETE FROM pages;")
    for l in range(1, 9):
        con.execute(f"INSERT INTO pages VALUES (1, {l}, 'ayah', 0, {(l-1)*3 + 1}, {(l-1)*3 + 3}, NULL);")
    for l in range(1, 9):
        con.execute(f"INSERT INTO pages VALUES (3, {l}, 'ayah', 0, {30 + (l-1)*3 + 1}, {30 + (l-1)*3 + 3}, NULL);")
    with pytest.raises(ValueError, match="[Pp]age"):
        validate_page_lines(con, expected_pages=2, expected_lines=16)
    con.close()


def test_task_3_5_tajweed_markup_validation(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_mushaf import validate_tajweed_verses

    # Invalid tag in verse markup
    bad_data = {
        "1:1": {
            "id": 1,
            "verse_key": "1:1",
            "surah": 1,
            "ayah": 1,
            "text": "بِسۡمِ <foo>ٱ</foo>للَّهِ ١",
        }
    }
    bad_zip = tmp_path / "bad_tajweed.json.zip"
    bad_zip.write_bytes(_make_zip_bytes("bad_tajweed.json", json.dumps(bad_data).encode("utf-8")))

    with pytest.raises(ValueError, match="Invalid tag"):
        validate_tajweed_verses(bad_zip, {"1:1": 1}, expected_count=1)

    # Valid markup with <rule class=...> and nested depth <= 2
    good_data = {
        "1:1": {
            "id": 1,
            "verse_key": "1:1",
            "surah": 1,
            "ayah": 1,
            "text": "بِسۡمِ <rule class=ham_wasl>ٱ</rule>للَّهِ <rule class=ham_wasl>ٱ</rule><rule class=laam_shamsiyah>ل</rule>رَّحۡمَ<rule class=madda_normal>ـٰ</rule>نِ ١",
        },
        "1:2": {
            "id": 2,
            "verse_key": "1:2",
            "surah": 1,
            "ayah": 2,
            "text": "ٱلۡحَمۡدُ لِلَّهِ رَبِّ ٱلۡعَـٰلَمِينَ ٢",
        },
    }
    good_zip = tmp_path / "good_tajweed.json.zip"
    good_zip.write_bytes(_make_zip_bytes("good_tajweed.json", json.dumps(good_data).encode("utf-8")))

    rows = validate_tajweed_verses(good_zip, {"1:1": 1, "1:2": 2}, expected_count=2)
    assert len(rows) == 2
    assert rows[0] == (1, "1:1", good_data["1:1"]["text"])
    assert rows[1] == (2, "1:2", good_data["1:2"]["text"])


def test_task_3_5_mushaf_pack_writer_and_schema(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_mushaf import write_mushaf_pack

    repo_root = Path(__file__).resolve().parents[1]
    schema_path = repo_root / "specs" / "sql" / "quran_mushaf.v1.sql"
    out_db = tmp_path / "quran-mushaf-madani-v2.db"

    metadata = {
        "category": "quran",
        "completeness": "complete",
        "fts5_enabled": "0",
        "id": "quran-mushaf-madani-v2",
        "languages": "ar",
        "license": "LicenseRef-QUL-Unreviewed",
        "pack_version": "0.3.0",
        "provenance": "QUL",
        "schema_version": "1",
    }
    layout_info_row = ("QPC V2 15 Lines", 604, 15, "qpc_v2")
    page_lines_rows = [
        (1, 1, "surah_name", 1, None, None, 1),
        (1, 2, "basmallah", 1, None, None, None),
        (1, 3, "ayah", 0, 1, 4, None),
    ]
    tajweed_rows = [
        (1, "1:1", "بِسۡمِ <rule class=ham_wasl>ٱ</rule>للَّهِ ١"),
    ]

    write_mushaf_pack(
        out_db,
        schema_path,
        metadata,
        layout_info_row,
        page_lines_rows,
        tajweed_rows,
        expected_total=6236,
    )

    assert out_db.is_file()
    con = sqlite3.connect(f"{out_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("PRAGMA integrity_check;")
        assert cur.fetchall() == [("ok",)]

        # Verify pack_meta
        cur.execute("SELECT key, value FROM pack_meta ORDER BY key;")
        meta = dict(cur.fetchall())
        assert meta["pack_version"] == "0.3.0"
        assert meta["fts5_enabled"] == "0"
        assert meta["id"] == "quran-mushaf-madani-v2"

        # Verify coverage_report
        cur.execute("SELECT metric, count FROM coverage_report ORDER BY metric;")
        cov = dict(cur.fetchall())
        assert cov["expected_total"] == 6236
        assert cov["ar_present"] == 6236

        # Verify layout_info
        cur.execute("SELECT id, name, page_count, lines_per_page, font_name FROM layout_info;")
        info_rows = cur.fetchall()
        assert info_rows == [(1, "QPC V2 15 Lines", 604, 15, "qpc_v2")]

        # Verify page_lines representative rows
        cur.execute(
            "SELECT page_number, line_number, line_type, is_centered, "
            "first_word_id, last_word_id, surah_number FROM page_lines ORDER BY page_number, line_number;"
        )
        pl_rows = cur.fetchall()
        assert pl_rows[0] == (1, 1, "surah_name", 1, None, None, 1)
        assert pl_rows[1] == (1, 2, "basmallah", 1, None, None, None)
        assert pl_rows[2] == (1, 3, "ayah", 0, 1, 4, None)

        # Verify tajweed_ayahs
        cur.execute("SELECT ayah_id, verse_key, text_markup FROM tajweed_ayahs;")
        tj_rows = cur.fetchall()
        assert tj_rows == [(1, "1:1", "بِسۡمِ <rule class=ham_wasl>ٱ</rule>للَّهِ ١")]

        # Verify no FTS tables exist
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%fts%';")
        assert cur.fetchall() == []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Task 3.6 Tests: Recitation Timing Manifests and Narrow Decimal Decoding
# ---------------------------------------------------------------------------

def test_task_3_6_lock_inputs_parse_strict_json_decimal_mode() -> None:
    from decimal import Decimal
    from pipelines.common.lock_inputs import parse_strict_json

    json_text = '{"duration": 12.5, "count": 10}'

    # Default behavior: rejects float/decimal tokens strictly
    with pytest.raises(ValueError, match="Floating point token"):
        parse_strict_json(json_text)

    # allow_decimal=True: parses decimal token as Decimal
    res_dec = parse_strict_json(json_text, allow_decimal=True)
    assert isinstance(res_dec["duration"], Decimal)
    assert res_dec["duration"] == Decimal("12.5")
    assert res_dec["count"] == 10
    assert isinstance(res_dec["count"], int)


def test_task_3_6_duration_half_up_rounding() -> None:
    from decimal import Decimal
    from pipelines.builders.build_recitation_manifests import duration_seconds_to_reported_ms

    # Null duration
    assert duration_seconds_to_reported_ms(None) is None

    # Exact whole seconds
    assert duration_seconds_to_reported_ms(Decimal("6.0")) == 6000
    assert duration_seconds_to_reported_ms(Decimal("71.0")) == 71000
    assert duration_seconds_to_reported_ms(0) == 0

    # Half-up rounding tests
    assert duration_seconds_to_reported_ms(Decimal("1.0004")) == 1000
    assert duration_seconds_to_reported_ms(Decimal("1.0005")) == 1001
    assert duration_seconds_to_reported_ms(Decimal("1.0006")) == 1001
    assert duration_seconds_to_reported_ms(Decimal("0.0005")) == 1
    assert duration_seconds_to_reported_ms(Decimal("0.0004")) == 0

    # Rejection of invalid types and values
    with pytest.raises(ValueError, match="float"):
        duration_seconds_to_reported_ms(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bool"):
        duration_seconds_to_reported_ms(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="negative"):
        duration_seconds_to_reported_ms(Decimal("-1.0"))
    with pytest.raises(ValueError, match="negative"):
        duration_seconds_to_reported_ms(-10)


def test_task_3_6_manifest_schema_and_canonical_byte_dump(tmp_path: Path) -> None:
    import json
    from jsonschema import Draft202012Validator
    from pipelines.builders.build_recitation_manifests import (
        RECITATION_PACKS,
        render_recitation_manifest,
        build_recitation_manifests,
    )

    repo_root = Path(__file__).resolve().parents[1]
    schema_path = repo_root / "specs" / "recitation-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    # Test RECITATION_PACKS inventory
    assert len(RECITATION_PACKS) == 7
    expected_pack_ids = {
        "audio-recitation-abdul-basit-mujawwad",
        "audio-recitation-abdul-basit-murattal",
        "audio-recitation-al-husary-muallim",
        "audio-recitation-al-husary-mujawwad",
        "audio-recitation-al-husary-murattal",
        "audio-recitation-al-minshawi-murattal",
        "audio-recitation-al-tablawi-murattal",
    }
    assert set(RECITATION_PACKS.keys()) == expected_pack_ids

    from decimal import Decimal
    from pipelines.common.lock_inputs import author_lock
    from pipelines.common.tanzil_coordinates import load_tanzil_coordinates
    from tests.fixtures.tanzil import build_synthetic_metadata_xml

    sources_root = tmp_path / "repo"
    raw_root = sources_root / "raw-inputs"
    sources_dir = sources_root / "sources"
    specs_dir = sources_root / "specs"
    licenses_dir = sources_dir / "licenses"

    specs_dir.mkdir(parents=True)
    licenses_dir.mkdir(parents=True)
    (raw_root / "tanzil").mkdir(parents=True)
    (raw_root / "qul" / "recitation").mkdir(parents=True)

    # Copy schema into synthetic specs_dir
    (specs_dir / "recitation-manifest.schema.json").write_bytes(schema_path.read_bytes())

    # Locate or generate canonical Tanzil metadata XML
    tanzil_path = raw_root / "tanzil" / "quran-data-Tanzil.xml"
    tanzil_bytes = None
    for cand in [
        repo_root / "raw-inputs" / "tanzil" / "quran-data-Tanzil.xml",
        Path("/repo/raw-inputs/tanzil/quran-data-Tanzil.xml"),
        Path("C:/Users/moham/Downloads/islamic-core-content/raw-inputs/tanzil/quran-data-Tanzil.xml"),
    ]:
        if cand.is_file():
            tanzil_bytes = cand.read_bytes()
            break

    if tanzil_bytes is not None:
        tanzil_path.write_bytes(tanzil_bytes)
    else:
        canonical_counts = (
            7, 286, 200, 176, 120, 165, 206, 75, 129, 109, 123, 111, 43, 52, 99, 128, 111, 110, 98, 135,
            112, 78, 118, 64, 77, 227, 93, 88, 69, 60, 34, 30, 73, 54, 45, 83, 182, 88, 75, 85,
            54, 53, 89, 59, 37, 35, 38, 29, 18, 45, 60, 49, 62, 55, 78, 96, 29, 22, 24, 13,
            14, 11, 11, 18, 12, 12, 30, 52, 52, 44, 28, 28, 20, 56, 40, 31, 50, 40, 46, 42,
            29, 19, 36, 25, 22, 17, 19, 26, 30, 20, 15, 21, 11, 8, 8, 19, 5, 8, 8, 11,
            11, 8, 3, 9, 5, 4, 7, 3, 6, 3, 5, 4, 5, 6
        )
        canonical_starts = []
        acc = 0
        for c in canonical_counts:
            canonical_starts.append(acc)
            acc += c
        def _cid(gid: int) -> tuple[int, int]:
            for idx, (s, cnt) in enumerate(zip(canonical_starts, canonical_counts), start=1):
                if s < gid <= s + cnt:
                    return idx, gid - s
            raise ValueError(f"Out of range: {gid}")
        import tests.fixtures.tanzil as tf
        o_fn, o_cnt, o_st = tf.global_id_to_sura_aya, tf.SURAH_AYAH_COUNTS, tf.SURAH_STARTS
        try:
            tf.global_id_to_sura_aya = _cid
            tf.SURAH_AYAH_COUNTS = canonical_counts
            tf.SURAH_STARTS = canonical_starts
            tanzil_xml = build_synthetic_metadata_xml(surah_counts=canonical_counts)
        finally:
            tf.global_id_to_sura_aya, tf.SURAH_AYAH_COUNTS, tf.SURAH_STARTS = o_fn, o_cnt, o_st
        tanzil_path.write_text(tanzil_xml, encoding="utf-8")

    coords = load_tanzil_coordinates(tanzil_path)
    assert len(coords.ayah_ids) == 6236

    reg_data: dict[str, Any] = {
        "tanzil/quran-data-Tanzil.xml": {
            "packIds": sorted(expected_pack_ids),
            "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml",
            "sourceVersion": "1.0",
        }
    }

    # Build synthetic recitation data for all 7 packs
    from pipelines.builders.build_recitation_manifests import TABLAWI_KNOWN_INVALID_SEGMENTS
    tablawi_invalid_map = {(vk, idx): list(seg) for vk, idx, seg in TABLAWI_KNOWN_INVALID_SEGMENTS}

    for pack_id, pack_info in RECITATION_PACKS.items():
        ayah_data: dict[str, Any] = {}
        is_tablawi = (pack_id == "audio-recitation-al-tablawi-murattal")
        for vk in coords.ayah_ids:
            s, a = map(int, vk.split(":"))
            dur: Any = Decimal("6.0") if is_tablawi else None
            if is_tablawi:
                diag_for_vk = {d_idx: d_seg for (d_vk, d_idx), d_seg in tablawi_invalid_map.items() if d_vk == vk}
                if diag_for_vk:
                    max_idx = max(diag_for_vk.keys())
                    segs = []
                    for idx in range(1, max_idx + 1):
                        if idx in diag_for_vk:
                            segs.append(list(diag_for_vk[idx]))
                        else:
                            segs.append([idx, (idx - 1) * 100, idx * 100])
                else:
                    segs = [[1, 0, 500]]
            else:
                segs = [[1, 0, 500]]
            ayah_data[vk] = {
                "surah_number": s,
                "ayah_number": a,
                "audio_url": f"{pack_info['https_prefix']}/{s:03d}{a:03d}.mp3",
                "duration": dur,
                "segments": segs,
            }

        zip_bytes = _make_zip_bytes(pack_info["member_name"], json.dumps(ayah_data, default=float).encode("utf-8"))
        (raw_root / pack_info["archive_path"]).write_bytes(zip_bytes)
        archive_sha = hashlib.sha256(zip_bytes).hexdigest()

        reg_data[pack_info["archive_path"]] = {
            "packIds": [pack_id],
            "sourceUrl": f"https://example.com/recitation/{pack_id}",
            "sourceVersion": f"sha256:{archive_sha}",
        }

        lic_data = {
            "packId": pack_id,
            "licenseSpdx": "LicenseRef-QUL-Unreviewed",
            "primaryPath": pack_info["archive_path"],
            "status": "pending",
            "reviewedBy": None,
            "sources": [
                {
                    "attribution": "Tarteel",
                    "name": f"Recitation — {pack_info['reciter']}",
                    "notice": "Notice",
                    "path": pack_info["archive_path"],
                    "permittedUses": [],
                    "rightsEvidence": ["https://example.com"],
                    "sourceUrl": f"https://example.com/recitation/{pack_id}",
                    "sourceVersion": f"sha256:{archive_sha}",
                },
                {
                    "attribution": "Tanzil Project",
                    "name": "Tanzil Quran Metadata",
                    "notice": "Notice",
                    "path": "tanzil/quran-data-Tanzil.xml",
                    "permittedUses": [],
                    "rightsEvidence": ["https://example.com"],
                    "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml",
                    "sourceVersion": "1.0",
                },
            ],
        }
        (licenses_dir / f"{pack_id}.json").write_text(
            json.dumps(lic_data, sort_keys=True, indent=2), encoding="utf-8"
        )

    # Write registry and author lock
    (sources_dir / "registry.json").write_text(
        json.dumps(reg_data, sort_keys=True, indent=2), encoding="utf-8"
    )
    author_lock(sources_dir / "registry.json", raw_root, sources_dir / "manifest.lock.json")

    # Test render_recitation_manifest for Abdul Basit Mujawwad and Al-Tablawi Murattal
    m_abdul_basit = render_recitation_manifest(sources_root, "audio-recitation-abdul-basit-mujawwad")
    assert m_abdul_basit.endswith(b"\n")
    obj_ab = json.loads(m_abdul_basit.decode("utf-8"))
    validator.validate(obj_ab)
    assert obj_ab["packId"] == "audio-recitation-abdul-basit-mujawwad"
    assert obj_ab["style"] == "mujawwad"
    assert obj_ab["reciter"] == "Abdul Basit Abdul Samad"
    assert obj_ab["ayahs"][0]["reportedDurationMs"] is None
    assert obj_ab["ayahs"][0]["invalidSourceSegments"] == []

    m_tablawi = render_recitation_manifest(sources_root, "audio-recitation-al-tablawi-murattal")
    assert m_tablawi.endswith(b"\n")
    obj_tab = json.loads(m_tablawi.decode("utf-8"))
    validator.validate(obj_tab)
    assert obj_tab["packId"] == "audio-recitation-al-tablawi-murattal"
    assert obj_tab["style"] == "murattal"
    assert obj_tab["reciter"] == "Mohamed Al-Tablawi"
    assert obj_tab["ayahs"][0]["reportedDurationMs"] == 6000

    # Test build_recitation_manifests to output directory
    out_dir = tmp_path / "output_manifests"
    out_dir.mkdir()

    # Pre-existing file check: should refuse
    clash_file = out_dir / "audio-recitation-abdul-basit-mujawwad.json"
    clash_file.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        build_recitation_manifests(sources_root, out_dir)

    # Remove clash file and test dangling symlink refusal
    clash_file.unlink()
    dangling_link = out_dir / "audio-recitation-al-husary-muallim.json"
    try:
        dangling_link.symlink_to(out_dir / "nonexistent_target.json")
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(FileExistsError, match="already exists or is a symlink"):
            build_recitation_manifests(sources_root, out_dir)
        dangling_link.unlink()
    # Test output directory itself being a symlink
    symlink_out = tmp_path / "symlink_output"
    try:
        symlink_out.symlink_to(out_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(ValueError, match="Output directory cannot be a symlink or junction"):
            build_recitation_manifests(sources_root, symlink_out)


    # Build manifests cleanly
    written_paths = build_recitation_manifests(sources_root, out_dir)
    assert len(written_paths) == 7
    for p in written_paths:
        assert p.is_file()
        raw_content = p.read_bytes()
        assert raw_content.endswith(b"\n")
        parsed = json.loads(raw_content.decode("utf-8"))
        validator.validate(parsed)

    # Test moved/unlocked archive path or license primaryPath mismatch
    lic_ab_path = licenses_dir / "audio-recitation-abdul-basit-mujawwad.json"
    lic_ab_orig = lic_ab_path.read_text(encoding="utf-8")
    lic_ab_bad = json.loads(lic_ab_orig)
    lic_ab_bad["primaryPath"] = "qul/recitation/wrong_archive.json.zip"
    lic_ab_path.write_text(json.dumps(lic_ab_bad), encoding="utf-8")
    with pytest.raises(ValueError, match="primaryPath mismatch"):
        render_recitation_manifest(sources_root, "audio-recitation-abdul-basit-mujawwad")
    lic_ab_path.write_text(lic_ab_orig, encoding="utf-8")

    # Test sourceVersion mismatch in license
    lic_ab_bad = json.loads(lic_ab_orig)
    lic_ab_bad["sources"][0]["sourceVersion"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    lic_ab_path.write_text(json.dumps(lic_ab_bad), encoding="utf-8")
    with pytest.raises(ValueError, match="sourceVersion mismatch"):
        render_recitation_manifest(sources_root, "audio-recitation-abdul-basit-mujawwad")
    lic_ab_path.write_text(lic_ab_orig, encoding="utf-8")

    # Test moved/unlocked archive path (lock entries mismatch)
    extra_zip = raw_root / "qul" / "recitation" / "extra.zip"
    extra_zip.write_bytes(b"dummy")
    reg_bad = dict(reg_data)
    reg_bad["qul/recitation/extra.zip"] = {
        "packIds": ["audio-recitation-abdul-basit-mujawwad"],
        "sourceUrl": "https://example.com/extra",
        "sourceVersion": "sha256:" + hashlib.sha256(b"dummy").hexdigest(),
    }
    (sources_dir / "registry.json").write_text(json.dumps(reg_bad, sort_keys=True, indent=2), encoding="utf-8")
    author_lock(sources_dir / "registry.json", raw_root, sources_dir / "manifest.lock.json")
    with pytest.raises(ValueError, match="Lock entries mismatch"):
        render_recitation_manifest(sources_root, "audio-recitation-abdul-basit-mujawwad")
    extra_zip.unlink()
    (sources_dir / "registry.json").write_text(json.dumps(reg_data, sort_keys=True, indent=2), encoding="utf-8")
    author_lock(sources_dir / "registry.json", raw_root, sources_dir / "manifest.lock.json")

def test_task_3_6_tablawi_exact_reversed_intervals_and_reconstruction(tmp_path: Path) -> None:
    from pipelines.builders.build_recitation_manifests import process_ayah_segments

    # Test 2:164 segment with ordinal 22 having reversed timestamps [22, 39140, 39130] at sourceIndex 22
    raw_2_164_segments = (
        [[i, (i - 1) * 100, i * 100] for i in range(1, 22)]
        + [[22, 39140, 39130]]  # 22nd item: reversed interval!
        + [[23, 39140, 42840]]
    )

    # For Al-Tablawi, the known diagnostic is tolerated and partitioned
    valid_segs, invalid_segs = process_ayah_segments(
        "2:164",
        raw_2_164_segments,
        pack_id="audio-recitation-al-tablawi-murattal",
    )
    assert len(valid_segs) == 22
    assert valid_segs[-1] == [23, 39140, 42840]
    assert invalid_segs == [{"sourceIndex": 22, "segment": [22, 39140, 39130]}]

    # Verify source reconstruction by placing diagnostics back at their 1-based sourceIndex
    reconstructed = []
    inv_by_idx = {item["sourceIndex"]: item["segment"] for item in invalid_segs}
    valid_iter = iter(valid_segs)
    for i in range(1, len(raw_2_164_segments) + 1):
        if i in inv_by_idx:
            reconstructed.append(inv_by_idx[i])
        else:
            reconstructed.append(next(valid_iter))
    assert reconstructed == raw_2_164_segments

    # For any other edition, the exact same reversed interval MUST be rejected
    with pytest.raises(ValueError, match="Invalid timing interval"):
        process_ayah_segments(
            "2:164",
            raw_2_164_segments,
            pack_id="audio-recitation-abdul-basit-mujawwad",
        )

    # For Tablawi, an unexpected reversed interval outside the allowlist MUST be rejected
    bad_tablawi_segments = [[1, 1000, 500]]
    with pytest.raises(ValueError, match="Unexpected invalid timing interval"):
        process_ayah_segments(
            "2:164",
            bad_tablawi_segments,
            pack_id="audio-recitation-al-tablawi-murattal",
        )


def test_task_3_6_rejected_malformed_timings_and_wrong_urls() -> None:
    from pipelines.builders.build_recitation_manifests import (
        process_ayah_segments,
        validate_audio_url,
        validate_ayah_record,
    )

    # Negative start timing
    with pytest.raises(ValueError, match="negative"):
        process_ayah_segments("1:1", [[1, -10, 100]], pack_id="audio-recitation-abdul-basit-murattal")

    # Negative end timing
    with pytest.raises(ValueError, match="negative"):
        process_ayah_segments("1:1", [[1, 0, -5]], pack_id="audio-recitation-abdul-basit-murattal")

    # Boolean timing
    with pytest.raises(ValueError, match="integer"):
        process_ayah_segments("1:1", [[True, 0, 100]], pack_id="audio-recitation-abdul-basit-murattal")
    with pytest.raises(ValueError, match="integer"):
        process_ayah_segments("1:1", [[1, False, 100]], pack_id="audio-recitation-abdul-basit-murattal")

    # Float timing
    with pytest.raises(ValueError, match="integer"):
        process_ayah_segments("1:1", [[1, 10.5, 100]], pack_id="audio-recitation-abdul-basit-murattal")

    # Malformed tuple shape (length != 3)
    with pytest.raises(ValueError, match="3 elements"):
        process_ayah_segments("1:1", [[1, 100]], pack_id="audio-recitation-abdul-basit-murattal")

    # Invalid word position (pos <= 0)
    with pytest.raises(ValueError, match="positive"):
        process_ayah_segments("1:1", [[0, 0, 100]], pack_id="audio-recitation-abdul-basit-murattal")

    # Empty segments list
    with pytest.raises(ValueError, match="cannot be empty"):
        process_ayah_segments("1:1", [], pack_id="audio-recitation-abdul-basit-murattal")

    # URL validation: wrong prefix for pack
    with pytest.raises(ValueError, match="URL prefix"):
        validate_audio_url(
            "https://wrong-domain.com/001001.mp3",
            "audio-recitation-abdul-basit-mujawwad",
            1,
            1,
        )

    # URL validation: wrong ayah number in URL
    with pytest.raises(ValueError, match="filename"):
        validate_audio_url(
            "https://audio-cdn.tarteel.ai/quran/abdulBasitMujawwad/001002.mp3",
            "audio-recitation-abdul-basit-mujawwad",
            1,
            1,
        )

    # Ayah record validation: missing required fields
    base_ayah_data = {
        "surah_number": 1,
        "ayah_number": 1,
        "audio_url": "https://audio-cdn.tarteel.ai/quran/abdulBasitMujawwad/001001.mp3",
        "duration": None,
        "segments": [[1, 0, 500]],
    }

    # Non-dict record
    with pytest.raises(ValueError, match="must be a dictionary"):
        validate_ayah_record("invalid", "1:1", "audio-recitation-abdul-basit-mujawwad")

    # Missing duration
    missing_dur = dict(base_ayah_data)
    del missing_dur["duration"]
    with pytest.raises(ValueError, match="fields mismatch"):
        validate_ayah_record(missing_dur, "1:1", "audio-recitation-abdul-basit-mujawwad")

    # Missing segments
    missing_segs = dict(base_ayah_data)
    del missing_segs["segments"]
    with pytest.raises(ValueError, match="fields mismatch"):
        validate_ayah_record(missing_segs, "1:1", "audio-recitation-abdul-basit-mujawwad")

    # Extra field in record
    extra_field = dict(base_ayah_data, unexpected=123)
    with pytest.raises(ValueError, match="fields mismatch"):
        validate_ayah_record(extra_field, "1:1", "audio-recitation-abdul-basit-mujawwad")

    # Boolean surah_number
    bool_surah = dict(base_ayah_data, surah_number=True)
    with pytest.raises(ValueError, match="must be integers"):
        validate_ayah_record(bool_surah, "1:1", "audio-recitation-abdul-basit-mujawwad")

    # Boolean ayah_number
    bool_ayah = dict(base_ayah_data, ayah_number=False)
    with pytest.raises(ValueError, match="must be integers"):
        validate_ayah_record(bool_ayah, "1:1", "audio-recitation-abdul-basit-mujawwad")

    # Boolean duration
    bool_dur = dict(base_ayah_data, duration=True)
    with pytest.raises(ValueError, match="bool"):
        validate_ayah_record(bool_dur, "1:1", "audio-recitation-abdul-basit-mujawwad")

    # Coordinate mismatch with verse_key
    wrong_coord = dict(base_ayah_data, ayah_number=2)
    with pytest.raises(ValueError, match="Surah/ayah number mismatch"):
        validate_ayah_record(wrong_coord, "1:1", "audio-recitation-abdul-basit-mujawwad")
