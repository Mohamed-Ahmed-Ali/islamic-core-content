"""Standardized audio probe utility using pinned ffprobe executable."""

from __future__ import annotations

from fractions import Fraction
import functools
import json
from pathlib import Path
import re
import subprocess
from typing import Literal


@functools.lru_cache(maxsize=1)
def get_pinned_ffprobe() -> Path:
    """Resolve and verify absolute pinned ffprobe binary once from pinned toolchain."""
    from pipelines.toolchain.check_runtime import (
        detect_platform,
        load_expected_identity,
        _resolve_and_check_tool,
    )

    platform_key = detect_platform()
    expected = load_expected_identity(platform_key)
    return _resolve_and_check_tool("ffprobe", expected["ffprobeVersion"])


def probe_asset(
    path: Path,
    *,
    variant: Literal["full", "ios"],
) -> tuple[str, Fraction]:
    """Probe an audio asset using pinned ffprobe and return (codec_name, duration_seconds).

    Requirements:
    - Exactly one stream, mono audio (1 channel).
    - For variant='full': MP4/M4A container ('mp4' in format_name), codec 'aac', 44100 Hz.
    - For variant='ios': CAF container ('caf' in format_name), codec_tag_string 'ima4',
      codec 'adpcm_ima_qt', 32000 Hz.
    - Exact duration computed as integer duration_ts multiplied by rational time_base.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Audio asset not found: {path}")

    ffprobe_path = get_pinned_ffprobe()

    cmd = [
        str(ffprobe_path),
        "-v",
        "error",
        "-of",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
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
        raise RuntimeError(f"ffprobe failed on {path} (exit code {proc.returncode}): {proc.stderr}")

    try:
        data = json.loads(proc.stdout)
    except Exception as exc:
        raise RuntimeError(f"Failed parsing ffprobe output for {path}: {exc}") from exc

    streams = data.get("streams", [])
    if len(streams) != 1:
        raise ValueError(f"Expected exactly 1 stream in {path}, got {len(streams)}")

    stream = streams[0]
    if stream.get("codec_type") != "audio":
        raise ValueError(f"Expected audio stream in {path}, got codec_type {stream.get('codec_type')!r}")

    channels = stream.get("channels")
    if channels != 1:
        raise ValueError(f"Expected mono audio (1 channel) in {path}, got {channels}")

    format_info = data.get("format", {})
    format_name = format_info.get("format_name", "")
    format_names = [f.strip() for f in format_name.split(",") if f.strip()]

    codec_name = stream.get("codec_name")
    if not codec_name:
        raise ValueError(f"Missing codec_name in stream for {path}")

    try:
        sample_rate = int(stream.get("sample_rate", 0))
    except (ValueError, TypeError):
        sample_rate = 0

    if variant == "full":
        if "mp4" not in format_names:
            raise ValueError(f"Expected MP4/M4A container containing 'mp4' for full variant, got {format_name!r}")
        if codec_name != "aac":
            raise ValueError(f"Expected codec 'aac' for full variant, got {codec_name!r}")
        if sample_rate != 44100:
            raise ValueError(f"Expected sample rate 44100 for full variant, got {sample_rate}")
    elif variant == "ios":
        if format_name != "caf":
            raise ValueError(f"Expected exact format_name 'caf' for ios variant, got {format_name!r}")
        codec_tag_string = stream.get("codec_tag_string")
        if codec_tag_string != "ima4":
            raise ValueError(f"Expected codec_tag_string 'ima4' for ios variant, got {codec_tag_string!r}")
        if codec_name != "adpcm_ima_qt":
            raise ValueError(f"Expected codec 'adpcm_ima_qt' for ios variant, got {codec_name!r}")
        if sample_rate != 32000:
            raise ValueError(f"Expected sample rate 32000 for ios variant, got {sample_rate}")
    else:
        raise ValueError(f"Unsupported variant: {variant!r}")

    # Compute duration as integer duration_ts * rational time_base
    duration_ts_raw = stream.get("duration_ts")
    if duration_ts_raw is None:
        raise ValueError(f"Missing duration_ts in stream for {path}")
    if isinstance(duration_ts_raw, bool) or isinstance(duration_ts_raw, float):
        raise ValueError(
            f"duration_ts must be an integer, got {type(duration_ts_raw).__name__} {duration_ts_raw!r} in {path}"
        )
    if isinstance(duration_ts_raw, int):
        duration_ts = duration_ts_raw
    elif isinstance(duration_ts_raw, str):
        if not re.fullmatch(r"^[0-9]+$", duration_ts_raw):
            raise ValueError(f"Invalid duration_ts integer string {duration_ts_raw!r} in {path}")
        duration_ts = int(duration_ts_raw)
    else:
        raise ValueError(f"Invalid duration_ts type {type(duration_ts_raw).__name__} in {path}")
    if duration_ts <= 0:
        raise ValueError(f"Nonpositive duration_ts {duration_ts} in {path}")

    time_base_raw = stream.get("time_base")
    if not time_base_raw:
        raise ValueError(f"Missing time_base in stream for {path}")
    if isinstance(time_base_raw, Fraction):
        if time_base_raw.numerator <= 0 or time_base_raw.denominator <= 0:
            raise ValueError(f"Nonpositive time_base {time_base_raw} in {path}")
        time_base = time_base_raw
    elif isinstance(time_base_raw, str):
        m = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", time_base_raw)
        if not m:
            raise ValueError(
                f"Invalid time_base format {time_base_raw!r} in {path}; expected positive integer rational 'num/den'"
            )
        time_base = Fraction(int(m.group(1)), int(m.group(2)))
    else:
        raise ValueError(
            f"time_base must be a string or Fraction, got {type(time_base_raw).__name__} {time_base_raw!r} in {path}"
        )

    duration_seconds = Fraction(duration_ts) * time_base
    if duration_seconds <= 0:
        raise ValueError(f"Nonpositive calculated duration {duration_seconds} in {path}")

    return str(codec_name), duration_seconds
