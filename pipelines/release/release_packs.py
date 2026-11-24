"""Release packaging, bundle acquisition, companion assembly, and download verification."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import Any

from pipelines.common.lock_inputs import (
    LockEntry,
    parse_and_validate_lock,
    parse_and_validate_registry,
    parse_strict_json,
    validate_case_prefix_collisions,
    validate_safe_relative_path,
    verify_exact_file_on_disk,
    verify_lock,
    _is_symlink_or_junction,
)

MORPHOLOGY_SOURCE_ARCHIVE_MEMBERS: tuple[str, ...] = (
    ".dockerignore",
    "ARCHITECTURE.md",
    "LICENSE",
    "pipelines/__init__.py",
    "pipelines/builders/build_quran_morphology.py",
    "pipelines/common/__init__.py",
    "pipelines/common/lock_inputs.py",
    "pipelines/common/search_text.py",
    "pipelines/common/sqlite_utils.py",
    "pipelines/common/tanzil_coordinates.py",
    "pipelines/toolchain/Dockerfile",
    "pipelines/toolchain/check_runtime.py",
    "pipelines/toolchain/identity.json",
    "raw-inputs/corpus/quranic-corpus-morphology-0.4.zip",
    "raw-inputs/tanzil/quran-data-Tanzil.xml",
    "requirements-test.txt",
    "sources/licenses/evidence/quran-morphology/CORPUS-NOTICE.txt",
    "sources/licenses/evidence/quran-morphology/GPL-3.0.txt",
    "sources/licenses/evidence/quran-morphology/REBUILD.md",
    "sources/licenses/evidence/quran-morphology/TANZIL-NOTICE.txt",
    "sources/licenses/quran-morphology.json",
    "sources/manifest.lock.json",
    "sources/registry.json",
    "specs/sql/common_meta.sql",
    "specs/sql/quran_morphology.v1.sql",
)


class StrictHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Ensure HTTP redirects strictly target HTTPS URLs."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if not newurl.startswith("https://"):
            raise ValueError("Insecure redirect to non-HTTPS URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def load_release_descriptor(sources_root: Path) -> dict[str, Any]:
    """Load and strictly validate sources/release-v0.1.0.json per ARCHITECTURE §7.1."""
    desc_path = sources_root / "sources" / "release-v0.1.0.json"
    if not desc_path.is_file() or _is_symlink_or_junction(desc_path):
        raise ValueError(f"Release descriptor missing or invalid link: {desc_path}")
    verify_exact_file_on_disk(sources_root, "sources/release-v0.1.0.json")

    raw_text = desc_path.read_text(encoding="utf-8")
    data = parse_strict_json(raw_text)
    if not isinstance(data, dict):
        raise ValueError("Release descriptor must be a JSON object")

    expected_keys = {"repository", "generatedAt", "bundlePaths", "companions"}
    if set(data.keys()) != expected_keys:
        raise ValueError(f"Release descriptor keys mismatch: expected {expected_keys}, got {set(data.keys())}")

    repo = data["repository"]
    if not isinstance(repo, str):
        raise ValueError("repository must be a string")
    repo_parts = repo.split("/")
    if len(repo_parts) != 2 or any(
        p in (".", "..") or p.startswith(".") or not re.fullmatch(r"^[a-zA-Z0-9_.-]+$", p)
        for p in repo_parts
    ):
        raise ValueError(f"Invalid repository in release descriptor: {repo!r}")

    gen_at = data["generatedAt"]
    if not isinstance(gen_at, str) or not re.fullmatch(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", gen_at):
        raise ValueError(f"Invalid generatedAt format in release descriptor: {gen_at!r}")
    try:
        datetime.strptime(gen_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"Invalid calendar date in generatedAt: {exc}") from exc

    bundle_paths = data["bundlePaths"]
    if not isinstance(bundle_paths, list):
        raise ValueError("bundlePaths must be a list")
    for bp in bundle_paths:
        if not isinstance(bp, str):
            raise ValueError(f"bundlePaths item must be string, got {bp!r}")
        validate_safe_relative_path(bp)
    if bundle_paths != sorted(list(set(bundle_paths))):
        raise ValueError(f"bundlePaths must be uniquely sorted without duplicates: {bundle_paths}")
    validate_case_prefix_collisions(bundle_paths)

    # Validate bundlePaths against lock
    lock_file = sources_root / "sources" / "manifest.lock.json"
    if not lock_file.is_file() or _is_symlink_or_junction(lock_file):
        raise ValueError(f"Lock file missing or invalid link: {lock_file}")
    verify_exact_file_on_disk(sources_root, "sources/manifest.lock.json")
    lock_entries = parse_and_validate_lock(lock_file)
    known_paths = {e.path for e in lock_entries}
    unknown_bp = set(bundle_paths) - known_paths
    if unknown_bp:
        raise ValueError(f"bundlePaths contains paths not present in lock: {sorted(unknown_bp)}")

    companions = data["companions"]
    if not isinstance(companions, list):
        raise ValueError("companions must be a list")

    morph_comps = [c for c in companions if isinstance(c, dict) and c.get("filename") == "quran-morphology-source.zip"]
    if len(morph_comps) != 1:
        raise ValueError("companions must contain exactly one quran-morphology-source.zip entry")

    comp_filenames: list[str] = []
    comp_expected_keys = {"filename", "sizeBytes", "sha256", "sourcePath"}
    for comp in companions:
        if not isinstance(comp, dict):
            raise ValueError("companion item must be an object")
        if set(comp.keys()) != comp_expected_keys:
            raise ValueError(f"companion item keys mismatch: {set(comp.keys())}")
        fn = comp["filename"]
        if not isinstance(fn, str):
            raise ValueError("companion filename must be string")
        validate_safe_relative_path(fn)
        if "/" in fn or "\\" in fn:
            raise ValueError(f"companion filename must be a flat basename: {fn!r}")
        if fn in ("catalog.json", "audio-manifest.json"):
            raise ValueError(f"companion filename collides with manifest: {fn}")
        if fn.endswith(".db") or fn.startswith("adhan-") or fn.endswith(".m4a") or fn.endswith(".caf"):
            raise ValueError(f"companion filename collides with pack assets: {fn}")
        comp_filenames.append(fn)

        size_b = comp["sizeBytes"]
        if not isinstance(size_b, int) or isinstance(size_b, bool) or size_b <= 0:
            raise ValueError(f"companion sizeBytes must be positive int, got {size_b!r}")

        sha = comp["sha256"]
        if not isinstance(sha, str) or not re.fullmatch(r"^[0-9a-f]{64}$", sha):
            raise ValueError(f"companion sha256 must be 64 lowercase hex digits: {sha!r}")

        sp = comp["sourcePath"]
        if fn == "quran-morphology-source.zip":
            if sp is not None:
                raise ValueError("quran-morphology-source.zip sourcePath must be null")
        else:
            if not isinstance(sp, str):
                raise ValueError(f"companion {fn} sourcePath must be string, got {sp!r}")
            validate_safe_relative_path(sp)
            if not sp.startswith("sources/licenses/evidence/"):
                raise ValueError(f"companion sourcePath must be under sources/licenses/evidence/: {sp}")
            sp_path = sources_root / sp
            if not sp_path.is_file() or _is_symlink_or_junction(sp_path):
                raise ValueError(f"companion sourcePath does not exist as regular file: {sp_path}")
            verify_exact_file_on_disk(sources_root, sp)
            if fn != Path(sp).name:
                raise ValueError(f"companion filename {fn!r} does not match sourcePath basename {Path(sp).name!r}")

    comp_lower = [fn.lower() for fn in comp_filenames]
    if len(comp_lower) != len(set(comp_lower)):
        raise ValueError("Case-insensitive duplicate companion filenames detected")
    validate_case_prefix_collisions(comp_filenames)

    if comp_filenames != sorted(comp_filenames):
        raise ValueError(f"companions must be sorted by filename without duplicates: {comp_filenames}")

    return data


def assemble_companions(
    sources_root: Path,
    asset_root: Path,
    notice_paths: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Assemble deterministic whitelist-only morphology source ZIP and verbatim companions."""
    morphology_zip_name = "quran-morphology-source.zip"
    zip_dest = asset_root / morphology_zip_name
    if zip_dest.exists() or _is_symlink_or_junction(zip_dest):
        raise FileExistsError(f"Target companion already exists or is link: {zip_dest}")

    # Preflight notice paths
    for np_str in notice_paths:
        validate_safe_relative_path(np_str)
        src_notice = sources_root / np_str
        if not src_notice.is_file() or _is_symlink_or_junction(src_notice):
            raise ValueError(f"Notice companion source missing or link: {np_str}")
        verify_exact_file_on_disk(sources_root, np_str)
        dest_np = asset_root / Path(np_str).name
        if dest_np.exists() or _is_symlink_or_junction(dest_np):
            raise FileExistsError(f"Target companion notice already exists or is link: {dest_np}")

    # Preflight all whitelist members and verify locked raw bytes
    lock_file = sources_root / "sources" / "manifest.lock.json"
    if not lock_file.is_file() or _is_symlink_or_junction(lock_file):
        raise ValueError(f"Lock file missing or link: {lock_file}")
    verify_exact_file_on_disk(sources_root, "sources/manifest.lock.json")
    lock_entries = parse_and_validate_lock(lock_file)
    lock_by_path = {e.path: e for e in lock_entries}

    sorted_members = sorted(MORPHOLOGY_SOURCE_ARCHIVE_MEMBERS)
    member_bytes: dict[str, bytes] = {}
    for member in sorted_members:
        validate_safe_relative_path(member)
        src_member = sources_root / member
        if not src_member.is_file() or _is_symlink_or_junction(src_member):
            raise ValueError(f"Whitelisted source archive member missing or link: {member}")
        verify_exact_file_on_disk(sources_root, member)
        content = src_member.read_bytes()
        if member.startswith("raw-inputs/"):
            rel_lock_path = member[len("raw-inputs/") :]
            exp_entry = lock_by_path.get(rel_lock_path)
            if exp_entry is None:
                raise ValueError(f"Raw archive member not found in lock: {member}")
            if len(content) != exp_entry.size_bytes:
                raise ValueError(f"Raw archive member size mismatch for {member}: expected {exp_entry.size_bytes}, got {len(content)}")
            act_sha = hashlib.sha256(content).hexdigest().lower()
            if act_sha != exp_entry.sha256:
                raise ValueError(f"Raw archive member sha256 mismatch for {member}: expected {exp_entry.sha256}, got {act_sha}")
        member_bytes[member] = content

    asset_root.mkdir(parents=True, exist_ok=True)

    # Private staging directory
    stage_dir = Path(tempfile.mkdtemp(prefix=".companions-stage-", dir=asset_root))
    placed: list[tuple[Path, Path]] = []
    try:
        # 1. Author quran-morphology-source.zip in private stage
        stage_zip = stage_dir / morphology_zip_name
        with zipfile.ZipFile(stage_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.comment = b""
            for member in sorted_members:
                content = member_bytes[member]
                zinfo = zipfile.ZipInfo(filename=member, date_time=(1980, 1, 1, 0, 0, 0))
                zinfo.create_system = 3  # Unix
                zinfo.compress_type = zipfile.ZIP_STORED
                zinfo.external_attr = 0o100644 << 16
                zinfo.extra = b""
                zinfo.comment = b""
                zf.writestr(zinfo, content)

        zip_size = stage_zip.stat().st_size
        zip_hash = _compute_sha256(stage_zip)

        results: list[dict[str, Any]] = [
            {
                "filename": morphology_zip_name,
                "sizeBytes": zip_size,
                "sha256": zip_hash,
                "sourcePath": None,
            }
        ]

        # 2. Copy verbatim notice paths into private stage
        for np_str in notice_paths:
            src_notice = sources_root / np_str
            dest_notice_fn = Path(np_str).name
            stage_notice = stage_dir / dest_notice_fn
            content = src_notice.read_bytes()
            stage_notice.write_bytes(content)
            results.append({
                "filename": dest_notice_fn,
                "sizeBytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest().lower(),
                "sourcePath": np_str,
            })

        results.sort(key=lambda c: c["filename"])

        # 3. Check targets and move to asset_root via no-replace
        for comp in results:
            target_file = asset_root / comp["filename"]
            if target_file.exists() or _is_symlink_or_junction(target_file):
                raise FileExistsError(f"Target companion already exists or is link: {target_file}")

        for comp in results:
            fn = comp["filename"]
            src_file = stage_dir / fn
            target_file = asset_root / fn
            if target_file.exists() or _is_symlink_or_junction(target_file):
                raise FileExistsError(f"Target companion already exists or is link: {target_file}")
            try:
                os.link(src_file, target_file)
            except OSError:
                if target_file.exists() or _is_symlink_or_junction(target_file):
                    raise FileExistsError(f"Target companion already exists or is link: {target_file}") from None
                raise
            placed.append((src_file, target_file))

        return results
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


def acquire_sources(
    sources_root: Path,
    bundle_archive: Path | None = None,
    allow_network: bool = True,
) -> None:
    """Acquire locked inputs securely via authorized bundle and HTTPS URLs into raw-inputs."""
    desc = load_release_descriptor(sources_root)
    bundle_paths = set(desc["bundlePaths"])

    sources_dir = sources_root / "sources"
    reg_path = sources_dir / "registry.json"
    lock_path = sources_dir / "manifest.lock.json"
    raw_root = sources_root / "raw-inputs"

    if not lock_path.is_file() or _is_symlink_or_junction(lock_path):
        raise ValueError(f"Lock file missing or link: {lock_path}")
    verify_exact_file_on_disk(sources_root, "sources/manifest.lock.json")
    if not reg_path.is_file() or _is_symlink_or_junction(reg_path):
        raise ValueError(f"Registry file missing or link: {reg_path}")
    verify_exact_file_on_disk(sources_root, "sources/registry.json")

    if raw_root.exists() or _is_symlink_or_junction(raw_root):
        raise FileExistsError(f"Target raw-inputs directory already exists or is link: {raw_root}")

    lock_entries = parse_and_validate_lock(lock_path)
    reg_entries = parse_and_validate_registry(reg_path)
    lock_by_path = {e.path: e for e in lock_entries}

    # Verify lock registry correspondence before network
    lock_paths = {e.path for e in lock_entries}
    reg_paths = set(reg_entries.keys())
    if lock_paths != reg_paths:
        raise ValueError(f"Lock and registry paths mismatch: lock has {sorted(lock_paths)}, registry has {sorted(reg_paths)}")
    for rel_p, reg_entry in reg_entries.items():
        lock_entry = lock_by_path[rel_p]
        src_url = reg_entry["sourceUrl"] if isinstance(reg_entry, dict) else reg_entry.source_url
        src_ver = reg_entry["sourceVersion"] if isinstance(reg_entry, dict) else reg_entry.source_version
        if not src_url.startswith("https://"):
            raise ValueError(f"Registry entry for {rel_p} has non-HTTPS source URL: {src_url}")
        if lock_entry.source_url != src_url:
            raise ValueError(f"sourceUrl mismatch for {rel_p}: registry has {src_url!r}, lock has {lock_entry.source_url!r}")
        if lock_entry.source_version != src_ver:
            raise ValueError(f"sourceVersion mismatch for {rel_p}: registry has {src_ver!r}, lock has {lock_entry.source_version!r}")
    opener = urllib.request.build_opener(StrictHTTPSRedirectHandler())

    temp_raw = Path(tempfile.mkdtemp(prefix="raw-staging-", dir=sources_root))
    bundle_temp_dir: Path | None = None
    try:
        # Handle bundle if declared
        if bundle_paths:
            bundle_zip_path = bundle_archive
            if bundle_zip_path is None:
                bundle_url = os.environ.get("CONTENT_AUTHORIZED_INPUT_BUNDLE_URL")
                if not bundle_url:
                    raise ValueError("CONTENT_AUTHORIZED_INPUT_BUNDLE_URL environment variable required for bundle acquisition")
                if not bundle_url.startswith("https://"):
                    raise ValueError("CONTENT_AUTHORIZED_INPUT_BUNDLE_URL must be an HTTPS URL")

                if not allow_network:
                    raise RuntimeError("Network acquisition required for bundle but network disabled")

                # Private bundle archive outside staged raw subtree
                bundle_temp_dir = Path(tempfile.mkdtemp(prefix=".bundle-stage-", dir=sources_root))
                download_dest = bundle_temp_dir / "bundle.zip"
                try:
                    req = urllib.request.Request(bundle_url, headers={"User-Agent": "islamic-core-content-release/1.0"})
                    with opener.open(req) as resp:
                        with open(download_dest, "wb") as out_f:
                            while chunk := resp.read(65536):
                                out_f.write(chunk)
                except Exception:
                    raise RuntimeError("Failed to securely download bundle archive from configured URL") from None

                bundle_zip_path = download_dest

            if not bundle_zip_path.is_file() or _is_symlink_or_junction(bundle_zip_path):
                raise ValueError("Bundle archive not found or invalid link")

            with zipfile.ZipFile(bundle_zip_path, "r") as zf:
                infolist = zf.infolist()
                if len(infolist) != len(bundle_paths):
                    raise ValueError(f"Bundle member count mismatch: expected {len(bundle_paths)}, got {len(infolist)}")
                zip_names = [info.filename for info in infolist]
                if len(zip_names) != len(set(zip_names)):
                    raise ValueError("Duplicate members detected in bundle ZIP")
                if set(zip_names) != bundle_paths:
                    raise ValueError(f"Bundle member set mismatch: expected {sorted(bundle_paths)}, got {sorted(zip_names)}")
                validate_case_prefix_collisions(zip_names)

                for info in infolist:
                    validate_safe_relative_path(info.filename)
                    if info.is_dir() or info.filename.endswith("/") or info.filename.endswith("\\"):
                        raise ValueError(f"Bundle directory member not permitted: {info.filename}")
                    if (info.external_attr & 0x10) != 0:
                        raise ValueError(f"Bundle member has MS-DOS directory attribute: {info.filename}")
                    mode = info.external_attr >> 16
                    type_bits = mode & 0o170000
                    if type_bits != 0 and type_bits != 0o100000:
                        raise ValueError(f"Bundle ZIP member is explicitly non-regular: {info.filename}")

                    exp_entry = lock_by_path.get(info.filename)
                    if exp_entry is None:
                        raise ValueError(f"Unlisted bundle member: {info.filename}")
                    if info.file_size != exp_entry.size_bytes:
                        raise ValueError(f"Bundle member size mismatch for {info.filename}: expected {exp_entry.size_bytes}, got {info.file_size}")

                    data = zf.read(info)
                    if len(data) != exp_entry.size_bytes:
                        raise ValueError(f"Bundle member read size mismatch for {info.filename}")
                    actual_h = hashlib.sha256(data).hexdigest().lower()
                    if actual_h != exp_entry.sha256:
                        raise ValueError(f"Bundle member sha256 mismatch for {info.filename}")

                    target = temp_raw / info.filename
                    if target.exists() or _is_symlink_or_junction(target):
                        raise FileExistsError(f"Raw input target already exists or is link: {target}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
        # Handle non-bundle paths
        non_bundle_paths = [e.path for e in lock_entries if e.path not in bundle_paths]
        for rel_p in non_bundle_paths:
            exp_entry = lock_by_path[rel_p]
            reg_entry = reg_entries.get(rel_p)
            if not reg_entry:
                raise ValueError(f"Registry entry missing for {rel_p}")
            src_url = reg_entry["sourceUrl"] if isinstance(reg_entry, dict) else reg_entry.source_url
            if not src_url.startswith("https://"):
                raise ValueError(f"Non-HTTPS source URL for {rel_p}")

            if not allow_network:
                raise RuntimeError(f"Network acquisition required for {rel_p} but network disabled")

            req = urllib.request.Request(src_url, headers={"User-Agent": "islamic-core-content-release/1.0"})
            h = hashlib.sha256()
            received_bytes = 0
            target = temp_raw / rel_p
            target.parent.mkdir(parents=True, exist_ok=True)
            with opener.open(req) as resp:
                cl = resp.headers.get("Content-Length")
                if cl is not None:
                    try:
                        cl_int = int(cl)
                        if cl_int != exp_entry.size_bytes:
                            raise ValueError(f"Content-Length mismatch for {rel_p}: expected {exp_entry.size_bytes}, got {cl_int}")
                    except (ValueError, TypeError):
                        pass

                with open(target, "wb") as out_f:
                    while chunk := resp.read(65536):
                        received_bytes += len(chunk)
                        if received_bytes > exp_entry.size_bytes:
                            raise ValueError(f"Downloaded stream exceeded expected size for {rel_p}")
                        h.update(chunk)
                        out_f.write(chunk)

            if received_bytes != exp_entry.size_bytes:
                raise ValueError(f"Downloaded size mismatch for {rel_p}: expected {exp_entry.size_bytes}, got {received_bytes}")
            actual_h = h.hexdigest().lower()
            if actual_h != exp_entry.sha256:
                raise ValueError(f"Downloaded sha256 mismatch for {rel_p}")

        # Verify staging with verify_lock
        verify_lock(reg_path, temp_raw, lock_path)

        if raw_root.exists() or _is_symlink_or_junction(raw_root):
            raise FileExistsError(f"Target raw-inputs directory already exists or is link: {raw_root}")
        temp_raw.rename(raw_root)
    finally:
        if bundle_temp_dir and bundle_temp_dir.exists():
            shutil.rmtree(bundle_temp_dir, ignore_errors=True)
        if temp_raw.exists():
            shutil.rmtree(temp_raw, ignore_errors=True)

def require_clearance(sources_root: Path) -> None:
    """Pre-build gate to ensure all production licenses are strictly approved."""
    from pipelines.validators.quality_gate import validate_release_sources

    validate_release_sources(sources_root, local_review=False)


def verify_download(expected_root: Path, asset_root: Path) -> None:
    """Offline verification of complete downloaded release assets and consumer query."""
    if not expected_root.is_dir() or _is_symlink_or_junction(expected_root):
        raise ValueError(f"expected_root must be a regular directory: {expected_root}")
    if not asset_root.is_dir() or _is_symlink_or_junction(asset_root):
        raise ValueError(f"asset_root must be a regular directory: {asset_root}")

    # Inspect complete directory inventory: no directories, links, or extra entries
    exp_entries: dict[str, Path] = {}
    for entry in expected_root.iterdir():
        if not entry.is_file() or _is_symlink_or_junction(entry):
            raise ValueError(f"Non-regular entry in expected_root: {entry.name}")
        exp_entries[entry.name] = entry

    act_entries: dict[str, Path] = {}
    for entry in asset_root.iterdir():
        if not entry.is_file() or _is_symlink_or_junction(entry):
            raise ValueError(f"Non-regular entry in asset_root: {entry.name}")
        act_entries[entry.name] = entry

    if sorted(act_entries.keys()) != sorted(exp_entries.keys()):
        raise ValueError(
            f"Downloaded asset inventory mismatch: expected {sorted(exp_entries.keys())}, got {sorted(act_entries.keys())}"
        )

    for fn, act_p in act_entries.items():
        exp_p = exp_entries[fn]
        if exp_p.stat().st_size != act_p.stat().st_size:
            raise ValueError(f"Size mismatch on {fn}: expected {exp_p.stat().st_size}, got {act_p.stat().st_size}")
        exp_h = _compute_sha256(exp_p)
        act_h = _compute_sha256(act_p)
        if exp_h != act_h:
            raise ValueError(f"SHA-256 mismatch on {fn}: expected {exp_h}, got {act_h}")

    # Consumer query verification on quran-core.db
    core_db = asset_root / "quran-core.db"
    if not core_db.is_file():
        raise ValueError("quran-core.db missing from downloaded assets")

    uri = f"{core_db.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA user_version;")
        row = cur.fetchone()
        if not row or row[0] != 1:
            raise ValueError(f"quran-core.db user_version mismatch: expected 1, got {row[0] if row else None}")

        cur.execute(
            "SELECT a.id, a.verse_key FROM ayahs_fts f JOIN ayahs a ON f.rowid = a.id "
            "WHERE ayahs_fts MATCH ? AND f.rowid = 2 ORDER BY a.id;",
            ['"رب العالمين"'],
        )
        rows = cur.fetchall()
        if len(rows) != 1 or rows[0] != (2, "1:2"):
            raise ValueError(f"Consumer FTS query for 'رب العالمين' expected [(2, '1:2')], got {rows}")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release packs CLI helper", add_help=False)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # assemble-companions
    p_comp = subparsers.add_parser("assemble-companions")
    p_comp.add_argument("--sources-root", required=True)
    p_comp.add_argument("--asset-root", required=True)

    # acquire
    p_acq = subparsers.add_parser("acquire")
    p_acq.add_argument("--sources-root", required=True)

    # require-clearance
    p_clear = subparsers.add_parser("require-clearance")
    p_clear.add_argument("--sources-root", required=True)

    # verify-download
    p_vd = subparsers.add_parser("verify-download")
    p_vd.add_argument("--expected-root", required=True)
    p_vd.add_argument("--asset-root", required=True)

    try:
        args = parser.parse_args(argv)
    except Exception as exc:
        sys.stderr.write(f"CLI error: {exc}\n")
        return 2

    if args.subcommand == "assemble-companions":
        sources_root = Path(args.sources_root)
        asset_root = Path(args.asset_root)
        desc = load_release_descriptor(sources_root)
        notice_paths = tuple(
            c["sourcePath"] for c in desc["companions"] if c.get("sourcePath") is not None
        )
        try:
            assembled = assemble_companions(sources_root, asset_root, notice_paths)
            if assembled != desc["companions"]:
                for c in assembled:
                    p = asset_root / c["filename"]
                    if p.exists() or _is_symlink_or_junction(p):
                        try:
                            p.unlink()
                        except OSError:
                            pass
                sys.stderr.write(f"Assembled companions mismatch against descriptor: {assembled} vs {desc['companions']}\n")
                return 1
            return 0
        except Exception as exc:
            sys.stderr.write(f"assemble-companions failed: {exc}\n")
            return 1

    elif args.subcommand == "acquire":
        sources_root = Path(args.sources_root)
        try:
            acquire_sources(sources_root)
            return 0
        except Exception as exc:
            sys.stderr.write(f"acquire failed: {exc}\n")
            return 1

    elif args.subcommand == "require-clearance":
        sources_root = Path(args.sources_root)
        try:
            require_clearance(sources_root)
            return 0
        except Exception as exc:
            sys.stderr.write(f"Clearance check failed: {exc}\n")
            return 1

    elif args.subcommand == "verify-download":
        expected_root = Path(args.expected_root)
        asset_root = Path(args.asset_root)
        try:
            verify_download(expected_root, asset_root)
            return 0
        except Exception as exc:
            sys.stderr.write(f"verify-download failed: {exc}\n")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
