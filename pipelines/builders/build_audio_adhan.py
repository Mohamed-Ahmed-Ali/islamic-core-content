"""Audio Adhan pack builder CLI and pipeline."""

from __future__ import annotations

import argparse
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any
import wave

from pipelines.common.audio_probe import probe_asset
from pipelines.common.lock_inputs import (
    LockEntry,
    parse_strict_json,
    validate_https_url,
    validate_safe_relative_path,
    verify_exact_file_on_disk,
    verify_lock,
)
from pipelines.toolchain.check_runtime import verify_runtime

PACK_ID = "audio-adhan-core"

SLUG_TO_RECITER: dict[str, str] = {
    "abdul-basit-abdul-samad-2": "Abdul Basit Abdul Samad",
    "abdul-basit-abdul-samad": "Abdul Basit Abdul Samad",
    "abul-ainain-shuaisha": "Abul Ainain Shu’aisha",
    "mahmoud-khalil-al-hussary": "Mahmoud Khalil Al-Hussary",
    "mohamed-refaat": "Mohamed Refaat",
    "mustafa-ismail": "Mustafa Ismail",
}

ORDERED_PATHS: tuple[str, ...] = tuple(
    sorted(f"audio/adhan/{slug}.mp3" for slug in SLUG_TO_RECITER)
)
SLUG_MAP: dict[str, str] = {
    f"audio/adhan/{slug}.mp3": slug for slug in SLUG_TO_RECITER
}
RECITER_MAP: dict[str, str] = {
    f"audio/adhan/{slug}.mp3": reciter for slug, reciter in SLUG_TO_RECITER.items()
}


def _source_name_for(slug: str, reciter: str) -> str:
    if slug == "abdul-basit-abdul-samad-2":
        return f"Adhan — {reciter} (Fajr)"
    return f"Adhan — {reciter}"


NAME_MAP: dict[str, str] = {
    f"audio/adhan/{slug}.mp3": _source_name_for(slug, reciter)
    for slug, reciter in SLUG_TO_RECITER.items()
}


def _deterministic_ffmpeg_flags() -> tuple[str, ...]:
    return (
        "-threads", "1",
        "-filter_threads", "1",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-fflags", "+bitexact",
        "-flags:a", "+bitexact",
    )


_DECIMAL_TOKEN_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"Duplicate JSON key in loudnorm output: {k!r}")
        d[k] = v
    return d

EXPECTED_SRC_KEYS: frozenset[str] = frozenset({
    "attribution",
    "clipStartMs",
    "name",
    "notice",
    "path",
    "permittedUses",
    "reciter",
    "rightsEvidence",
    "slug",
    "sourceUrl",
    "sourceVersion",
})

_VALID_PERMITTED_USES: frozenset[str] = frozenset({
    "public_redistribution",
    "commercial_use",
    "sqlite_conversion",
    "search_projection",
    "transcode",
    "clip",
    "fade",
    "loudness_normalization",
})


def _is_link_or_junction(p: Path | str) -> bool:
    if os.path.islink(p):
        return True
    if sys.platform == "win32" and hasattr(os.path, "isjunction") and os.path.isjunction(p):
        return True
    return False


def load_adhan_license(sources_root: Path) -> dict[str, Any]:
    """Validate and return sources/licenses/audio-adhan-core.json against locked inputs."""
    sources_root = Path(sources_root)
    registry_path = sources_root / "sources" / "registry.json"
    raw_root = sources_root / "raw-inputs"
    lock_path = sources_root / "sources" / "manifest.lock.json"
    lock_entries = verify_lock(registry_path, raw_root, lock_path, pack_id=PACK_ID)
    return _load_adhan_license(sources_root, lock_entries)


def _load_adhan_license(
    sources_root: Path,
    lock_entries: tuple[LockEntry, ...],
) -> dict[str, Any]:
    """Validate and return sources/licenses/audio-adhan-core.json using verified lock entries."""
    sources_root = Path(sources_root)
    locked_paths = tuple(e.path for e in lock_entries)
    if locked_paths != ORDERED_PATHS:
        raise ValueError(
            f"Lock entries for {PACK_ID} must match exact expected paths {list(ORDERED_PATHS)}, got {list(locked_paths)}"
        )

    rel_lic_path = f"sources/licenses/{PACK_ID}.json"
    verify_exact_file_on_disk(sources_root, rel_lic_path)
    abs_lic_path = sources_root / rel_lic_path

    raw_lic_bytes = abs_lic_path.read_bytes()
    try:
        lic_text = raw_lic_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"License file {rel_lic_path} is not valid UTF-8: {exc}") from exc

    lic_data: dict[str, Any] = parse_strict_json(lic_text)
    if not isinstance(lic_data, dict):
        raise ValueError(f"License root must be a JSON object: {rel_lic_path}")

    expected_lic_keys = {"packId", "licenseSpdx", "primaryPath", "status", "reviewedBy", "sources"}
    if set(lic_data.keys()) != expected_lic_keys:
        raise ValueError(
            f"License file {rel_lic_path} keys mismatch: expected {sorted(expected_lic_keys)}, got {sorted(lic_data.keys())}"
        )

    if lic_data["packId"] != PACK_ID:
        raise ValueError(f"packId mismatch in {rel_lic_path}: expected {PACK_ID!r}, got {lic_data['packId']!r}")

    lic_spdx = lic_data["licenseSpdx"]
    if lic_spdx != "LicenseRef-Adhan-Recordings":
        raise ValueError(
            f"licenseSpdx must be 'LicenseRef-Adhan-Recordings' in {rel_lic_path}, got {lic_spdx!r}"
        )

    primary_path = lic_data["primaryPath"]
    if primary_path != "audio/adhan/abdul-basit-abdul-samad.mp3":
        raise ValueError(
            f"primaryPath must be 'audio/adhan/abdul-basit-abdul-samad.mp3', got {primary_path!r}"
        )

    status = lic_data["status"]
    if status not in ("approved", "pending"):
        raise ValueError(f"License status must be 'approved' or 'pending', got {status!r}")

    reviewed_by = lic_data["reviewedBy"]
    if status == "pending":
        if reviewed_by is not None:
            raise ValueError(f"reviewedBy must be null when status is 'pending' in {rel_lic_path}")
    elif status == "approved":
        if not isinstance(reviewed_by, str) or not reviewed_by.strip():
            raise ValueError(f"reviewedBy must be non-empty string when status is 'approved' in {rel_lic_path}")

    sources_list = lic_data["sources"]
    if not isinstance(sources_list, list):
        raise ValueError(f"sources must be a list in {rel_lic_path}")

    actual_paths = [s.get("path") for s in sources_list if isinstance(s, dict)]
    if actual_paths != list(ORDERED_PATHS):
        raise ValueError(
            f"License sources list must be sorted and match expected locked paths: expected {list(ORDERED_PATHS)}, got {actual_paths}"
        )

    lock_by_path = {e.path: e for e in lock_entries}

    for src in sources_list:
        if not isinstance(src, dict):
            raise ValueError(f"Source entry must be an object in {rel_lic_path}")
        if set(src.keys()) != EXPECTED_SRC_KEYS:
            raise ValueError(
                f"Source keys mismatch in {rel_lic_path} for {src.get('path')}: got {sorted(src.keys())}"
            )

        p = src["path"]
        if p not in lock_by_path:
            raise ValueError(f"Source path {p!r} not found in verified lock")
        lock_e = lock_by_path[p]

        if src["sourceUrl"] != lock_e.source_url:
            raise ValueError(
                f"sourceUrl mismatch for {p}: license has {src['sourceUrl']!r}, lock has {lock_e.source_url!r}"
            )
        if src["sourceVersion"] != lock_e.source_version:
            raise ValueError(
                f"sourceVersion mismatch for {p}: license has {src['sourceVersion']!r}, lock has {lock_e.source_version!r}"
            )

        if not isinstance(src["name"], str) or not src["name"].strip():
            raise ValueError(f"Empty source name in {rel_lic_path} for {p}")
        if src["name"] != NAME_MAP[p]:
            raise ValueError(f"Source name mismatch for {p}: expected {NAME_MAP[p]!r}, got {src['name']!r}")

        if not isinstance(src["slug"], str) or not src["slug"].strip():
            raise ValueError(f"Empty slug in {rel_lic_path} for {p}")
        if src["slug"] != SLUG_MAP[p]:
            raise ValueError(f"Slug mismatch for {p}: expected {SLUG_MAP[p]!r}, got {src['slug']!r}")

        if not isinstance(src["reciter"], str) or not src["reciter"].strip():
            raise ValueError(f"Empty reciter in {rel_lic_path} for {p}")
        if src["reciter"] != RECITER_MAP[p]:
            raise ValueError(f"Reciter mismatch for {p}: expected {RECITER_MAP[p]!r}, got {src['reciter']!r}")

        clip_start = src["clipStartMs"]
        if type(clip_start) is not int or isinstance(clip_start, bool) or clip_start < 0:
            raise ValueError(
                f"clipStartMs must be a non-negative integer JSON token (not bool) in {rel_lic_path} for {p}, got {clip_start!r}"
            )

        validate_https_url(src["sourceUrl"])
        if not isinstance(src["sourceVersion"], str) or not src["sourceVersion"].strip():
            raise ValueError(f"Empty sourceVersion in {rel_lic_path} for {p}")

        if not isinstance(src["notice"], str):
            raise ValueError(f"notice must be string in {rel_lic_path} for {p}")
        if not isinstance(src["attribution"], str):
            raise ValueError(f"attribution must be string in {rel_lic_path} for {p}")

        if status == "approved":
            if not src["notice"].strip():
                raise ValueError(f"notice must be non-empty string in approved {rel_lic_path} for {p}")
            if not src["attribution"].strip():
                raise ValueError(f"attribution must be non-empty string in approved {rel_lic_path} for {p}")

        if not isinstance(src["rightsEvidence"], list):
            raise ValueError(f"rightsEvidence must be a list in {rel_lic_path} for {p}")
        for ev in src["rightsEvidence"]:
            if not isinstance(ev, str):
                raise ValueError(
                    f"rightsEvidence entries must be strings in {rel_lic_path} for {p}, got {type(ev).__name__}"
                )
            if not ev.strip():
                raise ValueError(f"Invalid rightsEvidence entry in {rel_lic_path} for {p}")
            if ev.startswith("https://"):
                validate_https_url(ev)
            elif ev.startswith("sources/licenses/evidence/"):
                validate_safe_relative_path(ev)
                verify_exact_file_on_disk(sources_root, ev)
            else:
                raise ValueError(
                    f"rightsEvidence entry must be HTTPS URL or exact-case path under sources/licenses/evidence/: {ev!r}"
                )
        if len(src["rightsEvidence"]) != len(set(src["rightsEvidence"])):
            raise ValueError(f"rightsEvidence contains duplicates in {rel_lic_path} for {p}")

        if status == "approved" and not src["rightsEvidence"]:
            raise ValueError(f"rightsEvidence cannot be empty in approved {rel_lic_path} for {p}")

        if not isinstance(src["permittedUses"], list):
            raise ValueError(f"permittedUses must be a list in {rel_lic_path} for {p}")
        for u in src["permittedUses"]:
            if not isinstance(u, str):
                raise ValueError(
                    f"permittedUses entries must be strings in {rel_lic_path} for {p}, got {type(u).__name__}"
                )
            if u not in _VALID_PERMITTED_USES:
                raise ValueError(f"Invalid permittedUses entry {u!r} in {rel_lic_path} for {p}")
        if len(src["permittedUses"]) != len(set(src["permittedUses"])):
            raise ValueError(f"permittedUses contains duplicates in {rel_lic_path} for {p}")

        if status == "pending" and src["permittedUses"]:
            raise ValueError(f"Pending license record cannot grant permittedUses in {rel_lic_path} for {p}")
        if status == "approved" and not src["permittedUses"]:
            raise ValueError(f"Empty permittedUses in approved {rel_lic_path} for {p}")

    return lic_data


def _probe_raw_master(ffprobe_path: Path, master_path: Path) -> None:
    """Probe raw input master: check positive finite bitrate, sample rate, duration."""
    cmd = [
        str(ffprobe_path),
        "-v", "error",
        "-of", "json",
        "-show_format",
        "-show_streams",
        str(master_path),
    ]
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {master_path} (exit code {proc.returncode}): {proc.stderr}")

    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError(f"Failed parsing ffprobe output for {master_path}: {exc}") from exc

    streams = data.get("streams", [])
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise ValueError(f"No audio stream found in {master_path}")

    stream = audio_streams[0]
    format_info = data.get("format", {})

    try:
        sample_rate = int(stream.get("sample_rate", 0))
    except (ValueError, TypeError):
        sample_rate = 0
    if sample_rate <= 0:
        raise ValueError(f"Nonpositive sample_rate {sample_rate} in {master_path}")

    # Bitrate from stream or format
    raw_bitrate = stream.get("bit_rate") or format_info.get("bit_rate")
    if raw_bitrate is None:
        raise ValueError(f"Missing bit_rate in {master_path}")
    try:
        bit_rate = float(raw_bitrate)
    except (ValueError, TypeError):
        bit_rate = 0.0
    if not math.isfinite(bit_rate) or bit_rate <= 0:
        raise ValueError(f"Invalid or nonpositive bit_rate {raw_bitrate!r} in {master_path}")

    # Duration from stream or format
    raw_duration = stream.get("duration") or format_info.get("duration")
    if raw_duration is None:
        raise ValueError(f"Missing duration in {master_path}")
    try:
        duration = float(raw_duration)
    except (ValueError, TypeError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"Invalid or nonpositive duration {raw_duration!r} in {master_path}")


def _measure_loudnorm(ffmpeg_path: Path, master_path: Path) -> dict[str, str]:
    """Execute pass 1 loudnorm measurement and return strict finite metrics."""
    cmd = [
        str(ffmpeg_path),
        "-nostdin",
        "-threads", "1",
        "-i", str(master_path),
        "-map", "0:a:0",
        "-af", "aformat=channel_layouts=mono,loudnorm=I=-16:LRA=7:TP=-1.5:print_format=json",
        *_deterministic_ffmpeg_flags(),
        "-f", "null",
        "-",
    ]
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg loudnorm pass 1 failed on {master_path}: {proc.stderr}")

    stderr = proc.stderr
    start_idx = stderr.rfind("{")
    end_idx = stderr.rfind("}")
    if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
        raise RuntimeError(f"Could not locate loudnorm JSON output in stderr for {master_path}")

    json_str = stderr[start_idx : end_idx + 1]
    try:
        metrics = json.loads(json_str, object_pairs_hook=_reject_duplicate_keys)
    except Exception as exc:
        raise ValueError(f"Failed parsing loudnorm JSON for {master_path}: {exc}") from exc

    required_keys = ("input_i", "input_lra", "input_tp", "input_thresh", "target_offset")
    parsed: dict[str, str] = {}
    for k in required_keys:
        if k not in metrics:
            raise ValueError(f"Missing loudnorm measurement {k!r} for {master_path}")
        raw_val = metrics[k]
        val_str = str(raw_val)
        if not _DECIMAL_TOKEN_RE.match(val_str):
            raise ValueError(
                f"Invalid decimal token for loudnorm measurement {k}={val_str!r} for {master_path}"
            )
        try:
            val_float = float(val_str)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Non-numeric loudnorm measurement {k}={val_str!r} for {master_path}") from exc
        if not math.isfinite(val_float):
            raise ValueError(f"Non-finite loudnorm measurement {k}={val_str!r} for {master_path}")
        parsed[k] = val_str

    return parsed


def build_audio_adhan(sources_root: Path, output_dir: Path) -> None:
    """Build deterministic Adhan audio assets."""
    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    # 1. Pinned runtime verification before any writes
    ffmpeg_path, ffprobe_path = verify_runtime()

    # 2. License and locked inputs validation
    lic_data = load_adhan_license(sources_root)
    sources = lic_data["sources"]

    # 3. Check for preexisting output files before encoding
    for src in sources:
        slug = src["slug"]
        full_dest = output_dir / f"adhan-{slug}-full.m4a"
        ios_dest = output_dir / f"adhan-{slug}-ios-notif.caf"
        if _is_link_or_junction(full_dest) or os.path.lexists(full_dest) or full_dest.exists():
            raise FileExistsError(f"Output asset or link already exists: {full_dest}")
        if _is_link_or_junction(ios_dest) or os.path.lexists(ios_dest) or ios_dest.exists():
            raise FileExistsError(f"Output asset or link already exists: {ios_dest}")

    # 4. Stage on destination filesystem in private temporary storage
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_dir, prefix=".tmp_adhan_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        candidate_assets: list[tuple[Path, Path]] = []

        for src in sources:
            rel_path = src["path"]
            slug = src["slug"]
            clip_start_ms = src["clipStartMs"]
            master_path = sources_root / "raw-inputs" / rel_path

            # A. Probe raw master
            _probe_raw_master(ffprobe_path, master_path)

            # B. Two-pass Loudnorm (Pass 1 mono measurement)
            measurements = _measure_loudnorm(ffmpeg_path, master_path)

            # C. Render single lossless normalized temporary signal (WAV mono PCM 16-bit)
            norm_wav = tmp_dir / f"{slug}_norm.wav"
            af_norm = (
                "aformat=channel_layouts=mono,"
                "loudnorm=I=-16:LRA=7:TP=-1.5:"
                f"measured_I={measurements['input_i']}:"
                f"measured_LRA={measurements['input_lra']}:"
                f"measured_TP={measurements['input_tp']}:"
                f"measured_thresh={measurements['input_thresh']}:"
                f"offset={measurements['target_offset']}:linear=true"
            )
            cmd_norm = [
                str(ffmpeg_path),
                "-y",
                "-nostdin",
                "-threads", "1",
                "-i", str(master_path),
                "-map", "0:a:0",
                "-af", af_norm,
                "-c:a", "pcm_s16le",
                *_deterministic_ffmpeg_flags(),
                str(norm_wav),
            ]
            subprocess.run(
                cmd_norm,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                shell=False,
            )

            # D. Render Full Adhan (M4A / AAC-LC mono 44.1 kHz 128 kbps)
            temp_full_m4a = tmp_dir / f"adhan-{slug}-full.m4a"
            cmd_full = [
                str(ffmpeg_path),
                "-y",
                "-nostdin",
                "-threads", "1",
                "-i", str(norm_wav),
                "-map", "0:a:0",
                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "128k",
                "-ar", "44100",
                *_deterministic_ffmpeg_flags(),
                str(temp_full_m4a),
            ]
            subprocess.run(
                cmd_full,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                shell=False,
            )

            # E. Render iOS Notification Clip (CAF / ima4 mono 32 kHz)
            # Resample normalized signal to 32 kHz mono
            resampled_32k = tmp_dir / f"{slug}_32k.wav"
            cmd_resample = [
                str(ffmpeg_path),
                "-y",
                "-nostdin",
                "-threads", "1",
                "-i", str(norm_wav),
                "-map", "0:a:0",
                "-ar", "32000",
                "-c:a", "pcm_s16le",
                *_deterministic_ffmpeg_flags(),
                str(resampled_32k),
            ]
            subprocess.run(
                cmd_resample,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                shell=False,
            )

            with wave.open(str(resampled_32k), "rb") as wf:
                total_samples = wf.getnframes()

            start_sample = clip_start_ms * 32
            if start_sample >= total_samples:
                raise ValueError(
                    f"clipStartMs {clip_start_ms} is at or beyond EOF in {master_path} "
                    f"(start sample {start_sample} >= total samples {total_samples})"
                )

            retained_samples = min(912000, total_samples - start_sample)
            fade_samples = min(48000, retained_samples)
            fade_start_sample = retained_samples - fade_samples

            temp_ios_caf = tmp_dir / f"adhan-{slug}-ios-notif.caf"
            filter_chain = (
                f"atrim=start_sample={start_sample}:end_sample={start_sample + retained_samples},"
                f"asetpts=PTS-STARTPTS,"
                f"afade=t=out:ss={fade_start_sample}:ns={fade_samples}"
            )
            cmd_caf = [
                str(ffmpeg_path),
                "-y",
                "-nostdin",
                "-threads", "1",
                "-i", str(resampled_32k),
                "-map", "0:a:0",
                "-af", filter_chain,
                "-c:a", "adpcm_ima_qt",
                "-ar", "32000",
                *_deterministic_ffmpeg_flags(),
                str(temp_ios_caf),
            ]
            subprocess.run(
                cmd_caf,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                shell=False,
            )

            # F. Probe both renditions before publishing
            _, full_duration = probe_asset(temp_full_m4a, variant="full")
            if full_duration <= 0:
                raise ValueError(f"Nonpositive full duration {full_duration} in {temp_full_m4a}")

            _, ios_duration = probe_asset(temp_ios_caf, variant="ios")
            if not (0 < ios_duration < 29):
                raise ValueError(
                    f"iOS notification clip duration must be strictly < 29s, got {ios_duration} in {temp_ios_caf}"
                )

            final_full = output_dir / f"adhan-{slug}-full.m4a"
            final_ios = output_dir / f"adhan-{slug}-ios-notif.caf"
            candidate_assets.append((temp_full_m4a, final_full))
            candidate_assets.append((temp_ios_caf, final_ios))

        # 5. Atomic no-replace output placement via os.link and rollback tracking
        placed_assets: list[tuple[Path, Path]] = []
        try:
            for staged_file, dest_file in candidate_assets:
                if _is_link_or_junction(dest_file) or os.path.lexists(dest_file) or dest_file.exists():
                    raise FileExistsError(f"Output asset or link already exists: {dest_file}")
                os.link(staged_file, dest_file)
                placed_assets.append((staged_file, dest_file))
        except Exception:
            for staged_file, dest_file in placed_assets:
                try:
                    if os.path.lexists(dest_file) and os.path.samefile(staged_file, dest_file):
                        dest_file.unlink()
                except OSError:
                    pass
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build audio-adhan-core audio assets.")
    parser.add_argument(
        "--sources-root",
        required=True,
        type=Path,
        help="Root directory containing sources/ and raw-inputs/",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Output directory where 12 audio files will be written",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_audio_adhan(args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building audio-adhan: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
