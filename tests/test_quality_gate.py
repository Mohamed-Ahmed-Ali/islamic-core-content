"""Quality gate tests driving required DoD mutations and asserting check diagnostics.

Tests drive the 9 quality gate checks against synthetic fixtures and verify that:
1. Baseline valid candidate & rebuild pass with exit 0, Check #8 not applicable,
   and matn_split_verified='true'.
2. Specific mutations trigger expected check failures and diagnostics:
   - Malformed UTF-8, lone surrogate, U+FFFD, BLOB in TEXT, mismatched split (Check #2)
   - Invalid DB schema or foreign key violation (Check #3)
   - Empty FTS index with populated content table without changing DDL (Check #4)
   - Changed checksum with same file size (Check #5)
   - Invalid manifest schema (Check #5)
   - Wrong CAF codec (Check #5)
   - Valid 29.0s CAF at 32 kHz (Check #5: duration must be strictly < 29.0s)
   - Omitted file declared in a manifest (Check #5)
   - Floating 1.0 for integer version fields (Check #5)
   - Actual distributable byte difference with free box while both roots pass internally (Check #6)
   - Same physical root supplied twice (Check #6)
   - Missing license record (Check #7)
   - Lock boundary checks: wrong-case, duplicate key, case collision, missing file,
     path traversal, symlink, hash mismatch (Check #7)
   - Tafsir interval / map mismatch (Check #9)
   - Malformed expectations file (exit code 2)
   - Canonical literal UTF-8 and duration 2.0/1.5 spellings.
"""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

from tests.fixtures.build import build_fixtures

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATIONS_PATH = REPO_ROOT / "tests" / "fixtures" / "expectations.json"



def run_quality_gate(
    *,
    candidate: Path,
    rebuild: Path,
    sources_root: Path,
    expectations: Path | None = EXPECTATIONS_PATH,
    profile: str = "fixture",
    extra_args: list[str] | None = None,
) -> tuple[int, str, str]:
    """Execute documented quality gate CLI with explicit interpreter args:
    python -m pipelines.validators.quality_gate --profile <profile> --candidate <A> --rebuild <B> --sources-root <S> [--expectations <E>]
    Returns (exit_code, stdout, stderr).
    """
    cmd = [
        sys.executable,
        "-m",
        "pipelines.validators.quality_gate",
        "--profile",
        profile,
        "--candidate",
        str(candidate),
        "--rebuild",
        str(rebuild),
        "--sources-root",
        str(sources_root),
    ]
    if expectations is not None:
        cmd.extend(["--expectations", str(expectations)])
    if extra_args:
        cmd.extend(extra_args)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _recompute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def _canonical_dump(data: Any) -> bytes:
    """Canonical JSON serialization: sorted keys, no whitespace, literal UTF-8, trailing LF."""
    return (
        json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

def _canonical_lock_dump(data: Any) -> bytes:
    """Canonical lock serialization: fixed insertion key order, no whitespace, literal UTF-8, trailing LF."""
    return (
        json.dumps(data, sort_keys=False, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def run_lock_verify(
    *,
    registry_path: Path,
    raw_root: Path,
    lock_path: Path,
    pack_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute documented direct lock CLI verification:
    python -m pipelines.common.lock_inputs --registry <R> --raw-root <RAW> --output <LOCK> --verify [--pack <P>]
    """
    cmd = [
        sys.executable,
        "-m",
        "pipelines.common.lock_inputs",
        "--registry",
        str(registry_path),
        "--raw-root",
        str(raw_root),
        "--output",
        str(lock_path),
        "--verify",
    ]
    if pack_id is not None:
        cmd.extend(["--pack", pack_id])
    return subprocess.run(cmd, capture_output=True, text=True, shell=False)


def _update_manifest_asset(
    manifest_path: Path, filename: str, new_size: int, new_sha: str
) -> None:
    """Update sizeBytes and sha256 for a named asset in catalog.json or audio-manifest.json."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if "packs" in data:
        for pack in data["packs"]:
            if pack.get("url", "").endswith(f"/{filename}"):
                pack["sizeBytes"] = new_size
                pack["sha256"] = new_sha
    elif "tracks" in data:
        for track in data["tracks"]:
            for variant in ("full", "ios"):
                if track.get(variant, {}).get("filename") == filename:
                    track[variant]["sizeBytes"] = new_size
                    track[variant]["sha256"] = new_sha
    manifest_path.write_bytes(_canonical_dump(data))


def _probe_audio_timing(ffprobe_path: Path, audio_path: Path) -> tuple[str, Fraction]:
    """Probe audio codec and exact rational duration using ffprobe."""
    cmd = [
        str(ffprobe_path),
        "-v",
        "error",
        "-of",
        "json",
        "-show_streams",
        str(audio_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=False)
    data = json.loads(proc.stdout)
    stream = data["streams"][0]
    codec = stream["codec_name"]
    duration_ts = int(stream["duration_ts"])
    time_base = Fraction(stream["time_base"])
    duration = duration_ts * time_base
    return codec, duration


class TestQualityGateDoD:
    """DoD verification and mutation suite for pipelines.validators.quality_gate."""

    @pytest.fixture
    def fixture_setup(self, tmp_path: Path) -> dict[str, Path]:
        """Build fresh candidate, rebuild, and source trees."""
        return build_fixtures(tmp_path)

    def test_baseline_clean_fixture(self, fixture_setup: dict[str, Path]) -> None:
        """Verify baseline clean fixture passes all applicable checks (exit 0, #8 N/A)."""
        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 0, f"Expected 0, got {exit_code}. Output: {output}"
        assert "8" in output and ("not applicable" in output.lower() or "n/a" in output.lower())

        # Baseline assert: matn_split_verified must be 'true' in hadith-fixture.db
        hadith_db = fixture_setup["candidate"] / "hadith-fixture.db"
        conn = sqlite3.connect(hadith_db)
        cur = conn.cursor()
        cur.execute("SELECT value FROM pack_meta WHERE key = 'matn_split_verified';")
        row = cur.fetchone()
        conn.close()
        assert row is not None and row[0] == "true"

    def test_mutation_check2_malformed_utf8_bytes(self, fixture_setup: dict[str, Path]) -> None:
        """Check #2: Malformed UTF-8 byte sequences in candidate manifest JSON bytes."""
        cat_file = fixture_setup["candidate"] / "catalog.json"
        cat_bytes = cat_file.read_bytes()
        # Inject invalid UTF-8 continuation byte into candidate manifest (first #2 stage)
        bad_bytes = cat_bytes.replace(b'"releaseTag":"content-v0.0.0"', b'"releaseTag":"content-v0.0.0\x80\x81"')
        assert bad_bytes != cat_bytes
        cat_file.write_bytes(bad_bytes)

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 2" in output.lower() or "#2" in output

    def test_mutation_check2_decoded_lone_surrogate(self, fixture_setup: dict[str, Path]) -> None:
        """Check #2: Reject decoded lone surrogate escape in candidate manifest JSON bytes."""
        cat_file = fixture_setup["candidate"] / "catalog.json"
        cat_bytes = cat_file.read_bytes()
        # Inject lone surrogate escape into candidate manifest (first #2 stage)
        bad_bytes = cat_bytes.replace(b'"releaseTag":"content-v0.0.0"', b'"releaseTag":"content-v0.0.0\\ud800"')
        assert bad_bytes != cat_bytes
        # Prove mutation is valid JSON syntax that decodes to a lone surrogate
        decoded = json.loads(bad_bytes.decode("utf-8"))
        assert decoded["releaseTag"] == "content-v0.0.0\ud800"
        cat_file.write_bytes(bad_bytes)

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 2" in output.lower() or "#2" in output

    def test_mutation_check2_escaped_fffd(self, fixture_setup: dict[str, Path]) -> None:
        """Check #2: Reject escaped U+FFFD replacement character in candidate manifest JSON bytes."""
        cat_file = fixture_setup["candidate"] / "catalog.json"
        cat_bytes = cat_file.read_bytes()
        # Inject escaped U+FFFD into candidate manifest (first #2 stage)
        bad_bytes = cat_bytes.replace(b'"releaseTag":"content-v0.0.0"', b'"releaseTag":"content-v0.0.0\\ufffd"')
        assert bad_bytes != cat_bytes
        # Prove mutation is valid JSON syntax that decodes to U+FFFD
        decoded = json.loads(bad_bytes.decode("utf-8"))
        assert decoded["releaseTag"] == "content-v0.0.0\ufffd"
        cat_file.write_bytes(bad_bytes)

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 2" in output.lower() or "#2" in output
    def test_mutation_check2_blob_in_text_column(self, fixture_setup: dict[str, Path]) -> None:
        """Check #2: Reject raw BLOB in a TEXT column."""
        core_db = fixture_setup["candidate"] / "quran-core.db"
        conn = sqlite3.connect(core_db)
        conn.execute("UPDATE ayahs SET text_simple_clean = ? WHERE id = 1;", [sqlite3.Binary(b"\x01\x02\x03")])
        conn.commit()
        conn.close()

        _update_manifest_asset(
            fixture_setup["candidate"] / "catalog.json",
            "quran-core.db",
            core_db.stat().st_size,
            _recompute_sha256(core_db),
        )

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 2" in output.lower() or "#2" in output

    def test_mutation_check2_mismatched_verified_split(self, fixture_setup: dict[str, Path]) -> None:
        """Check #2: Reject mismatched verified split evidence."""
        hadith_db = fixture_setup["candidate"] / "hadith-fixture.db"
        conn = sqlite3.connect(hadith_db)
        conn.execute("UPDATE hadiths SET arabic_matn = 'غير_مطابق' WHERE id = 2;")
        conn.commit()
        conn.close()

        _update_manifest_asset(
            fixture_setup["candidate"] / "catalog.json",
            "hadith-fixture.db",
            hadith_db.stat().st_size,
            _recompute_sha256(hadith_db),
        )

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 2" in output.lower() or "#2" in output

    def test_mutation_check3_invalid_db_schema_or_foreign_key(self, fixture_setup: dict[str, Path]) -> None:
        """Check #3: Invalid DB schema or foreign key violation."""
        hadith_db = fixture_setup["candidate"] / "hadith-fixture.db"
        conn = sqlite3.connect(hadith_db)
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute(
            """INSERT INTO hadiths (id, book_id, chapter_id, hadith_number, text_split_mode)
               VALUES (99, 999, NULL, '99', 'unsplit_raw');"""
        )
        conn.commit()
        conn.close()

        _update_manifest_asset(
            fixture_setup["candidate"] / "catalog.json",
            "hadith-fixture.db",
            hadith_db.stat().st_size,
            _recompute_sha256(hadith_db),
        )

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 3" in output.lower() or "#3" in output
        assert "hadith-fixture.db" in output

    def test_mutation_check4_empty_fts_index(self, fixture_setup: dict[str, Path]) -> None:
        """Check #4: Empty FTS index with populated content table without changing DDL."""
        core_db = fixture_setup["candidate"] / "quran-core.db"
        conn = sqlite3.connect(core_db)
        # Empty external-content FTS table via delete-all without changing DDL
        conn.execute("INSERT INTO ayahs_fts(ayahs_fts) VALUES('delete-all');")
        conn.commit()
        conn.close()

        _update_manifest_asset(
            fixture_setup["candidate"] / "catalog.json",
            "quran-core.db",
            core_db.stat().st_size,
            _recompute_sha256(core_db),
        )

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 4" in output.lower() or "#4" in output
        assert "quran-core.db" in output

    def test_mutation_check5_changed_checksum_same_size(self, fixture_setup: dict[str, Path]) -> None:
        """Check #5: Mismatched file checksum with identical file size."""
        core_db = fixture_setup["candidate"] / "quran-core.db"
        orig_size = core_db.stat().st_size
        with open(core_db, "r+b") as f:
            b = f.read(1)
            f.seek(0)
            f.write(bytes([b[0] ^ 0xFF]))
        assert core_db.stat().st_size == orig_size

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 5" in output.lower() or "#5" in output
        assert "quran-core.db" in output

    def test_mutation_check5_invalid_manifest_schema(self, fixture_setup: dict[str, Path]) -> None:
        """Check #5: Invalid manifest schema (e.g. catalog missing required property)."""
        cat_file = fixture_setup["candidate"] / "catalog.json"
        cat = json.loads(cat_file.read_text(encoding="utf-8"))
        del cat["catalogVersion"]
        cat_file.write_bytes(_canonical_dump(cat))

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 5" in output.lower() or "#5" in output
        assert "catalog.json" in output

    def test_mutation_check5_wrong_caf_codec(self, fixture_setup: dict[str, Path]) -> None:
        """Check #5: iOS notification audio asset has wrong codec on disk."""
        from pipelines.toolchain.check_runtime import verify_runtime

        ffmpeg_path, ffprobe_path = verify_runtime()
        caf_file = fixture_setup["candidate"] / "adhan-fixture-ios-notif.caf"
        wav_file = fixture_setup["source"] / "raw-inputs" / "audio" / "adhan" / "fixture.wav"

        # Transcode CAF using pcm_s16le instead of adpcm_ima_qt
        cmd = [
            str(ffmpeg_path),
            "-y",
            "-nostdin",
            "-threads",
            "1",
            "-i",
            str(wav_file),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "32000",
            str(caf_file),
        ]
        subprocess.run(cmd, check=True, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Confirm probe detects the wrong codec
        codec, _ = _probe_audio_timing(ffprobe_path, caf_file)
        assert codec != "adpcm_ima_qt"

        # Rehash manifest so #5 triggers on codec check, not sha mismatch
        _update_manifest_asset(
            fixture_setup["candidate"] / "audio-manifest.json",
            "adhan-fixture-ios-notif.caf",
            caf_file.stat().st_size,
            _recompute_sha256(caf_file),
        )

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 5" in output.lower() or "#5" in output
        assert "adhan-fixture-ios-notif.caf" in output

    def test_mutation_check5_valid_29s_caf_rejected(self, fixture_setup: dict[str, Path]) -> None:
        """Check #5: iOS CAF duration must be strictly < 29.0s; exactly 29.0s is rejected."""
        from pipelines.toolchain.check_runtime import verify_runtime

        ffmpeg_path, ffprobe_path = verify_runtime()
        caf_file = fixture_setup["candidate"] / "adhan-fixture-ios-notif.caf"

        # Generate exactly 29.0s CAF at 32 kHz with adpcm_ima_qt (928,000 samples)
        cmd = [
            str(ffmpeg_path),
            "-y",
            "-nostdin",
            "-threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=32000:duration=29",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-c:a",
            "adpcm_ima_qt",
            "-ar",
            "32000",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            str(caf_file),
        ]
        subprocess.run(cmd, check=True, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Confirm final probe confirms duration is exactly 29.0s
        codec, duration = _probe_audio_timing(ffprobe_path, caf_file)
        assert codec == "adpcm_ima_qt"
        assert duration == Fraction(29, 1)

        # Rehash manifest with canonical serialization
        audio_man = fixture_setup["candidate"] / "audio-manifest.json"
        data = json.loads(audio_man.read_text(encoding="utf-8"))
        data["tracks"][0]["ios"]["sizeBytes"] = caf_file.stat().st_size
        data["tracks"][0]["ios"]["sha256"] = _recompute_sha256(caf_file)
        data["tracks"][0]["ios"]["durationSeconds"] = 29.0
        audio_man.write_bytes(_canonical_dump(data))

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 5" in output.lower() or "#5" in output
        assert "adhan-fixture-ios-notif.caf" in output or "29" in output

    def test_mutation_check5_omitted_file_declared_in_manifest(self, fixture_setup: dict[str, Path]) -> None:
        """Check #5: Manifest declares an asset that is missing on disk."""
        (fixture_setup["candidate"] / "adhan-fixture-full.m4a").unlink()

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 5" in output.lower() or "#5" in output
        assert "adhan-fixture-full.m4a" in output

    def test_mutation_check5_floating_point_version_field(self, fixture_setup: dict[str, Path]) -> None:
        """Check #5: Floating 1.0 for integer version fields rejected."""
        cat_file = fixture_setup["candidate"] / "catalog.json"
        raw_text = cat_file.read_text(encoding="utf-8")
        raw_text = raw_text.replace('"catalogVersion":1', '"catalogVersion":1.0', 1)
        cat_file.write_text(raw_text, encoding="utf-8")

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 5" in output.lower() or "#5" in output

    def test_mutation_check6_distributable_byte_difference_free_box(self, fixture_setup: dict[str, Path]) -> None:
        """Check #6: Append valid MP4 free box to adhan-fixture-full.m4a in rebuild root."""
        rebuild_m4a = fixture_setup["rebuild"] / "adhan-fixture-full.m4a"
        free_box = bytes.fromhex("0000000866726565")
        with open(rebuild_m4a, "ab") as f:
            f.write(free_box)

        # Update rebuild audio-manifest with new size and hash with canonical serialization
        _update_manifest_asset(
            fixture_setup["rebuild"] / "audio-manifest.json",
            "adhan-fixture-full.m4a",
            rebuild_m4a.stat().st_size,
            _recompute_sha256(rebuild_m4a),
        )

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 6" in output.lower() or "#6" in output

    def test_mutation_check6_same_physical_root_supplied_twice(self, fixture_setup: dict[str, Path]) -> None:
        """Check #6: Reject candidate and rebuild pointing to the exact same physical directory."""
        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["candidate"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 6" in output.lower() or "#6" in output

    def test_mutation_check7_missing_license(self, fixture_setup: dict[str, Path]) -> None:
        """Check #7: Missing or unreconciled license clearance record."""
        lic_file = fixture_setup["source"] / "sources" / "licenses" / "quran-core.json"
        lic_file.unlink()

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 7" in output.lower() or "#7" in output

    def test_lock_boundary_wrong_case(self, fixture_setup: dict[str, Path]) -> None:
        """Lock boundary: Case-changed filesystem spelling of listed file is rejected."""
        target = fixture_setup["source"] / "raw-inputs" / "sqlite" / "quran-core.json"
        temp_target = target.with_name("quran-core.tmp")
        wrong_target = target.with_name("Quran-core.json")
        # Two-step rename on Windows to change case of listed file
        target.rename(temp_target)
        temp_target.rename(wrong_target)

        lock_file = fixture_setup["source"] / "sources" / "manifest.lock.json"
        orig_lock_bytes = lock_file.read_bytes()

        # Direct lock CLI check preferred to avoid gate misattribution
        proc = run_lock_verify(
            registry_path=fixture_setup["source"] / "sources" / "registry.json",
            raw_root=fixture_setup["source"] / "raw-inputs",
            lock_path=lock_file,
        )
        assert proc.returncode != 0
        # Verify does not write
        assert lock_file.read_bytes() == orig_lock_bytes

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 7" in output.lower() or "#7" in output

    def test_lock_boundary_duplicate_key(self, fixture_setup: dict[str, Path]) -> None:
        """Lock boundary: Duplicate JSON object key in raw lock bytes is rejected."""
        lock_file = fixture_setup["source"] / "sources" / "manifest.lock.json"
        orig_lock_bytes = lock_file.read_bytes()
        # Duplicate root object key in raw bytes
        bad_lock_bytes = orig_lock_bytes.replace(b'{"lockVersion":1,', b'{"lockVersion":1,"lockVersion":1,', 1)
        assert bad_lock_bytes != orig_lock_bytes
        lock_file.write_bytes(bad_lock_bytes)

        # Direct lock CLI check preferred
        proc = run_lock_verify(
            registry_path=fixture_setup["source"] / "sources" / "registry.json",
            raw_root=fixture_setup["source"] / "raw-inputs",
            lock_path=lock_file,
        )
        assert proc.returncode != 0
        # Verify does not write
        assert lock_file.read_bytes() == bad_lock_bytes

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 7" in output.lower() or "#7" in output

    def test_lock_boundary_case_collision(self, fixture_setup: dict[str, Path]) -> None:
        """Lock boundary: Case-colliding paths in registry and lock are rejected."""
        reg_file = fixture_setup["source"] / "sources" / "registry.json"
        reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
        colliding_path = "sqlite/Quran-core.json"
        reg_data[colliding_path] = {
            "sourceUrl": "https://example.org/fixture/sqlite/quran-core.json",
            "sourceVersion": "1",
            "packIds": ["quran-core"],
        }
        reg_file.write_bytes(json.dumps(reg_data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n")

        lock_file = fixture_setup["source"] / "sources" / "manifest.lock.json"
        lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
        dup_entry = {
            "path": colliding_path,
            "sourceUrl": "https://example.org/fixture/sqlite/quran-core.json",
            "sourceVersion": "1",
            "sizeBytes": lock_data["inputs"][0]["sizeBytes"],
            "sha256": lock_data["inputs"][0]["sha256"],
        }
        lock_data["inputs"].append(dup_entry)
        lock_data["inputs"].sort(key=lambda x: x["path"])
        mutated_lock_bytes = _canonical_lock_dump(lock_data)
        lock_file.write_bytes(mutated_lock_bytes)

        # Direct lock CLI check
        proc = run_lock_verify(
            registry_path=reg_file,
            raw_root=fixture_setup["source"] / "raw-inputs",
            lock_path=lock_file,
        )
        assert proc.returncode != 0
        # Verify does not write
        assert lock_file.read_bytes() == mutated_lock_bytes

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 7" in output.lower() or "#7" in output

    def test_lock_boundary_missing_file(self, fixture_setup: dict[str, Path]) -> None:
        """Lock boundary: File listed in lock is missing on disk."""
        target = fixture_setup["source"] / "raw-inputs" / "sqlite" / "quran-core.json"
        target.unlink()

        lock_file = fixture_setup["source"] / "sources" / "manifest.lock.json"
        orig_lock_bytes = lock_file.read_bytes()

        # Direct lock CLI check
        proc = run_lock_verify(
            registry_path=fixture_setup["source"] / "sources" / "registry.json",
            raw_root=fixture_setup["source"] / "raw-inputs",
            lock_path=lock_file,
        )
        assert proc.returncode != 0
        # Verify does not write
        assert lock_file.read_bytes() == orig_lock_bytes

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 7" in output.lower() or "#7" in output

    def test_lock_boundary_path_traversal(self, fixture_setup: dict[str, Path]) -> None:
        """Lock boundary: Path traversal in registry and lock is rejected."""
        reg_file = fixture_setup["source"] / "sources" / "registry.json"
        reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
        bad_path = "sqlite/../sqlite/quran-core.json"
        reg_data[bad_path] = {
            "sourceUrl": "https://example.org/fixture/sqlite/quran-core.json",
            "sourceVersion": "1",
            "packIds": ["quran-core"],
        }
        reg_file.write_bytes(json.dumps(reg_data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n")

        lock_file = fixture_setup["source"] / "sources" / "manifest.lock.json"
        lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
        bad_entry = {
            "path": bad_path,
            "sourceUrl": "https://example.org/fixture/sqlite/quran-core.json",
            "sourceVersion": "1",
            "sizeBytes": lock_data["inputs"][0]["sizeBytes"],
            "sha256": lock_data["inputs"][0]["sha256"],
        }
        lock_data["inputs"].append(bad_entry)
        lock_data["inputs"].sort(key=lambda x: x["path"])
        mutated_lock_bytes = _canonical_lock_dump(lock_data)
        lock_file.write_bytes(mutated_lock_bytes)

        # Direct lock CLI check
        proc = run_lock_verify(
            registry_path=reg_file,
            raw_root=fixture_setup["source"] / "raw-inputs",
            lock_path=lock_file,
        )
        assert proc.returncode != 0
        # Verify does not write
        assert lock_file.read_bytes() == mutated_lock_bytes

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 7" in output.lower() or "#7" in output

    def test_lock_boundary_symlink(self, fixture_setup: dict[str, Path]) -> None:
        """Lock boundary: Symlink replacing exact authorized path with same bytes is rejected without unlisted lock entry."""
        authorized_path = fixture_setup["source"] / "raw-inputs" / "sqlite" / "quran-core.json"
        orig_bytes = authorized_path.read_bytes()
        real_target = authorized_path.with_name("quran-core.real")
        real_target.write_bytes(orig_bytes)
        authorized_path.unlink()
        try:
            authorized_path.symlink_to(real_target)
        except (OSError, NotImplementedError):
            real_target.unlink(missing_ok=True)
            authorized_path.write_bytes(orig_bytes)
            pytest.skip("Symlink creation not supported on this platform/privilege level")

        lock_file = fixture_setup["source"] / "sources" / "manifest.lock.json"
        orig_lock_bytes = lock_file.read_bytes()

        # Direct lock CLI check (untouched lock, exact authorized path replaced with symlink)
        proc = run_lock_verify(
            registry_path=fixture_setup["source"] / "sources" / "registry.json",
            raw_root=fixture_setup["source"] / "raw-inputs",
            lock_path=lock_file,
        )
        assert proc.returncode != 0
        # Verify does not write
        assert lock_file.read_bytes() == orig_lock_bytes

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 7" in output.lower() or "#7" in output

    def test_lock_boundary_hash_mismatch(self, fixture_setup: dict[str, Path]) -> None:
        """Lock boundary: Hash mismatch changes same-sized raw bytes with untouched lock."""
        raw_file = fixture_setup["source"] / "raw-inputs" / "sqlite" / "quran-core.json"
        raw_bytes = raw_file.read_bytes()
        # Mutate content without changing file size
        mutated_bytes = raw_bytes.replace(b'"name_ar"', b'"name_xx"', 1)
        assert len(mutated_bytes) == len(raw_bytes)
        assert mutated_bytes != raw_bytes
        raw_file.write_bytes(mutated_bytes)

        lock_file = fixture_setup["source"] / "sources" / "manifest.lock.json"
        orig_lock_bytes = lock_file.read_bytes()

        # Direct lock CLI check (untouched lock)
        proc = run_lock_verify(
            registry_path=fixture_setup["source"] / "sources" / "registry.json",
            raw_root=fixture_setup["source"] / "raw-inputs",
            lock_path=lock_file,
        )
        assert proc.returncode != 0
        # Verify does not write
        assert lock_file.read_bytes() == orig_lock_bytes

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 7" in output.lower() or "#7" in output

    def test_mutation_check9_tafsir_interval_map_mismatch(self, fixture_setup: dict[str, Path]) -> None:
        """Check #9: Tafsir interval/key grammar, nonoverlap, or map pointer discrepancy."""
        tafsir_db = fixture_setup["candidate"] / "quran-tafsir-ar-fixture.db"
        conn = sqlite3.connect(tafsir_db)
        # Shift map entry_id pointer to existing entry 2 outside ayah 1's interval
        conn.execute("UPDATE tafsir_ayah_map SET entry_id = 2 WHERE ayah_id = 1;")
        conn.commit()
        conn.close()

        _update_manifest_asset(
            fixture_setup["candidate"] / "catalog.json",
            "quran-tafsir-ar-fixture.db",
            tafsir_db.stat().st_size,
            _recompute_sha256(tafsir_db),
        )

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=EXPECTATIONS_PATH,
        )
        output = stdout + "\n" + stderr
        assert exit_code == 1
        assert "check 9" in output.lower() or "#9" in output
        assert "quran-tafsir-ar-fixture.db" in output

    def test_malformed_expectations_file_exits_2(self, fixture_setup: dict[str, Path], tmp_path: Path) -> None:
        """Malformed expectations file causes gate to exit with status 2."""
        bad_exp = tmp_path / "bad_expectations.json"
        bad_exp.write_text('{"packs": "not_an_object"}', encoding="utf-8")

        exit_code, stdout, stderr = run_quality_gate(
            candidate=fixture_setup["candidate"],
            rebuild=fixture_setup["rebuild"],
            sources_root=fixture_setup["source"],
            expectations=bad_exp,
        )
        assert exit_code == 2

    def test_canonical_literal_utf8_and_duration_spellings(self, fixture_setup: dict[str, Path]) -> None:
        """Verify canonical literal UTF-8 (no ASCII escape sequences) and exact duration 2.0/1.5 spellings."""
        from tests.fixtures.build import format_duration_seconds

        cat_bytes = (fixture_setup["candidate"] / "catalog.json").read_bytes()
        audio_man_bytes = (fixture_setup["candidate"] / "audio-manifest.json").read_bytes()

        # Meaningful isolated non-ASCII validation: canonical dump must preserve literal UTF-8 bytes without \u escapes
        sample_non_ascii = {"name": "Synthetic fixture", "arabic": "رَبِّ"}
        dumped_bytes = _canonical_dump(sample_non_ascii)
        assert "رَبِّ".encode("utf-8") in dumped_bytes
        assert b"\\u" not in dumped_bytes

        # Manifest bytes must not contain \u escaped non-ASCII in JSON output
        assert b"\\u" not in cat_bytes
        assert b"\\u" not in audio_man_bytes

        # Exact both duration spellings required: 2.0 and 1.5 in audio manifest
        assert b'"durationSeconds":2.0' in audio_man_bytes
        assert b'"durationSeconds":1.5' in audio_man_bytes

        # Verify duration formatting helper produces exact expected floats
        assert format_duration_seconds(Fraction(2, 1)) == 2.0
        assert format_duration_seconds(Fraction(3, 2)) == 1.5
