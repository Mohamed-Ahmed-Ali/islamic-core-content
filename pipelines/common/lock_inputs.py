"""Cryptographic input lockfile authoring, canonical serialization, and verification."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit
import unicodedata

_WINDOWS_DEVICE_NAMES = frozenset({
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

_VALID_COMPONENT_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")


@dataclass(frozen=True)
class LockEntry:
    path: str
    source_url: str
    source_version: str
    size_bytes: int
    sha256: str


def validate_safe_relative_path(path_str: str) -> None:
    """Validate that path uses '/', has no traversal, no device names, and valid characters."""
    if not isinstance(path_str, str) or not path_str:
        raise ValueError(f"Path must be a non-empty string, got {path_str!r}")
    if "\\" in path_str:
        raise ValueError(f"Backslashes not permitted in path: {path_str!r}")
    if path_str.startswith("/") or path_str.endswith("/"):
        raise ValueError(f"Path must not have leading or trailing slashes: {path_str!r}")

    parts = path_str.split("/")
    for part in parts:
        if not part:
            raise ValueError(f"Empty path component in {path_str!r}")
        if part in (".", ".."):
            raise ValueError(f"Path traversal component {part!r} in {path_str!r}")
        if not _VALID_COMPONENT_PATTERN.fullmatch(part):
            raise ValueError(f"Invalid characters in path component {part!r} of {path_str!r}")
        if part.endswith("."):
            raise ValueError(f"Component ends with dot in {path_str!r}")
        base_name = part.split(".")[0].upper()
        if base_name in _WINDOWS_DEVICE_NAMES:
            raise ValueError(f"Reserved Windows device name {base_name!r} in {path_str!r}")


def validate_case_prefix_collisions(paths: Sequence[str]) -> None:
    """Ensure no two paths or directory prefixes collide case-insensitively,
    no duplicates, and no file-as-ancestor incompatibilities.
    """
    seen_prefixes: dict[str, str] = {}
    path_set = set(paths)
    if len(paths) != len(path_set):
        raise ValueError("Paths contain duplicates")
    lower_file_paths: dict[str, str] = {}
    for p in paths:
        lower = p.lower()
        if lower in lower_file_paths:
            raise ValueError(f"Case collision between paths: {p!r} and {lower_file_paths[lower]!r}")
        lower_file_paths[lower] = p
    for p in paths:
        parts = p.split("/")
        for i in range(1, len(parts) + 1):
            subpath = "/".join(parts[:i])
            lower = subpath.lower()
            if lower in seen_prefixes and seen_prefixes[lower] != subpath:
                raise ValueError(
                    f"Case-prefix collision detected between {seen_prefixes[lower]!r} and {subpath!r}"
                )
            seen_prefixes[lower] = subpath
            if i < len(parts) and lower in lower_file_paths:
                raise ValueError(
                    f"File-as-ancestor inventory incompatibility: {lower_file_paths[lower]!r} cannot be both a file and directory prefix of {p!r}"
                )

def validate_https_url(url: str) -> None:
    """Ensure url is a valid absolute HTTPS URL."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"URL must start with 'https://', got {url!r}")
    for ch in url:
        if ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or unicodedata.category(ch).startswith("C"):
            raise ValueError(f"Whitespace or control character not permitted in URL: {url!r}")
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"URL scheme must be 'https', got {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(f"User credentials not permitted in URL: {url!r}")
    try:
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"Malformed hostname in URL {url!r}: {exc}") from exc
    if not hostname:
        raise ValueError(f"Missing or empty hostname in URL: {url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Malformed port in URL {url!r}: {exc}") from exc
    host_port_suffix = parsed.netloc.split("]")[-1]
    if ":" in host_port_suffix and port is None:
        raise ValueError(f"Malformed empty port in URL: {url!r}")
    if port is not None and (port < 1 or port > 65535):
        raise ValueError(f"Port {port} out of range in URL: {url!r}")

def _no_duplicate_keys_object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for k, v in pairs:
        if k in res:
            raise ValueError(f"Duplicate JSON object key: {k!r}")
        res[k] = v
    return res


def _reject_float_tokens(token: str) -> None:
    raise ValueError(f"Floating point token {token!r} not allowed")


def _reject_constant(token: str) -> None:
    raise ValueError(f"Nonfinite JSON constant {token!r} not allowed")


def _validate_decoded_string(s: str) -> None:
    if "\ufffd" in s:
        raise ValueError("Decoded JSON contains replacement character U+FFFD")
    for ch in s:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise ValueError(f"Decoded JSON contains surrogate character U+{ord(ch):04X}")


def _validate_decoded_chars_recursively(obj: Any) -> None:
    if isinstance(obj, str):
        _validate_decoded_string(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                _validate_decoded_string(k)
            _validate_decoded_chars_recursively(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _validate_decoded_chars_recursively(item)


def parse_strict_json(raw_text: str) -> Any:
    """Parse JSON enforcing strict UTF-8, no duplicate keys, and no float literals."""
    _validate_decoded_string(raw_text)
    data = json.loads(
        raw_text,
        object_pairs_hook=_no_duplicate_keys_object_pairs_hook,
        parse_float=_reject_float_tokens,
        parse_constant=_reject_constant,
    )
    _validate_decoded_chars_recursively(data)
    return data


def _is_symlink_or_junction(p: Path | str) -> bool:
    if os.path.islink(p):
        return True
    if sys.platform == "win32" and hasattr(os.path, "isjunction") and os.path.isjunction(p):
        return True
    return False


def _validate_raw_root(raw_root: Path | str) -> Path:
    p = Path(raw_root)
    if _is_symlink_or_junction(p):
        raise ValueError(f"Symlink or junction not permitted at raw root: {p}")
    if not p.is_dir():
        raise FileNotFoundError(f"Raw root directory not found or not a directory: {p}")
    return p


def verify_exact_file_on_disk(raw_root: Path, rel_path: str) -> tuple[int, str]:
    """Verify that rel_path exists under raw_root with exact case, is not a symlink/junction,
    and return (size_bytes, sha256).
    """
    current = _validate_raw_root(raw_root)
    components = rel_path.split("/")
    for idx, comp in enumerate(components):
        is_last = (idx == len(components) - 1)
        found_entry = None
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name == comp:
                        found_entry = entry
                        break
        except FileNotFoundError:
            raise FileNotFoundError(f"Directory not found: {current}")

        if found_entry is None:
            raise FileNotFoundError(
                f"Component {comp!r} of path {rel_path!r} not found with exact case in {current}"
            )

        if _is_symlink_or_junction(found_entry.path):
            raise ValueError(f"Symlink or junction not permitted at {found_entry.path}")

        if is_last:
            if not found_entry.is_file(follow_symlinks=False):
                raise ValueError(f"Expected regular file at {found_entry.path}")
            current = Path(found_entry.path)
        else:
            if not found_entry.is_dir(follow_symlinks=False):
                raise ValueError(f"Expected directory at {found_entry.path}")
            current = Path(found_entry.path)

    # Compute streaming hash and size
    hasher = hashlib.sha256()
    size_bytes = 0
    with open(current, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
            size_bytes += len(chunk)

    return size_bytes, hasher.hexdigest().lower()


def canonical_entries_json(entries: Sequence[LockEntry | dict[str, Any]]) -> bytes:
    """Serialize a sequence of lock entries to canonical JSON array with trailing LF.

    Fixed ordered keys: path, sourceUrl, sourceVersion, sizeBytes, sha256.
    Compact separators, literal UTF-8, no BOM, trailing LF.
    """
    raw_list: list[dict[str, Any]] = []
    for e in entries:
        if isinstance(e, LockEntry):
            raw_list.append({
                "path": e.path,
                "sourceUrl": e.source_url,
                "sourceVersion": e.source_version,
                "sizeBytes": e.size_bytes,
                "sha256": e.sha256,
            })
        else:
            raw_list.append({
                "path": e["path"],
                "sourceUrl": e["sourceUrl"],
                "sourceVersion": e["sourceVersion"],
                "sizeBytes": e["sizeBytes"],
                "sha256": e["sha256"],
            })
    return (
        json.dumps(raw_list, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        + b"\n"
    )


def canonical_lock_json(entries: Sequence[LockEntry | dict[str, Any]]) -> bytes:
    """Serialize complete lock document in fixed key order to canonical JSON with trailing LF."""
    array_bytes = canonical_entries_json(entries)
    return b'{"lockVersion":1,"inputs":' + array_bytes.rstrip(b"\n") + b"}\n"


def parse_and_validate_registry(registry_path: Path) -> dict[str, dict[str, Any]]:
    raw_bytes = registry_path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Registry file is not valid UTF-8: {exc}") from exc

    data = parse_strict_json(text)
    if not isinstance(data, dict):
        raise ValueError("Registry root must be a JSON object")

    paths = list(data.keys())
    validate_case_prefix_collisions(paths)

    for p, val in data.items():
        validate_safe_relative_path(p)
        if not isinstance(val, dict):
            raise ValueError(f"Registry value for {p!r} must be an object")
        keys = list(val.keys())
        if sorted(keys) != ["packIds", "sourceUrl", "sourceVersion"]:
            raise ValueError(f"Registry entry for {p!r} must have exactly sourceUrl, sourceVersion, packIds")

        validate_https_url(val["sourceUrl"])

        src_ver = val["sourceVersion"]
        if not isinstance(src_ver, str) or not src_ver:
            raise ValueError(f"sourceVersion for {p!r} must be non-empty string")

        pids = val["packIds"]
        if not isinstance(pids, list) or not pids:
            raise ValueError(f"packIds for {p!r} must be a non-empty list")
        if not all(isinstance(x, str) and x for x in pids):
            raise ValueError(f"packIds elements for {p!r} must be non-empty strings")
        if len(pids) != len(set(pids)):
            raise ValueError(f"packIds for {p!r} contains duplicates: {pids}")
        if pids != sorted(pids):
            raise ValueError(f"packIds for {p!r} must be lexically sorted: {pids}")

    return data


def parse_and_validate_lock(lock_path: Path) -> list[LockEntry]:
    """Parse and validate sources/manifest.lock.json."""
    if not lock_path.is_file():
        raise FileNotFoundError(f"Lock file not found: {lock_path}")

    raw_bytes = lock_path.read_bytes()
    if not raw_bytes.endswith(b"\n"):
        raise ValueError(f"Lock file {lock_path} must end with LF")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Lock file is not valid UTF-8: {exc}") from exc

    data = parse_strict_json(text)
    if not isinstance(data, dict):
        raise ValueError("Lock root must be a JSON object")

    root_keys = list(data.keys())
    if root_keys != ["lockVersion", "inputs"]:
        raise ValueError(f"Lock root must have exactly keys ['lockVersion', 'inputs'], got {root_keys}")

    lock_version = data["lockVersion"]
    if type(lock_version) is not int or lock_version != 1:
        raise ValueError(f"lockVersion must be integer 1, got {lock_version!r}")

    inputs = data["inputs"]
    if not isinstance(inputs, list):
        raise ValueError("Lock inputs must be a list")

    entries: list[LockEntry] = []
    paths: list[str] = []
    seen_paths: set[str] = set()

    for idx, item in enumerate(inputs):
        if not isinstance(item, dict):
            raise ValueError(f"Lock input entry {idx} must be an object")

        item_keys = list(item.keys())
        expected_keys = ["path", "sourceUrl", "sourceVersion", "sizeBytes", "sha256"]
        if item_keys != expected_keys:
            raise ValueError(f"Lock entry {idx} keys must be exactly {expected_keys}, got {item_keys}")

        p = item["path"]
        validate_safe_relative_path(p)
        if p in seen_paths:
            raise ValueError(f"Duplicate path in lock entries: {p!r}")
        seen_paths.add(p)
        paths.append(p)

        src_url = item["sourceUrl"]
        validate_https_url(src_url)

        src_ver = item["sourceVersion"]
        if not isinstance(src_ver, str) or not src_ver:
            raise ValueError(f"sourceVersion for {p!r} must be non-empty string")

        size_b = item["sizeBytes"]
        if type(size_b) is not int or isinstance(size_b, bool) or size_b < 0:
            raise ValueError(f"sizeBytes for {p!r} must be non-negative integer, got {size_b!r}")

        h_val = item["sha256"]
        if not isinstance(h_val, str) or len(h_val) != 64 or not all(c in "0123456789abcdef" for c in h_val):
            raise ValueError(f"sha256 for {p!r} must be 64 lowercase hex digits, got {h_val!r}")

        entries.append(LockEntry(
            path=p,
            source_url=src_url,
            source_version=src_ver,
            size_bytes=size_b,
            sha256=h_val,
        ))

    validate_case_prefix_collisions(paths)

    if paths != sorted(paths):
        raise ValueError("Lock entries must be sorted lexically by path")

    # Verify canonical bytes byte-for-byte
    canonical_bytes = canonical_lock_json(entries)
    if raw_bytes != canonical_bytes:
        raise ValueError("Lock file bytes do not match canonical compact serialization")

    return entries


def verify_lock(
    registry_path: Path,
    raw_root: Path,
    lock_path: Path,
    *,
    pack_id: str | None = None,
) -> tuple[LockEntry, ...]:
    """Validate entire registry, entire lock, and correspondence.

    If pack_id is provided, verifies that pack_id is known in the registry and verifies
    raw file bytes on disk only for inputs belonging to that pack.
    Returns matching LockEntry tuple in lock order.
    Never rewrites lock_path.
    """
    registry_path = Path(registry_path)
    raw_root = _validate_raw_root(raw_root)
    lock_path = Path(lock_path)

    registry_data = parse_and_validate_registry(registry_path)
    lock_entries = parse_and_validate_lock(lock_path)

    reg_paths = set(registry_data.keys())
    lock_paths = {e.path for e in lock_entries}

    if reg_paths != lock_paths:
        diff_reg = sorted(reg_paths - lock_paths)
        diff_lock = sorted(lock_paths - reg_paths)
        raise ValueError(
            f"Registry and lock path sets mismatch. In registry only: {diff_reg}; in lock only: {diff_lock}"
        )

    # Validate provenance correspondence
    for e in lock_entries:
        reg_item = registry_data[e.path]
        if e.source_url != reg_item["sourceUrl"]:
            raise ValueError(
                f"sourceUrl mismatch for {e.path!r}: lock has {e.source_url!r}, registry has {reg_item['sourceUrl']!r}"
            )
        if e.source_version != reg_item["sourceVersion"]:
            raise ValueError(
                f"sourceVersion mismatch for {e.path!r}: lock has {e.source_version!r}, registry has {reg_item['sourceVersion']!r}"
            )

    # Check pack selection
    if pack_id is not None:
        all_packs = {pid for reg_item in registry_data.values() for pid in reg_item["packIds"]}
        if pack_id not in all_packs:
            raise ValueError(f"Unknown pack ID {pack_id!r} not found in registry")
        selected_entries = [
            e for e in lock_entries
            if pack_id in registry_data[e.path]["packIds"]
        ]
    else:
        selected_entries = lock_entries

    # Verify physical raw files only for selected entries
    for e in selected_entries:
        actual_size, actual_sha256 = verify_exact_file_on_disk(raw_root, e.path)
        if actual_size != e.size_bytes:
            raise ValueError(
                f"File size mismatch for {e.path!r}: expected {e.size_bytes}, got {actual_size}"
            )
        if actual_sha256 != e.sha256:
            raise ValueError(
                f"SHA-256 hash mismatch for {e.path!r}: expected {e.sha256}, got {actual_sha256}"
            )

    return tuple(selected_entries)


def author_lock(
    registry_path: Path,
    raw_root: Path,
    output_path: Path,
) -> None:
    """Author manifest.lock.json from sources/registry.json and physical raw files."""
    registry_path = Path(registry_path)
    raw_root = _validate_raw_root(raw_root)
    output_path = Path(output_path)

    registry_data = parse_and_validate_registry(registry_path)

    entries: list[LockEntry] = []
    for rel_path in sorted(registry_data.keys()):
        actual_size, actual_sha256 = verify_exact_file_on_disk(raw_root, rel_path)
        reg_item = registry_data[rel_path]
        entries.append(LockEntry(
            path=rel_path,
            source_url=reg_item["sourceUrl"],
            source_version=reg_item["sourceVersion"],
            size_bytes=actual_size,
            sha256=actual_sha256,
        ))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_bytes = canonical_lock_json(entries)
    output_path.write_bytes(canonical_bytes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Author or verify input manifest lockfile.")
    parser.add_argument("--registry", required=True, type=Path, help="Path to registry.json")
    parser.add_argument("--raw-root", required=True, type=Path, help="Root directory containing raw inputs")
    parser.add_argument("--output", required=True, type=Path, help="Path to manifest.lock.json")
    parser.add_argument("--verify", action="store_true", help="Verify expected lock without writing")
    parser.add_argument("--pack", default=None, type=str, help="Pack ID filter (only valid with --verify)")

    args = parser.parse_args(argv)

    if args.pack is not None and not args.verify:
        sys.stderr.write("CLI misuse: --pack option is only permitted with --verify\n")
        return 2

    try:
        if args.verify:
            verify_lock(args.registry, args.raw_root, args.output, pack_id=args.pack)
        else:
            author_lock(args.registry, args.raw_root, args.output)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Lock inputs error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
