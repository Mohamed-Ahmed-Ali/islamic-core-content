"""Tests for Task 2.3: Hadith sources registry, manifest lock, licenses, and pilot lock verification."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from pipelines.common.lock_inputs import (
    LockEntry,
    parse_and_validate_lock,
    parse_and_validate_registry,
    verify_lock,
)
from pipelines.common.pack_sources import load_sqlite_source_metadata

REPO_ROOT = Path(__file__).resolve().parent.parent

HADITH_SLUGS = [
    "abudawud",
    "ahmad",
    "bukhari",
    "darimi",
    "ibnmajah",
    "malik",
    "muslim",
    "nasai",
    "tirmidhi",
]

PHASE_1_PATHS = [
    "audio/adhan/abdul-basit-abdul-samad-2.mp3",
    "audio/adhan/abdul-basit-abdul-samad.mp3",
    "audio/adhan/abul-ainain-shuaisha.mp3",
    "audio/adhan/mahmoud-khalil-al-hussary.mp3",
    "audio/adhan/mohamed-refaat.mp3",
    "audio/adhan/mustafa-ismail.mp3",
    "corpus/quranic-corpus-morphology-0.4.zip",
    "tanzil/ar.jalalayn.xml",
    "tanzil/en.pickthall.xml",
    "tanzil/quran-data-Tanzil.xml",
    "tanzil/quran-simple-clean.xml",
    "tanzil/quran-simple-min.xml",
    "tanzil/quran-simple-plain.xml",
    "tanzil/quran-simple.xml",
    "tanzil/quran-uthmani-min.xml",
    "tanzil/quran-uthmani.xml",
]


def test_manifest_lock_has_25_entries() -> None:
    lock_path = REPO_ROOT / "sources" / "manifest.lock.json"
    entries = parse_and_validate_lock(lock_path)
    assert len(entries) == 25, f"Expected exactly 25 lock entries, got {len(entries)}"

    entry_paths = [e.path for e in entries]
    assert entry_paths == sorted(entry_paths), "Lock entries must be sorted lexically by path"

    for p in PHASE_1_PATHS:
        assert p in entry_paths, f"Missing Phase 1 path {p} in lock entries"

    for slug in HADITH_SLUGS:
        hadith_path = f"hadith/{slug}.json"
        assert hadith_path in entry_paths, f"Missing hadith path {hadith_path} in lock entries"


def test_malik_lock_entry() -> None:
    lock_path = REPO_ROOT / "sources" / "manifest.lock.json"
    entries = parse_and_validate_lock(lock_path)
    malik_entry = next((e for e in entries if e.path == "hadith/malik.json"), None)
    assert malik_entry is not None, "hadith/malik.json missing from lock"
    assert malik_entry.sha256 == "5dab3037718896293bbcc9454983d13f48e8f0fd428642338e488bf323b46f42"
    assert malik_entry.size_bytes == 5224529
    assert malik_entry.source_version == "local-composite-sha256-61473d46b5a87ca095e4bc300bed3412843844509d09615c99f4d85ab70e0e0c"
    assert malik_entry.source_url == "https://raw.githubusercontent.com/AhmedBaset/hadith-json/70b83d6d21995bb32f8d7271cd75501be5a922a7/db/by_book/the_9_books/malik.json"


def test_registry_has_25_entries_matching_lock() -> None:
    reg_path = REPO_ROOT / "sources" / "registry.json"
    lock_path = REPO_ROOT / "sources" / "manifest.lock.json"
    registry_data = parse_and_validate_registry(reg_path)
    lock_entries = parse_and_validate_lock(lock_path)

    assert len(registry_data) == 25, f"Expected exactly 25 registry entries, got {len(registry_data)}"
    assert sorted(registry_data.keys()) == [e.path for e in lock_entries]

    for slug in HADITH_SLUGS:
        hadith_path = f"hadith/{slug}.json"
        reg_entry = registry_data[hadith_path]
        assert reg_entry["packIds"] == [f"hadith-{slug}"]
        lock_entry = next(e for e in lock_entries if e.path == hadith_path)
        assert reg_entry["sourceUrl"] == lock_entry.source_url
        assert reg_entry["sourceVersion"] == lock_entry.source_version


def test_all_9_hadith_licenses_valid_and_pending() -> None:
    lock_path = REPO_ROOT / "sources" / "manifest.lock.json"
    lock_entries = parse_and_validate_lock(lock_path)

    for slug in HADITH_SLUGS:
        pack_id = f"hadith-{slug}"
        lic_path = REPO_ROOT / "sources" / "licenses" / f"{pack_id}.json"
        assert lic_path.is_file(), f"License file missing: {lic_path}"

        raw_bytes = lic_path.read_bytes()
        data = json.loads(raw_bytes.decode("utf-8"))

        assert data["packId"] == pack_id
        assert data["primaryPath"] == f"hadith/{slug}.json"
        assert data["status"] == "pending"
        assert data["reviewedBy"] is None
        assert isinstance(data["sources"], list)
        assert len(data["sources"]) == 1

        src = data["sources"][0]
        assert src["path"] == f"hadith/{slug}.json"
        assert src["permittedUses"] == []
        assert isinstance(src["rightsEvidence"], list) and len(src["rightsEvidence"]) > 0

        # Load through pack_sources validation
        matching_entries = tuple(e for e in lock_entries if e.path == f"hadith/{slug}.json")
        meta = load_sqlite_source_metadata(
            REPO_ROOT,
            pack_id=pack_id,
            entries=matching_entries,
            primary_path=f"hadith/{slug}.json",
            pack_version="0.2.0",
            completeness="partial" if slug in ("darimi", "ahmad") else "complete",
            fts5_enabled=True,
        )
        assert meta["pack_id"] == pack_id
        assert meta["pack_version"] == "0.2.0"
        assert meta["license_ref"] == f"sources/licenses/{pack_id}.json"


def test_pilot_bukhari_muslim_lock_verification(tmp_path: Path) -> None:
    """Verify that verify_lock succeeds for hadith-bukhari and hadith-muslim on staged pilot inputs."""
    reg_path = REPO_ROOT / "sources" / "registry.json"
    lock_path = REPO_ROOT / "sources" / "manifest.lock.json"

    # Try local repo raw-inputs first, fallback to parent checkout
    raw_candidates = [
        REPO_ROOT / "raw-inputs",
        Path("C:/Users/moham/Downloads/islamic-core-content/raw-inputs"),
    ]
    raw_root = next((r for r in raw_candidates if (r / "hadith" / "bukhari.json").is_file()), None)
    assert raw_root is not None, "Could not find raw-inputs with hadith/bukhari.json"

    bukhari_entries = verify_lock(reg_path, raw_root, lock_path, pack_id="hadith-bukhari")
    assert len(bukhari_entries) == 1
    assert bukhari_entries[0].path == "hadith/bukhari.json"

    muslim_entries = verify_lock(reg_path, raw_root, lock_path, pack_id="hadith-muslim")
    assert len(muslim_entries) == 1
    assert muslim_entries[0].path == "hadith/muslim.json"
