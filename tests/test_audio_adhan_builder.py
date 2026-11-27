"""Tests for audio-adhan-core builder and license loader."""

from __future__ import annotations

from fractions import Fraction
import json
import hashlib
import math
import os
from pathlib import Path
import struct
import subprocess
import wave
import sys
import pytest

from pipelines.common.audio_probe import probe_asset
from pipelines.common.lock_inputs import author_lock
from pipelines.builders.build_audio_adhan import (
    build_audio_adhan,
    load_adhan_license,
    main,
)

ORDERED_PATHS = (
    "audio/adhan/abdul-basit-abdul-samad-2.mp3",
    "audio/adhan/abdul-basit-abdul-samad.mp3",
    "audio/adhan/abul-ainain-shuaisha.mp3",
    "audio/adhan/mahmoud-khalil-al-hussary.mp3",
    "audio/adhan/mohamed-refaat.mp3",
    "audio/adhan/mustafa-ismail.mp3",
)

SLUG_MAP = {
    "audio/adhan/abdul-basit-abdul-samad-2.mp3": "abdul-basit-abdul-samad-2",
    "audio/adhan/abdul-basit-abdul-samad.mp3": "abdul-basit-abdul-samad",
    "audio/adhan/abul-ainain-shuaisha.mp3": "abul-ainain-shuaisha",
    "audio/adhan/mahmoud-khalil-al-hussary.mp3": "mahmoud-khalil-al-hussary",
    "audio/adhan/mohamed-refaat.mp3": "mohamed-refaat",
    "audio/adhan/mustafa-ismail.mp3": "mustafa-ismail",
}

RECITER_MAP = {
    "audio/adhan/abdul-basit-abdul-samad-2.mp3": "Abdul Basit Abdul Samad",
    "audio/adhan/abdul-basit-abdul-samad.mp3": "Abdul Basit Abdul Samad",
    "audio/adhan/abul-ainain-shuaisha.mp3": "Abul Ainain Shu’aisha",
    "audio/adhan/mahmoud-khalil-al-hussary.mp3": "Mahmoud Khalil Al-Hussary",
    "audio/adhan/mohamed-refaat.mp3": "Mohamed Refaat",
    "audio/adhan/mustafa-ismail.mp3": "Mustafa Ismail",
}

NAME_MAP = {
    "audio/adhan/abdul-basit-abdul-samad-2.mp3": "Adhan — Abdul Basit Abdul Samad (Fajr)",
    "audio/adhan/abdul-basit-abdul-samad.mp3": "Adhan — Abdul Basit Abdul Samad",
    "audio/adhan/abul-ainain-shuaisha.mp3": "Adhan — Abul Ainain Shu’aisha",
    "audio/adhan/mahmoud-khalil-al-hussary.mp3": "Adhan — Mahmoud Khalil Al-Hussary",
    "audio/adhan/mohamed-refaat.mp3": "Adhan — Mohamed Refaat",
    "audio/adhan/mustafa-ismail.mp3": "Adhan — Mustafa Ismail",
}

URL_MAP = {
    "audio/adhan/abdul-basit-abdul-samad-2.mp3": "https://www.assabile.com/download/adhan/140",
    "audio/adhan/abdul-basit-abdul-samad.mp3": "https://www.assabile.com/download/adhan/138",
    "audio/adhan/abul-ainain-shuaisha.mp3": "https://www.assabile.com/download/adhan/145",
    "audio/adhan/mahmoud-khalil-al-hussary.mp3": "https://www.assabile.com/download/adhan/142",
    "audio/adhan/mohamed-refaat.mp3": "https://www.assabile.com/download/adhan/149",
    "audio/adhan/mustafa-ismail.mp3": "https://www.assabile.com/download/adhan/141",
}

VERSION_MAP = {
    "audio/adhan/abdul-basit-abdul-samad-2.mp3": "Assabile recording 140",
    "audio/adhan/abdul-basit-abdul-samad.mp3": "Assabile recording 138",
    "audio/adhan/abul-ainain-shuaisha.mp3": "Assabile recording 145",
    "audio/adhan/mahmoud-khalil-al-hussary.mp3": "Assabile recording 142",
    "audio/adhan/mohamed-refaat.mp3": "Assabile recording 149",
    "audio/adhan/mustafa-ismail.mp3": "Assabile recording 141",
}


def _generate_synthetic_tone(
    dest_path: Path,
    *,
    duration_s: float = 2.0,
    sample_rate: int = 32000,
    freq: int = 1000,
    channels: int = 1,
    channel_scales: tuple[float, ...] = (1.0,),
    segments: list[tuple[float, int]] | None = None,
) -> None:
    """Generate deterministic 16-bit LE PCM audio."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if segments is None:
        segments = [(duration_s, freq)]

    samples_left: list[int] = []
    samples_right: list[int] = []
    scale_l = channel_scales[0] if len(channel_scales) > 0 else 1.0
    scale_r = channel_scales[1] if len(channel_scales) > 1 else 0.0

    for seg_dur, seg_freq in segments:
        seg_samples = int(seg_dur * sample_rate)
        for i in range(seg_samples):
            val = int(8000 * math.sin(2 * math.pi * seg_freq * i / sample_rate))
            samples_left.append(int(val * scale_l))
            if channels == 2:
                samples_right.append(int(val * scale_r))

    total_samples = len(samples_left)
    if channels == 2:
        raw_frames = bytearray()
        for l_val, r_val in zip(samples_left, samples_right):
            raw_frames.extend(struct.pack("<2h", l_val, r_val))
        raw_data = bytes(raw_frames)
    else:
        raw_data = struct.pack(f"<{total_samples}h", *samples_left)

    with wave.open(str(dest_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw_data)

def _setup_synthetic_adhan_env(
    root: Path,
    *,
    clip_starts: dict[str, Any] | None = None,
    license_status: str = "pending",
    reviewed_by: str | None = None,
    corrupt_audio_path: str | None = None,
    durations: dict[str, float] | None = None,
    custom_generators: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    sources_root = root
    raw_root = root / "raw-inputs"
    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    if clip_starts is None:
        clip_starts = {p: 0 for p in ORDERED_PATHS}
    if durations is None:
        durations = {p: 2.0 for p in ORDERED_PATHS}

    registry_data: dict[str, Any] = {}
    for p in ORDERED_PATHS:
        dest = raw_root / p
        if corrupt_audio_path == p:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"NOT_A_VALID_AUDIO_FILE")
        elif custom_generators and p in custom_generators:
            dest.parent.mkdir(parents=True, exist_ok=True)
            custom_generators[p](dest)
        else:
            _generate_synthetic_tone(dest, duration_s=durations.get(p, 2.0))
        registry_data[p] = {
            "packIds": ["audio-adhan-core"],
            "sourceUrl": URL_MAP[p],
            "sourceVersion": VERSION_MAP[p],
        }

    sources_dir = root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    reg_path = sources_dir / "registry.json"
    reg_path.write_text(json.dumps(registry_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lock_path = sources_dir / "manifest.lock.json"
    author_lock(reg_path, raw_root, lock_path)

    sources_list = []
    for p in ORDERED_PATHS:
        src_obj = {
            "attribution": "",
            "clipStartMs": clip_starts[p],
            "name": NAME_MAP[p],
            "notice": "",
            "path": p,
            "permittedUses": [],
            "reciter": RECITER_MAP[p],
            "rightsEvidence": [],
            "slug": SLUG_MAP[p],
            "sourceUrl": URL_MAP[p],
            "sourceVersion": VERSION_MAP[p],
        }
        sources_list.append(src_obj)

    lic_data = {
        "licenseSpdx": "LicenseRef-Adhan-Recordings",
        "packId": "audio-adhan-core",
        "primaryPath": "audio/adhan/abdul-basit-abdul-samad.mp3",
        "reviewedBy": reviewed_by,
        "sources": sources_list,
        "status": license_status,
    }

    lic_dir = sources_dir / "licenses"
    lic_dir.mkdir(parents=True, exist_ok=True)
    lic_path = lic_dir / "audio-adhan-core.json"
    lic_path.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return sources_root, out_dir


def test_near_eof_clip_duration_fade_and_no_pad(tmp_path: Path) -> None:
    """Test nonzero near-EOF clipStartMs: check exact clip duration, terminal fade, and no pad."""
    near_eof_path = "audio/adhan/abdul-basit-abdul-samad-2.mp3"
    clip_starts = {p: 0 for p in ORDERED_PATHS}
    clip_starts[near_eof_path] = 1900

    custom_gen = {
        near_eof_path: lambda p: _generate_synthetic_tone(
            p,
            duration_s=2.0,
            sample_rate=32000,
            segments=[(1.9, 220), (0.1, 880)],
        )
    }

    sources_root, out_dir = _setup_synthetic_adhan_env(
        tmp_path, clip_starts=clip_starts, custom_generators=custom_gen
    )

    build_audio_adhan(sources_root, out_dir)

    # Verify all 12 assets exist
    for p in ORDERED_PATHS:
        slug = SLUG_MAP[p]
        m4a = out_dir / f"adhan-{slug}-full.m4a"
        caf = out_dir / f"adhan-{slug}-ios-notif.caf"
        assert m4a.is_file(), f"Missing {m4a}"
        assert caf.is_file(), f"Missing {caf}"

    target_caf = out_dir / "adhan-abdul-basit-abdul-samad-2-ios-notif.caf"
    codec, duration = probe_asset(target_caf, variant="ios")
    assert codec == "adpcm_ima_qt"
    assert duration == Fraction(1, 10), f"Expected exactly 1/10s duration, got {duration}"
    assert float(duration) < 29.0

    from pipelines.toolchain.check_runtime import verify_runtime
    ffmpeg_path, _ = verify_runtime()

    decoded_wav = tmp_path / "decoded.wav"
    cmd_decode = [
        str(ffmpeg_path),
        "-y",
        "-nostdin",
        "-threads", "1",
        "-i", str(target_caf),
        "-c:a", "pcm_s16le",
        "-ar", "32000",
        str(decoded_wav),
    ]
    subprocess.run(cmd_decode, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    with wave.open(str(decoded_wav), "rb") as wf:
        nframes = wf.getnframes()
        assert nframes == 3200, f"Expected exactly 3200 frames (no padding), got {nframes}"
        pcm_bytes = wf.readframes(nframes)
        samples = struct.unpack(f"<{nframes}h", pcm_bytes)

    # 1. Prove selected source window/time:
    # 880 Hz at 32kHz in first 1600 samples produces ~88 zero crossings, whereas 220 Hz produces ~22.
    zc_count = sum(
        1 for i in range(1, 1600)
        if (samples[i - 1] >= 0 > samples[i]) or (samples[i - 1] <= 0 < samples[i])
    )
    assert zc_count > 60, f"Expected >60 zero crossings for 880 Hz tone from late source window, got {zc_count}"

    # 2. Prove length and progression of linear fade:
    early_window = [abs(s) for s in samples[:400]]
    mid_window = [abs(s) for s in samples[1400:1800]]
    last_window = [abs(s) for s in samples[2800:3200]]

    mean_early = sum(early_window) / len(early_window)
    mean_mid = sum(mid_window) / len(mid_window)
    mean_last = sum(last_window) / len(last_window)

    assert 1.4 < (mean_early / mean_mid) < 2.5, f"Expected early/mid ratio ~1.88, got {mean_early / mean_mid}"
    assert (mean_mid / mean_last) > 3.5, f"Expected mid/last ratio > 3.5, got {mean_mid / mean_last}"
    assert (mean_last / mean_early) < 0.15, f"Expected last window < 15% of early, got {mean_last / mean_early}"


def test_start_at_or_beyond_eof_rejected(tmp_path: Path) -> None:
    """Test start at or beyond EOF is rejected."""
    # Master is 2.0s = 2000ms. Start at 2000ms is at EOF.
    clip_starts = {p: 0 for p in ORDERED_PATHS}
    clip_starts["audio/adhan/abdul-basit-abdul-samad.mp3"] = 2000

    sources_root, out_dir = _setup_synthetic_adhan_env(tmp_path, clip_starts=clip_starts)

    with pytest.raises((ValueError, RuntimeError), match=r"(?i)EOF"):
        build_audio_adhan(sources_root, out_dir)


def test_invalid_clip_start_tokens(tmp_path: Path) -> None:
    """Test that non-integer tokens, bools, and negative values are rejected."""
    sources_root, out_dir = _setup_synthetic_adhan_env(tmp_path)
    lic_file = sources_root / "sources" / "licenses" / "audio-adhan-core.json"
    lic_data = json.loads(lic_file.read_text(encoding="utf-8"))

    invalid_tokens = ["0", True, False, -1, 1.5, None, "100"]
    for bad_token in invalid_tokens:
        lic_data["sources"][0]["clipStartMs"] = bad_token
        lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_adhan_license(sources_root)


def test_existing_output_untouched(tmp_path: Path) -> None:
    """Test that preexisting output files cause build refusal and remain untouched."""
    sources_root, out_dir = _setup_synthetic_adhan_env(tmp_path)
    preexisting_file = out_dir / "adhan-abdul-basit-abdul-samad-full.m4a"
    sentinel = b"PREEXISTING_ORIGINAL_CONTENT"
    preexisting_file.write_bytes(sentinel)

    with pytest.raises(FileExistsError):
        build_audio_adhan(sources_root, out_dir)

    assert preexisting_file.read_bytes() == sentinel
    # Ensure no other files were created
    assert list(out_dir.iterdir()) == [preexisting_file]


def test_no_candidate_assets_on_failure(tmp_path: Path) -> None:
    """Test that a failure during probe/processing leaves no output files in output_dir."""
    corrupt_path = "audio/adhan/mustafa-ismail.mp3"
    sources_root, out_dir = _setup_synthetic_adhan_env(
        tmp_path, corrupt_audio_path=corrupt_path
    )

    with pytest.raises(Exception):
        build_audio_adhan(sources_root, out_dir)

    assert list(out_dir.iterdir()) == []


def test_load_adhan_license_validation(tmp_path: Path) -> None:
    """Test strict validation rules of load_adhan_license."""
    sources_root, out_dir = _setup_synthetic_adhan_env(tmp_path)

    # Valid pending record loads cleanly
    data = load_adhan_license(sources_root)
    assert data["packId"] == "audio-adhan-core"
    assert data["status"] == "pending"
    assert data["reviewedBy"] is None

    lic_file = sources_root / "sources" / "licenses" / "audio-adhan-core.json"

    # Reject fixture status
    lic_data = json.loads(lic_file.read_text(encoding="utf-8"))
    lic_data["status"] = "fixture"
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?i)fixture"):
        load_adhan_license(sources_root)

    # Reject non-null reviewedBy when pending
    lic_data["status"] = "pending"
    lic_data["reviewedBy"] = "Reviewer Name"
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?i)reviewedBy"):
        load_adhan_license(sources_root)

    # Reject permittedUses when pending
    lic_data["reviewedBy"] = None
    lic_data["sources"][0]["permittedUses"] = ["transcode"]
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?i)permittedUses"):
        load_adhan_license(sources_root)


def test_cli_main(tmp_path: Path) -> None:
    """Test CLI exit codes and behavior."""
    sources_root, out_dir = _setup_synthetic_adhan_env(tmp_path)

    rc = main(["--sources-root", str(sources_root), "--output-dir", str(out_dir)])
    assert rc == 0

    # Preexisting output causes CLI exit code 1
    rc_fail = main(["--sources-root", str(sources_root), "--output-dir", str(out_dir)])
    assert rc_fail == 1

    # Bad arguments cause exit code 2
    rc_args = main(["--unknown-flag"])
    assert rc_args == 2

def test_stereo_mono_downmix_loudnorm_order(tmp_path: Path) -> None:
    """Test that stereo input is downmixed to mono BEFORE loudnorm passes, producing compliant loudness."""
    from pipelines.toolchain.check_runtime import verify_runtime
    ffmpeg_path, _ = verify_runtime()

    stereo_path = "audio/adhan/abdul-basit-abdul-samad.mp3"
    custom_gen = {
        stereo_path: lambda p: _generate_synthetic_tone(
            p,
            duration_s=3.0,
            sample_rate=44100,
            freq=440,
            channels=2,
            channel_scales=(1.0, 0.0),
        )
    }

    sources_root, out_dir = _setup_synthetic_adhan_env(
        tmp_path, custom_generators=custom_gen
    )

    build_audio_adhan(sources_root, out_dir)

    target_full = out_dir / "adhan-abdul-basit-abdul-samad-full.m4a"
    assert target_full.is_file()

    proc = subprocess.run(
        [
            str(ffmpeg_path),
            "-nostdin",
            "-threads", "1",
            "-i", str(target_full),
            "-af", "loudnorm=print_format=json",
            "-f", "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stderr = proc.stderr
    out_metrics = json.loads(stderr[stderr.rfind("{") : stderr.rfind("}") + 1])
    measured_output_i = float(out_metrics["input_i"])

    # §7.2 target I = -16.0 LUFS.
    # Downmixing before loudnorm achieves ~ -16.0 LUFS (within 1.0 LUFS).
    # Downmixing after loudnorm (old order) causes ~ -22.0 LUFS, failing this check.
    assert abs(measured_output_i - (-16.0)) < 1.0, (
        f"Output loudness {measured_output_i} LUFS deviated from -16.0 target. "
        "Stereo was measured/normalized before mono downmix!"
    )


def test_publication_boundary_collision_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that concurrent/preexisting collision at publication boundary preserves incumbent and leaves no partial assets."""
    sources_root, out_dir = _setup_synthetic_adhan_env(tmp_path)

    collision_target = out_dir / "adhan-abdul-basit-abdul-samad-2-ios-notif.caf"
    incumbent_bytes = b"INCUMBENT_ORIGINAL_BYTES_MUST_PRESERVE"

    real_link = os.link

    def mock_link(src: str | os.PathLike[str], dst: str | os.PathLike[str], **kwargs: Any) -> None:
        if Path(dst) == collision_target:
            collision_target.write_bytes(incumbent_bytes)
            raise FileExistsError(f"[Errno 17] File exists: {dst}")
        real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", mock_link)

    with pytest.raises(FileExistsError):
        build_audio_adhan(sources_root, out_dir)

    assert collision_target.is_file()
    assert collision_target.read_bytes() == incumbent_bytes
    assert list(out_dir.iterdir()) == [collision_target]


def test_loudnorm_strict_json_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test strict loudnorm JSON parsing: decimal tokens, no duplicate keys, finite numerics."""
    from pipelines.builders.build_audio_adhan import _measure_loudnorm
    from pipelines.toolchain.check_runtime import verify_runtime
    ffmpeg_path, _ = verify_runtime()

    test_audio = tmp_path / "test.wav"
    _generate_synthetic_tone(test_audio)

    def mock_ffmpeg_output(json_content: str) -> None:
        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout="",
                stderr=f"some logs\n{json_content}\nmore logs\n",
            )
        monkeypatch.setattr(subprocess, "run", fake_run)

    # Duplicate key rejection
    mock_ffmpeg_output(
        '{\n"input_i": "-16.0",\n"input_i": "-16.0",\n"input_lra": "7.0",\n"input_tp": "-1.5",\n"input_thresh": "-26.0",\n"target_offset": "0.0"\n}'
    )
    with pytest.raises(ValueError, match=r"(?i)duplicate"):
        _measure_loudnorm(ffmpeg_path, test_audio)

    # Non-decimal token rejection ("nan", "inf", "1e3", " -16.0 ")
    for bad_token in ["nan", "inf", "1e3", " -16.0 ", "NaN", "1.0foo"]:
        mock_ffmpeg_output(
            f'{{\n"input_i": "{bad_token}",\n"input_lra": "7.0",\n"input_tp": "-1.5",\n"input_thresh": "-26.0",\n"target_offset": "0.0"\n}}'
        )
        with pytest.raises(ValueError, match=r"(?i)(token|numeric|loudnorm|decimal)"):
            _measure_loudnorm(ffmpeg_path, test_audio)


def test_local_adhan_stages_and_builds_only_pinned_tracks(tmp_path: Path) -> None:
    sources_root = tmp_path / "repo"
    contant_adhan = sources_root / "Contant" / "adhan"
    contant_adhan.mkdir(parents=True, exist_ok=True)
    sources_dir = sources_root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    sample_file = contant_adhan / "sample_fajr.mp3"
    _generate_synthetic_tone(sample_file, duration_s=2.0)
    sample_bytes = sample_file.read_bytes()
    sample_sha256 = hashlib.sha256(sample_bytes).hexdigest()

    unlisted_file = contant_adhan / "unlisted.mp3"
    _generate_synthetic_tone(unlisted_file, duration_s=2.0)
    unlisted_bytes = unlisted_file.read_bytes()

    intake_json = sources_dir / "adhan-local-intake.json"
    intake_data = {
        "status": "pending",
        "tracks": [
            {
                "basename": "sample_fajr.mp3",
                "reciter": "Sample Reciter",
                "sha256": sample_sha256,
                "slug": "sample-fajr",
            }
        ],
    }
    intake_json.write_text(json.dumps(intake_data, indent=2) + "\n", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pipelines.builders.build_audio_adhan_local",
            "stage",
            "--sources-root",
            str(sources_root),
        ],
        check=True,
        capture_output=True,
    )

    staged_file = sources_root / "raw-inputs" / "audio" / "adhan-local" / "sample_fajr.mp3"
    assert staged_file.is_file()
    assert staged_file.read_bytes() == sample_bytes
    assert not (sources_root / "raw-inputs" / "audio" / "adhan-local" / "unlisted.mp3").exists()

    # Repeat stage: succeeds, does not corrupt or rewrite files
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pipelines.builders.build_audio_adhan_local",
            "stage",
            "--sources-root",
            str(sources_root),
        ],
        check=True,
        capture_output=True,
    )
    assert staged_file.read_bytes() == sample_bytes
    assert sample_file.read_bytes() == sample_bytes
    assert unlisted_file.read_bytes() == unlisted_bytes

    # Rename original source directory away before build
    (sources_root / "Contant").rename(sources_root / "Contant_away")

    out_dir = tmp_path / "out"
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pipelines.builders.build_audio_adhan_local",
            "build",
            "--sources-root",
            str(sources_root),
            "--output-dir",
            str(out_dir),
        ],
        check=True,
        capture_output=True,
    )

    full_asset = out_dir / "adhan-sample-fajr-full.m4a"
    ios_asset = out_dir / "adhan-sample-fajr-ios-notif.caf"
    assert full_asset.is_file()
    assert ios_asset.is_file()

    full_codec, full_dur = probe_asset(full_asset, variant="full")
    assert full_codec == "aac"
    assert full_dur > 0

    ios_codec, ios_dur = probe_asset(ios_asset, variant="ios")
    assert ios_codec == "adpcm_ima_qt"
    assert 0 < ios_dur < 29

    assert (sources_root / "catalog").exists() is False


def test_local_adhan_rejects_changed_staged_master(tmp_path: Path) -> None:
    sources_root = tmp_path / "repo"
    contant_adhan = sources_root / "Contant" / "adhan"
    contant_adhan.mkdir(parents=True, exist_ok=True)
    sources_dir = sources_root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)

    sample_file = contant_adhan / "sample_fajr.mp3"
    _generate_synthetic_tone(sample_file, duration_s=2.0)
    sample_bytes = sample_file.read_bytes()
    sample_sha256 = hashlib.sha256(sample_bytes).hexdigest()

    intake_json = sources_dir / "adhan-local-intake.json"
    intake_data = {
        "status": "pending",
        "tracks": [
            {
                "basename": "sample_fajr.mp3",
                "reciter": "Sample Reciter",
                "sha256": sample_sha256,
                "slug": "sample-fajr",
            }
        ],
    }
    intake_json.write_text(json.dumps(intake_data, indent=2) + "\n", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pipelines.builders.build_audio_adhan_local",
            "stage",
            "--sources-root",
            str(sources_root),
        ],
        check=True,
        capture_output=True,
    )

    staged_file = sources_root / "raw-inputs" / "audio" / "adhan-local" / "sample_fajr.mp3"
    assert staged_file.is_file()

    # Mutate the staged copy
    staged_file.write_bytes(b"MUTATED")

    # Re-running stage fails with nonzero exit status and does not overwrite mutated file
    res_stage = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pipelines.builders.build_audio_adhan_local",
            "stage",
            "--sources-root",
            str(sources_root),
        ],
        capture_output=True,
    )
    assert res_stage.returncode != 0
    assert staged_file.read_bytes() == b"MUTATED"

    # Running build fails with exit code 1, sample_fajr.mp3 mentioned in stderr, no output dir
    out_dir = tmp_path / "out"
    res_build = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pipelines.builders.build_audio_adhan_local",
            "build",
            "--sources-root",
            str(sources_root),
            "--output-dir",
            str(out_dir),
        ],
        capture_output=True,
    )
    assert res_build.returncode == 1
    stderr_text = res_build.stderr.decode("utf-8", errors="replace")
    assert "sample_fajr.mp3" in stderr_text
    assert not out_dir.exists()
