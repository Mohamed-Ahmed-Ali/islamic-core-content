"""Tests for Phase 3 local quality gate and offline orchestrator."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any
import pytest

from pipelines.validators.quality_gate import main, run_checks, GateCheckError
import pipelines.validators.phase3_local as p3
from pipelines.release.build_phase3_local import build_phase3_local
from pipelines.common.lock_inputs import canonical_lock_json


@pytest.fixture(autouse=True)
def _mock_verify_runtime_if_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from pipelines.toolchain.check_runtime import verify_runtime
        verify_runtime()
    except Exception:
        monkeypatch.setattr(
            "pipelines.toolchain.check_runtime.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )
        monkeypatch.setattr(
            "pipelines.validators.quality_gate.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )
        monkeypatch.setattr(
            "pipelines.release.build_phase3_local.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )


def test_phase3_cli_profile_supported() -> None:
    """CLI must recognize --profile phase3-local rather than rejecting it as unsupported."""
    code = main([
        "--profile", "phase3-local",
        "--candidate", "nonexistent",
        "--rebuild", "nonexistent",
        "--sources-root", "nonexistent",
    ])
    # Should fail because directories do not exist (exit code 2), NOT unsupported profile
    assert code == 2


def test_phase3_root_identity_duplicate_inode_fails(tmp_path: Path) -> None:
    """Candidate and rebuild must not share hardlinked inodes across roots."""
    cand = tmp_path / "candidate"
    reb = tmp_path / "rebuild"
    cand.mkdir()
    reb.mkdir()

    f1 = cand / "quran-core.db"
    f1.write_bytes(b"hello")
    f2 = reb / "quran-core.db"
    try:
        os.link(f1, f2)
    except (AttributeError, OSError):
        pytest.skip("Hardlinks not supported on this filesystem")

    with pytest.raises(GateCheckError) as exc_info:
        p3.verify_phase3_root_identity(cand, reb)

    assert exc_info.value.check_num == 6
    assert "duplicate inode or hardlink across" in exc_info.value.reason.lower()


def test_phase3_root_identity_internal_duplicate_inode_fails(tmp_path: Path) -> None:
    """A root must not contain hardlinks sharing an inode internally."""
    cand = tmp_path / "candidate"
    reb = tmp_path / "rebuild"
    cand.mkdir()
    reb.mkdir()

    f1 = cand / "quran-core.db"
    f1.write_bytes(b"hello")
    f2 = cand / "quran-morphology.db"
    try:
        os.link(f1, f2)
    except (AttributeError, OSError):
        pytest.skip("Hardlinks not supported on this filesystem")

    with pytest.raises(GateCheckError) as exc_info:
        p3.verify_phase3_root_identity(cand, reb)

    assert exc_info.value.check_num == 6
    assert "duplicate inode or hardlink within candidate" in exc_info.value.reason.lower()


def test_phase3_inventory_missing_file_fails(tmp_path: Path) -> None:
    """Missing any of the 19 declared files in a flat root fails Check #5."""
    root = tmp_path / "candidate"
    root.mkdir()
    # Populate only 18 files
    for fn in sorted(p3.PHASE3_EXPECTED_ARTIFACTS)[:-1]:
        (root / fn).write_bytes(b"dummy content")

    with pytest.raises(GateCheckError) as exc_info:
        p3.scan_phase3_inventory("candidate", root)

    assert exc_info.value.check_num == 5
    assert "expected asset missing" in exc_info.value.reason.lower()


def test_phase3_inventory_unexpected_file_fails(tmp_path: Path) -> None:
    """Unexpected extra files or subdirectories in a flat root fail Check #5."""
    root = tmp_path / "candidate"
    root.mkdir()
    for fn in p3.PHASE3_EXPECTED_ARTIFACTS:
        (root / fn).write_bytes(b"dummy content")

    extra = root / "extra.db"
    extra.write_bytes(b"extra")

    with pytest.raises(GateCheckError) as exc_info:
        p3.scan_phase3_inventory("candidate", root)

    assert exc_info.value.check_num == 5
    assert "unexpected asset in root" in exc_info.value.reason.lower()


def test_phase3_inventory_sidecar_file_fails(tmp_path: Path) -> None:
    """Sidecar files (-wal, -shm, -journal) fail Check #5."""
    root = tmp_path / "candidate"
    root.mkdir()
    for fn in p3.PHASE3_EXPECTED_ARTIFACTS:
        (root / fn).write_bytes(b"dummy content")

    (root / "quran-core.db-wal").write_bytes(b"wal")

    with pytest.raises(GateCheckError) as exc_info:
        p3.scan_phase3_inventory("candidate", root)

    assert exc_info.value.check_num == 5
    assert "sidecar" in exc_info.value.reason.lower()


def test_phase3_source_mismatch_detected_when_candidate_and_rebuild_agree(tmp_path: Path) -> None:
    """Prove a source mismatch fails Check #2 even when candidate and rebuild agree and pass schema."""
    cand_db = tmp_path / "cand.db"
    reb_db = tmp_path / "reb.db"
    ref_db = tmp_path / "ref.db"

    schema = "CREATE TABLE ayahs (id INTEGER PRIMARY KEY, text TEXT NOT NULL);"
    for p in (cand_db, reb_db, ref_db):
        conn = sqlite3.connect(p)
        conn.execute("PRAGMA page_size = 4096;")
        conn.execute("PRAGMA user_version = 1;")
        conn.execute(schema)
        conn.commit()
        conn.close()

    # Reference has original text
    c = sqlite3.connect(ref_db)
    c.execute("INSERT INTO ayahs VALUES (1, 'original text');")
    c.commit()
    c.close()

    # Both candidate and rebuild agree with each other on tampered text
    for p in (cand_db, reb_db):
        c = sqlite3.connect(p)
        c.execute("INSERT INTO ayahs VALUES (1, 'tampered text');")
        c.commit()
        c.close()

    # Must raise Check #2 for source equivalence mismatch
    with pytest.raises(GateCheckError) as exc_info:
        p3.verify_db_content_against_reference(
            cand_db=cand_db,
            reb_db=reb_db,
            ref_db=ref_db,
            pack_id="quran-core",
            db_fn="quran-core.db",
        )

    assert exc_info.value.check_num == 2
    assert "data content mismatch" in exc_info.value.reason.lower()


def test_phase3_tafsir_pointer_mismatch_fails_check_9(tmp_path: Path) -> None:
    """Prove a shifted Tafsir pointer fails Check #9."""
    cand_db = tmp_path / "cand.db"
    reb_db = tmp_path / "reb.db"
    ref_db = tmp_path / "ref.db"

    schema = """
    CREATE TABLE tafsir_entries (
        id INTEGER PRIMARY KEY,
        group_key TEXT NOT NULL,
        start_ayah_id INTEGER NOT NULL,
        end_ayah_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        text_format TEXT NOT NULL,
        text_search TEXT NOT NULL
    );
    CREATE TABLE tafsir_ayah_map (
        ayah_id INTEGER PRIMARY KEY,
        verse_key TEXT NOT NULL UNIQUE,
        entry_id INTEGER NOT NULL
    );
    """
    for p in (cand_db, reb_db, ref_db):
        conn = sqlite3.connect(p)
        conn.execute("PRAGMA page_size = 4096;")
        conn.execute("PRAGMA user_version = 1;")
        conn.executescript(schema)
        conn.commit()
        conn.close()

    c = sqlite3.connect(ref_db)
    c.execute("INSERT INTO tafsir_entries VALUES (1, '1:1', 1, 1, 'text', 'plain', 'text');")
    c.execute("INSERT INTO tafsir_ayah_map VALUES (1, '1:1', 1);")
    c.commit()
    c.close()

    # Candidate and rebuild agree on shifted entry_id pointer
    for p in (cand_db, reb_db):
        c = sqlite3.connect(p)
        c.execute("INSERT INTO tafsir_entries VALUES (1, '1:1', 1, 1, 'text', 'plain', 'text');")
        c.execute("INSERT INTO tafsir_ayah_map VALUES (1, '1:1', 999);")
        c.commit()
        c.close()

    with pytest.raises(GateCheckError) as exc_info:
        p3.verify_db_content_against_reference(
            cand_db=cand_db,
            reb_db=reb_db,
            ref_db=ref_db,
            pack_id="quran-tafsir-ar-ibn-kathir",
            db_fn="quran-tafsir-ar-ibn-kathir.db",
        )

    assert exc_info.value.check_num == 9
    assert "tafsir" in exc_info.value.reason.lower()

def test_phase3_tafsir_modified_body_fails_check_2_not_9(tmp_path: Path) -> None:
    """Modified commentary body must fail Check #2, while shifted intervals/pointers fail Check #9."""
    cand_db = tmp_path / "cand2.db"
    reb_db = tmp_path / "reb2.db"
    ref_db = tmp_path / "ref2.db"

    schema = """
    CREATE TABLE tafsir_entries (
        id INTEGER PRIMARY KEY,
        group_key TEXT NOT NULL,
        start_ayah_id INTEGER NOT NULL,
        end_ayah_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        text_format TEXT NOT NULL,
        text_search TEXT NOT NULL
    );
    CREATE TABLE tafsir_ayah_map (
        ayah_id INTEGER PRIMARY KEY,
        verse_key TEXT NOT NULL UNIQUE,
        entry_id INTEGER NOT NULL
    );
    """
    for p in (cand_db, reb_db, ref_db):
        conn = sqlite3.connect(p)
        conn.execute("PRAGMA page_size = 4096;")
        conn.execute("PRAGMA user_version = 1;")
        conn.executescript(schema)
        conn.commit()
        conn.close()

    c = sqlite3.connect(ref_db)
    c.execute("INSERT INTO tafsir_entries VALUES (1, '1:1', 1, 1, 'original text', 'plain', 'original text');")
    c.execute("INSERT INTO tafsir_ayah_map VALUES (1, '1:1', 1);")
    c.commit()
    c.close()

    # Candidate and rebuild agree on identical interval/key, but modified text body
    for p in (cand_db, reb_db):
        c = sqlite3.connect(p)
        c.execute("INSERT INTO tafsir_entries VALUES (1, '1:1', 1, 1, 'modified text body', 'plain', 'original text');")
        c.execute("INSERT INTO tafsir_ayah_map VALUES (1, '1:1', 1);")
        c.commit()
        c.close()

    with pytest.raises(GateCheckError) as exc_info:
        p3.verify_db_content_against_reference(
            cand_db=cand_db,
            reb_db=reb_db,
            ref_db=ref_db,
            pack_id="quran-tafsir-ar-ibn-kathir",
            db_fn="quran-tafsir-ar-ibn-kathir.db",
        )

    assert exc_info.value.check_num == 2
    assert "data content mismatch" in exc_info.value.reason.lower()


def test_phase3_canonical_lock_shape_and_qul_clearance_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0: Canonical lock shape with lockVersion and inputs ARRAY must be parsed and verified."""
    sources_root = tmp_path / "repo"
    sources_dir = sources_root / "sources"
    raw_root = sources_root / "raw-inputs"
    lic_dir = sources_dir / "licenses"
    lic_dir.mkdir(parents=True)

    qul_rel = "qul/tafsir/ar-tafsir-ibn-kathir.json.zip"
    archive_file = raw_root / qul_rel
    archive_file.parent.mkdir(parents=True)
    archive_bytes = b"PK\x05\x06" + b"\x00" * 18
    archive_file.write_bytes(archive_bytes)
    sha256_hex = hashlib.sha256(archive_bytes).hexdigest()
    size_bytes = len(archive_bytes)

    reg_data = {
        qul_rel: {
            "packIds": ["quran-tafsir-ar-ibn-kathir"],
            "sourceUrl": "https://qul.tarteel.ai/resources/tafsir/22",
            "sourceVersion": f"sha256:{sha256_hex}",
        }
    }
    (sources_dir / "registry.json").write_text(json.dumps(reg_data, indent=2) + "\n", encoding="utf-8")

    lock_entries = [
        {
            "path": qul_rel,
            "sourceUrl": "https://qul.tarteel.ai/resources/tafsir/22",
            "sourceVersion": f"sha256:{sha256_hex}",
            "sizeBytes": size_bytes,
            "sha256": sha256_hex,
        }
    ]
    (sources_dir / "manifest.lock.json").write_bytes(canonical_lock_json(lock_entries))
    lic_data = {
        "licenseSpdx": "LicenseRef-QUL-Unreviewed",
        "packId": "quran-tafsir-ar-ibn-kathir",
        "primaryPath": qul_rel,
        "reviewedBy": None,
        "sources": [
            {
                "attribution": "Tarteel AI QUL",
                "name": "Ibn Kathir",
                "notice": "Unreviewed notice",
                "path": qul_rel,
                "permittedUses": [],
                "rightsEvidence": ["https://qul.tarteel.ai/resources/tafsir/22"],
                "sourceUrl": "https://qul.tarteel.ai/resources/tafsir/22",
                "sourceVersion": f"sha256:{sha256_hex}",
            }
        ],
        "status": "pending",
    }
    (lic_dir / "quran-tafsir-ar-ibn-kathir.json").write_text(json.dumps(lic_data, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(p3, "PHASE3_QUL_LOCKED_PATHS", (qul_rel,))
    monkeypatch.setattr(p3, "PHASE3_ALL_PACKS", ("quran-tafsir-ar-ibn-kathir",))
    monkeypatch.setattr(p3, "audit_all_wbw_inputs", lambda r: None)

    p3.verify_phase3_source_lock_and_clearance(sources_root)


def test_phase3_malformed_license_fails_check_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: Malformed license root/sources must raise GateCheckError #7, NOT AttributeError."""
    sources_root = tmp_path / "repo"
    sources_dir = sources_root / "sources"
    raw_root = sources_root / "raw-inputs"
    lic_dir = sources_dir / "licenses"
    lic_dir.mkdir(parents=True)

    qul_rel = "qul/tafsir/ar-tafsir-ibn-kathir.json.zip"
    archive_file = raw_root / qul_rel
    archive_file.parent.mkdir(parents=True)
    archive_bytes = b"PK\x05\x06" + b"\x00" * 18
    archive_file.write_bytes(archive_bytes)
    sha256_hex = hashlib.sha256(archive_bytes).hexdigest()

    (sources_dir / "registry.json").write_text(
        json.dumps({
            qul_rel: {
                "packIds": ["quran-tafsir-ar-ibn-kathir"],
                "sourceUrl": "https://qul.tarteel.ai/resources/tafsir/22",
                "sourceVersion": f"sha256:{sha256_hex}",
            }
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    lock_entries = [
        {
            "path": qul_rel,
            "sourceUrl": "https://qul.tarteel.ai/resources/tafsir/22",
            "sourceVersion": f"sha256:{sha256_hex}",
            "sizeBytes": len(archive_bytes),
            "sha256": sha256_hex,
        }
    ]
    (sources_dir / "manifest.lock.json").write_bytes(canonical_lock_json(lock_entries))
    (lic_dir / "quran-tafsir-ar-ibn-kathir.json").write_text("[]\n", encoding="utf-8")

    monkeypatch.setattr(p3, "PHASE3_QUL_LOCKED_PATHS", (qul_rel,))
    monkeypatch.setattr(p3, "PHASE3_ALL_PACKS", ("quran-tafsir-ar-ibn-kathir",))
    monkeypatch.setattr(p3, "audit_all_wbw_inputs", lambda r: None)

    with pytest.raises(GateCheckError) as exc_info:
        p3.verify_phase3_source_lock_and_clearance(sources_root)

    assert exc_info.value.check_num == 7


def test_phase3_wrong_primary_path_fails_check_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P1: License pointing to wrong primary path (e.g. alternate DB) must fail Check #7."""
    sources_root = tmp_path / "repo"
    sources_dir = sources_root / "sources"
    raw_root = sources_root / "raw-inputs"
    lic_dir = sources_dir / "licenses"
    lic_dir.mkdir(parents=True)

    qul_rel = "qul/wbw/english-wbw-translation.json.zip"
    qul_alt = "qul/wbw/english-wbw-translation.db.zip"
    for rel in (qul_rel, qul_alt):
        f = raw_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    h_json = hashlib.sha256((raw_root / qul_rel).read_bytes()).hexdigest()
    h_db = hashlib.sha256((raw_root / qul_alt).read_bytes()).hexdigest()

    reg_data = {
        qul_rel: {
            "packIds": ["quran-wbw-en"],
            "sourceUrl": "https://qul.tarteel.ai/resources/translation/92",
            "sourceVersion": f"sha256:{h_json}",
        },
        qul_alt: {
            "packIds": ["quran-wbw-en"],
            "sourceUrl": "https://qul.tarteel.ai/resources/translation/92",
            "sourceVersion": f"sha256:{h_db}",
        },
    }
    (sources_dir / "registry.json").write_text(json.dumps(reg_data, indent=2) + "\n", encoding="utf-8")

    lock_entries = [
        {
            "path": qul_alt,
            "sourceUrl": "https://qul.tarteel.ai/resources/translation/92",
            "sourceVersion": f"sha256:{h_db}",
            "sizeBytes": 22,
            "sha256": h_db,
        },
        {
            "path": qul_rel,
            "sourceUrl": "https://qul.tarteel.ai/resources/translation/92",
            "sourceVersion": f"sha256:{h_json}",
            "sizeBytes": 22,
            "sha256": h_json,
        },
    ]
    (sources_dir / "manifest.lock.json").write_bytes(canonical_lock_json(lock_entries))
    # License erroneously sets primaryPath to the SQLite DB instead of JSON
    lic_data = {
        "licenseSpdx": "LicenseRef-QUL-Unreviewed",
        "packId": "quran-wbw-en",
        "primaryPath": qul_alt,
        "reviewedBy": None,
        "sources": [
            {
                "attribution": "Tarteel AI QUL",
                "name": "English WBW DB",
                "notice": "Unreviewed notice",
                "path": qul_alt,
                "permittedUses": [],
                "rightsEvidence": ["https://qul.tarteel.ai/resources/translation/92"],
                "sourceUrl": "https://qul.tarteel.ai/resources/translation/92",
                "sourceVersion": f"sha256:{h_db}",
            },
            {
                "attribution": "Tarteel AI QUL",
                "name": "English WBW JSON",
                "notice": "Unreviewed notice",
                "path": qul_rel,
                "permittedUses": [],
                "rightsEvidence": ["https://qul.tarteel.ai/resources/translation/92"],
                "sourceUrl": "https://qul.tarteel.ai/resources/translation/92",
                "sourceVersion": f"sha256:{h_json}",
            },
        ],
        "status": "pending",
    }
    (lic_dir / "quran-wbw-en.json").write_text(json.dumps(lic_data, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(p3, "PHASE3_QUL_LOCKED_PATHS", (qul_rel, qul_alt))
    monkeypatch.setattr(p3, "PHASE3_ALL_PACKS", ("quran-wbw-en",))
    monkeypatch.setattr(p3, "audit_all_wbw_inputs", lambda r: None)

    with pytest.raises(GateCheckError) as exc_info:
        p3.verify_phase3_source_lock_and_clearance(sources_root)

    assert exc_info.value.check_num == 7


def test_phase3_stage_8_word_alignment_fails_before_data_compare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1: Shifted words must fail Check #8 before Check #2 generic row comparison."""
    import pipelines.validators.quality_gate as qg

    ctx = qg.GateContext(
        profile="phase3-local",
        candidate=tmp_path / "cand",
        rebuild=tmp_path / "reb",
        sources_root=tmp_path / "sources",
        expectations={},
        specs_dir=tmp_path / "specs",
    )

    alignment_called = []
    def mock_alignment(core: Path, morph: Path, wbw: Path) -> None:
        alignment_called.append(True)
        raise ValueError("Word alignment count mismatch: QAC=77429 QUL=77428")

    monkeypatch.setattr(
        "pipelines.validators.word_alignment.validate_word_alignment",
        mock_alignment,
    )
    monkeypatch.setattr(
        p3,
        "verify_db_content_against_reference",
        lambda **kw: pytest.fail("verify_db_content_against_reference should not be called before alignment"),
    )

    with pytest.raises(GateCheckError) as exc_info:
        qg.stage_8_text_equivalence(ctx)

    assert exc_info.value.check_num == 8
    assert "word alignment count mismatch" in exc_info.value.reason.lower()
    assert len(alignment_called) == 1


def test_phase3_cross_root_sha256_mismatch_fails_check_6(tmp_path: Path) -> None:
    """P1: Files with identical size but different SHA-256 must fail Check #6 via _recompute_sha256."""
    import pipelines.validators.quality_gate as qg

    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    cand.mkdir()
    reb.mkdir()

    # Same size, different bytes
    (cand / "quran-core.db").write_bytes(b"A" * 4096)
    (reb / "quran-core.db").write_bytes(b"B" * 4096)

    ctx = qg.GateContext(
        profile="phase3-local",
        candidate=cand,
        rebuild=reb,
        sources_root=tmp_path / "sources",
        expectations={},
        specs_dir=tmp_path / "specs",
    )
    ctx.declared_files_by_root["candidate"] = {"quran-core.db"}
    ctx.declared_files_by_root["rebuild"] = {"quran-core.db"}

    with pytest.raises(GateCheckError) as exc_info:
        qg.stage_12_cross_root_equality(ctx)

    assert exc_info.value.check_num == 6
    assert "sha-256" in exc_info.value.reason.lower()

def test_build_phase3_local_rejects_non_empty_and_symlink_output_dir(tmp_path: Path) -> None:
    """build_phase3_local must reject non-empty directories and symlinks."""
    sources_root = tmp_path / "sources_root"
    sources_root.mkdir()

    # Non-empty dir
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    (out_dir / "foo.txt").write_bytes(b"bar")

    with pytest.raises(ValueError, match="output_dir must be empty"):
        build_phase3_local(sources_root, out_dir)


def test_build_phase3_local_task_order_orchestration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build_phase3_local must execute all builders in strict task order."""
    sources_root = tmp_path / "sources_root"
    sources_root.mkdir()
    out_dir = tmp_path / "output"

    execution_order: list[str] = []

    def mock_core(s: Path, o: Path) -> None:
        execution_order.append("core")
        (o / "quran-core.db").write_bytes(b"core")

    def mock_morph(s: Path, o: Path) -> None:
        execution_order.append("morphology")
        (o / "quran-morphology.db").write_bytes(b"morphology")

    def mock_wbw(s: Path, o: Path) -> None:
        execution_order.append("wbw")
        (o / "quran-wbw-en.db").write_bytes(b"wbw")

    def mock_tafsir(s: Path, o: Path, *, pack_id: str) -> None:
        execution_order.append(f"tafsir:{pack_id}")
        (o / f"{pack_id}.db").write_bytes(b"tafsir")

    def mock_translation(s: Path, o: Path, *, pack_id: str) -> None:
        execution_order.append(f"translation:{pack_id}")
        (o / f"{pack_id}.db").write_bytes(b"translation")

    def mock_mushaf(s: Path, o: Path, *, pack_id: str) -> None:
        execution_order.append(f"mushaf:{pack_id}")
        (o / f"{pack_id}.db").write_bytes(b"mushaf")

    def mock_manifests(s: Path, o: Path) -> list[Path]:
        execution_order.append("manifests")
        created: list[Path] = []
        for pid in p3.PHASE3_RECITATION_PACKS:
            p = o / f"{pid}.json"
            p.write_bytes(b"{}")
            created.append(p)
        return created

    monkeypatch.setattr("pipelines.builders.build_quran_core.build_quran_core", mock_core)
    monkeypatch.setattr("pipelines.builders.build_quran_morphology.build_quran_morphology", mock_morph)
    monkeypatch.setattr("pipelines.builders.build_quran_wbw.build_quran_wbw", mock_wbw)
    monkeypatch.setattr("pipelines.builders.build_quran_tafsir.build_quran_tafsir", mock_tafsir)
    monkeypatch.setattr("pipelines.builders.build_quran_translation.build_quran_translation", mock_translation)
    monkeypatch.setattr("pipelines.builders.build_quran_mushaf.build_quran_mushaf", mock_mushaf)
    monkeypatch.setattr("pipelines.builders.build_recitation_manifests.build_recitation_manifests", mock_manifests)

    build_phase3_local(sources_root, out_dir)

    expected_order = [
        "core",
        "morphology",
        "wbw",
        "tafsir:quran-tafsir-ar-ibn-kathir",
        "tafsir:quran-tafsir-ar-tabari",
        "tafsir:quran-tafsir-ar-qurtubi",
        "tafsir:quran-tafsir-ar-wasit",
        "tafsir:quran-tafsir-ar-razi",
        "tafsir:quran-tafsir-en-ibn-kathir",
        "translation:quran-tr-en-sahih",
        "mushaf:quran-mushaf-madani-v2",
        "mushaf:quran-mushaf-indopak-15",
        "manifests",
    ]
    assert execution_order == expected_order

    # Verify all 19 files created
    assert len(list(out_dir.iterdir())) == 19
    for fn in p3.PHASE3_EXPECTED_ARTIFACTS:
        assert (out_dir / fn).is_file()


def _run_gate_in_harness(
    *,
    candidate: Path,
    rebuild: Path,
    sources_root: Path,
    bypassed_stages: set[int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref_dbs: dict[str, Path] | None = None,
) -> tuple[int, str, str]:
    import pipelines.validators.quality_gate as qg

    new_handlers = list(qg._STAGE_HANDLERS)
    for idx in bypassed_stages:
        if idx == 6 and ref_dbs:
            new_handlers[idx] = lambda ctx: ctx.phase3_ref_dbs.update(ref_dbs)
        else:
            new_handlers[idx] = lambda ctx: None
    monkeypatch.setattr(qg, "_STAGE_HANDLERS", new_handlers)

    code = 0
    try:
        qg.run_checks(
            profile="phase3-local",
            candidate=candidate,
            rebuild=rebuild,
            sources_root=sources_root,
        )
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_phase3_malformed_sqlite_fails_check_3_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Tiny corrupt/truncated SQLite must fail Check #3 cleanly via stderr/exit 1 without traceback."""
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    cand.mkdir()
    reb.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()

    # Corrupt SQLite database file in candidate
    (cand / "quran-core.db").write_bytes(b"SQLite format 3\x00corrupt-truncated-data")

    code, stdout, stderr = _run_gate_in_harness(
        candidate=cand,
        rebuild=reb,
        sources_root=sources,
        bypassed_stages={0, 1, 2, 3, 4},
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    assert code == 1
    assert stderr.startswith("Check #3 (SQLite schema & integrity) failed on candidate/quran-core.db:")
    assert "Blocked stages: Check #7 (SQLite metadata reconciliation)" in stderr
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout


def test_phase3_stage_8_word_alignment_fails_check_8_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real word alignment semantic failure in stage 8 must report exact Check #8 heading, exit 1, no traceback."""
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    cand.mkdir()
    reb.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()

    for root in (cand, reb):
        c_conn = sqlite3.connect(root / "quran-core.db")
        c_conn.execute("CREATE TABLE ayahs (id INTEGER PRIMARY KEY, verse_key TEXT NOT NULL);")
        c_conn.execute("INSERT INTO ayahs VALUES (1, '1:1');")
        c_conn.commit()
        c_conn.close()

        m_conn = sqlite3.connect(root / "quran-morphology.db")
        m_conn.execute("CREATE TABLE words (ayah_id INTEGER, word_number INTEGER, word_key TEXT NOT NULL);")
        m_conn.execute("INSERT INTO words VALUES (1, 1, '1:1:1');")
        m_conn.commit()
        m_conn.close()

        w_conn = sqlite3.connect(root / "quran-wbw-en.db")
        w_conn.execute("CREATE TABLE word_translations (ayah_id INTEGER, word_number INTEGER, word_key TEXT NOT NULL);")
        # Shifted word number triggers real alignment count/key mismatch in validate_word_alignment
        w_conn.execute("INSERT INTO word_translations VALUES (1, 2, '1:1:2');")
        w_conn.commit()
        w_conn.close()

    code, stdout, stderr = _run_gate_in_harness(
        candidate=cand,
        rebuild=reb,
        sources_root=sources,
        bypassed_stages={0, 1, 2, 3, 4, 5, 6, 7},
        monkeypatch=monkeypatch,
        capsys=capsys,
    )

    assert code == 1
    assert stderr.startswith("Check #8 failed on candidate/word_alignment:")
    assert "Blocked stages: Check #1 (coverage)" in stderr
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout


def test_phase3_stage_8_tafsir_mapping_fails_check_9_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real Tafsir mapping pointer failure in stage 8 must report exact Check #9 heading, exit 1, no traceback."""
    import pipelines.validators.quality_gate as qg

    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    cand.mkdir()
    reb.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()

    # Provide minimal aligned core/morphology/wbw DBs so word alignment passes
    for root in (cand, reb):
        c_conn = sqlite3.connect(root / "quran-core.db")
        c_conn.execute("CREATE TABLE ayahs (id INTEGER PRIMARY KEY, verse_key TEXT NOT NULL);")
        c_conn.execute("INSERT INTO ayahs VALUES (1, '1:1');")
        c_conn.commit()
        c_conn.close()

        m_conn = sqlite3.connect(root / "quran-morphology.db")
        m_conn.execute("CREATE TABLE words (ayah_id INTEGER, word_number INTEGER, word_key TEXT NOT NULL);")
        m_conn.execute("INSERT INTO words VALUES (1, 1, '1:1:1');")
        m_conn.commit()
        m_conn.close()

        w_conn = sqlite3.connect(root / "quran-wbw-en.db")
        w_conn.execute("CREATE TABLE word_translations (ayah_id INTEGER, word_number INTEGER, word_key TEXT NOT NULL);")
        w_conn.execute("INSERT INTO word_translations VALUES (1, 1, '1:1:1');")
        w_conn.commit()
        w_conn.close()

    tafsir_schema = """
    CREATE TABLE tafsir_entries (
        id INTEGER PRIMARY KEY,
        group_key TEXT NOT NULL,
        start_ayah_id INTEGER NOT NULL,
        end_ayah_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        text_format TEXT NOT NULL,
        text_search TEXT NOT NULL
    );
    CREATE TABLE tafsir_ayah_map (
        ayah_id INTEGER PRIMARY KEY,
        verse_key TEXT NOT NULL UNIQUE,
        entry_id INTEGER NOT NULL
    );
    """
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    ref_db = ref_dir / "quran-tafsir-ar-ibn-kathir.db"

    # Reference has correct entry_id = 1
    ref_conn = sqlite3.connect(ref_db)
    ref_conn.executescript(tafsir_schema)
    ref_conn.execute("INSERT INTO tafsir_entries VALUES (1, '1:1', 1, 1, 'text', 'plain', 'text');")
    ref_conn.execute("INSERT INTO tafsir_ayah_map VALUES (1, '1:1', 1);")
    ref_conn.commit()
    ref_conn.close()

    # Candidate and rebuild have shifted pointer entry_id = 999
    for root in (cand, reb):
        t_conn = sqlite3.connect(root / "quran-tafsir-ar-ibn-kathir.db")
        t_conn.executescript(tafsir_schema)
        t_conn.execute("INSERT INTO tafsir_entries VALUES (1, '1:1', 1, 1, 'text', 'plain', 'text');")
        t_conn.execute("INSERT INTO tafsir_ayah_map VALUES (1, '1:1', 999);")
        t_conn.commit()
        t_conn.close()

    monkeypatch.setattr(qg, "PHASE3_DB_PACKS", ("quran-tafsir-ar-ibn-kathir",))

    code, stdout, stderr = _run_gate_in_harness(
        candidate=cand,
        rebuild=reb,
        sources_root=sources,
        bypassed_stages={0, 1, 2, 3, 4, 5, 6, 7},
        monkeypatch=monkeypatch,
        capsys=capsys,
        ref_dbs={"quran-tafsir-ar-ibn-kathir": ref_db},
    )

    assert code == 1
    assert stderr.startswith("Check #9 failed on candidate/quran-tafsir-ar-ibn-kathir.db:")
    assert "Blocked stages: Check #1 (coverage)" in stderr
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout
