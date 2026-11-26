"""Recitation timing manifest builder for Phase 3.6."""

from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

import jsonschema

from pipelines.common.lock_inputs import (
    canonical_entries_json,
    parse_strict_json,
    verify_lock,
)
from pipelines.common.pack_sources import _is_symlink_or_junction, _verify_exact_case_regular_file
from pipelines.common.qul_input import read_zip_strict_json
from pipelines.common.tanzil_coordinates import load_tanzil_coordinates
from pipelines.toolchain.check_runtime import verify_runtime

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _REPO_ROOT / "specs" / "recitation-manifest.schema.json"

TABLAWI_KNOWN_INVALID_SEGMENTS: frozenset[tuple[str, int, tuple[int, int, int]]] = frozenset({
    ("2:164", 22, (22, 39140, 39130)),
    ("2:170", 11, (11, 8540, 8530)),
    ("6:124", 10, (10, 11930, 11930)),
    ("7:48", 8, (8, 11490, 11480)),
    ("7:189", 4, (4, 3040, 3030)),
    ("10:49", 2, (2, 540, 530)),
    ("11:87", 11, (11, 11550, 11530)),
    ("12:79", 7, (7, 5150, 5150)),
    ("12:110", 12, (12, 13730, 13730)),
    ("13:30", 21, (21, 24450, 24430)),
    ("13:37", 16, (16, 17420, 17380)),
    ("22:76", 2, (2, 1310, 1310)),
    ("25:12", 3, (3, 2440, 2430)),
    ("26:84", 2, (2, 1270, 1270)),
    ("41:40", 14, (14, 15380, 15380)),
    ("45:21", 6, (6, 4050, 4030)),
    ("48:16", 1, (1, 350, 50)),
    ("77:20", 3, (3, 3450, 3440)),
})

RECITATION_PACKS: dict[str, dict[str, Any]] = {
    "audio-recitation-abdul-basit-mujawwad": {
        "pack_id": "audio-recitation-abdul-basit-mujawwad",
        "slug": "abdul-basit-mujawwad",
        "archive_path": "qul/recitation/ayah-recitation-abdul-basit-abdul-samad-mujawwad-hafs-949.json.zip",
        "member_name": "ayah-recitation-abdul-basit-abdul-samad-mujawwad-hafs-949.json",
        "reciter": "Abdul Basit Abdul Samad",
        "style": "mujawwad",
        "https_prefix": "https://audio-cdn.tarteel.ai/quran/abdulBasitMujawwad",
        "expected_invalid_count": 0,
    },
    "audio-recitation-abdul-basit-murattal": {
        "pack_id": "audio-recitation-abdul-basit-murattal",
        "slug": "abdul-basit-murattal",
        "archive_path": "qul/recitation/ayah-recitation-abdul-basit-abdul-samad-murattal-hafs-950.json.zip",
        "member_name": "ayah-recitation-abdul-basit-abdul-samad-murattal-hafs-950.json",
        "reciter": "Abdul Basit Abdul Samad",
        "style": "murattal",
        "https_prefix": "https://audio-cdn.tarteel.ai/quran/abdulBasitMurattal",
        "expected_invalid_count": 0,
    },
    "audio-recitation-al-husary-muallim": {
        "pack_id": "audio-recitation-al-husary-muallim",
        "slug": "al-husary-muallim",
        "archive_path": "qul/recitation/ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-955.json.zip",
        "member_name": "ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-955.json",
        "reciter": "Mahmoud Khalil Al-Husary",
        "style": "muallim",
        "https_prefix": "https://audio-cdn.tarteel.ai/quran/husaryMuallim",
        "expected_invalid_count": 0,
    },
    "audio-recitation-al-husary-mujawwad": {
        "pack_id": "audio-recitation-al-husary-mujawwad",
        "slug": "al-husary-mujawwad",
        "archive_path": "qul/recitation/ayah-recitation-mahmoud-khalil-al-husary-mujawwad-hafs-956.json.zip",
        "member_name": "ayah-recitation-mahmoud-khalil-al-husary-mujawwad-hafs-956.json",
        "reciter": "Mahmoud Khalil Al-Husary",
        "style": "mujawwad",
        "https_prefix": "https://audio-cdn.tarteel.ai/quran/husaryMujawwad",
        "expected_invalid_count": 0,
    },
    "audio-recitation-al-husary-murattal": {
        "pack_id": "audio-recitation-al-husary-murattal",
        "slug": "al-husary-murattal",
        "archive_path": "qul/recitation/ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-957-Muallim.json.zip",
        "member_name": "ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-957.json",
        "reciter": "Mahmoud Khalil Al-Husary",
        "style": "murattal",
        "https_prefix": "https://audio-cdn.tarteel.ai/quran/husary",
        "expected_invalid_count": 0,
    },
    "audio-recitation-al-minshawi-murattal": {
        "pack_id": "audio-recitation-al-minshawi-murattal",
        "slug": "al-minshawi-murattal",
        "archive_path": "qul/recitation/ayah-recitation-muhammad-siddiq-al-minshawi-murattal-hafs-959.json.zip",
        "member_name": "ayah-recitation-muhammad-siddiq-al-minshawi-murattal-hafs-959.json",
        "reciter": "Muhammad Siddiq Al-Minshawi",
        "style": "murattal",
        "https_prefix": "https://audio-cdn.tarteel.ai/quran/minshawyMurattal",
        "expected_invalid_count": 0,
    },
    "audio-recitation-al-tablawi-murattal": {
        "pack_id": "audio-recitation-al-tablawi-murattal",
        "slug": "al-tablawi-murattal",
        "archive_path": "qul/recitation/ayah-recitation-mohamed-al-tablawi-recitation-murattal-hafs-73.json.zip",
        "member_name": "ayah-recitation-mohamed-al-tablawi-recitation-murattal-hafs-73.json",
        "reciter": "Mohamed Al-Tablawi",
        "style": "murattal",
        "https_prefix": "https://mirrors.quranicaudio.com/everyayah/Mohammad_al_Tablaway_128kbps",
        "expected_invalid_count": 18,
    },
}


def duration_seconds_to_reported_ms(duration: Any) -> int | None:
    """Convert source duration in seconds to integer milliseconds using exact half-up rounding."""
    if duration is None:
        return None
    if isinstance(duration, bool):
        raise ValueError(f"Duration cannot be bool: {duration!r}")
    if isinstance(duration, float):
        raise ValueError(f"Duration cannot be float: {duration!r}")
    if type(duration) is int:
        if duration < 0:
            raise ValueError(f"Duration cannot be negative, got {duration}")
        return duration * 1000
    if isinstance(duration, Decimal):
        if duration < 0:
            raise ValueError(f"Duration cannot be negative, got {duration}")
        num, den = duration.as_integer_ratio()
        return (2 * num * 1000 + den) // (2 * den)
    raise ValueError(f"Duration must be Decimal, int, or None, got {type(duration)}")


def validate_audio_url(url: str, pack_id: str, surah: int, ayah: int) -> None:
    """Validate that audio URL matches the pinned edition prefix and canonical filename."""
    if type(url) is not str:
        raise ValueError(f"Audio URL must be string, got {type(url)}")
    if pack_id not in RECITATION_PACKS:
        raise ValueError(f"Unknown pack ID {pack_id!r}")
    expected_prefix = RECITATION_PACKS[pack_id]["https_prefix"]
    expected_filename = f"{surah:03d}{ayah:03d}.mp3"
    expected_url = f"{expected_prefix}/{expected_filename}"

    if not url.startswith(expected_prefix + "/"):
        raise ValueError(
            f"Audio URL prefix mismatch for {pack_id}: got {url!r}, expected prefix {expected_prefix!r}"
        )
    if url != expected_url:
        raise ValueError(
            f"Audio URL filename mismatch for {surah}:{ayah}: got {url!r}, expected {expected_url!r}"
        )


def process_ayah_segments(
    verse_key: str,
    raw_segments: Any,
    *,
    pack_id: str,
) -> tuple[list[list[int]], list[dict[str, Any]]]:
    """Validate segments, separating valid intervals from tolerated diagnostic intervals."""
    if type(raw_segments) is not list:
        raise ValueError(f"segments must be a list for {verse_key}, got {type(raw_segments)}")
    if len(raw_segments) == 0:
        raise ValueError(f"segments cannot be empty for {verse_key}")

    valid_segments: list[list[int]] = []
    invalid_segments: list[dict[str, Any]] = []

    for source_idx, seg in enumerate(raw_segments, start=1):
        if type(seg) is not list or len(seg) != 3:
            raise ValueError(f"Segment must be a list of 3 elements in {verse_key}: got {seg!r}")
        pos, start_ms, end_ms = seg
        for name, val in [("pos", pos), ("start", start_ms), ("end", end_ms)]:
            if type(val) is not int:
                raise ValueError(f"Segment {name} must be integer in {verse_key}: got {type(val)} ({val!r})")
        if pos <= 0:
            raise ValueError(f"Word position must be positive in {verse_key}: got {pos}")
        if start_ms < 0:
            raise ValueError(f"Segment start time cannot be negative in {verse_key}: got {start_ms}")
        if end_ms < 0:
            raise ValueError(f"Segment end time cannot be negative in {verse_key}: got {end_ms}")

        if end_ms > start_ms:
            valid_segments.append([pos, start_ms, end_ms])
        else:
            if pack_id == "audio-recitation-al-tablawi-murattal":
                candidate = (verse_key, source_idx, (pos, start_ms, end_ms))
                if candidate in TABLAWI_KNOWN_INVALID_SEGMENTS:
                    invalid_segments.append({
                        "sourceIndex": source_idx,
                        "segment": [pos, start_ms, end_ms],
                    })
                else:
                    raise ValueError(
                        f"Unexpected invalid timing interval in {pack_id} for {verse_key} (idx {source_idx}): [{pos}, {start_ms}, {end_ms}]"
                    )
            else:
                raise ValueError(
                    f"Invalid timing interval endMs <= startMs in {pack_id} for {verse_key} (idx {source_idx}): [{pos}, {start_ms}, {end_ms}]"
                )

    return valid_segments, invalid_segments


def validate_ayah_record(
    ayah_data: Any,
    verse_key: str,
    pack_id: str,
) -> tuple[int, int, str, int | None, list[list[int]], list[dict[str, Any]]]:
    """Validate a single ayah source record, returning canonical fields and processed segments."""
    if not isinstance(ayah_data, dict):
        raise ValueError(f"Ayah data must be a dictionary for {verse_key}")
    expected_record_fields = {"surah_number", "ayah_number", "audio_url", "duration", "segments"}
    if set(ayah_data.keys()) != expected_record_fields:
        raise ValueError(
            f"Ayah record fields mismatch for {verse_key}: expected {sorted(expected_record_fields)}, got {sorted(ayah_data.keys())}"
        )

    surah = ayah_data["surah_number"]
    ayah = ayah_data["ayah_number"]
    if type(surah) is not int or type(ayah) is not int:
        raise ValueError(
            f"Surah and ayah numbers must be integers for {verse_key}: got surah={type(surah)}, ayah={type(ayah)}"
        )

    expected_surah, expected_ayah = map(int, verse_key.split(":"))
    if surah != expected_surah or ayah != expected_ayah:
        raise ValueError(
            f"Surah/ayah number mismatch for {verse_key}: "
            f"surah={surah} (expected {expected_surah}), ayah={ayah} (expected {expected_ayah})"
        )

    audio_url = ayah_data["audio_url"]
    validate_audio_url(audio_url, pack_id, surah, ayah)

    duration_val = ayah_data["duration"]
    reported_duration_ms = duration_seconds_to_reported_ms(duration_val)

    raw_segs = ayah_data["segments"]
    valid_segs, invalid_segs = process_ayah_segments(verse_key, raw_segs, pack_id=pack_id)

    return surah, ayah, audio_url, reported_duration_ms, valid_segs, invalid_segs


def _reconcile_license_and_lock(
    sources_root: Path,
    pack_id: str,
    pack_info: dict[str, Any],
) -> tuple[str, str]:
    """Reconcile lock entries and license record, returning (license_ref, build_input_sha256)."""
    registry_path = sources_root / "sources" / "registry.json"
    raw_root = sources_root / "raw-inputs"
    lock_path = sources_root / "sources" / "manifest.lock.json"

    lock_entries = verify_lock(registry_path, raw_root, lock_path, pack_id=pack_id)
    archive_path = pack_info["archive_path"]
    expected_lock_paths = {"tanzil/quran-data-Tanzil.xml", archive_path}

    actual_paths = {e.path for e in lock_entries}
    if actual_paths != expected_lock_paths:
        raise ValueError(
            f"Lock entries mismatch for {pack_id}: expected {sorted(expected_lock_paths)}, got {sorted(actual_paths)}"
        )

    zip_entry = next(e for e in lock_entries if e.path == archive_path)

    rel_lic_path = f"sources/licenses/{pack_id}.json"
    abs_lic_path = _verify_exact_case_regular_file(sources_root, rel_lic_path)
    lic_text = abs_lic_path.read_text(encoding="utf-8")
    lic_data = parse_strict_json(lic_text)
    if not isinstance(lic_data, dict):
        raise ValueError(f"License file {rel_lic_path} root must be a JSON object")

    if lic_data.get("packId") != pack_id:
        raise ValueError(f"packId mismatch in {rel_lic_path}: expected {pack_id!r}, got {lic_data.get('packId')!r}")

    if lic_data.get("primaryPath") != archive_path:
        raise ValueError(
            f"primaryPath mismatch in {rel_lic_path}: expected {archive_path!r}, got {lic_data.get('primaryPath')!r}"
        )

    sources_list = lic_data.get("sources")
    if not isinstance(sources_list, list):
        raise ValueError(f"sources must be a list in {rel_lic_path}")

    archive_sources = [s for s in sources_list if isinstance(s, dict) and s.get("path") == archive_path]
    if len(archive_sources) != 1:
        raise ValueError(f"Expected exactly 1 source entry for {archive_path} in {rel_lic_path}, got {len(archive_sources)}")

    archive_src = archive_sources[0]
    expected_source_version = f"sha256:{zip_entry.sha256}"
    if archive_src.get("sourceVersion") != expected_source_version:
        raise ValueError(
            f"sourceVersion mismatch in {rel_lic_path} for {archive_path}: "
            f"expected {expected_source_version}, got {archive_src.get('sourceVersion')!r}"
        )

    build_input_sha256 = hashlib.sha256(canonical_entries_json(lock_entries)).hexdigest()
    return rel_lic_path, build_input_sha256


def render_recitation_manifest(sources_root: Path | str, pack_id: str) -> bytes:
    """Render canonical JSON bytes for a single recitation manifest."""
    if pack_id not in RECITATION_PACKS:
        raise ValueError(f"Unknown recitation pack ID {pack_id!r}")

    sources_root = Path(sources_root)
    pack_info = RECITATION_PACKS[pack_id]

    license_ref, build_input_sha256 = _reconcile_license_and_lock(sources_root, pack_id, pack_info)

    raw_root = sources_root / "raw-inputs"
    coords = load_tanzil_coordinates(raw_root / "tanzil" / "quran-data-Tanzil.xml")
    ayah_id_to_verse_key = {ayah_id: vk for vk, ayah_id in coords.ayah_ids.items()}

    archive_rel = pack_info["archive_path"]
    member_name = pack_info["member_name"]
    archive_file_path = _verify_exact_case_regular_file(raw_root, archive_rel)
    source_data = read_zip_strict_json(archive_file_path, expected_name=member_name, allow_decimal=True)

    if not isinstance(source_data, dict):
        raise ValueError(f"Source JSON root must be an object for {pack_id}")
    if set(source_data.keys()) != set(coords.ayah_ids.keys()):
        raise ValueError(f"Source verse keys do not match canonical 6,236 verses for {pack_id}")

    ayahs: list[dict[str, Any]] = []
    total_invalid = 0

    for ayah_id in range(1, 6237):
        verse_key = ayah_id_to_verse_key[ayah_id]
        ayah_data = source_data[verse_key]
        _, _, audio_url, reported_duration_ms, valid_segs, invalid_segs = validate_ayah_record(
            ayah_data, verse_key, pack_id
        )
        total_invalid += len(invalid_segs)

        ayahs.append({
            "ayahId": ayah_id,
            "verseKey": verse_key,
            "audioUrl": audio_url,
            "reportedDurationMs": reported_duration_ms,
            "segments": valid_segs,
            "invalidSourceSegments": invalid_segs,
        })

    if total_invalid != pack_info["expected_invalid_count"]:
        raise ValueError(
            f"Total invalid segments {total_invalid} != expected {pack_info['expected_invalid_count']} for {pack_id}"
        )

    manifest_data = {
        "recitationManifestVersion": 1,
        "packId": pack_id,
        "licenseRef": license_ref,
        "buildInputSha256": build_input_sha256,
        "reciter": pack_info["reciter"],
        "style": pack_info["style"],
        "riwayah": "hafs",
        "timingOrigin": "ayah-audio-start",
        "ayahs": ayahs,
    }

    if not _SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"Schema file not found at {_SCHEMA_PATH}")
    schema = parse_strict_json(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(manifest_data)

    canonical_bytes = (
        json.dumps(
            manifest_data,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    return canonical_bytes


def build_recitation_manifests(
    sources_root: Path | str,
    output_dir: Path | str,
) -> list[Path]:
    """Build all seven recitation timing manifests into output_dir."""
    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    if _is_symlink_or_junction(output_dir):
        raise ValueError(f"Output directory cannot be a symlink or junction: {output_dir}")
    if not output_dir.is_dir():
        raise FileNotFoundError(f"Output directory does not exist or is not a directory: {output_dir}")

    # Refuse preexisting outputs and symlinks (including dangling)
    for pack_id in RECITATION_PACKS:
        target = output_dir / f"{pack_id}.json"
        if os.path.lexists(target) or _is_symlink_or_junction(target):
            raise FileExistsError(f"Target output file already exists or is a symlink: {target}")

    # Render all manifests into memory first so no partial files exist on failure
    rendered: dict[str, bytes] = {}
    for pack_id in RECITATION_PACKS:
        rendered[pack_id] = render_recitation_manifest(sources_root, pack_id)

    # Write all rendered manifests with exclusive creation
    written: list[Path] = []
    for pack_id, raw_bytes in rendered.items():
        target = output_dir / f"{pack_id}.json"
        if _is_symlink_or_junction(target):
            raise FileExistsError(f"Target cannot be a symlink: {target}")
        with open(target, "xb") as f:
            f.write(raw_bytes)
        written.append(target)

    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build canonical recitation timing manifests")
    parser.add_argument("--sources-root", type=Path, required=True, help="Path to sources repository root")
    parser.add_argument("--output-dir", type=Path, required=True, help="Destination directory for JSON manifests")
    args = parser.parse_args(argv)

    verify_runtime()

    try:
        written = build_recitation_manifests(args.sources_root, args.output_dir)
        print(f"Successfully generated {len(written)} recitation manifests in {args.output_dir}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
