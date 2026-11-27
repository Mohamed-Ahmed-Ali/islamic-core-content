"""Local Adhan intake staging and derivative builder."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

from pipelines.builders.build_audio_adhan import _build_adhan_assets
from pipelines.common.lock_inputs import (
    _is_symlink_or_junction,
    parse_strict_json,
    validate_safe_relative_path,
    verify_exact_file_on_disk,
)
from pipelines.common.stage_qul_inputs import _validate_path_hierarchy
from pipelines.toolchain.check_runtime import verify_runtime

_BASENAME_RE = re.compile(r"^[a-z0-9_]+\.mp3$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load_and_validate_intake(sources_root: Path) -> list[dict[str, Any]]:
    """Load and strictly validate sources/adhan-local-intake.json."""
    sources_root = Path(sources_root)
    intake_path = sources_root / "sources" / "adhan-local-intake.json"
    if not intake_path.is_file():
        raise FileNotFoundError(f"Intake inventory file not found: {intake_path}")

    raw_text = intake_path.read_text(encoding="utf-8")
    data = parse_strict_json(raw_text)
    if not isinstance(data, dict):
        raise ValueError(f"Intake root must be an object, got {type(data).__name__}")

    if set(data.keys()) != {"status", "tracks"}:
        raise ValueError(
            f"Intake object keys must be exactly {{'status', 'tracks'}}, got {sorted(data.keys())}"
        )

    if data["status"] != "pending":
        raise ValueError(f"Intake status must be 'pending', got {data['status']!r}")

    tracks = data["tracks"]
    if not isinstance(tracks, list) or len(tracks) == 0:
        raise ValueError("Intake tracks must be a non-empty array")

    seen_basenames_lower: set[str] = set()
    seen_slugs: set[str] = set()
    slug_list: list[str] = []

    for idx, track in enumerate(tracks):
        if not isinstance(track, dict):
            raise ValueError(f"Track at index {idx} must be an object, got {type(track).__name__}")

        if set(track.keys()) != {"basename", "reciter", "sha256", "slug"}:
            raise ValueError(
                f"Track at index {idx} keys must be exactly {{'basename', 'reciter', 'sha256', 'slug'}}, got {sorted(track.keys())}"
            )

        basename = track["basename"]
        reciter = track["reciter"]
        sha256_val = track["sha256"]
        slug = track["slug"]

        if not isinstance(basename, str) or not _BASENAME_RE.match(basename):
            raise ValueError(f"Invalid basename at index {idx}: {basename!r}")
        validate_safe_relative_path(f"audio/adhan-local/{basename}")

        if not isinstance(reciter, str) or not reciter.strip():
            raise ValueError(f"Blank or non-string reciter at index {idx}: {reciter!r}")

        if not isinstance(sha256_val, str) or not _SHA256_RE.match(sha256_val):
            raise ValueError(f"Invalid sha256 hex digest at index {idx}: {sha256_val!r}")

        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            raise ValueError(f"Invalid slug at index {idx}: {slug!r}")

        lower_b = basename.lower()
        if lower_b in seen_basenames_lower:
            raise ValueError(f"Duplicate basename (case-insensitive) at index {idx}: {basename!r}")
        seen_basenames_lower.add(lower_b)

        if slug in seen_slugs:
            raise ValueError(f"Duplicate slug at index {idx}: {slug!r}")
        seen_slugs.add(slug)

        slug_list.append(slug)

    if slug_list != sorted(slug_list):
        raise ValueError("Intake tracks must be strictly sorted alphabetically by slug")

    return tracks


def stage_audio_adhan_local(sources_root: Path) -> None:
    """Stage local Adhan masters into raw-inputs/audio/adhan-local."""
    sources_root = Path(sources_root)
    tracks = load_and_validate_intake(sources_root)

    to_stage: list[dict[str, Any]] = []

    # 1. Preflight all source files and any existing destination files before writing
    for track in tracks:
        basename = track["basename"]
        expected_sha = track["sha256"]
        rel_src = f"Contant/adhan/{basename}"
        src_path = sources_root / "Contant" / "adhan" / basename

        _validate_path_hierarchy(src_path, must_exist=True)
        _, actual_src_sha = verify_exact_file_on_disk(sources_root, rel_src)
        if actual_src_sha != expected_sha:
            raise ValueError(
                f"Source hash mismatch for {basename}: expected {expected_sha}, got {actual_src_sha}"
            )

        rel_dst = f"raw-inputs/audio/adhan-local/{basename}"
        dst_path = sources_root / "raw-inputs" / "audio" / "adhan-local" / basename
        _validate_path_hierarchy(dst_path, must_exist=False)

        if dst_path.exists() or os.path.lexists(dst_path) or _is_symlink_or_junction(dst_path):
            _validate_path_hierarchy(dst_path, must_exist=True)
            _, actual_dst_sha = verify_exact_file_on_disk(sources_root, rel_dst)
            if actual_dst_sha != expected_sha:
                raise ValueError(
                    f"Existing staged file hash mismatch for {basename}: expected {expected_sha}, got {actual_dst_sha}"
                )
            # Existing copy matches expected digest; leave untouched
        else:
            to_stage.append(track)

    if not to_stage:
        return

    dst_dir = sources_root / "raw-inputs" / "audio" / "adhan-local"
    dst_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=dst_dir, prefix=".tmp_stage_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        for track in to_stage:
            basename = track["basename"]
            expected_sha = track["sha256"]
            src_file = sources_root / "Contant" / "adhan" / basename
            tmp_file = tmp_dir / basename

            hasher = hashlib.sha256()
            with open(src_file, "rb") as fsrc, open(tmp_file, "wb") as fdst:
                while chunk := fsrc.read(65536):
                    hasher.update(chunk)
                    fdst.write(chunk)

            staged_sha = hasher.hexdigest().lower()
            if staged_sha != expected_sha:
                raise ValueError(
                    f"Staged file hash mismatch for {basename}: expected {expected_sha}, got {staged_sha}"
                )

            dst_file = dst_dir / basename
            if dst_file.exists() or os.path.lexists(dst_file) or _is_symlink_or_junction(dst_file):
                raise FileExistsError(f"Staged destination already exists: {dst_file}")
            os.link(tmp_file, dst_file)


def build_audio_adhan_local(sources_root: Path, output_dir: Path) -> None:
    """Build local Adhan audio derivatives."""
    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    ffmpeg_path, ffprobe_path = verify_runtime()
    tracks = load_and_validate_intake(sources_root)

    # 1. Preflight every audio/adhan-local/{basename} under sources_root/raw-inputs
    raw_root = sources_root / "raw-inputs"
    for track in tracks:
        basename = track["basename"]
        expected_sha = track["sha256"]
        rel_path = f"audio/adhan-local/{basename}"
        master_path = raw_root / rel_path

        _validate_path_hierarchy(master_path, must_exist=True)
        _, actual_sha = verify_exact_file_on_disk(raw_root, rel_path)
        if actual_sha != expected_sha:
            raise ValueError(
                f"Master file hash mismatch for {basename}: expected {expected_sha}, got {actual_sha}"
            )

    # 2. Fresh output directory check
    if output_dir.exists() or os.path.lexists(output_dir) or _is_symlink_or_junction(output_dir):
        raise FileExistsError(f"Output directory already exists: {output_dir}")

    # 3. Construct source descriptors in slug order
    sources = [
        {
            "path": f"audio/adhan-local/{t['basename']}",
            "slug": t["slug"],
            "clipStartMs": 0,
        }
        for t in tracks
    ]

    _build_adhan_assets(
        raw_root,
        output_dir,
        sources,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for build_audio_adhan_local."""
    parser = argparse.ArgumentParser(
        prog="pipelines.builders.build_audio_adhan_local",
        description="Stage and build local Adhan audio assets.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    stage_parser = subparsers.add_parser("stage", help="Stage local Adhan masters.")
    stage_parser.add_argument(
        "--sources-root",
        required=True,
        type=Path,
        help="Root directory containing sources/ and raw-inputs/",
    )

    build_parser = subparsers.add_parser("build", help="Build local Adhan derivatives.")
    build_parser.add_argument(
        "--sources-root",
        required=True,
        type=Path,
        help="Root directory containing sources/ and raw-inputs/",
    )
    build_parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Output directory where audio assets will be written",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        if args.subcommand == "stage":
            stage_audio_adhan_local(args.sources_root)
        elif args.subcommand == "build":
            build_audio_adhan_local(args.sources_root, args.output_dir)
        else:
            sys.stderr.write(f"Unknown subcommand: {args.subcommand}\n")
            return 2
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
