"""Unit and synthetic integration tests for Hadith quality gate profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any
import pytest

from pipelines.common.lock_inputs import author_lock
from pipelines.builders.build_hadith_book import build_hadith_book
from pipelines.validators.quality_gate import _HADITH_LOCAL_ORACLE, main, run_checks, GateCheckError

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _mock_verify_runtime_if_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from pipelines.toolchain.check_runtime import verify_runtime
        verify_runtime()
    except Exception:
        monkeypatch.setattr(
            "pipelines.builders.build_hadith_book.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )
        monkeypatch.setattr(
            "pipelines.validators.quality_gate.verify_runtime",
            lambda: (Path("ffmpeg"), Path("ffprobe")),
        )


def _make_hadith_input(
    *,
    book_id: int,
    book_name_ar: str,
    book_name_en: str,
    hadith_id: int,
    ar_full: str,
    en_body: str,
    en_narrator: str = "Narrated Someone:",
) -> dict[str, Any]:
    en_full = f"{en_narrator}\n{en_body}" if en_body else None
    return {
        "books": [
            {
                "id": book_id,
                "number": str(book_id),
                "nameAr": book_name_ar,
                "nameEn": book_name_en,
            }
        ],
        "chapters": [
            {
                "id": 1,
                "bookId": book_id,
                "number": "1",
                "titleAr": "كتاب الإيمان",
                "titleEn": "Book of Faith",
            }
        ],
        "hadiths": [
            {
                "id": hadith_id,
                "bookId": book_id,
                "chapterId": 1,
                "hadithNumber": "1",
                "urn": None,
                "arabicFull": ar_full,
                "englishFull": en_full,
                "englishNarrator": en_narrator,
                "englishBody": en_body,
                "gradeAr": None,
                "gradeEn": None,
                "reference": {"inBook": "Book 1, Hadith 1", "uscMsa": None},
                "textSplitMode": "unsplit_raw",
                "arabicSanad": None,
                "arabicMatn": None,
            }
        ],
    }


def _setup_synthetic_pilot_sources(sources_root: Path) -> None:
    sources_dir = sources_root / "sources"
    raw_root = sources_root / "raw-inputs"
    licenses_dir = sources_dir / "licenses"

    licenses_dir.mkdir(parents=True, exist_ok=True)
    (raw_root / "hadith").mkdir(parents=True, exist_ok=True)

    # 1. Bukhari data matching oracle id=1
    bukhari_data = _make_hadith_input(
        book_id=1,
        book_name_ar="صحيح البخاري",
        book_name_en="Sahih al-Bukhari",
        hadith_id=1,
        ar_full="حَدَّثَنَا الْحُمَيْدِيُّ قَالَ حَدَّثَنَا سُفْيَانُ",
        en_body="I heard the Messenger of Allah say...",
    )
    bukhari_file = raw_root / "hadith" / "bukhari.json"
    bukhari_file.write_text(
        json.dumps(bukhari_data, sort_keys=True, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 2. Muslim data matching oracle id=7278
    muslim_data = _make_hadith_input(
        book_id=2,
        book_name_ar="صحيح مسلم",
        book_name_en="Sahih Muslim",
        hadith_id=7278,
        ar_full="حَدَّثَنِي أَبُو خَيْثَمَةَ زُهَيْرُ بْنُ حَرْبٍ",
        en_body="Should it so happen that you enter upon...",
    )
    muslim_file = raw_root / "hadith" / "muslim.json"
    muslim_file.write_text(
        json.dumps(muslim_data, sort_keys=True, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 3. Registry
    registry = {
        "hadith/bukhari.json": {
            "sourceUrl": "https://example.com/bukhari.json",
            "sourceVersion": "v1.0.0",
            "packIds": ["hadith-bukhari"],
        },
        "hadith/muslim.json": {
            "sourceUrl": "https://example.com/muslim.json",
            "sourceVersion": "v1.0.0",
            "packIds": ["hadith-muslim"],
        },
    }
    reg_file = sources_dir / "registry.json"
    reg_file.write_text(json.dumps(registry, sort_keys=True, indent=2), encoding="utf-8")

    # 4. Lock
    lock_file = sources_dir / "manifest.lock.json"
    author_lock(reg_file, raw_root, lock_file)

    # 5. Licenses
    for book in ("bukhari", "muslim"):
        lic_data = {
            "packId": f"hadith-{book}",
            "licenseSpdx": "LicenseRef-Hadith-Unreviewed",
            "primaryPath": f"hadith/{book}.json",
            "status": "pending",
            "reviewedBy": None,
            "sources": [
                {
                    "path": f"hadith/{book}.json",
                    "sourceUrl": f"https://example.com/{book}.json",
                    "sourceVersion": "v1.0.0",
                    "name": f"Sahih {book.capitalize()}",
                    "notice": "Unreviewed hadith text",
                    "attribution": "Attribution to collector",
                    "permittedUses": [],
                    "rightsEvidence": ["https://example.com/evidence"],
                }
            ],
        }
        lic_file = licenses_dir / f"hadith-{book}.json"
        lic_file.write_text(json.dumps(lic_data, sort_keys=True, indent=2), encoding="utf-8")


def _build_synthetic_pilot_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    sources_root = tmp_path / "sources_root"
    candidate = tmp_path / "candidate"
    rebuild = tmp_path / "rebuild"

    candidate.mkdir(parents=True, exist_ok=True)
    rebuild.mkdir(parents=True, exist_ok=True)

    _setup_synthetic_pilot_sources(sources_root)

    # Monkeypatch oracle counts for Bukhari and Muslim to match single-record synthetic data
    monkeypatch.setitem(
        _HADITH_LOCAL_ORACLE["bukhari"], "expected_counts", (1, 1, 1, 0, 1)
    )
    monkeypatch.setitem(
        _HADITH_LOCAL_ORACLE["muslim"], "expected_counts", (1, 1, 1, 0, 1)
    )

    # Build DBs for candidate and rebuild
    for out in (candidate, rebuild):
        for book in ("bukhari", "muslim"):
            build_hadith_book(book, sources_root, out)

    return sources_root, candidate, rebuild


def test_cli_expectations_rejected(tmp_path: Path) -> None:
    """CLI rejects --expectations for hadith-pilot and hadith-local profiles with exit 2."""
    dummy = tmp_path / "dummy.json"
    dummy.write_text("{}", encoding="utf-8")
    for profile in ("hadith-pilot", "hadith-local"):
        ret = main([
            "--profile", profile,
            "--candidate", str(tmp_path),
            "--rebuild", str(tmp_path),
            "--sources-root", str(tmp_path),
            "--expectations", str(dummy),
        ])
        assert ret == 2
def run_gate_in_process(
    *,
    candidate: Path,
    rebuild: Path,
    sources_root: Path,
    profile: str = "hadith-pilot",
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    try:
        run_checks(
            profile=profile,
            candidate=candidate,
            rebuild=rebuild,
            sources_root=sources_root,
        )
        captured = capsys.readouterr()
        return 0, captured.out, captured.err
    except SystemExit as exc:
        captured = capsys.readouterr()
        code = exc.code if isinstance(exc.code, int) else 1
        return code, captured.out, captured.err


def test_synthetic_hadith_pilot_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Minimal synthetic candidate & rebuild pass all checks with exit 0."""
    sources_root, candidate, rebuild = _build_synthetic_pilot_roots(tmp_path, monkeypatch)
    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 0
    assert "Clearance pending (local verification only): hadith-bukhari, hadith-muslim" in stdout
    assert "Check #6: Passed" in stdout

def test_mutation_check2_changed_stored_source_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Changed stored source text with valid schema triggers Check #2 failure."""
    sources_root, candidate, rebuild = _build_synthetic_pilot_roots(tmp_path, monkeypatch)

    db_path = candidate / "hadith-bukhari.db"
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE hadiths SET arabic_full = 'نص مختلف تماما' WHERE id = 1;")
    conn.commit()
    conn.close()

    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #2" in stderr
    assert "arabic_full mismatch" in stderr

def test_mutation_check2_blob_in_text_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A UTF-8 BLOB in a text field triggers Check #2 failure."""
    sources_root, candidate, rebuild = _build_synthetic_pilot_roots(tmp_path, monkeypatch)

    db_path = candidate / "hadith-bukhari.db"
    conn = sqlite3.connect(db_path)
    # Store BLOB in arabic_full
    conn.execute(
        "UPDATE hadiths SET arabic_full = CAST('حَدَّثَنَا الْحُمَيْدِيُّ قَالَ حَدَّثَنَا سُفْيَانُ' AS BLOB) WHERE id = 1;"
    )
    conn.commit()
    conn.close()

    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #2" in stderr
    assert "storage class" in stderr or "BLOB" in stderr

def test_mutation_check4_corrupt_or_empty_secondary_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty or corrupt secondary index triggers Check #4 failure."""
    sources_root, candidate, rebuild = _build_synthetic_pilot_roots(tmp_path, monkeypatch)

    db_path = candidate / "hadith-bukhari.db"
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM hadiths_full_fts;")
    conn.commit()
    conn.close()

    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #4" in stderr

def test_mutation_check5_swapped_injected_missing_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Extra file, missing file, or dotfile triggers Check #5 failure."""
    sources_root, candidate, rebuild = _build_synthetic_pilot_roots(tmp_path, monkeypatch)

    # 1. Extra file
    extra_file = candidate / "hadith-nasai.db"
    extra_file.write_text("extra", encoding="utf-8")
    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #5" in stderr
    assert "hadith-nasai.db" in stderr
    extra_file.unlink()

    # 2. Missing file
    bukhari_db = candidate / "hadith-bukhari.db"
    bukhari_backup = tmp_path / "bukhari_backup.db"
    shutil.move(bukhari_db, bukhari_backup)
    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #5" in stderr
    assert "Expected asset missing" in stderr
    shutil.move(bukhari_backup, bukhari_db)

    # 3. Dotfile
    dotfile = candidate / ".DS_Store"
    dotfile.write_text("junk", encoding="utf-8")
    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #5" in stderr
    assert "Dotfile not permitted" in stderr
    dotfile.unlink()

def test_mutation_check7_forbidden_license_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status other than 'pending' (e.g. 'rejected') triggers Check #7 failure."""
    sources_root, candidate, rebuild = _build_synthetic_pilot_roots(tmp_path, monkeypatch)

    lic_file = sources_root / "sources" / "licenses" / "hadith-bukhari.json"
    lic_data = json.loads(lic_file.read_text(encoding="utf-8"))
    lic_data["status"] = "rejected"
    lic_file.write_text(json.dumps(lic_data, sort_keys=True, indent=2), encoding="utf-8")

    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #7" in stderr
    assert "rejected" in stderr

def test_mutation_check6_same_root_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """candidate == rebuild triggers Check #6 failure."""
    sources_root, candidate, _ = _build_synthetic_pilot_roots(tmp_path, monkeypatch)

    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=candidate,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #6" in stderr
    assert "same physical directory" in stderr

def test_mutation_check6_db_byte_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A DB byte difference between candidate and rebuild triggers Check #6 failure."""
    sources_root, candidate, rebuild = _build_synthetic_pilot_roots(tmp_path, monkeypatch)

    reb_db = rebuild / "hadith-bukhari.db"
    with open(reb_db, "r+b") as f:
        f.seek(68)  # application_id
        f.write(b"\x01\x02\x03\x04")

    code, stdout, stderr = run_gate_in_process(
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        capsys=capsys,
    )
    assert code == 1
    assert "Check #6" in stderr
    assert "Distributable byte difference" in stderr
