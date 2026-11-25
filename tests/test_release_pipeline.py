"""Tests for release packaging, catalog generation, descriptor validation, and quality gate v0.1.0."""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
import pytest

from pipelines.common.lock_inputs import (
    LockEntry,
    author_lock,
    canonical_entries_json,
    canonical_lock_json,
    parse_and_validate_lock,
    parse_strict_json,
    verify_lock,
)
from pipelines.release.release_packs import (
    MORPHOLOGY_SOURCE_ARCHIVE_MEMBERS,
    acquire_sources,
    assemble_companions,
    load_release_descriptor,
    require_clearance,
    verify_download,
)
from pipelines.release.generate_catalog import generate_catalog
from pipelines.validators.quality_gate import (
    _V010_PATH_PACK_MAP,
    GateCheckError,
    format_duration_seconds,
    run_checks,
    validate_manifest_schema_and_canonical,
    validate_release_sources,
)


def _compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()

def _copy_v010_source_tree(repo_root: Path, sources_dir: Path) -> None:
    shutil_copy = __import__("shutil").copytree
    shutil_copy(repo_root / "sources" / "licenses", sources_dir / "licenses")
    reg_data = json.loads((repo_root / "sources" / "registry.json").read_text(encoding="utf-8"))
    v010_all_packs = {p for packs in _V010_PATH_PACK_MAP.values() for p in packs}
    v010_reg = {}
    for k, v in reg_data.items():
        if k in _V010_PATH_PACK_MAP:
            entry = dict(v)
            if "packIds" in entry:
                entry["packIds"] = [p for p in entry["packIds"] if p in v010_all_packs]
            v010_reg[k] = entry
    (sources_dir / "registry.json").write_text(json.dumps(v010_reg), encoding="utf-8")
    lock_data = json.loads((repo_root / "sources" / "manifest.lock.json").read_text(encoding="utf-8"))
    v010_inputs = [i for i in lock_data["inputs"] if i["path"] in _V010_PATH_PACK_MAP]
    entries = [
        LockEntry(
            path=i["path"],
            source_url=i["sourceUrl"],
            source_version=i["sourceVersion"],
            size_bytes=i["sizeBytes"],
            sha256=i["sha256"],
        )
        for i in v010_inputs
    ]
    (sources_dir / "manifest.lock.json").write_bytes(canonical_lock_json(entries))


def test_release_descriptor_validation(tmp_path: Path) -> None:
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True)
    desc_path = sources_dir / "release-v0.1.0.json"

    # Minimal valid descriptor
    valid_desc = {
        "repository": "Mohamed-Ahmed-Ali/islamic-core-content",
        "generatedAt": "2026-11-23T13:31:00Z",
        "bundlePaths": ["corpus/quranic-corpus-morphology-0.4.zip"],
        "companions": [
            {
                "filename": "quran-morphology-source.zip",
                "sizeBytes": 1339290,
                "sha256": "25cfa31be6062e651c9df9eb1e49e729f3d104403fcf3202b034d15686b3454c",
                "sourcePath": None,
            }
        ],
    }

    # Also create a minimal lock file with the bundle path
    raw_root = tmp_path / "raw-inputs"
    raw_root.mkdir(parents=True)
    corpus_dir = raw_root / "corpus"
    corpus_dir.mkdir(parents=True)
    zip_bytes = b"dummy zip"
    (corpus_dir / "quranic-corpus-morphology-0.4.zip").write_bytes(zip_bytes)
    reg = {
        "corpus/quranic-corpus-morphology-0.4.zip": {
            "packIds": ["quran-morphology"],
            "sourceUrl": "https://corpus.quran.com/download/",
            "sourceVersion": "0.4",
        }
    }
    (sources_dir / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    author_lock(sources_dir / "registry.json", raw_root, sources_dir / "manifest.lock.json")

    desc_path.write_text(json.dumps(valid_desc, indent=2), encoding="utf-8")
    loaded = load_release_descriptor(tmp_path)
    assert loaded["repository"] == "Mohamed-Ahmed-Ali/islamic-core-content"
    assert loaded["bundlePaths"] == ["corpus/quranic-corpus-morphology-0.4.zip"]
    assert len(loaded["companions"]) == 1

    # Extra unexpected key rejected
    bad_desc = dict(valid_desc)
    bad_desc["extraKey"] = "not-allowed"
    desc_path.write_text(json.dumps(bad_desc), encoding="utf-8")
    with pytest.raises(Exception, match="extraKey|unexpected|schema"):
        load_release_descriptor(tmp_path)

    # Invalid repo format rejected
    bad_desc = dict(valid_desc)
    bad_desc["repository"] = "invalid_repo"
    desc_path.write_text(json.dumps(bad_desc), encoding="utf-8")
    with pytest.raises(Exception, match="repository|owner/repo"):
        load_release_descriptor(tmp_path)

    # Unsorted bundlePaths rejected
    bad_desc = dict(valid_desc)
    bad_desc["bundlePaths"] = ["b/path", "a/path"]
    desc_path.write_text(json.dumps(bad_desc), encoding="utf-8")
    with pytest.raises(Exception, match="sorted|bundlePaths"):
        load_release_descriptor(tmp_path)

    # Companion safe filename check: path traversal rejected
    bad_desc = dict(valid_desc)
    bad_desc["companions"] = [
        {
            "filename": "quran-morphology-source.zip",
            "sizeBytes": 1339290,
            "sha256": "25cfa31be6062e651c9df9eb1e49e729f3d104403fcf3202b034d15686b3454c",
            "sourcePath": None,
        },
        {
            "filename": "../secret.zip",
            "sizeBytes": 100,
            "sha256": "a" * 64,
            "sourcePath": "sources/licenses/evidence/quran-morphology/GPL-3.0.txt",
        },
    ]
    desc_path.write_text(json.dumps(bad_desc), encoding="utf-8")
    with pytest.raises(Exception, match="filename|safe|basename|traversal"):
        load_release_descriptor(tmp_path)


def test_acquire_bundle_and_transport_validation_no_network(tmp_path: Path) -> None:
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True)
    raw_root = tmp_path / "raw-inputs"

    # Setup locked files: 1 bundle file and 1 normal file
    bundle_path = "corpus/quranic-corpus-morphology-0.4.zip"
    normal_path = "tanzil/quran-data-Tanzil.xml"

    # Create dummy files for locking
    stage_tmp = tmp_path / "stage_tmp"
    (stage_tmp / "corpus").mkdir(parents=True)
    (stage_tmp / "tanzil").mkdir(parents=True)
    dummy_bundle_member_bytes = b"morphology data"
    (stage_tmp / bundle_path).write_bytes(dummy_bundle_member_bytes)
    dummy_xml_bytes = b"<quran>test</quran>"
    (stage_tmp / normal_path).write_bytes(dummy_xml_bytes)

    reg = {
        bundle_path: {
            "packIds": ["quran-morphology"],
            "sourceUrl": "https://corpus.quran.com/download/",
            "sourceVersion": "0.4",
        },
        normal_path: {
            "packIds": ["quran-core"],
            "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml",
            "sourceVersion": "1.0",
        },
    }
    (sources_dir / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    author_lock(sources_dir / "registry.json", stage_tmp, sources_dir / "manifest.lock.json")

    # Descriptor declaring bundle
    desc = {
        "repository": "Mohamed-Ahmed-Ali/islamic-core-content",
        "generatedAt": "2026-11-23T13:31:00Z",
        "bundlePaths": [bundle_path],
        "companions": [
            {
                "filename": "quran-morphology-source.zip",
                "sizeBytes": 1339290,
                "sha256": "25cfa31be6062e651c9df9eb1e49e729f3d104403fcf3202b034d15686b3454c",
                "sourcePath": None,
            }
        ],
    }
    (sources_dir / "release-v0.1.0.json").write_text(json.dumps(desc), encoding="utf-8")

    # Helper to author a test bundle ZIP
    def make_bundle_zip(members: dict[str, bytes]) -> Path:
        b_zip = tmp_path / "test_bundle.zip"
        if b_zip.exists():
            b_zip.unlink()
        with zipfile.ZipFile(b_zip, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        return b_zip

    # Test A: Bundle missing expected member -> rejected
    bad_bundle = make_bundle_zip({"wrong/member.txt": b"wrong"})
    with pytest.raises(Exception, match="member|missing|bundle"):
        acquire_sources(
            sources_root=tmp_path,
            bundle_archive=bad_bundle,
            allow_network=False,
        )
    assert not raw_root.exists() or len(list(raw_root.iterdir())) == 0

    # Test B: Bundle containing unlisted member -> rejected
    bad_bundle = make_bundle_zip({
        bundle_path: dummy_bundle_member_bytes,
        "unlisted/member.txt": b"sneak",
    })
    with pytest.raises(Exception, match="unlisted|unexpected|member"):
        acquire_sources(
            sources_root=tmp_path,
            bundle_archive=bad_bundle,
            allow_network=False,
        )
    assert not raw_root.exists() or len(list(raw_root.iterdir())) == 0

    # Test C: Bundle with wrong member hash -> rejected
    bad_bundle = make_bundle_zip({
        bundle_path: b"corrupted bytes",
    })
    with pytest.raises(Exception, match="hash|digest|sha256"):
        acquire_sources(
            sources_root=tmp_path,
            bundle_archive=bad_bundle,
            allow_network=False,
        )
    assert not raw_root.exists() or len(list(raw_root.iterdir())) == 0

    # Test D: Bundle with path traversal -> rejected
    b_zip = tmp_path / "traversal_bundle.zip"
    with zipfile.ZipFile(b_zip, "w") as zf:
        zf.writestr("../traversal.txt", b"bad")
    with pytest.raises(Exception, match="traversal|safe|relative"):
        acquire_sources(
            sources_root=tmp_path,
            bundle_archive=b_zip,
            allow_network=False,
        )
    assert not raw_root.exists() or len(list(raw_root.iterdir())) == 0


def test_assemble_companions_deterministic_and_whitelist(tmp_path: Path) -> None:
    sources_root = tmp_path / "source_tree"
    sources_root.mkdir(parents=True)
    asset_root = tmp_path / "dist"
    asset_root.mkdir(parents=True)

    # Populate dummy files for all 25 whitelisted members
    for rel_path in MORPHOLOGY_SOURCE_ARCHIVE_MEMBERS:
        p = sources_root / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"content of {rel_path}".encode("utf-8"))

    reg = {
        "corpus/quranic-corpus-morphology-0.4.zip": {"packIds": ["quran-morphology"], "sourceUrl": "https://corpus.quran.com/download/", "sourceVersion": "0.4"},
        "tanzil/quran-data-Tanzil.xml": {"packIds": ["quran-core"], "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml", "sourceVersion": "1.0"},
    }
    (sources_root / "sources" / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    author_lock(sources_root / "sources" / "registry.json", sources_root / "raw-inputs", sources_root / "sources" / "manifest.lock.json")

    # Also add an unlisted file to sources_root to prove whitelist filtering
    unlisted = sources_root / "pipelines" / "unlisted_script.py"
    unlisted.write_bytes(b"print('unlisted')")

    # Assemble companions
    companions = assemble_companions(sources_root, asset_root, notice_paths=())
    assert len(companions) == 1
    c = companions[0]
    assert c["filename"] == "quran-morphology-source.zip"
    assert c["sourcePath"] is None
    assert c["sizeBytes"] > 0
    assert len(c["sha256"]) == 64

    # Inspect the generated zip
    zip_path = asset_root / "quran-morphology-source.zip"
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        # Must match exactly the 25 members in sorted order
        assert namelist == sorted(MORPHOLOGY_SOURCE_ARCHIVE_MEMBERS)
        assert "pipelines/unlisted_script.py" not in namelist
        # Verify ZIP_STORED, DOS epoch, and 0100644 mode
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert (info.external_attr >> 16) == 0o100644

    # Refuse existing output
    with pytest.raises((FileExistsError, RuntimeError), match="exists|refuse"):
        assemble_companions(sources_root, asset_root, notice_paths=())


def test_independently_named_aliased_inode_fails_check6(tmp_path: Path) -> None:
    # Set up candidate and rebuild roots with a dummy file hardlinked between them
    cand = tmp_path / "candidate"
    reb = tmp_path / "rebuild"
    cand.mkdir()
    reb.mkdir()

    f1 = cand / "asset.db"
    f1.write_bytes(b"same content")
    f2 = reb / "asset.db"
    try:
        os.link(f1, f2)
    except OSError:
        pytest.skip("Hardlinks not supported on this filesystem")

    assert os.path.samefile(f1, f2)

    # Calling stage 12 directly or via quality gate check #6 must detect the inode alias
    from pipelines.validators.quality_gate import GateContext, stage_12_cross_root_equality
    ctx = GateContext(
        profile="v0.1.0",
        candidate=cand,
        rebuild=reb,
        sources_root=tmp_path,
        expectations={},
        specs_dir=Path(__file__).resolve().parent.parent / "specs",
    )
    ctx.declared_files_by_root["candidate"] = {"asset.db"}
    with pytest.raises(GateCheckError) as exc_info:
        stage_12_cross_root_equality(ctx)
    assert exc_info.value.check_num == 6
    assert "inode" in exc_info.value.reason.lower() or "alias" in exc_info.value.reason.lower()


def test_fixture_license_rejected_in_v0_1_0_both_strict_and_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipelines.validators.quality_gate as qg
    monkeypatch.setattr(qg, "verify_lock", lambda r, rw, l: None)

    repo_root = Path(__file__).resolve().parent.parent
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True)
    raw_root = tmp_path / "raw-inputs"
    raw_root.mkdir(parents=True)

    # Copy the Phase 1 v0.1.0 registry, lock, and license files
    _copy_v010_source_tree(repo_root, sources_dir)

    # Mutate audio-adhan-core.json status to "fixture"
    lic_path = sources_dir / "licenses" / "audio-adhan-core.json"
    lic_data = json.loads(lic_path.read_text(encoding="utf-8"))
    lic_data["status"] = "fixture"
    lic_path.write_text(json.dumps(lic_data), encoding="utf-8")

    # Strict mode must reject fixture
    with pytest.raises(GateCheckError) as exc_info:
        validate_release_sources(tmp_path, local_review=False)
    assert exc_info.value.check_num == 7
    assert "fixture" in exc_info.value.reason.lower() or "not permitted" in exc_info.value.reason.lower()

    # Local review mode must ALSO reject fixture
    with pytest.raises(GateCheckError) as exc_info:
        validate_release_sources(tmp_path, local_review=True)
    assert exc_info.value.check_num == 7
    assert "fixture" in exc_info.value.reason.lower() or "not permitted" in exc_info.value.reason.lower()

def test_pending_record_strict_check7_fails_but_local_review_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipelines.validators.quality_gate as qg
    monkeypatch.setattr(qg, "verify_lock", lambda r, rw, l: None)

    repo_root = Path(__file__).resolve().parent.parent
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True)
    raw_root = tmp_path / "raw-inputs"
    raw_root.mkdir(parents=True)

    # Copy the Phase 1 v0.1.0 registry, lock, and license files
    _copy_v010_source_tree(repo_root, sources_dir)
    reg_data = json.loads((sources_dir / "registry.json").read_text(encoding="utf-8"))
    for p in reg_data.keys():
        target = raw_root / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"dummy")
    # Strict mode must fail check #7 because records are pending
    with pytest.raises(GateCheckError) as exc_info:
        validate_release_sources(tmp_path, local_review=False)
    assert exc_info.value.check_num == 7
    assert "pending" in exc_info.value.reason.lower()

    # Local review mode accepts pending records and verifies their structure and evidence paths
    entries, reg, lic_records = validate_release_sources(tmp_path, local_review=True)
    assert len(lic_records) == 6
    for pid, lic in lic_records.items():
        assert lic["status"] == "pending"


def test_companion_mutation_and_inventory_fails_check5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipelines.validators.quality_gate import (
        GateContext,
        stage_3_manifest_schemas_inventory,
        _V010_PACK_IDS,
        _AUDIO_SLUGS,
    )
    import pipelines.validators.quality_gate as qg
    monkeypatch.setattr(qg, "probe_asset", lambda p, variant="ios": ("adpcm_ima_qt", Fraction(10, 1)))

    specs_dir = Path(__file__).resolve().parent.parent / "specs"
    cand = tmp_path / "candidate"
    reb = tmp_path / "rebuild"
    cand.mkdir()
    reb.mkdir()
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    repo_root = Path(__file__).resolve().parent.parent
    lock_bytes = (repo_root / "sources" / "manifest.lock.json").read_bytes().replace(b"\r\n", b"\n")
    (sources_dir / "manifest.lock.json").write_bytes(lock_bytes)
    desc = {
        "repository": "Mohamed-Ahmed-Ali/islamic-core-content",
        "generatedAt": "2026-11-23T13:31:00Z",
        "bundlePaths": [],
        "companions": [
            {
                "filename": "quran-morphology-source.zip",
                "sizeBytes": 10,
                "sha256": _compute_sha256(b"0123456789"),
                "sourcePath": None,
            }
        ],
    }
    (sources_dir / "release-v0.1.0.json").write_text(json.dumps(desc), encoding="utf-8")

    packs: list[dict[str, Any]] = []
    for pid in sorted(_V010_PACK_IDS):
        if pid == "audio-adhan-core":
            packs.append({
                "id": pid,
                "category": "audio",
                "version": "0.1.0",
                "format": "audio",
                "languages": ["ar"],
                "completeness": "complete",
                "licenseRef": f"sources/licenses/{pid}.json",
                "required": False,
                "audioManifestRef": "audio-manifest.json",
            })
        else:
            packs.append({
                "id": pid,
                "category": "quran",
                "version": "0.1.0",
                "format": "sqlite",
                "languages": ["en"] if pid == "quran-tr-en-pickthall" else ["ar"],
                "completeness": "complete",
                "licenseRef": f"sources/licenses/{pid}.json",
                "required": (pid == "quran-core"),
                "compression": "none",
                "schemaVersion": 1,
                "url": f"https://github.com/Mohamed-Ahmed-Ali/islamic-core-content/releases/download/content-v0.1.0/{pid}.db",
                "sizeBytes": 100,
                "sha256": _compute_sha256(b"\0" * 100),
                "coverage": {"expected": 6236, "available": 6236},
                "requiresFts5": pid in ("quran-core", "quran-tr-en-pickthall", "quran-tafsir-ar-jalalayn"),
            })

    tracks: list[dict[str, Any]] = []
    for slug in sorted(_AUDIO_SLUGS):
        full_fn = f"adhan-{slug}-full.m4a"
        ios_fn = f"adhan-{slug}-ios-notif.caf"
        tracks.append({
            "slug": slug,
            "reciter": "Reciter Name",
            "full": {
                "filename": full_fn,
                "url": f"https://github.com/Mohamed-Ahmed-Ali/islamic-core-content/releases/download/content-v0.1.0/{full_fn}",
                "sizeBytes": 100,
                "sha256": _compute_sha256(b"\0" * 100),
                "codec": "aac",
                "durationSeconds": 10.0,
            },
            "ios": {
                "filename": ios_fn,
                "url": f"https://github.com/Mohamed-Ahmed-Ali/islamic-core-content/releases/download/content-v0.1.0/{ios_fn}",
                "sizeBytes": 100,
                "sha256": _compute_sha256(b"\0" * 100),
                "codec": "adpcm_ima_qt",
                "durationSeconds": 10.0,
            },
        })

    cat_data = {
        "catalogVersion": 1,
        "generatedAt": "2026-11-23T13:31:00Z",
        "releaseTag": "content-v0.1.0",
        "packs": packs,
    }
    audio_data = {
        "audioManifestVersion": 1,
        "releaseTag": "content-v0.1.0",
        "packId": "audio-adhan-core",
        "tracks": tracks,
    }

    cat_bytes = json.dumps(cat_data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    audio_bytes = json.dumps(audio_data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"

    for root in (cand, reb):
        (root / "catalog.json").write_bytes(cat_bytes)
        (root / "audio-manifest.json").write_bytes(audio_bytes)
        for p in packs:
            if p["format"] == "sqlite":
                (root / f"{p['id']}.db").write_bytes(b"\0" * 100)
        for t in tracks:
            (root / t["full"]["filename"]).write_bytes(b"\0" * 100)
            (root / t["ios"]["filename"]).write_bytes(b"\0" * 100)
        (root / "quran-morphology-source.zip").write_bytes(b"badcontent")

    ctx = GateContext(
        profile="v0.1.0",
        candidate=cand,
        rebuild=reb,
        sources_root=tmp_path,
        expectations={},
        specs_dir=specs_dir,
    )
    ctx.raw_manifest_bytes["candidate"] = {"catalog.json": cat_bytes, "audio-manifest.json": audio_bytes}
    ctx.raw_manifest_bytes["rebuild"] = {"catalog.json": cat_bytes, "audio-manifest.json": audio_bytes}
    ctx.manifest_data_by_root["candidate"] = (cat_data, audio_data)
    ctx.manifest_data_by_root["rebuild"] = (cat_data, audio_data)

    # Hash mismatch on companion must fail check #5
    with pytest.raises(GateCheckError) as exc_info:
        stage_3_manifest_schemas_inventory(ctx)
    assert exc_info.value.check_num == 5
    assert "companion sha256 mismatch" in exc_info.value.reason.lower()

    # Now make companion valid, but add unmanifested file
    for root in (cand, reb):
        (root / "quran-morphology-source.zip").write_bytes(b"0123456789")
    (cand / "extra_unmanifested.txt").write_bytes(b"surprise")
    with pytest.raises(GateCheckError) as exc_info:
        stage_3_manifest_schemas_inventory(ctx)
    assert exc_info.value.check_num == 5
    assert "unexpected" in exc_info.value.reason.lower() or "unmanifested" in exc_info.value.reason.lower()


def test_verify_download_offline_consumer_proof(tmp_path: Path) -> None:
    expected_root = tmp_path / "expected"
    download_root = tmp_path / "download"
    expected_root.mkdir()
    download_root.mkdir()

    # Create minimal quran-core.db with user_version=1 and ayahs_fts rowid 2
    db_path = expected_root / "quran-core.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 1;")
    conn.execute("CREATE TABLE ayahs (id INTEGER PRIMARY KEY, verse_key TEXT, text_search TEXT);")
    conn.execute("CREATE VIRTUAL TABLE ayahs_fts USING fts5(text_search, content='ayahs', content_rowid='id');")
    conn.execute("INSERT INTO ayahs(id, verse_key, text_search) VALUES (1, '1:1', 'بسم الله الرحمن الرحيم');")
    conn.execute("INSERT INTO ayahs(id, verse_key, text_search) VALUES (2, '1:2', 'الحمد لله رب العالمين');")
    conn.execute("INSERT INTO ayahs_fts(ayahs_fts) VALUES ('rebuild');")
    conn.commit()
    conn.close()

    # Copy identical file to download_root
    (download_root / "quran-core.db").write_bytes(db_path.read_bytes())

    # verify_download succeeds
    verify_download(expected_root=expected_root, asset_root=download_root)

    # Mutating single byte in download_root causes failure
    corrupted = bytearray(db_path.read_bytes())
    corrupted[100] ^= 0xFF
    (download_root / "quran-core.db").write_bytes(bytes(corrupted))
    with pytest.raises(Exception, match="hash|mismatch|sha256|byte"):
        verify_download(expected_root=expected_root, asset_root=download_root)


def test_rehashed_sqlite_mutation_reaches_semantic_check_not_check5(tmp_path: Path) -> None:
    from tests.fixtures.tanzil import build_synthetic_metadata_xml, build_synthetic_scripture_xml
    from pipelines.builders.build_quran_core import build_quran_core
    from pipelines.validators.quality_gate import GateContext, stage_3_manifest_schemas_inventory, stage_8_text_equivalence
    specs_dir = Path(__file__).resolve().parent.parent / "specs"

    sources_root = tmp_path / "src"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    raw_root = sources_root / "raw-inputs"
    (raw_root / "tanzil").mkdir(parents=True)
    (sources_root / "sources" / "licenses").mkdir(parents=True)
    shutil_copy_tree = __import__("shutil").copytree
    shutil_copy_tree(
        Path(__file__).resolve().parent.parent / "sources" / "licenses" / "evidence",
        sources_root / "sources" / "licenses" / "evidence",
    )
    cand.mkdir(parents=True)
    reb.mkdir(parents=True)

    # Create synthetic tanzil files
    meta_xml = build_synthetic_metadata_xml()
    uthmani_xml = build_synthetic_scripture_xml("Uthmani")
    clean_xml = build_synthetic_scripture_xml("Simple Clean")
    (raw_root / "tanzil" / "quran-data-Tanzil.xml").write_text(meta_xml, encoding="utf-8")
    (raw_root / "tanzil" / "quran-uthmani.xml").write_text(uthmani_xml, encoding="utf-8")
    (raw_root / "tanzil" / "quran-simple-clean.xml").write_text(clean_xml, encoding="utf-8")

    repo_root = Path(__file__).resolve().parent.parent
    full_reg = json.loads((repo_root / "sources" / "registry.json").read_text(encoding="utf-8"))
    reg = {
        k: full_reg[k]
        for k in (
            "tanzil/quran-data-Tanzil.xml",
            "tanzil/quran-uthmani.xml",
            "tanzil/quran-simple-clean.xml",
        )
    }
    (sources_root / "sources" / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    author_lock(sources_root / "sources" / "registry.json", raw_root, sources_root / "sources" / "manifest.lock.json")

    # Copy quran-core license
    shutil_copy = __import__("shutil").copyfile
    shutil_copy(
        Path(__file__).resolve().parent.parent / "sources" / "licenses" / "quran-core.json",
        sources_root / "sources" / "licenses" / "quran-core.json",
    )

    # Build valid candidate
    build_quran_core(sources_root, cand)
    shutil_copy(cand / "quran-core.db", reb / "quran-core.db")

    # Mutate text in cand DB
    conn = sqlite3.connect(cand / "quran-core.db")
    conn.execute("UPDATE ayahs SET text_simple_clean = 'متحور فاسد' WHERE id = 1;")
    conn.commit()
    conn.close()

    # Rehash cand DB so check 5 sees matching sha256
    mutated_hash = _compute_sha256((cand / "quran-core.db").read_bytes())
    mutated_size = (cand / "quran-core.db").stat().st_size

    cat_data = {
        "catalogVersion": 1,
        "generatedAt": "2026-11-23T13:31:00Z",
        "releaseTag": "content-v0.1.0",
        "packs": [
            {
                "id": "quran-core",
                "category": "quran",
                "version": "0.1.0",
                "format": "sqlite",
                "languages": ["ar"],
                "completeness": "complete",
                "licenseRef": "sources/licenses/quran-core.json",
                "required": True,
                "compression": "none",
                "schemaVersion": 1,
                "url": "https://github.com/Mohamed-Ahmed-Ali/islamic-core-content/releases/download/content-v0.1.0/quran-core.db",
                "sizeBytes": mutated_size,
                "sha256": mutated_hash,
                "coverage": {"expected": 6236, "available": 6236},
                "requiresFts5": True,
            }
        ],
    }
    audio_data = {
        "audioManifestVersion": 1,
        "releaseTag": "content-v0.1.0",
        "packId": "audio-adhan-core",
        "tracks": [],
    }
    desc = {
        "repository": "Mohamed-Ahmed-Ali/islamic-core-content",
        "generatedAt": "2026-11-23T13:31:00Z",
        "bundlePaths": [],
        "companions": [
            {
                "filename": "quran-morphology-source.zip",
                "sizeBytes": 10,
                "sha256": _compute_sha256(b"0123456789"),
                "sourcePath": None,
            }
        ],
    }
    (cand / "quran-morphology-source.zip").write_bytes(b"0123456789")
    (reb / "quran-morphology-source.zip").write_bytes(b"0123456789")
    (sources_root / "sources" / "release-v0.1.0.json").write_text(json.dumps(desc), encoding="utf-8")
    cat_bytes = json.dumps(cat_data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    audio_bytes = json.dumps(audio_data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"

    (cand / "catalog.json").write_bytes(cat_bytes)
    (cand / "audio-manifest.json").write_bytes(audio_bytes)
    (reb / "catalog.json").write_bytes(cat_bytes)
    (reb / "audio-manifest.json").write_bytes(audio_bytes)

    ctx = GateContext(
        profile="v0.1.0",
        candidate=cand,
        rebuild=reb,
        sources_root=sources_root,
        expectations={},
        specs_dir=specs_dir,
    )
    ctx.raw_manifest_bytes["candidate"] = {"catalog.json": cat_bytes, "audio-manifest.json": audio_bytes}
    ctx.raw_manifest_bytes["rebuild"] = {"catalog.json": cat_bytes, "audio-manifest.json": audio_bytes}
    ctx.manifest_data_by_root["candidate"] = (cat_data, audio_data)
    ctx.manifest_data_by_root["rebuild"] = (cat_data, audio_data)


    # Stage 8 (check #2, text equivalence) MUST detect the mutation against locked Tanzil source!
    with pytest.raises(GateCheckError) as exc_info:
        stage_8_text_equivalence(ctx)
    assert exc_info.value.check_num == 2
    assert "mismatch" in exc_info.value.reason.lower() or "text" in exc_info.value.reason.lower()


def test_validate_release_sources_rejects_malformed_records(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    sources_root = tmp_path / "src"
    sources_dir = sources_root / "sources"
    licenses_dir = sources_dir / "licenses"
    licenses_dir.mkdir(parents=True)
    (sources_root / "raw-inputs").mkdir(parents=True)

    # Copy registry, lock, and valid licenses
    import shutil
    shutil.copyfile(repo_root / "sources" / "registry.json", sources_dir / "registry.json")
    shutil.copyfile(repo_root / "sources" / "manifest.lock.json", sources_dir / "manifest.lock.json")
    for f in (repo_root / "sources" / "licenses").glob("*.json"):
        shutil.copyfile(f, licenses_dir / f.name)
    shutil.copytree(repo_root / "sources" / "licenses" / "evidence", licenses_dir / "evidence")

    # Create dummy raw-inputs matching lock
    full_lock = json.loads((repo_root / "sources" / "manifest.lock.json").read_text(encoding="utf-8"))
    for entry in full_lock["inputs"]:
        p = entry["path"]
        target = sources_root / "raw-inputs" / p
        target.parent.mkdir(parents=True, exist_ok=True)
        real_src = repo_root / "raw-inputs" / p
        if real_src.is_file():
            shutil.copyfile(real_src, target)
        else:
            target.write_bytes(b"\x00" * entry["sizeBytes"])
    author_lock(sources_dir / "registry.json", sources_root / "raw-inputs", sources_dir / "manifest.lock.json")
    adhan_lic_path = licenses_dir / "audio-adhan-core.json"
    bad_adhan = json.loads(adhan_lic_path.read_text(encoding="utf-8"))
    bad_adhan["licenseSpdx"] = []
    bad_adhan["sources"][0]["reciter"] = {"not": "a string"}
    bad_adhan["sources"][0]["slug"] = "wrong-recording"
    bad_adhan["sources"][0]["name"] = None
    bad_adhan["sources"][0]["permittedUses"] = ["public_redistribution", "unknown", "unknown"]
    adhan_lic_path.write_text(json.dumps(bad_adhan), encoding="utf-8")

    with pytest.raises(GateCheckError) as exc_info:
        validate_release_sources(sources_root, local_review=True)
    assert exc_info.value.check_num == 7


def test_stage3_inventory_detects_dotfiles_and_directories(tmp_path: Path) -> None:
    from pipelines.validators.quality_gate import GateContext, stage_3_manifest_schemas_inventory
    specs_dir = Path(__file__).resolve().parent.parent / "specs"
    repo_root = Path(__file__).resolve().parent.parent
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    cand.mkdir(parents=True)
    reb.mkdir(parents=True)
    # Create valid minimal manifests and descriptor
    desc = json.loads((repo_root / "sources" / "release-v0.1.0.json").read_text(encoding="utf-8"))
    sources_root = tmp_path / "src"
    (sources_root / "sources").mkdir(parents=True)
    (sources_root / "sources" / "release-v0.1.0.json").write_text(json.dumps(desc), encoding="utf-8")
    (sources_root / "sources" / "manifest.lock.json").write_bytes((repo_root / "sources" / "manifest.lock.json").read_bytes().replace(b"\r\n", b"\n"))

    cat_data = json.loads((repo_root / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    audio_data = json.loads((repo_root / "catalog" / "audio-manifest.json").read_text(encoding="utf-8"))
    cat_bytes = (repo_root / "catalog" / "catalog.json").read_bytes().replace(b"\r\n", b"\n")
    audio_bytes = (repo_root / "catalog" / "audio-manifest.json").read_bytes().replace(b"\r\n", b"\n")

    (cand / "catalog.json").write_bytes(cat_bytes)
    (cand / "audio-manifest.json").write_bytes(audio_bytes)
    (reb / "catalog.json").write_bytes(cat_bytes)
    (reb / "audio-manifest.json").write_bytes(audio_bytes)

    # Add declared assets in candidate and rebuild
    for p_info in cat_data["packs"]:
        if p_info["format"] == "sqlite":
            (cand / f"{p_info['id']}.db").write_bytes(b"\x00" * p_info["sizeBytes"])
            (reb / f"{p_info['id']}.db").write_bytes(b"\x00" * p_info["sizeBytes"])
    for tr in audio_data["tracks"]:
        for v in ("full", "ios"):
            fn = tr[v]["filename"]
            (cand / fn).write_bytes(b"\x00" * tr[v]["sizeBytes"])
            (reb / fn).write_bytes(b"\x00" * tr[v]["sizeBytes"])
    for c in desc["companions"]:
        (cand / c["filename"]).write_bytes(b"\x00" * c["sizeBytes"])
        (reb / c["filename"]).write_bytes(b"\x00" * c["sizeBytes"])

    # Add an unexpected dotfile to candidate
    (cand / ".hidden").write_bytes(b"secret")

    ctx = GateContext(
        profile="v0.1.0",
        candidate=cand,
        rebuild=reb,
        sources_root=sources_root,
        expectations={},
        specs_dir=specs_dir,
    )
    ctx.raw_manifest_bytes["candidate"] = {"catalog.json": cat_bytes, "audio-manifest.json": audio_bytes}
    ctx.raw_manifest_bytes["rebuild"] = {"catalog.json": cat_bytes, "audio-manifest.json": audio_bytes}
    ctx.manifest_data_by_root["candidate"] = (cat_data, audio_data)
    ctx.manifest_data_by_root["rebuild"] = (cat_data, audio_data)
    with pytest.raises(GateCheckError) as exc_info:
        stage_3_manifest_schemas_inventory(ctx)
    assert exc_info.value.check_num == 5
    assert "inventory" in exc_info.value.reason.lower() or "dotfile" in exc_info.value.reason.lower()


def test_stage12_cross_root_inode_alias_different_names(tmp_path: Path) -> None:
    from pipelines.validators.quality_gate import GateContext, stage_12_cross_root_equality
    specs_dir = Path(__file__).resolve().parent.parent / "specs"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    cand.mkdir(parents=True)
    reb.mkdir(parents=True)

    (cand / "file1.txt").write_bytes(b"content")
    (cand / "file2.txt").write_bytes(b"content")
    (reb / "file1.txt").write_bytes(b"content")

    # Create hardlink between candidate/file1.txt and rebuild/file2.txt (cross-name alias!)
    try:
        os.link(cand / "file1.txt", reb / "file2.txt")
    except OSError:
        pytest.skip("Hardlinks not supported on this filesystem")

    ctx = GateContext(
        profile="v0.1.0",
        candidate=cand,
        rebuild=reb,
        sources_root=tmp_path,
        expectations={},
        specs_dir=specs_dir,
    )
    ctx.declared_files_by_root["candidate"] = {"file1.txt", "file2.txt"}

    # stage_12 must reject any shared inode between candidate and rebuild!
    with pytest.raises(GateCheckError) as exc_info:
        stage_12_cross_root_equality(ctx)
    assert exc_info.value.check_num == 6
    assert "inode" in exc_info.value.reason.lower() or "alias" in exc_info.value.reason.lower()


def test_stage9_corpus_segment_coordinates_attribute_fix(tmp_path: Path) -> None:
    from pipelines.builders.build_quran_morphology import CorpusSegment
    from pipelines.validators.quality_gate import GateContext, stage_9_coverage
    specs_dir = Path(__file__).resolve().parent.parent / "specs"
    cand = tmp_path / "cand"
    reb = tmp_path / "reb"
    cand.mkdir(parents=True)
    reb.mkdir(parents=True)

    # Create minimal candidate and rebuild DB for quran-morphology
    for root in (cand, reb):
        db_path = root / "quran-morphology.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE words (id INTEGER PRIMARY KEY, word_key TEXT, word_ar_uthmani TEXT, word_ar_clean TEXT);")
        conn.execute("INSERT INTO words (id, word_key, word_ar_uthmani, word_ar_clean) VALUES (1, '1:1:1', 'بِسْمِ', 'بسم');")
        conn.execute("CREATE TABLE coverage_report (metric TEXT PRIMARY KEY, count INTEGER, notes TEXT);")
        conn.execute("INSERT INTO coverage_report (metric, count, notes) VALUES ('expected_total', 1, NULL);")
        conn.execute("INSERT INTO coverage_report (metric, count, notes) VALUES ('ar_present', 1, NULL);")
        conn.commit()
        conn.close()

    ctx = GateContext(
        profile="v0.1.0",
        candidate=cand,
        rebuild=reb,
        sources_root=tmp_path,
        expectations={},
        specs_dir=specs_dir,
    )
    cat_data = {
        "catalogVersion": 1,
        "generatedAt": "2026-11-23T13:31:00Z",
        "releaseTag": "content-v0.1.0",
        "packs": [{
            "id": "quran-morphology",
            "format": "sqlite",
            "version": "0.1.0",
            "schemaVersion": 1,
            "completeness": "complete",
            "licenseRef": "sources/licenses/quran-morphology.json",
            "coverage": {"expected": 1, "available": 1},
            "requiresFts5": False,
        }],
    }
    ctx.manifest_data_by_root["candidate"] = (cat_data, {})
    ctx.manifest_data_by_root["rebuild"] = (cat_data, {})

    # Real CorpusSegment has NO word_key attribute
    seg = CorpusSegment(
        surah=1,
        ayah=1,
        word=1,
        segment=1,
        form_buckwalter="bi",
        form_ar="بِ",
        pos_tag="P",
        features_raw="PREFIX|bi+",
        role="PREFIX",
    )
    ctx._corpus_segments = (seg,)
    with pytest.raises(GateCheckError) as exc_info:
        stage_9_coverage(ctx)
    assert exc_info.value.check_num == 1
    assert "77429" in exc_info.value.reason
    assert "got {'expected_total': 1, 'ar_present': 1}" in exc_info.value.reason


def test_release_packs_workflow_shell_syntax() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    wf_path = repo_root / ".github" / "workflows" / "release-packs.yml"
    assert wf_path.is_file()
    content = wf_path.read_text(encoding="utf-8")

    # Extract all multi-line run: | blocks
    lines = content.splitlines()
    run_blocks: list[str] = []
    curr_block: list[str] = []
    in_block = False
    block_indent = 0

    for line in lines:
        if not in_block:
            if line.strip().startswith("run: |"):
                in_block = True
                block_indent = len(line) - len(line.lstrip())
                curr_block = []
        else:
            indent = len(line) - len(line.lstrip()) if line.strip() else block_indent + 2
            if indent > block_indent:
                curr_block.append(line)
            else:
                run_blocks.append("\n".join(curr_block))
                in_block = False
                curr_block = []
                if line.strip().startswith("run: |"):
                    in_block = True
                    block_indent = len(line) - len(line.lstrip())
                    curr_block = []
    if in_block and curr_block:
        run_blocks.append("\n".join(curr_block))

    assert len(run_blocks) >= 5
    for i, block in enumerate(run_blocks):
        # Replace action expression tokens for bash syntax checking
        clean_script = block
        # Check syntax using bash -n
        proc = subprocess.run(
            ["bash", "-n"],
            input=clean_script,
            text=True,
            capture_output=True,
        )
        assert proc.returncode == 0, f"Workflow run block #{i} has invalid shell syntax: {proc.stderr}\nScript:\n{clean_script}"


def test_acquire_sources_private_bundle_outside_raw_subtree(tmp_path: Path) -> None:
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True)
    raw_root = tmp_path / "raw-inputs"

    bundle_path = "corpus/quranic-corpus-morphology-0.4.zip"
    bundle_member_bytes = b"sample corpus content"
    bundle_zip_file = tmp_path / "external_bundle.zip"
    with zipfile.ZipFile(bundle_zip_file, "w", compression=zipfile.ZIP_STORED) as zf:
        zinfo = zipfile.ZipInfo(filename=bundle_path, date_time=(1980, 1, 1, 0, 0, 0))
        zinfo.external_attr = 0o100644 << 16
        zf.writestr(zinfo, bundle_member_bytes)

    reg = {
        bundle_path: {
            "packIds": ["quran-morphology"],
            "sourceUrl": "https://corpus.quran.com/download/",
            "sourceVersion": "0.4",
        }
    }
    (sources_dir / "registry.json").write_text(json.dumps(reg), encoding="utf-8")

    temp_stage = tmp_path / "stage_raw"
    (temp_stage / "corpus").mkdir(parents=True)
    (temp_stage / bundle_path).write_bytes(bundle_member_bytes)
    author_lock(sources_dir / "registry.json", temp_stage, sources_dir / "manifest.lock.json")
    shutil_rm = __import__("shutil").rmtree
    shutil_rm(temp_stage)

    desc = {
        "repository": "Mohamed-Ahmed-Ali/islamic-core-content",
        "generatedAt": "2026-11-23T13:31:00Z",
        "bundlePaths": [bundle_path],
        "companions": [
            {
                "filename": "quran-morphology-source.zip",
                "sizeBytes": 100,
                "sha256": "0" * 64,
                "sourcePath": None,
            }
        ],
    }
    (sources_dir / "release-v0.1.0.json").write_text(json.dumps(desc), encoding="utf-8")

    acquire_sources(
        sources_root=tmp_path,
        bundle_archive=bundle_zip_file,
        allow_network=False,
    )

    assert raw_root.is_dir()
    assert (raw_root / bundle_path).is_file()
    assert (raw_root / bundle_path).read_bytes() == bundle_member_bytes
    # Ensure .bundle.zip was NOT published or left inside raw-inputs
    assert not (raw_root / ".bundle.zip").exists()
    assert not (raw_root / "bundle.zip").exists()


def test_acquire_sources_verifies_lock_registry_correspondence_before_network(tmp_path: Path) -> None:
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True)
    raw_root = tmp_path / "raw-inputs"

    # Create registry with path A, but lock with path B
    reg = {
        "file_a.txt": {"packIds": ["quran-core"], "sourceUrl": "https://example.com/a", "sourceVersion": "1"}
    }
    (sources_dir / "registry.json").write_text(json.dumps(reg), encoding="utf-8")

    temp_stage = tmp_path / "stage_raw"
    temp_stage.mkdir()
    (temp_stage / "file_b.txt").write_bytes(b"content b")
    reg_b = {
        "file_b.txt": {"packIds": ["quran-core"], "sourceUrl": "https://example.com/b", "sourceVersion": "1"}
    }
    (sources_dir / "registry_b.json").write_text(json.dumps(reg_b), encoding="utf-8")
    author_lock(sources_dir / "registry_b.json", temp_stage, sources_dir / "manifest.lock.json")
    (sources_dir / "registry_b.json").unlink()

    desc = {
        "repository": "Mohamed-Ahmed-Ali/islamic-core-content",
        "generatedAt": "2026-11-23T13:31:00Z",
        "bundlePaths": [],
        "companions": [
            {
                "filename": "quran-morphology-source.zip",
                "sizeBytes": 100,
                "sha256": "0" * 64,
                "sourcePath": None,
            }
        ],
    }
    (sources_dir / "release-v0.1.0.json").write_text(json.dumps(desc), encoding="utf-8")

    with pytest.raises(ValueError, match="mismatch"):
        acquire_sources(sources_root=tmp_path, allow_network=False)


def test_acquire_sources_rejects_dos_dir_attribute_in_bundle(tmp_path: Path) -> None:
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(parents=True)

    bundle_path = "corpus/quranic-corpus-morphology-0.4.zip"
    bundle_zip_file = tmp_path / "dos_dir_bundle.zip"
    with zipfile.ZipFile(bundle_zip_file, "w", compression=zipfile.ZIP_STORED) as zf:
        zinfo = zipfile.ZipInfo(filename=bundle_path, date_time=(1980, 1, 1, 0, 0, 0))
        # Set MS-DOS directory attribute 0x10
        zinfo.external_attr = 0x10
        zf.writestr(zinfo, b"data")

    reg = {
        bundle_path: {
            "packIds": ["quran-morphology"],
            "sourceUrl": "https://corpus.quran.com/download/",
            "sourceVersion": "0.4",
        }
    }
    (sources_dir / "registry.json").write_text(json.dumps(reg), encoding="utf-8")

    temp_stage = tmp_path / "stage_raw"
    (temp_stage / "corpus").mkdir(parents=True)
    (temp_stage / bundle_path).write_bytes(b"data")
    author_lock(sources_dir / "registry.json", temp_stage, sources_dir / "manifest.lock.json")

    desc = {
        "repository": "Mohamed-Ahmed-Ali/islamic-core-content",
        "generatedAt": "2026-11-23T13:31:00Z",
        "bundlePaths": [bundle_path],
        "companions": [
            {
                "filename": "quran-morphology-source.zip",
                "sizeBytes": 100,
                "sha256": "0" * 64,
                "sourcePath": None,
            }
        ],
    }
    (sources_dir / "release-v0.1.0.json").write_text(json.dumps(desc), encoding="utf-8")

    with pytest.raises(ValueError, match="MS-DOS directory|directory"):
        acquire_sources(
            sources_root=tmp_path,
            bundle_archive=bundle_zip_file,
            allow_network=False,
        )


def test_assemble_companions_mismatch_cleans_up_owned_outputs(tmp_path: Path) -> None:
    from pipelines.release.release_packs import main as rp_main
    sources_root = tmp_path / "sources_tree"
    sources_root.mkdir(parents=True)
    asset_root = tmp_path / "dist"
    asset_root.mkdir(parents=True)

    for member in MORPHOLOGY_SOURCE_ARCHIVE_MEMBERS:
        p = sources_root / member
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"content of {member}".encode("utf-8"))

    reg = {
        "corpus/quranic-corpus-morphology-0.4.zip": {"packIds": ["quran-morphology"], "sourceUrl": "https://corpus.quran.com/download/", "sourceVersion": "0.4"},
        "tanzil/quran-data-Tanzil.xml": {"packIds": ["quran-core"], "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml", "sourceVersion": "1.0"},
    }
    (sources_root / "sources" / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
    author_lock(sources_root / "sources" / "registry.json", sources_root / "raw-inputs", sources_root / "sources" / "manifest.lock.json")

    # Descriptor has wrong sha256
    desc = {
        "repository": "Mohamed-Ahmed-Ali/islamic-core-content",
        "generatedAt": "2026-11-23T13:31:00Z",
        "bundlePaths": [],
        "companions": [
            {
                "filename": "quran-morphology-source.zip",
                "sizeBytes": 12345,
                "sha256": "f" * 64,
                "sourcePath": None,
            }
        ],
    }
    (sources_root / "sources" / "release-v0.1.0.json").write_text(json.dumps(desc), encoding="utf-8")

    ret = rp_main([
        "assemble-companions",
        "--sources-root",
        str(sources_root),
        "--asset-root",
        str(asset_root),
    ])
    assert ret == 1
    # Mismatch must leave NO owned outputs
    assert not (asset_root / "quran-morphology-source.zip").exists()
    assert len(list(asset_root.iterdir())) == 0


def test_generate_catalog_reconciliation_and_coverage_null_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pipelines.release.generate_catalog as gc
    import pipelines.validators.quality_gate as qg
    from pipelines.common.lock_inputs import canonical_entries_json
    monkeypatch.setattr(gc, "probe_asset", lambda p, variant="full": ("aac" if variant == "full" else "adpcm_ima_qt", Fraction(10, 1)))
    monkeypatch.setattr(qg, "verify_lock", lambda r, rw, l: parse_and_validate_lock(l))

    repo_root = Path(__file__).resolve().parent.parent
    sources_root = tmp_path / "src"
    asset_root = tmp_path / "assets"
    sources_root.mkdir(parents=True)
    asset_root.mkdir(parents=True)

    # Copy licenses, registry, lock, descriptor
    _copy_v010_source_tree(repo_root, sources_root / "sources")
    (sources_root / "sources" / "release-v0.1.0.json").write_bytes((repo_root / "sources" / "release-v0.1.0.json").read_bytes())

    raw_root = sources_root / "raw-inputs"
    reg_data = json.loads((sources_root / "sources" / "registry.json").read_text(encoding="utf-8"))
    for p in reg_data.keys():
        target = raw_root / p
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"dummy raw content")

    # Create 12 dummy audio files
    from pipelines.release.generate_catalog import _AUDIO_SLUGS, _SQLITE_PACK_IDS
    for slug in _AUDIO_SLUGS:
        (asset_root / f"adhan-{slug}-full.m4a").write_bytes(b"\x00" * 100)
        (asset_root / f"adhan-{slug}-ios-notif.caf").write_bytes(b"\x00" * 100)

    # Create 5 valid SQLite packs matching all 11 keys and coverage report
    lock_entries = parse_and_validate_lock(sources_root / "sources" / "manifest.lock.json")
    for pack_id in _SQLITE_PACK_IDS:
        db_path = asset_root / f"{pack_id}.db"
        lic_data = json.loads((sources_root / "sources" / "licenses" / f"{pack_id}.json").read_text(encoding="utf-8"))
        primary_src = [s for s in lic_data["sources"] if s["path"] == lic_data["primaryPath"]][0]
        pack_lock_paths = {s["path"] for s in lic_data["sources"]}
        pack_lock_entries = tuple(e for e in lock_entries if e.path in pack_lock_paths)
        expected_hash = hashlib.sha256(canonical_entries_json(pack_lock_entries)).hexdigest().lower()
        requires_fts = pack_id in ("quran-core", "quran-tr-en-pickthall", "quran-tafsir-ar-jalalayn")
        exp_count = 77429 if pack_id == "quran-morphology" else 6236

        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA user_version = 1;")
        conn.execute("CREATE TABLE pack_meta (key TEXT PRIMARY KEY, value TEXT);")
        meta_entries = [
            ("pack_id", pack_id),
            ("pack_version", "0.1.0"),
            ("schema_version", "1"),
            ("source_name", primary_src["name"]),
            ("source_url", primary_src["sourceUrl"]),
            ("source_version", primary_src["sourceVersion"]),
            ("license_spdx", lic_data["licenseSpdx"]),
            ("license_ref", f"sources/licenses/{pack_id}.json"),
            ("build_input_sha256", expected_hash),
            ("completeness", "complete"),
            ("fts5_enabled", "true" if requires_fts else "false"),
        ]
        for k, v in meta_entries:
            conn.execute("INSERT INTO pack_meta (key, value) VALUES (?, ?);", (k, v))

        conn.execute("CREATE TABLE coverage_report (metric TEXT PRIMARY KEY, count INTEGER, notes TEXT);")
        conn.execute("INSERT INTO coverage_report (metric, count, notes) VALUES ('expected_total', ?, NULL);", (exp_count,))
        if pack_id == "quran-tr-en-pickthall":
            conn.execute("INSERT INTO coverage_report (metric, count, notes) VALUES ('en_present', ?, NULL);", (exp_count,))
            conn.execute("INSERT INTO coverage_report (metric, count, notes) VALUES ('en_missing', 0, NULL);")
        else:
            conn.execute("INSERT INTO coverage_report (metric, count, notes) VALUES ('ar_present', ?, NULL);", (exp_count,))
        conn.commit()
        conn.close()

    # Running generate_catalog succeeds
    generate_catalog(sources_root, asset_root)
    assert (asset_root / "catalog.json").is_file()
    assert (asset_root / "audio-manifest.json").is_file()

    # Refuses preexisting manifest
    with pytest.raises(FileExistsError, match="already exists"):
        generate_catalog(sources_root, asset_root)

    # Remove manifests for mutation tests
    (asset_root / "catalog.json").unlink()
    (asset_root / "audio-manifest.json").unlink()

    # Mutate coverage_report to have non-NULL notes: must raise ValueError
    conn = sqlite3.connect(asset_root / "quran-core.db")
    conn.execute("UPDATE coverage_report SET notes = 'invalid note' WHERE metric = 'expected_total';")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="notes must be NULL"):
        generate_catalog(sources_root, asset_root)

    # Restore valid notes
    conn = sqlite3.connect(asset_root / "quran-core.db")
    conn.execute("UPDATE coverage_report SET notes = NULL WHERE metric = 'expected_total';")
    conn.commit()
    conn.close()

    # Mutate pack_meta to have mismatched source_name: must raise ValueError
    conn = sqlite3.connect(asset_root / "quran-core.db")
    conn.execute("UPDATE pack_meta SET value = 'Wrong Name' WHERE key = 'source_name';")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="source_name mismatch"):
        generate_catalog(sources_root, asset_root)
