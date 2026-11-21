"""Deterministic reference runtime and toolchain preflight checker."""

from __future__ import annotations

import json
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys


def detect_platform() -> str:
    """Return platform key for identity.json ('linux-x64' or 'windows-x64')."""
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform.startswith("linux"):
        return "linux-x64"
    raise RuntimeError(f"Unsupported runtime platform: {sys.platform}")


def load_expected_identity(platform_key: str | None = None) -> dict:
    """Load expected identity dictionary for the specified or current platform."""
    if platform_key is None:
        platform_key = detect_platform()
    identity_path = Path(__file__).resolve().parent / "identity.json"
    if not identity_path.is_file():
        raise RuntimeError(f"identity.json not found at {identity_path}")
    with open(identity_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if platform_key not in data:
        raise RuntimeError(f"Platform {platform_key!r} not defined in identity.json")
    return data[platform_key]


def _resolve_and_check_tool(name: str, expected_version: str) -> Path:
    """Resolve tool from PATH and verify complete -version output."""
    exec_path = shutil.which(name)
    if not exec_path:
        raise RuntimeError(f"{name} executable not found on PATH")
    path = Path(exec_path).resolve()

    cmd = [str(path)]
    if name == "ffmpeg":
        cmd.append("-nostdin")
    cmd.append("-version")

    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{name} -version failed with exit code {proc.returncode}: {proc.stderr}"
        )
    actual_ver = proc.stdout.replace("\r\n", "\n")
    if actual_ver != expected_version:
        raise RuntimeError(
            f"{name} version mismatch: expected {expected_version!r}, got {actual_ver!r}"
        )
    return path


def verify_runtime() -> tuple[Path, Path]:
    """Verify interpreter, SQLite, FTS5, media tools and codecs against pinned identity.

    Returns:
        tuple[Path, Path]: Absolute resolved paths to (ffmpeg, ffprobe).

    Raises:
        RuntimeError: On any identity or capability mismatch.
    """
    platform_key = detect_platform()
    expected = load_expected_identity(platform_key)

    # 1. Interpreter verification
    if sys.implementation.name != "cpython":
        raise RuntimeError(
            f"Expected CPython implementation, got {sys.implementation.name!r}"
        )
    py_ver = platform.python_version()
    if py_ver != expected["pythonVersion"]:
        raise RuntimeError(
            f"Python version mismatch: expected {expected['pythonVersion']!r}, got {py_ver!r}"
        )
    if sys.maxsize <= 2**32:
        raise RuntimeError("Expected 64-bit Python interpreter")
    machine = platform.machine().lower()
    if machine not in ("x86_64", "amd64"):
        raise RuntimeError(f"Expected x86-64 architecture, got {platform.machine()!r}")

    # 2. SQLite version, source ID, and compile options
    if sqlite3.sqlite_version != expected["sqliteVersion"]:
        raise RuntimeError(
            f"SQLite version mismatch: expected {expected['sqliteVersion']!r}, got {sqlite3.sqlite_version!r}"
        )

    con = sqlite3.connect(":memory:")
    try:
        source_id = con.execute("SELECT sqlite_source_id()").fetchone()[0]
        if source_id != expected["sqliteSourceId"]:
            raise RuntimeError(
                f"SQLite source ID mismatch: expected {expected['sqliteSourceId']!r}, got {source_id!r}"
            )

        opts = sorted([r[0] for r in con.execute("PRAGMA compile_options").fetchall()])
        if opts != expected["sqliteCompileOptions"]:
            diff = set(opts) ^ set(expected["sqliteCompileOptions"])
            raise RuntimeError(
                f"SQLite compile options mismatch. Symmetric difference: {sorted(diff)}"
            )

        # 3. Create and query in-memory FTS5
        con.execute("CREATE VIRTUAL TABLE _check_fts USING fts5(content);")
        con.execute("INSERT INTO _check_fts(content) VALUES ('preflight fts5 verification token');")
        rows = con.execute(
            "SELECT content FROM _check_fts WHERE _check_fts MATCH 'verification';"
        ).fetchall()
        if not rows or rows[0][0] != "preflight fts5 verification token":
            raise RuntimeError("FTS5 query verification failed to return expected token")
    finally:
        con.close()

    # 4. Resolve media tools and verify identity
    ffmpeg_path = _resolve_and_check_tool("ffmpeg", expected["ffmpegVersion"])
    ffprobe_path = _resolve_and_check_tool("ffprobe", expected["ffprobeVersion"])

    # 5. CAF and codec capability checks
    muxers_proc = subprocess.run(
        [str(ffmpeg_path), "-nostdin", "-muxers"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if muxers_proc.returncode != 0 or " caf " not in muxers_proc.stdout:
        raise RuntimeError("ffmpeg missing CAF muxer capability")

    encoders_proc = subprocess.run(
        [str(ffmpeg_path), "-nostdin", "-encoders"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if encoders_proc.returncode != 0:
        raise RuntimeError("ffmpeg failed listing encoders")
    if " aac " not in encoders_proc.stdout:
        raise RuntimeError("ffmpeg missing AAC encoder capability")
    if " adpcm_ima_qt " not in encoders_proc.stdout:
        raise RuntimeError("ffmpeg missing adpcm_ima_qt encoder capability")

    # In-memory CAF encoding probe
    test_encode = subprocess.run(
        [
            str(ffmpeg_path),
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=32000:cl=mono",
            "-t",
            "0.05",
            "-c:a",
            "adpcm_ima_qt",
            "-f",
            "caf",
            "-",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        shell=False,
    )
    if test_encode.returncode != 0:
        raise RuntimeError(
            f"CAF encode preflight test failed: {test_encode.stderr.decode(errors='replace')}"
        )

    return ffmpeg_path, ffprobe_path


def main() -> int:
    try:
        ffmpeg_p, ffprobe_p = verify_runtime()
        platform_key = detect_platform()
        print(f"Runtime verified ({platform_key}):")
        print(f"  Python:  {platform.python_version()}")
        print(f"  SQLite:  {sqlite3.sqlite_version}")
        print(f"  ffmpeg:  {ffmpeg_p}")
        print(f"  ffprobe: {ffprobe_p}")
        return 0
    except Exception as exc:
        sys.stderr.write(f"Runtime check failed: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
