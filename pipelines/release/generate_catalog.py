"""Generate catalog/catalog.json and catalog/audio-manifest.json for distribution."""

from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

import shutil
import tempfile
from pipelines.common.audio_probe import probe_asset
from pipelines.common.lock_inputs import (
    canonical_entries_json,
    parse_strict_json,
    _is_symlink_or_junction,
)
from pipelines.release.release_packs import load_release_descriptor
from pipelines.validators.quality_gate import (
    StrictDraft202012Validator,
    format_duration_seconds,
    validate_manifest_schema_and_canonical,
    validate_release_sources,
)

_SQLITE_PACK_IDS: tuple[str, ...] = (
    "quran-core",
    "quran-morphology",
    "quran-scripts-extended",
    "quran-tafsir-ar-jalalayn",
    "quran-tr-en-pickthall",
)

_AUDIO_SLUGS: tuple[str, ...] = (
    "abdul-basit-abdul-samad",
    "abdul-basit-abdul-samad-2",
    "abul-ainain-shuaisha",
    "mahmoud-khalil-al-hussary",
    "mohamed-refaat",
    "mustafa-ismail",
)


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def generate_catalog(sources_root: Path, asset_root: Path) -> None:
    """Generate both catalog.json and audio-manifest.json in asset_root."""
    if not sources_root.is_dir() or _is_symlink_or_junction(sources_root):
        raise ValueError(f"sources_root missing or link: {sources_root}")
    if not asset_root.is_dir() or _is_symlink_or_junction(asset_root):
        raise ValueError(f"asset_root missing or link: {asset_root}")

    cat_target = asset_root / "catalog.json"
    audio_target = asset_root / "audio-manifest.json"
    if (
        cat_target.exists()
        or _is_symlink_or_junction(cat_target)
        or audio_target.exists()
        or _is_symlink_or_junction(audio_target)
    ):
        raise FileExistsError("Target manifest already exists or is link in asset_root")
    desc = load_release_descriptor(sources_root)
    repository = desc["repository"]
    generated_at = desc["generatedAt"]
    release_tag = "content-v0.1.0"

    # Validate release sources and extract verified license records
    lock_entries, reg_entries, lic_records = validate_release_sources(sources_root, local_review=True)
    adhan_lic = lic_records.get("audio-adhan-core")
    if not adhan_lic:
        raise ValueError("audio-adhan-core license record missing from release sources")
    reciter_by_slug = {s["slug"]: s["reciter"] for s in adhan_lic["sources"]}

    repo_root = Path(__file__).resolve().parents[2]
    specs_dir = repo_root / "specs"

    # 1. Process 5 SQLite packs
    sqlite_packs_meta: list[dict[str, Any]] = []
    for pack_id in _SQLITE_PACK_IDS:
        db_path = asset_root / f"{pack_id}.db"
        if not db_path.is_file() or _is_symlink_or_junction(db_path):
            raise ValueError(f"SQLite pack missing or link: {db_path}")

        db_size = db_path.stat().st_size
        db_sha256 = _compute_sha256(db_path)

        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA trusted_schema = OFF;")
        try:
            cur = conn.cursor()

            cur.execute("PRAGMA user_version;")
            u_ver = cur.fetchone()
            if not u_ver or u_ver[0] != 1:
                raise ValueError(f"pack {pack_id} user_version mismatch: expected 1, got {u_ver[0] if u_ver else None}")

            cur.execute("SELECT key, value FROM pack_meta;")
            meta = {k: v for k, v in cur.fetchall()}

            mandatory_meta_keys = {
                "pack_id", "pack_version", "schema_version", "source_name", "source_url",
                "source_version", "license_spdx", "license_ref", "build_input_sha256",
                "completeness", "fts5_enabled",
            }
            if set(meta.keys()) != mandatory_meta_keys:
                raise ValueError(f"pack_meta keys mismatch in {db_path}: {set(meta.keys())} vs {mandatory_meta_keys}")

            if meta.get("pack_id") != pack_id:
                raise ValueError(f"pack_meta pack_id mismatch in {db_path}: {meta.get('pack_id')}")
            if meta.get("pack_version") != "0.1.0":
                raise ValueError(f"pack_meta pack_version mismatch in {db_path}: {meta.get('pack_version')}")
            if meta.get("schema_version") != "1":
                raise ValueError(f"pack_meta schema_version mismatch in {db_path}: {meta.get('schema_version')}")
            if meta.get("completeness") != "complete":
                raise ValueError(f"pack_meta completeness not complete in {db_path}: {meta.get('completeness')}")
            if meta.get("license_ref") != f"sources/licenses/{pack_id}.json":
                raise ValueError(f"pack_meta license_ref mismatch in {db_path}")

            lic_data = lic_records.get(pack_id)
            if not lic_data:
                raise ValueError(f"License record missing for {pack_id}")
            if meta.get("license_spdx") != lic_data["licenseSpdx"]:
                raise ValueError(f"pack_meta license_spdx mismatch in {db_path}")

            primary_src = [s for s in lic_data["sources"] if s["path"] == lic_data["primaryPath"]][0]
            if meta.get("source_name") != primary_src["name"]:
                raise ValueError(f"pack_meta source_name mismatch in {db_path}")
            if meta.get("source_url") != primary_src["sourceUrl"]:
                raise ValueError(f"pack_meta source_url mismatch in {db_path}")
            if meta.get("source_version") != primary_src["sourceVersion"]:
                raise ValueError(f"pack_meta source_version mismatch in {db_path}")

            pack_lock_paths = {s["path"] for s in lic_data["sources"]}
            pack_lock_entries = tuple(e for e in lock_entries if e.path in pack_lock_paths)
            expected_subset_hash = hashlib.sha256(canonical_entries_json(pack_lock_entries)).hexdigest().lower()
            if meta.get("build_input_sha256") != expected_subset_hash:
                raise ValueError(f"pack_meta build_input_sha256 mismatch in {db_path}")

            requires_fts = pack_id in ("quran-core", "quran-tr-en-pickthall", "quran-tafsir-ar-jalalayn")
            if (meta.get("fts5_enabled") == "true") != requires_fts:
                raise ValueError(f"fts5_enabled mismatch in {db_path}")

            cur.execute("SELECT metric, count, notes FROM coverage_report;")
            cov_counts: dict[str, int] = {}
            for r in cur.fetchall():
                m_metric, m_count, m_notes = r[0], r[1], r[2]
                if m_notes is not None:
                    raise ValueError(f"coverage_report notes must be NULL in {db_path}, got {m_notes!r} for {m_metric}")
                cov_counts[m_metric] = m_count

            expected_count = 77429 if pack_id == "quran-morphology" else 6236
            exp_total = cov_counts.get("expected_total")
            if exp_total != expected_count:
                raise ValueError(f"Invalid expected_total in {db_path}: expected {expected_count}, got {exp_total}")

            if pack_id == "quran-tr-en-pickthall":
                avail = cov_counts.get("en_present", 0)
                if cov_counts.get("en_missing") != 0:
                    raise ValueError(f"en_missing must be 0 in {db_path}")
                languages = ["en"]
            else:
                avail = cov_counts.get("ar_present", 0)
                languages = ["ar"]

            if avail != expected_count:
                raise ValueError(f"Coverage not complete in {db_path}: avail={avail}, exp={expected_count}")
            sqlite_packs_meta.append({
                "id": pack_id,
                "category": "quran",
                "version": "0.1.0",
                "format": "sqlite",
                "languages": languages,
                "completeness": "complete",
                "licenseRef": f"sources/licenses/{pack_id}.json",
                "required": (pack_id == "quran-core"),
                "compression": "none",
                "schemaVersion": 1,
                "url": f"https://github.com/{repository}/releases/download/{release_tag}/{pack_id}.db",
                "sizeBytes": db_size,
                "sha256": db_sha256,
                "coverage": {
                    "expected": exp_total,
                    "available": avail,
                },
                "requiresFts5": requires_fts,
            })
        finally:
            conn.close()

    # 2. Process audio pack and tracks
    tracks_data: list[dict[str, Any]] = []
    for slug in sorted(_AUDIO_SLUGS):
        reciter = reciter_by_slug.get(slug)
        if not reciter:
            raise ValueError(f"Reciter not found for slug {slug}")

        full_fn = f"adhan-{slug}-full.m4a"
        ios_fn = f"adhan-{slug}-ios-notif.caf"
        full_path = asset_root / full_fn
        ios_path = asset_root / ios_fn

        if not full_path.is_file() or _is_symlink_or_junction(full_path):
            raise ValueError(f"Full audio asset missing or link: {full_path}")
        if not ios_path.is_file() or _is_symlink_or_junction(ios_path):
            raise ValueError(f"iOS audio asset missing or link: {ios_path}")

        full_codec, full_duration = probe_asset(full_path, variant="full")
        if full_codec != "aac":
            raise ValueError(f"Expected aac codec for {full_fn}, got {full_codec}")
        if full_duration <= 0:
            raise ValueError(f"Non-positive duration for {full_fn}")

        ios_codec, ios_duration = probe_asset(ios_path, variant="ios")
        if ios_codec != "adpcm_ima_qt":
            raise ValueError(f"Expected adpcm_ima_qt codec for {ios_fn}, got {ios_codec}")
        if not (Fraction(0) < ios_duration < Fraction(29, 1)):
            raise ValueError(f"iOS notification CAF duration {ios_duration} not strictly 0 < duration < 29.0s")

        full_size = full_path.stat().st_size
        full_hash = _compute_sha256(full_path)
        ios_size = ios_path.stat().st_size
        ios_hash = _compute_sha256(ios_path)

        tracks_data.append({
            "slug": slug,
            "reciter": reciter,
            "full": {
                "filename": full_fn,
                "url": f"https://github.com/{repository}/releases/download/{release_tag}/{full_fn}",
                "sizeBytes": full_size,
                "sha256": full_hash,
                "codec": "aac",
                "durationSeconds": format_duration_seconds(full_duration),
            },
            "ios": {
                "filename": ios_fn,
                "url": f"https://github.com/{repository}/releases/download/{release_tag}/{ios_fn}",
                "sizeBytes": ios_size,
                "sha256": ios_hash,
                "codec": "adpcm_ima_qt",
                "durationSeconds": format_duration_seconds(ios_duration),
            },
        })

    tracks_data.sort(key=lambda t: t["slug"])

    audio_pack_meta = {
        "id": "audio-adhan-core",
        "category": "audio",
        "version": "0.1.0",
        "format": "audio",
        "languages": ["ar"],
        "completeness": "complete",
        "licenseRef": "sources/licenses/audio-adhan-core.json",
        "required": False,
        "audioManifestRef": "audio-manifest.json",
    }

    all_packs = sqlite_packs_meta + [audio_pack_meta]
    all_packs.sort(key=lambda p: p["id"])

    catalog_data = {
        "catalogVersion": 1,
        "generatedAt": generated_at,
        "releaseTag": release_tag,
        "packs": all_packs,
    }

    audio_manifest_data = {
        "audioManifestVersion": 1,
        "releaseTag": release_tag,
        "packId": "audio-adhan-core",
        "tracks": tracks_data,
    }

    cat_bytes = (
        json.dumps(catalog_data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    audio_bytes = (
        json.dumps(audio_manifest_data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    # Validate against schemas and canonical byte serialization
    validate_manifest_schema_and_canonical(cat_bytes, catalog_data, specs_dir / "catalog.schema.json", "catalog.json")
    validate_manifest_schema_and_canonical(audio_bytes, audio_manifest_data, specs_dir / "audio-manifest.schema.json", "audio-manifest.json")

    # Private same-FS temp directory + no-replace hardlinks with only-owned rollback
    stage_dir = Path(tempfile.mkdtemp(prefix=".manifest-stage-", dir=asset_root))
    stage_cat = stage_dir / "catalog.json"
    stage_audio = stage_dir / "audio-manifest.json"
    placed: list[tuple[Path, Path]] = []
    try:
        stage_cat.write_bytes(cat_bytes)
        stage_audio.write_bytes(audio_bytes)

        if (
            cat_target.exists()
            or _is_symlink_or_junction(cat_target)
            or audio_target.exists()
            or _is_symlink_or_junction(audio_target)
        ):
            raise FileExistsError("Target manifest already exists in asset_root")

        try:
            os.link(stage_cat, cat_target)
        except OSError:
            if cat_target.exists() or _is_symlink_or_junction(cat_target):
                raise FileExistsError(f"Target catalog.json already exists: {cat_target}") from None
            raise
        placed.append((stage_cat, cat_target))

        if audio_target.exists() or _is_symlink_or_junction(audio_target):
            raise FileExistsError(f"Target audio-manifest.json already exists: {audio_target}")

        try:
            os.link(stage_audio, audio_target)
        except OSError:
            if audio_target.exists() or _is_symlink_or_junction(audio_target):
                raise FileExistsError(f"Target audio-manifest.json already exists: {audio_target}") from None
            raise
        placed.append((stage_audio, audio_target))
    except Exception:
        for src_file, target_file in placed:
            try:
                if (
                    target_file.exists()
                    and not _is_symlink_or_junction(target_file)
                    and src_file.exists()
                    and target_file.samefile(src_file)
                ):
                    target_file.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release catalog generator", add_help=False)
    parser.add_argument("--sources-root", required=True)
    parser.add_argument("--asset-root", required=True)

    try:
        args = parser.parse_args(argv)
    except Exception as exc:
        sys.stderr.write(f"CLI error: {exc}\n")
        return 2

    sources_root = Path(args.sources_root)
    asset_root = Path(args.asset_root)

    try:
        generate_catalog(sources_root, asset_root)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error generating catalog: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
