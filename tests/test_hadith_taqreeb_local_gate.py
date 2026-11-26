"""Tests for Taqreeb local quality gate profile (hadith-taqreeb-local)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any
import pytest

from pipelines.common.lock_inputs import (
    LockEntry,
    canonical_lock_json,
    parse_strict_json,
)
from pipelines.common.stage_taqreeb_inputs import TAQREEB_SOURCES
from pipelines.builders.hadith_taqreeb_builder import build_hadith_taqreeb
from pipelines.validators.quality_gate import (
    GateCheckError,
    GateContext,
    main,
    run_checks,
)
from tests.test_hadith_taqreeb import _make_synthetic_taqreeb_json


@pytest.fixture(autouse=True)
def _mock_verify_runtime_if_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from pipelines.toolchain.check_runtime import verify_runtime
        verify_runtime()
    except Exception:
        monkeypatch.setattr(
            "pipelines.builders.hadith_taqreeb_builder.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )
        monkeypatch.setattr(
            "pipelines.validators.quality_gate.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )


def _setup_synthetic_sources_all_packs(sources_root: Path) -> dict[str, dict[str, Any]]:
    sources_dir = sources_root / "sources"
    raw_root = sources_root / "raw-inputs" / "hadith-taqreeb"
    lic_dir = sources_dir / "licenses"
    sources_dir.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    lic_dir.mkdir(parents=True, exist_ok=True)

    docs: dict[str, dict[str, Any]] = {}
    lock_entries: list[LockEntry] = []
    registry_data: dict[str, Any] = {}

    for pack_id, (book_id, basename) in TAQREEB_SOURCES.items():
        doc = _make_synthetic_taqreeb_json()
        doc["book_id"] = book_id
        doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["id"] = 6000 if pack_id == "hadith-taqreeb-jami" else (
            1 if pack_id == "hadith-taqreeb-maalim" else (
                4325 if pack_id == "hadith-taqreeb-wajeez" else 28739
            )
        )
        if pack_id == "hadith-taqreeb-kulliyyah":
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["text_ar"] = "الأَعْمَالُ بالنِّيَّةِ"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["text_ar_simple"] = "الأعمال بالنية"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["footnotes"] = []
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][2]["text_ar"] = "حديث ثالث صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][3]["text_ar"] = "حديث رابع صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][4]["text_ar"] = "حديث خامس صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][4]["footnotes"] = []
        elif pack_id == "hadith-taqreeb-maalim":
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["text_ar"] = "الإِسْلَامُ عَلَىٰ خَمْسٍ"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["text_ar_simple"] = "الإسلام على خمس"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["footnotes"][0]["text"] = " English footnote for translation of first hadith"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][2]["text_ar"] = "حديث ثالث صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][3]["text_ar"] = "حديث رابع صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][4]["text_ar"] = "حديث خامس صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][4]["footnotes"] = []
        elif pack_id == "hadith-taqreeb-wajeez":
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["text_ar"] = "الإِسْلَامُ عَلَىٰ خَمْسٍ {[1]}"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["text_ar_simple"] = "الإسلام على خمس"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][2]["text_ar"] = "حديث ثالث صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][3]["text_ar"] = "حديث رابع صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][4]["text_ar"] = "حديث خامس صحيح"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][4]["footnotes"] = []
        else:
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["text_ar"] = "الإِسْلَامُ عَلَىٰ خَمْسٍ"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["text_ar_simple"] = "الإسلام على خمس"
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][0]["footnotes"] = []
            doc["maqsads"][0]["kutub"][0]["chapters"][1]["hadiths"][4]["footnotes"] = []
        docs[pack_id] = doc
        raw_bytes = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
        raw_file = raw_root / basename
        raw_file.write_bytes(raw_bytes)

        rel_path = f"hadith-taqreeb/{basename}"
        source_url = "https://sunnahenc.com/about"
        source_ver = f"android-apk-3.0.4-realm-export+sha256:{hashlib.sha256(raw_bytes).hexdigest().lower()}"

        registry_data[rel_path] = {
            "packIds": [pack_id],
            "sourceUrl": source_url,
            "sourceVersion": source_ver,
        }

        lock_entries.append(
            LockEntry(
                path=rel_path,
                source_url=source_url,
                source_version=source_ver,
                size_bytes=len(raw_bytes),
                sha256=hashlib.sha256(raw_bytes).hexdigest().lower(),
            )
        )

        lic_data = {
            "packId": pack_id,
            "licenseSpdx": "LicenseRef-Taqreeb-Al-Sunnah-Unreviewed",
            "primaryPath": rel_path,
            "status": "pending",
            "reviewedBy": None,
            "sources": [
                {
                    "path": rel_path,
                    "sourceUrl": source_url,
                    "sourceVersion": source_ver,
                    "name": f"Taqreeb {pack_id}",
                    "notice": "Unreviewed",
                    "attribution": "Attribution",
                    "permittedUses": [],
                    "rightsEvidence": [source_url],
                }
            ],
        }
        (lic_dir / f"{pack_id}.json").write_text(json.dumps(lic_data, indent=2), encoding="utf-8")

    (sources_dir / "registry.json").write_text(json.dumps(registry_data, indent=2), encoding="utf-8")
    (sources_dir / "manifest.lock.json").write_bytes(canonical_lock_json(lock_entries))

    return docs


def _build_candidate_and_rebuild(sources_root: Path, candidate: Path, rebuild: Path) -> None:
    candidate.mkdir(parents=True, exist_ok=True)
    rebuild.mkdir(parents=True, exist_ok=True)
    for out in (candidate, rebuild):
        for pack_id in TAQREEB_SOURCES:
            build_hadith_taqreeb(pack_id, sources_root, out)


def test_cli_profile_supported() -> None:
    """CLI must recognize --profile hadith-taqreeb-local rather than rejecting it."""
    code = main([
        "--profile", "hadith-taqreeb-local",
        "--candidate", "nonexistent_cand",
        "--rebuild", "nonexistent_reb",
        "--sources-root", "nonexistent_src",
    ])
    assert code == 2


def test_cli_expectations_rejected(tmp_path: Path) -> None:
    """CLI rejects --expectations for hadith-taqreeb-local profile."""
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    src = tmp_path / "src"
    cand.mkdir()
    reb.mkdir()
    src.mkdir()
    exp = tmp_path / "exp.json"
    exp.write_text("{}", encoding="utf-8")

    code = main([
        "--profile", "hadith-taqreeb-local",
        "--candidate", str(cand),
        "--rebuild", str(reb),
        "--sources-root", str(src),
        "--expectations", str(exp),
    ])
    assert code == 2


def test_local_review_rejected(tmp_path: Path) -> None:
    """run_checks rejects local_review for hadith-taqreeb-local."""
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    src = tmp_path / "src"
    cand.mkdir()
    reb.mkdir()
    src.mkdir()

    with pytest.raises(ValueError, match="local_review is only permitted for profile 'v0.1.0'"):
        run_checks(
            profile="hadith-taqreeb-local",
            candidate=cand,
            rebuild=reb,
            sources_root=src,
            local_review=True,
        )


def test_root_identity_equal_root_alias(tmp_path: Path) -> None:
    """Equal root alias fails Check #6."""
    from pipelines.validators.hadith_taqreeb_local import run_hadith_taqreeb_local

    cand = tmp_path / "candidate"
    cand.mkdir()
    specs_dir = Path(__file__).resolve().parents[1] / "specs"

    ctx = GateContext(
        profile="hadith-taqreeb-local",
        candidate=cand,
        rebuild=cand,
        sources_root=tmp_path,
        expectations={},
        specs_dir=specs_dir,
    )

    with pytest.raises(GateCheckError) as exc_info:
        run_hadith_taqreeb_local(ctx)
    assert exc_info.value.check_num == 6
    assert "same physical directory" in exc_info.value.reason.lower()


def test_inventory_extra_artifact_fails(tmp_path: Path) -> None:
    """Extra artifact in candidate fails Check #5."""
    from pipelines.validators.hadith_taqreeb_local import run_hadith_taqreeb_local

    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    extra_file = cand / "extra_file.db"
    extra_file.write_bytes(b"bad")

    specs_dir = Path(__file__).resolve().parents[1] / "specs"
    ctx = GateContext(
        profile="hadith-taqreeb-local",
        candidate=cand,
        rebuild=reb,
        sources_root=src,
        expectations={},
        specs_dir=specs_dir,
    )

    with pytest.raises(GateCheckError) as exc_info:
        run_hadith_taqreeb_local(ctx)
    assert exc_info.value.check_num == 5
    assert "unexpected asset in root" in exc_info.value.reason.lower()


def test_source_text_changed_only_in_rebuild_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Source text changed in rebuild fails Check #2 or Check #6."""
    from pipelines.validators.hadith_taqreeb_local import (
        verify_taqreeb_db_content,
        TAQREEB_INVENTORY,
    )

    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    docs = _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    # Modify rebuild hadith text
    reb_db = reb / "hadith-taqreeb-jami.db"
    conn = sqlite3.connect(reb_db)
    conn.execute("UPDATE hadiths SET text_ar = 'نص معدل' WHERE id = 6000;")
    conn.commit()
    conn.close()

    cand_db = cand / "hadith-taqreeb-jami.db"
    jami_doc = docs["hadith-taqreeb-jami"]

    with pytest.raises(GateCheckError) as exc_info:
        verify_taqreeb_db_content((cand_db, reb_db), jami_doc, {})
    assert exc_info.value.check_num == 2


def test_wrong_parent_or_dropped_atraf_fails(tmp_path: Path) -> None:
    """Wrong parent or dropped atraf fails Check #2."""
    from pipelines.validators.hadith_taqreeb_local import verify_taqreeb_db_content

    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    docs = _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    # Drop atraf in candidate
    cand_db = cand / "hadith-taqreeb-jami.db"
    conn = sqlite3.connect(cand_db)
    conn.execute("DELETE FROM hadith_atraf;")
    conn.commit()
    conn.close()

    reb_db = reb / "hadith-taqreeb-jami.db"
    jami_doc = docs["hadith-taqreeb-jami"]

    with pytest.raises(GateCheckError) as exc_info:
        verify_taqreeb_db_content((cand_db, reb_db), jami_doc, {})
    assert exc_info.value.check_num == 2


def test_delete_all_fts_fails(tmp_path: Path) -> None:
    """delete-all on FTS with content still present fails Check #4."""
    from pipelines.validators.hadith_taqreeb_local import verify_taqreeb_fts_smoke

    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    cand_db = cand / "hadith-taqreeb-jami.db"
    conn = sqlite3.connect(cand_db)
    conn.execute("INSERT INTO hadiths_fts(hadiths_fts) VALUES('delete-all');")
    conn.commit()
    conn.close()

    with pytest.raises(GateCheckError) as exc_info:
        verify_taqreeb_fts_smoke(cand_db, "hadith-taqreeb-jami")
    assert exc_info.value.check_num == 4


def test_cross_root_byte_equality_fails(tmp_path: Path) -> None:
    """Byte mismatch between candidate and rebuild fails Check #6."""
    from pipelines.validators.hadith_taqreeb_local import run_hadith_taqreeb_local

    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    # Modify one byte in rebuild
    reb_db = reb / "hadith-taqreeb-kulliyyah.db"
    b = bytearray(reb_db.read_bytes())
    b[100] = (b[100] + 1) % 256
    reb_db.write_bytes(bytes(b))

    specs_dir = Path(__file__).resolve().parents[1] / "specs"
    ctx = GateContext(
        profile="hadith-taqreeb-local",
        candidate=cand,
        rebuild=reb,
        sources_root=src,
        expectations={},
        specs_dir=specs_dir,
    )

    with pytest.raises(GateCheckError) as exc_info:
        run_hadith_taqreeb_local(ctx)
    # Can fail at Check #3 (integrity/schema) or Check #6 (byte equality)
    assert exc_info.value.check_num in (3, 6)

def test_cross_root_sha256_mismatch_fails_check_6(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte digest mismatch between candidate and rebuild fails Check #6."""
    from pipelines.validators.hadith_taqreeb_local import run_hadith_taqreeb_local
    import pipelines.validators.hadith_taqreeb_local as htl

    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    orig_recompute = htl._recompute_sha256
    monkeypatch.setattr(
        htl,
        "_recompute_sha256",
        lambda p: "0000000000000000000000000000000000000000000000000000000000000000" if "reb" in str(p) else orig_recompute(p),
    )

    specs_dir = Path(__file__).resolve().parents[1] / "specs"
    ctx = GateContext(
        profile="hadith-taqreeb-local",
        candidate=cand,
        rebuild=reb,
        sources_root=src,
        expectations={},
        specs_dir=specs_dir,
    )

    with pytest.raises(GateCheckError) as exc_info:
        run_hadith_taqreeb_local(ctx)
    assert exc_info.value.check_num == 6
    assert "byte digest mismatch" in exc_info.value.reason.lower()


def test_license_status_not_pending_fails(tmp_path: Path) -> None:
    """License status not pending fails Check #7."""
    from pipelines.validators.hadith_taqreeb_local import run_hadith_taqreeb_local

    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    lic_path = src / "sources" / "licenses" / "hadith-taqreeb-jami.json"
    lic_data = json.loads(lic_path.read_text(encoding="utf-8"))
    lic_data["status"] = "approved"
    lic_path.write_text(json.dumps(lic_data, indent=2), encoding="utf-8")

    specs_dir = Path(__file__).resolve().parents[1] / "specs"
    ctx = GateContext(
        profile="hadith-taqreeb-local",
        candidate=cand,
        rebuild=reb,
        sources_root=src,
        expectations={},
        specs_dir=specs_dir,
    )

    with pytest.raises(GateCheckError) as exc_info:
        run_hadith_taqreeb_local(ctx)
    assert exc_info.value.check_num == 7
    assert "license status must be 'pending'" in exc_info.value.reason.lower()


def test_end_to_end_synthetic_quality_gate(tmp_path: Path) -> None:
    """Full quality gate run passes with valid synthetic candidate and rebuild."""
    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    run_checks(
        profile="hadith-taqreeb-local",
        candidate=cand,
        rebuild=reb,
        sources_root=src,
    )


def test_cli_main_success(tmp_path: Path) -> None:
    """quality_gate CLI returns 0 on valid synthetic data."""
    src = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    _setup_synthetic_sources_all_packs(src)
    _build_candidate_and_rebuild(src, cand, reb)

    code = main([
        "--profile", "hadith-taqreeb-local",
        "--candidate", str(cand),
        "--rebuild", str(reb),
        "--sources-root", str(src),
    ])
    assert code == 0
