"""Deterministic quality gate validator enforcing the DoD checks for fixture candidates."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from typing import Any
import zipfile

from jsonschema import Draft202012Validator, ValidationError
import jsonschema.validators

from pipelines.common.audio_probe import probe_asset
from pipelines.common.lock_inputs import (
    LockEntry,
    canonical_entries_json,
    parse_strict_json,
    validate_https_url,
    validate_safe_relative_path,
    verify_exact_file_on_disk,
    verify_lock,
    _is_symlink_or_junction,
)
from pipelines.common.search_text import normalize_arabic, project_tafsir_text
from pipelines.common.tanzil_coordinates import load_tanzil_coordinates, load_tanzil_text
from pipelines.builders.build_audio_adhan import NAME_MAP, RECITER_MAP, SLUG_MAP
from pipelines.builders.build_quran_morphology import read_corpus_segments
from pipelines.release.release_packs import MORPHOLOGY_SOURCE_ARCHIVE_MEMBERS, load_release_descriptor
from pipelines.toolchain.check_runtime import verify_runtime
from pipelines.common.pack_sources import load_sqlite_source_metadata


class GateCheckError(Exception):
    def __init__(self, check_num: int, asset: str, reason: str) -> None:
        super().__init__(f"Check #{check_num} failed on {asset}: {reason}")
        self.check_num = check_num
        self.asset = asset
        self.reason = reason


_STAGES: list[tuple[str, int]] = [
    ("Check 0 (preflight)", 0),
    ("Check #6 (root identity)", 6),
    ("Check #2 (manifest text encoding)", 2),
    ("Check #5 (manifest schemas & asset inventory)", 5),
    ("Check #7 (source lock & clearance)", 7),
    ("Check #3 (SQLite schema & integrity)", 3),
    ("Check #7 (SQLite metadata reconciliation)", 7),
    ("Check #1 (record keys)", 1),
    ("Check #2 (text & source equivalence)", 2),
    ("Check #1 (coverage)", 1),
    ("Check #4 (FTS integrity & queries)", 4),
    ("Check #9 (Tafsir intervals & map)", 9),
    ("Check #6 (cross-root byte equality)", 6),
]

_FIXTURE_PATH_PACK_MAP: dict[str, tuple[str, ...]] = {
    "sqlite/quran-core.json": ("quran-core",),
    "sqlite/quran-scripts-extended.json": ("quran-scripts-extended",),
    "sqlite/quran-tr-en-fixture.json": ("quran-tr-en-fixture",),
    "qul/tafsir-ar-fixture.json": ("quran-tafsir-ar-fixture",),
    "sqlite/quran-morphology.json": ("quran-morphology",),
    "sqlite/quran-wbw-en.json": ("quran-wbw-en",),
    "hadith/fixture.json": ("hadith-fixture",),
    "hadith/fixture.splits.json": ("hadith-fixture",),
    "audio/adhan/fixture.wav": ("audio-adhan-core",),
}

_V010_PATH_PACK_MAP: dict[str, tuple[str, ...]] = {
    "audio/adhan/abdul-basit-abdul-samad-2.mp3": ("audio-adhan-core",),
    "audio/adhan/abdul-basit-abdul-samad.mp3": ("audio-adhan-core",),
    "audio/adhan/abul-ainain-shuaisha.mp3": ("audio-adhan-core",),
    "audio/adhan/mahmoud-khalil-al-hussary.mp3": ("audio-adhan-core",),
    "audio/adhan/mohamed-refaat.mp3": ("audio-adhan-core",),
    "audio/adhan/mustafa-ismail.mp3": ("audio-adhan-core",),
    "corpus/quranic-corpus-morphology-0.4.zip": ("quran-morphology",),
    "tanzil/ar.jalalayn.xml": ("quran-tafsir-ar-jalalayn",),
    "tanzil/en.pickthall.xml": ("quran-tr-en-pickthall",),
    "tanzil/quran-data-Tanzil.xml": (
        "quran-core",
        "quran-morphology",
        "quran-scripts-extended",
        "quran-tafsir-ar-jalalayn",
        "quran-tr-en-pickthall",
    ),
    "tanzil/quran-simple-clean.xml": ("quran-core",),
    "tanzil/quran-simple-min.xml": ("quran-scripts-extended",),
    "tanzil/quran-simple-plain.xml": ("quran-scripts-extended",),
    "tanzil/quran-simple.xml": ("quran-scripts-extended",),
    "tanzil/quran-uthmani-min.xml": ("quran-scripts-extended",),
    "tanzil/quran-uthmani.xml": ("quran-core",),
}

_V010_PACK_IDS: tuple[str, ...] = (
    "audio-adhan-core",
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

_V010_PACK_LICENSE_SPDX: dict[str, str] = {
    "quran-core": "LicenseRef-Tanzil-Quran-Text",
    "quran-scripts-extended": "LicenseRef-Tanzil-Quran-Text",
    "quran-tr-en-pickthall": "LicenseRef-Tanzil-Noncommercial-Edition",
    "quran-tafsir-ar-jalalayn": "LicenseRef-Tanzil-Noncommercial-Edition",
    "quran-morphology": "GPL-3.0",
    "audio-adhan-core": "LicenseRef-Adhan-Recordings",
}

_V010_PACK_PRIMARY_PATH: dict[str, str] = {
    "quran-core": "tanzil/quran-uthmani.xml",
    "quran-scripts-extended": "tanzil/quran-uthmani-min.xml",
    "quran-tr-en-pickthall": "tanzil/en.pickthall.xml",
    "quran-tafsir-ar-jalalayn": "tanzil/ar.jalalayn.xml",
    "quran-morphology": "corpus/quranic-corpus-morphology-0.4.zip",
    "audio-adhan-core": "audio/adhan/abdul-basit-abdul-samad.mp3",
}

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

_PACK_SCHEMA_MAP: dict[str, str] = {
    "quran-core": "quran_core.v1.sql",
    "quran-scripts-extended": "quran_scripts_extended.v1.sql",
    "quran-tr-en-fixture": "quran_translation.v1.sql",
    "quran-tr-en-pickthall": "quran_translation.v1.sql",
    "quran-tafsir-ar-fixture": "quran_tafsir.v1.sql",
    "quran-tafsir-ar-jalalayn": "quran_tafsir.v1.sql",
    "quran-morphology": "quran_morphology.v1.sql",
    "quran-wbw-en": "quran_wbw.v1.sql",
    "hadith-fixture": "hadith_book.v1.sql",
}

_PACK_FTS_MAP: dict[str, tuple[str, ...]] = {
    "quran-core": ("ayahs_fts",),
    "quran-scripts-extended": (),
    "quran-tr-en-fixture": ("translations_fts",),
    "quran-tr-en-pickthall": ("translations_fts",),
    "quran-tafsir-ar-fixture": ("tafsir_fts",),
    "quran-tafsir-ar-jalalayn": ("tafsir_fts",),
    "quran-morphology": (),
    "quran-wbw-en": ("word_translations_fts",),
    "hadith-fixture": ("hadiths_fts", "hadiths_full_fts"),
}

_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF]")
_ARABIC_FTS_TABLES = frozenset({"ayahs_fts", "tafsir_fts", "hadiths_fts", "hadiths_full_fts"})
_EXPECTED_COVERAGE_METRICS: dict[str, set[str]] = {
    "quran-core": {"expected_total", "ar_present"},
    "quran-scripts-extended": {"expected_total", "ar_present"},
    "quran-tr-en-fixture": {"expected_total", "en_present", "en_missing"},
    "quran-tr-en-pickthall": {"expected_total", "en_present", "en_missing"},
    "quran-tafsir-ar-fixture": {"expected_total", "ar_present"},
    "quran-tafsir-ar-jalalayn": {"expected_total", "ar_present"},
    "quran-morphology": {"expected_total", "ar_present"},
    "quran-wbw-en": {"expected_total", "ar_present", "en_present", "en_missing"},
    "hadith-fixture": {"expected_total", "ar_present", "en_present", "en_missing"},
}
_HADITH_LOCAL_ORACLE: dict[str, dict[str, Any]] = {
    "bukhari": {
        "id": 1,
        "ar_literal": "حَدَّثَنَا الْحُمَيْدِيُّ",
        "en_literal": "I heard",
        "expected_counts": (7277, 7277, 7276, 1, 7276),
    },
    "muslim": {
        "id": 7278,
        "ar_literal": "حَدَّثَنِي أَبُو خَيْثَمَةَ",
        "en_literal": "Should it so happen",
        "expected_counts": (7459, 7459, 7458, 1, 7458),
    },
    "nasai": {
        "id": 14737,
        "ar_literal": "أَخْبَرَنَا قُتَيْبَةُ",
        "en_literal": "When any one of",
        "expected_counts": (5768, 5768, 5768, 0, 5768),
    },
    "abudawud": {
        "id": 20505,
        "ar_literal": "حَدَّثَنَا عَبْدُ اللَّهِ",
        "en_literal": "When the Prophet",
        "expected_counts": (5276, 5276, 5276, 0, 5276),
    },
    "tirmidhi": {
        "id": 25781,
        "ar_literal": "حَدَّثَنَا قُتَيْبَةُ",
        "en_literal": "the Prophet said",
        "expected_counts": (4053, 4053, 4052, 1, 4052),
    },
    "ibnmajah": {
        "id": 29834,
        "ar_literal": "حَدَّثَنَا أَبُو بَكْرِ",
        "en_literal": "The Messenger of Allah",
        "expected_counts": (4345, 4345, 4345, 0, 4345),
    },
    "malik": {
        "id": 34179,
        "ar_literal": "قَالَ حَدَّثَنِي يَحْيَى",
        "en_literal": "He said",
        "expected_counts": (1985, 1985, 1985, 0, 1985),
    },
    "darimi": {
        "id": 37538,
        "ar_literal": "أَخْبَرَنَا عَلِيُّ",
        "en_literal": "none",
        "expected_counts": (3406, 3406, 0, 3406, 0),
    },
    "ahmad": {
        "id": 1,
        "ar_literal": "عَبْدُ اللَّهِ بْنُ نُمَيْرٍ",
        "en_literal": "none",
        "expected_counts": (26363, 26363, 0, 26363, 0),
    },
}

_HADITH_PILOT_PACK_IDS: tuple[str, ...] = ("hadith-bukhari", "hadith-muslim")
_HADITH_ALL_PACK_IDS: tuple[str, ...] = tuple(f"hadith-{_slug}" for _slug in _HADITH_LOCAL_ORACLE.keys())

for _slug in _HADITH_LOCAL_ORACLE:
    _PACK_SCHEMA_MAP[f"hadith-{_slug}"] = "hadith_book.v1.sql"
    _PACK_FTS_MAP[f"hadith-{_slug}"] = ("hadiths_fts", "hadiths_full_fts")
    _EXPECTED_COVERAGE_METRICS[f"hadith-{_slug}"] = {"expected_total", "ar_present", "en_present", "en_missing"}

_strict_type_checker = Draft202012Validator.TYPE_CHECKER.redefine(
    "integer",
    lambda checker, instance: isinstance(instance, int) and not isinstance(instance, bool),
)
StrictDraft202012Validator = jsonschema.validators.extend(
    Draft202012Validator,
    type_checker=_strict_type_checker,
)


def _recompute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def format_duration_seconds(duration: Fraction) -> float:
    num = duration.numerator
    den = duration.denominator
    if den <= 0 or num < 0:
        raise ValueError(f"Invalid duration fraction: {duration}")
    ms = (2 * num * 1000 + den) // (2 * den)
    if ms <= 0:
        raise ValueError(f"Duration rounded to nonpositive {ms} ms")
    return ms / 1000.0


def _reject_constant(token: str) -> None:
    raise ValueError(f"Nonfinite JSON constant {token!r} not allowed")


def _manifest_object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for k, v in pairs:
        if k in res:
            raise ValueError(f"Duplicate JSON object key: {k!r}")
        res[k] = v
    return res


def parse_manifest_json(raw_text: str) -> Any:
    return json.loads(
        raw_text,
        object_pairs_hook=_manifest_object_pairs_hook,
        parse_constant=_reject_constant,
    )


def _check_no_surrogates_or_fffd(obj: Any, filename: str) -> None:
    if isinstance(obj, str):
        if "\ufffd" in obj:
            raise GateCheckError(2, filename, "Decoded JSON contains replacement character U+FFFD")
        for ch in obj:
            if 0xD800 <= ord(ch) <= 0xDFFF:
                raise GateCheckError(2, filename, f"Decoded JSON contains surrogate character U+{ord(ch):04X}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_no_surrogates_or_fffd(k, filename)
            _check_no_surrogates_or_fffd(v, filename)
    elif isinstance(obj, list):
        for item in obj:
            _check_no_surrogates_or_fffd(item, filename)


def validate_manifest_text(raw_bytes: bytes, filename: str) -> tuple[str, dict[str, Any]]:
    """Check #2: Validate strict UTF-8, no replacement chars, no surrogates, reject duplicate keys."""
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateCheckError(2, filename, f"Malformed UTF-8 bytes: {exc}") from exc

    if "\ufffd" in raw_text or b"\xef\xbf\xbd" in raw_bytes:
        raise GateCheckError(2, filename, "Raw manifest contains replacement character U+FFFD")

    try:
        data = parse_manifest_json(raw_text)
    except Exception as exc:
        raise GateCheckError(2, filename, f"Failed strict JSON parsing: {exc}") from exc

    _check_no_surrogates_or_fffd(data, filename)
    return raw_text, data


def validate_manifest_schema_and_canonical(
    raw_bytes: bytes,
    data: dict[str, Any],
    schema_path: Path,
    filename: str,
) -> None:
    """Check #5: Validate schema with strict int token typing, UTC format, and canonical byte dump."""
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        validator = StrictDraft202012Validator(schema)
        validator.validate(data)
    except ValidationError as exc:
        raise GateCheckError(5, filename, f"Manifest schema validation error: {exc.message}") from exc

    # stdlib UTC date validation
    if "generatedAt" in data:
        ts = data["generatedAt"]
        if not re.fullmatch(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", ts):
            raise GateCheckError(5, filename, f"Invalid UTC timestamp format: {ts!r}")
        try:
            datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise GateCheckError(5, filename, f"Invalid calendar date in generatedAt: {exc}") from exc

    # Canonical bytes check enforces literal non-ASCII UTF-8 (no \u escapes), key sorting, whitespace
    canonical_bytes = (
        json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if raw_bytes != canonical_bytes:
        raise GateCheckError(5, filename, "Manifest bytes do not match canonical JSON serialization")


def validate_release_sources(
    sources_root: Path,
    *,
    local_review: bool = False,
) -> tuple[tuple[LockEntry, ...], dict[str, Any], dict[str, dict[str, Any]]]:
    sources_dir = sources_root / "sources"
    raw_root = sources_root / "raw-inputs"
    reg_path = sources_dir / "registry.json"
    lock_path = sources_dir / "manifest.lock.json"

    if not raw_root.is_dir() or _is_symlink_or_junction(raw_root):
        raise GateCheckError(7, "raw-inputs", "raw-inputs root missing or link")
    if not reg_path.is_file() or _is_symlink_or_junction(reg_path):
        raise GateCheckError(7, "sources/registry.json", "sources/registry.json missing or link")
    if not lock_path.is_file() or _is_symlink_or_junction(lock_path):
        raise GateCheckError(7, "sources/manifest.lock.json", "sources/manifest.lock.json missing or link")

    try:
        lock_entries = verify_lock(reg_path, raw_root, lock_path)
    except Exception as exc:
        raise GateCheckError(7, "lock verification", f"Lock verification failed: {exc}") from exc

    try:
        registry_data = parse_strict_json(reg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GateCheckError(7, "sources/registry.json", f"Failed parsing registry: {exc}") from exc

    expected_reg_paths = set(_V010_PATH_PACK_MAP.keys())
    actual_reg_paths = set(registry_data.keys())
    if actual_reg_paths != expected_reg_paths:
        raise GateCheckError(
            7,
            "sources/registry.json",
            f"Registry path set mismatch: expected {sorted(expected_reg_paths)}, got {sorted(actual_reg_paths)}",
        )

    for p_str, expected_packs in _V010_PATH_PACK_MAP.items():
        entry = registry_data[p_str]
        if tuple(entry.get("packIds", [])) != expected_packs:
            raise GateCheckError(7, "sources/registry.json", f"Pack IDs mismatch for {p_str}")

    license_records: dict[str, dict[str, Any]] = {}
    pending_packs: list[str] = []

    for pack_id in _V010_PACK_IDS:
        rel_lic_path = f"sources/licenses/{pack_id}.json"
        lic_file = sources_dir / "licenses" / f"{pack_id}.json"
        if not lic_file.is_file() or _is_symlink_or_junction(lic_file):
            raise GateCheckError(7, rel_lic_path, f"License record missing or link for {pack_id}")
        try:
            verify_exact_file_on_disk(sources_root, rel_lic_path)
        except Exception as exc:
            raise GateCheckError(7, rel_lic_path, f"Exact file check failed: {exc}") from exc

        try:
            lic_data = parse_strict_json(lic_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise GateCheckError(7, rel_lic_path, f"Malformed license record: {exc}") from exc

        if not isinstance(lic_data, dict):
            raise GateCheckError(7, rel_lic_path, "License record must be a JSON object")

        expected_lic_keys = {"packId", "licenseSpdx", "primaryPath", "status", "reviewedBy", "sources"}
        if set(lic_data.keys()) != expected_lic_keys:
            raise GateCheckError(7, rel_lic_path, f"License record keys mismatch: {set(lic_data.keys())}")

        if lic_data.get("packId") != pack_id:
            raise GateCheckError(7, rel_lic_path, f"packId mismatch: expected {pack_id}")

        expected_spdx = _V010_PACK_LICENSE_SPDX[pack_id]
        if lic_data.get("licenseSpdx") != expected_spdx:
            raise GateCheckError(7, rel_lic_path, f"licenseSpdx mismatch: expected {expected_spdx!r}, got {lic_data.get('licenseSpdx')!r}")

        expected_primary = _V010_PACK_PRIMARY_PATH[pack_id]
        if lic_data.get("primaryPath") != expected_primary:
            raise GateCheckError(7, rel_lic_path, f"primaryPath mismatch: expected {expected_primary!r}, got {lic_data.get('primaryPath')!r}")

        status = lic_data.get("status")
        if status == "fixture":
            raise GateCheckError(7, rel_lic_path, "Fixture status not permitted for release profile v0.1.0")
        if status == "rejected":
            raise GateCheckError(7, rel_lic_path, "License record has rejected status")
        if status not in ("approved", "pending"):
            raise GateCheckError(7, rel_lic_path, f"Unknown license status: {status!r}")

        reviewed_by = lic_data.get("reviewedBy")
        if status == "approved":
            if not isinstance(reviewed_by, str) or not reviewed_by.strip():
                raise GateCheckError(7, rel_lic_path, "Approved license must have non-empty reviewedBy")
        elif status == "pending":
            if reviewed_by is not None:
                raise GateCheckError(7, rel_lic_path, "Pending license must have null reviewedBy")
            if not local_review:
                raise GateCheckError(7, rel_lic_path, f"Clearance pending for pack {pack_id}")
            pending_packs.append(pack_id)

        sources_list = lic_data.get("sources")
        if not isinstance(sources_list, list):
            raise GateCheckError(7, rel_lic_path, "sources must be a list")

        expected_paths = sorted([path for path, packs in _V010_PATH_PACK_MAP.items() if pack_id in packs])
        actual_source_paths = [s.get("path") for s in sources_list if isinstance(s, dict)]
        if actual_source_paths != expected_paths:
            raise GateCheckError(7, rel_lic_path, f"Sources list mismatch: expected {expected_paths}, got {actual_source_paths}")

        for src in sources_list:
            if not isinstance(src, dict):
                raise GateCheckError(7, rel_lic_path, "Source entry must be a JSON object")

            src_path = src.get("path")
            if not isinstance(src_path, str):
                raise GateCheckError(7, rel_lic_path, "Source path must be string")
            validate_safe_relative_path(src_path)
            try:
                verify_exact_file_on_disk(raw_root, src_path)
            except Exception as exc:
                raise GateCheckError(7, rel_lic_path, f"Source path exact-case check failed for {src_path}: {exc}") from exc

            reg_entry = registry_data.get(src_path, {})
            if src.get("sourceUrl") != reg_entry.get("sourceUrl"):
                raise GateCheckError(7, rel_lic_path, f"sourceUrl mismatch for {src_path}")
            if src.get("sourceVersion") != reg_entry.get("sourceVersion"):
                raise GateCheckError(7, rel_lic_path, f"sourceVersion mismatch for {src_path}")

            if pack_id == "audio-adhan-core":
                exp_src_keys = {"attribution", "clipStartMs", "name", "notice", "path", "permittedUses", "reciter", "rightsEvidence", "slug", "sourceUrl", "sourceVersion"}
            else:
                exp_src_keys = {"attribution", "name", "notice", "path", "permittedUses", "rightsEvidence", "sourceUrl", "sourceVersion"}

            if set(src.keys()) != exp_src_keys:
                raise GateCheckError(7, rel_lic_path, f"Source keys mismatch for {src_path}: expected {sorted(exp_src_keys)}, got {sorted(src.keys())}")

            src_name = src.get("name")
            if not isinstance(src_name, str) or not src_name.strip():
                raise GateCheckError(7, rel_lic_path, f"Source name must be non-empty string for {src_path}")

            src_notice = src.get("notice")
            if not isinstance(src_notice, str):
                raise GateCheckError(7, rel_lic_path, f"notice must be string for {src_path}")

            src_attr = src.get("attribution")
            if not isinstance(src_attr, str):
                raise GateCheckError(7, rel_lic_path, f"attribution must be string for {src_path}")

            if status == "approved":
                if not src_notice.strip():
                    raise GateCheckError(7, rel_lic_path, f"notice must be non-empty string when approved for {src_path}")
                if not src_attr.strip():
                    raise GateCheckError(7, rel_lic_path, f"attribution must be non-empty string when approved for {src_path}")

            if pack_id == "audio-adhan-core":
                if src_name != NAME_MAP.get(src_path):
                    raise GateCheckError(7, rel_lic_path, f"Audio source name mismatch for {src_path}: expected {NAME_MAP.get(src_path)!r}, got {src_name!r}")
                slug = src.get("slug")
                if not isinstance(slug, str) or slug != SLUG_MAP.get(src_path):
                    raise GateCheckError(7, rel_lic_path, f"Audio slug mismatch for {src_path}: expected {SLUG_MAP.get(src_path)!r}, got {slug!r}")
                reciter = src.get("reciter")
                if not isinstance(reciter, str) or reciter != RECITER_MAP.get(src_path):
                    raise GateCheckError(7, rel_lic_path, f"Audio reciter mismatch for {src_path}: expected {RECITER_MAP.get(src_path)!r}, got {reciter!r}")
                clip_start = src.get("clipStartMs")
                if type(clip_start) is not int or isinstance(clip_start, bool) or clip_start < 0:
                    raise GateCheckError(7, rel_lic_path, f"clipStartMs must be non-negative integer token, got {clip_start!r}")

            # Validate rightsEvidence
            ev_list = src.get("rightsEvidence")
            if not isinstance(ev_list, list):
                raise GateCheckError(7, rel_lic_path, f"rightsEvidence must be a list for {src_path}")
            if status == "approved" and not ev_list:
                raise GateCheckError(7, rel_lic_path, f"rightsEvidence cannot be empty for approved record for {src_path}")
            if len(ev_list) != len(set(ev_list)):
                raise GateCheckError(7, rel_lic_path, f"rightsEvidence contains duplicates for {src_path}")
            for ev in ev_list:
                if not isinstance(ev, str) or not ev.strip():
                    raise GateCheckError(7, rel_lic_path, f"rightsEvidence item must be non-empty string for {src_path}")
                if ev.startswith("https://"):
                    validate_https_url(ev)
                else:
                    validate_safe_relative_path(ev)
                    if not ev.startswith("sources/licenses/evidence/"):
                        raise GateCheckError(7, rel_lic_path, f"Evidence path must be HTTPS URL or under sources/licenses/evidence/: {ev}")
                    try:
                        verify_exact_file_on_disk(sources_root, ev)
                    except Exception as exc:
                        raise GateCheckError(7, rel_lic_path, f"Evidence file exact check failed: {ev}: {exc}") from exc

            # Validate permittedUses
            perm_uses = src.get("permittedUses")
            if not isinstance(perm_uses, list):
                raise GateCheckError(7, rel_lic_path, f"permittedUses must be a list for {src_path}")
            for u in perm_uses:
                if not isinstance(u, str):
                    raise GateCheckError(7, rel_lic_path, f"permittedUses entry must be string, got {u!r}")
                if u not in _VALID_PERMITTED_USES:
                    raise GateCheckError(7, rel_lic_path, f"Invalid permittedUses token {u!r} for {src_path}")
            if len(perm_uses) != len(set(perm_uses)):
                raise GateCheckError(7, rel_lic_path, f"permittedUses contains duplicates for {src_path}")

            if status == "pending" and perm_uses:
                raise GateCheckError(7, rel_lic_path, f"Pending license cannot grant permittedUses for {src_path}")

            if status == "approved":
                if not perm_uses:
                    raise GateCheckError(7, rel_lic_path, f"Approved license must grant permittedUses for {src_path}")
                req_uses = {"public_redistribution", "commercial_use"}
                if pack_id == "audio-adhan-core":
                    req_uses |= {"transcode", "clip", "fade", "loudness_normalization"}
                else:
                    req_uses.add("sqlite_conversion")
                    if pack_id in ("quran-core", "quran-tr-en-pickthall", "quran-tafsir-ar-jalalayn", "quran-morphology"):
                        req_uses.add("search_projection")
                missing_uses = req_uses - set(perm_uses)
                if missing_uses:
                    raise GateCheckError(7, rel_lic_path, f"Missing required permittedUses for {src_path}: {sorted(missing_uses)}")

        license_records[pack_id] = lic_data

    return lock_entries, registry_data, license_records


class GateContext:
    def __init__(
        self,
        *,
        profile: str,
        candidate: Path,
        rebuild: Path,
        sources_root: Path,
        expectations: dict[str, Any],
        specs_dir: Path,
        local_review: bool = False,
    ) -> None:
        self.profile = profile
        self.candidate = candidate
        self.rebuild = rebuild
        self.sources_root = sources_root
        self.expectations = expectations
        self.specs_dir = specs_dir
        self.local_review = local_review
        self.specs_sql_dir = specs_dir / "sql"
        self.catalog_schema_path = specs_dir / "catalog.schema.json"
        self.audio_schema_path = specs_dir / "audio-manifest.schema.json"
        self.hadith_input_schema_path = specs_dir / "inputs" / "hadith-input.schema.json"
        self.manifest_data_by_root: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.raw_manifest_bytes: dict[str, dict[str, bytes]] = {}
        self.declared_files_by_root: dict[str, set[str]] = {}
        self.lock_entries: tuple[LockEntry, ...] = ()
        self.registry_data: dict[str, Any] = {}
        self.license_records: dict[str, dict[str, Any]] = {}
        self.locked_inputs: dict[str, Any] = {}
        self._tanzil_coords = None
        self._corpus_segments = None

    def get_tanzil_coordinates(self) -> Any:
        if self._tanzil_coords is None:
            self._tanzil_coords = load_tanzil_coordinates(self.sources_root / "raw-inputs" / "tanzil" / "quran-data-Tanzil.xml")
        return self._tanzil_coords

    def get_corpus_segments(self) -> tuple[Any, ...]:
        if self._corpus_segments is None:
            coords = self.get_tanzil_coordinates()
            corpus_zip = self.sources_root / "raw-inputs" / "corpus" / "quranic-corpus-morphology-0.4.zip"
            self._corpus_segments = read_corpus_segments(corpus_zip, coords.ayah_ids)
        return self._corpus_segments

def _fail(stage_idx: int, check_num: int, asset: str, reason: str) -> None:
    stage_name = _STAGES[stage_idx][0]
    blocked = [s[0] for s in _STAGES[stage_idx + 1 :]]
    blocked_str = ", ".join(blocked) if blocked else "none"
    sys.stderr.write(f"{stage_name} failed on {asset}: {reason}\n")
    sys.stderr.write(f"Blocked stages: {blocked_str}\n")
    sys.stderr.write("Check 8: Not applicable (no production QAC/QUL pair).\n")
    sys.exit(1)


def load_locked_json(ctx: GateContext, rel_path: str) -> Any:
    if rel_path in ctx.locked_inputs:
        return ctx.locked_inputs[rel_path]
    raw_root = ctx.sources_root / "raw-inputs"
    target_path = raw_root / rel_path
    rel_sources = str(target_path.relative_to(ctx.sources_root)).replace("\\", "/")
    if not target_path.is_file() or _is_symlink_or_junction(target_path):
        raise GateCheckError(2, rel_sources, f"Locked input {rel_path} missing or invalid link")
    try:
        raw_text = target_path.read_text(encoding="utf-8")
        data = parse_strict_json(raw_text)
        ctx.locked_inputs[rel_path] = data
        return data
    except Exception as exc:
        raise GateCheckError(2, rel_sources, f"Failed parsing locked JSON input: {exc}")


def _fetch_validated_keys(
    cur: sqlite3.Cursor,
    table: str,
    key_col: str,
    order_col: str,
    root_label: str,
    db_fn: str,
    *,
    is_int: bool = False,
) -> list[Any]:
    cur.execute(f"SELECT typeof({key_col}), CAST({key_col} AS BLOB), {key_col} FROM {table} ORDER BY {order_col};")
    keys = []
    for r in cur.fetchall():
        type_str = r[0].decode() if isinstance(r[0], bytes) else str(r[0])
        if is_int:
            if type_str.lower() != "integer":
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Non-INTEGER storage class {type_str!r} in key {table}.{key_col}")
            keys.append(int(r[2]))
        else:
            if type_str.lower() != "text":
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Non-TEXT storage class {type_str!r} in key {table}.{key_col}")
            col_b = r[1]
            if col_b is None:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"NULL key in {table}.{key_col}")
            try:
                decoded = col_b.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Malformed UTF-8 in key {table}.{key_col}: {exc}") from exc
            if "\ufffd" in decoded or b"\xef\xbf\xbd" in col_b:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"U+FFFD replacement character in key {table}.{key_col}")
            for ch in decoded:
                if 0xD800 <= ord(ch) <= 0xDFFF:
                    raise GateCheckError(2, f"{root_label}/{db_fn}", f"Lone surrogate in key {table}.{key_col}")
            keys.append(decoded)
    return keys


def _verify_table_rows_by_pk(
    cur: sqlite3.Cursor,
    table: str,
    columns: list[str],
    pk_col: str,
    expected_rows: list[dict[str, Any]],
    root_label: str,
    db_fn: str,
    *,
    projections: dict[str, Any] | None = None,
    pk_key: str | None = None,
) -> None:
    col_str = ", ".join(columns)
    cur.execute(f"SELECT {col_str} FROM {table} ORDER BY {pk_col};")
    db_rows = cur.fetchall()
    if len(db_rows) != len(expected_rows):
        raise GateCheckError(2, f"{root_label}/{db_fn}", f"Row count mismatch in {table}: DB has {len(db_rows)}, expected {len(expected_rows)}")

    lookup_key = pk_key if pk_key is not None else pk_col
    sorted_expected = sorted(expected_rows, key=lambda r: r[lookup_key])
    for db_row, exp_item in zip(db_rows, sorted_expected):
        for idx, col in enumerate(columns):
            val = db_row[idx]
            val = val.decode("utf-8") if isinstance(val, bytes) else val
            if projections and col in projections:
                exp_val = projections[col](exp_item)
            else:
                exp_val = exp_item[col]
            if val != exp_val:
                raise GateCheckError(
                    2,
                    f"{root_label}/{db_fn}",
                    f"Content mismatch in {table}.{col} at {pk_col}={exp_item[lookup_key]}: got {val!r}, expected {exp_val!r}",
                )

def stage_0_preflight(ctx: GateContext) -> None:
    try:
        verify_runtime()
    except Exception as exc:
        raise GateCheckError(0, "runtime", str(exc)) from exc


def stage_1_root_identity(ctx: GateContext) -> None:
    cand_resolved = ctx.candidate.resolve()
    reb_resolved = ctx.rebuild.resolve()
    if cand_resolved == reb_resolved or os.path.samefile(ctx.candidate, ctx.rebuild):
        raise GateCheckError(6, "candidate and rebuild", "candidate and rebuild point to the same physical directory")
    if _is_symlink_or_junction(ctx.candidate) or _is_symlink_or_junction(ctx.rebuild):
        raise GateCheckError(6, "roots", "symlink or junction not permitted for candidate or rebuild roots")
    if not ctx.candidate.is_dir() or not ctx.rebuild.is_dir():
        raise GateCheckError(6, "roots", "candidate and rebuild roots must be directories")


def stage_2_manifest_text(ctx: GateContext) -> None:
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        return
    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        ctx.raw_manifest_bytes[root_label] = {}
        parsed_manifests: dict[str, dict[str, Any]] = {}
        for manifest_fn in sorted(["audio-manifest.json", "catalog.json"]):
            m_path = root_path / manifest_fn
            if not m_path.exists():
                raise GateCheckError(5, f"{root_label}/{manifest_fn}", f"{manifest_fn} missing")
            if not m_path.is_file() or _is_symlink_or_junction(m_path):
                raise GateCheckError(5, f"{root_label}/{manifest_fn}", f"{manifest_fn} is a link or non-regular file")
            raw_b = m_path.read_bytes()
            ctx.raw_manifest_bytes[root_label][manifest_fn] = raw_b
            _, data = validate_manifest_text(raw_b, f"{root_label}/{manifest_fn}")
            parsed_manifests[manifest_fn] = data
        ctx.manifest_data_by_root[root_label] = (
            parsed_manifests["catalog.json"],
            parsed_manifests["audio-manifest.json"],
        )


def _validate_v010_manifests(
    ctx: GateContext,
    root_label: str,
    root_path: Path,
    cat_data: dict[str, Any],
    audio_data: dict[str, Any],
) -> None:
    desc = load_release_descriptor(ctx.sources_root)
    repo = desc["repository"]
    expected_gen_at = desc["generatedAt"]

    if cat_data["releaseTag"] != "content-v0.1.0":
        raise GateCheckError(5, f"{root_label}/catalog.json", f"Expected releaseTag 'content-v0.1.0', got {cat_data['releaseTag']!r}")
    if cat_data["generatedAt"] != expected_gen_at:
        raise GateCheckError(5, f"{root_label}/catalog.json", f"Expected generatedAt {expected_gen_at!r}, got {cat_data['generatedAt']!r}")
    if audio_data["releaseTag"] != "content-v0.1.0":
        raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Expected releaseTag 'content-v0.1.0', got {audio_data['releaseTag']!r}")
    if audio_data["audioManifestVersion"] != 1:
        raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Expected audioManifestVersion 1, got {audio_data['audioManifestVersion']!r}")
    if audio_data["packId"] != "audio-adhan-core":
        raise GateCheckError(5, f"{root_label}/audio-manifest.json", "audio-manifest packId must be 'audio-adhan-core'")

    # Validate catalog packs
    pack_ids = [p["id"] for p in cat_data["packs"]]
    if pack_ids != sorted(_V010_PACK_IDS):
        raise GateCheckError(5, f"{root_label}/catalog.json", f"Packs mismatch: expected {sorted(_V010_PACK_IDS)}, got {pack_ids}")

    declared_files: set[str] = {"audio-manifest.json", "catalog.json"}
    db_asset_map: dict[str, dict[str, Any]] = {}

    for p in cat_data["packs"]:
        pid = p["id"]
        if p["version"] != "0.1.0":
            raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} version must be '0.1.0'")
        if p.get("completeness") != "complete":
            raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} completeness must be 'complete'")
        if p.get("licenseRef") != f"sources/licenses/{pid}.json":
            raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} licenseRef mismatch")

        if pid == "audio-adhan-core":
            if p.get("category") != "audio" or p.get("format") != "audio":
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} category/format must be audio")
            if p.get("languages") != ["ar"]:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} languages must be ['ar']")
            if p.get("required") is not False:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} required must be false")
            if p.get("audioManifestRef") != "audio-manifest.json":
                raise GateCheckError(5, f"{root_label}/catalog.json", "Audio pack must have audioManifestRef: 'audio-manifest.json'")
        else:
            if p.get("category") != "quran" or p.get("format") != "sqlite":
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} category must be quran, format sqlite")
            exp_langs = ["en"] if pid == "quran-tr-en-pickthall" else ["ar"]
            if p.get("languages") != exp_langs:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} languages must be {exp_langs}")
            if p.get("compression") != "none":
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} compression must be 'none'")
            if p.get("schemaVersion") != 1:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} schemaVersion must be 1")
            exp_req = (pid == "quran-core")
            if p.get("required") is not exp_req:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} required must be {exp_req}")
            exp_fts = pid in ("quran-core", "quran-tr-en-pickthall", "quran-tafsir-ar-jalalayn")
            if p.get("requiresFts5") is not exp_fts:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} requiresFts5 must be {exp_fts}")

            fn = f"{pid}.db"
            if fn in declared_files:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Duplicate asset filename declared: {fn}")
            declared_files.add(fn)
            db_asset_map[fn] = p
            exp_url = f"https://github.com/{repo}/releases/download/content-v0.1.0/{fn}"
            if p.get("url") != exp_url:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} url expected {exp_url}, got {p.get('url')}")
            validate_https_url(p["url"])

    # Validate audio tracks
    track_slugs = [tr["slug"] for tr in audio_data["tracks"]]
    if track_slugs != sorted(_AUDIO_SLUGS):
        raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Audio track slugs mismatch: expected {sorted(_AUDIO_SLUGS)}, got {track_slugs}")

    full_audio_map: dict[str, dict[str, Any]] = {}
    ios_audio_map: dict[str, dict[str, Any]] = {}
    for tr in audio_data["tracks"]:
        slug = tr["slug"]
        for v in ("full", "ios"):
            v_info = tr[v]
            fn = v_info.get("filename")
            exp_fn = f"adhan-{slug}-full.m4a" if v == "full" else f"adhan-{slug}-ios-notif.caf"
            if fn != exp_fn:
                raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Track {slug} {v} filename must be {exp_fn}, got {fn}")
            if type(v_info.get("durationSeconds")) is not float:
                raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Track {slug} {v} durationSeconds must be float")
            if fn in declared_files:
                raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Duplicate asset filename: {fn}")
            declared_files.add(fn)
            exp_url = f"https://github.com/{repo}/releases/download/content-v0.1.0/{fn}"
            if v_info.get("url") != exp_url:
                raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Track {slug} {v} url expected {exp_url}, got {v_info.get('url')}")
            validate_https_url(v_info["url"])
            if v == "full":
                if v_info.get("codec") != "aac":
                    raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Full audio codec must be aac, got {v_info.get('codec')}")
                full_audio_map[fn] = v_info
            else:
                if v_info.get("codec") != "adpcm_ima_qt":
                    raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"iOS audio codec must be adpcm_ima_qt, got {v_info.get('codec')}")
                ios_audio_map[fn] = v_info

    # Validate release companions
    comp_map: dict[str, dict[str, Any]] = {}
    for comp in desc["companions"]:
        cfn = comp["filename"]
        if cfn in declared_files:
            raise GateCheckError(5, f"{root_label}", f"Duplicate companion filename: {cfn}")
        declared_files.add(cfn)
        comp_map[cfn] = comp

    ctx.declared_files_by_root[root_label] = declared_files

    # Inventory check on disk
    # Inventory check on disk: must include dotfiles, directories, broken links
    actual_entries = sorted(os.listdir(root_path))
    for entry_name in actual_entries:
        entry_p = root_path / entry_name
        if entry_name.startswith("."):
            raise GateCheckError(5, f"{root_label}/{entry_name}", f"Dotfile not permitted in release assets: {entry_name}")
        if entry_p.is_dir() or _is_symlink_or_junction(entry_p):
            raise GateCheckError(5, f"{root_label}/{entry_name}", f"Directory or link not permitted in release assets: {entry_name}")
        if entry_name not in declared_files:
            raise GateCheckError(5, f"{root_label}/{entry_name}", f"Undeclared asset file in release root: {entry_name}")
    if set(actual_entries) != declared_files:
        raise GateCheckError(
            5,
            f"{root_label}",
            f"Asset inventory mismatch: expected {sorted(declared_files)}, got {actual_entries}",
        )

    # Attachments cap check: < 2 GiB per attachment, <= 1000 attachments
    if len(declared_files) > 1000:
        raise GateCheckError(5, f"{root_label}", f"Too many attachments: {len(declared_files)} (cap 1000)")

    for fn in sorted(declared_files):
        asset_p = root_path / fn
        if not asset_p.is_file() or _is_symlink_or_junction(asset_p):
            raise GateCheckError(5, f"{root_label}/{fn}", "Asset missing or invalid link")
        actual_size = asset_p.stat().st_size
        if actual_size >= 2 * 1024 * 1024 * 1024:
            raise GateCheckError(5, f"{root_label}/{fn}", f"Asset size {actual_size} exceeds 2 GiB cap")
        actual_sha = _recompute_sha256(asset_p)

        if fn in db_asset_map:
            p_decl = db_asset_map[fn]
            if actual_size != p_decl["sizeBytes"]:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Size mismatch: expected {p_decl['sizeBytes']}, got {actual_size}")
            if actual_sha != p_decl["sha256"]:
                raise GateCheckError(5, f"{root_label}/{fn}", "SHA-256 digest mismatch")
        elif fn in full_audio_map:
            f_decl = full_audio_map[fn]
            if actual_size != f_decl["sizeBytes"]:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Size mismatch: expected {f_decl['sizeBytes']}, got {actual_size}")
            if actual_sha != f_decl["sha256"]:
                raise GateCheckError(5, f"{root_label}/{fn}", "SHA-256 digest mismatch")
        elif fn in ios_audio_map:
            i_decl = ios_audio_map[fn]
            if actual_size != i_decl["sizeBytes"]:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Size mismatch: expected {i_decl['sizeBytes']}, got {actual_size}")
            if actual_sha != i_decl["sha256"]:
                raise GateCheckError(5, f"{root_label}/{fn}", "SHA-256 digest mismatch")
            try:
                codec, duration_frac = probe_asset(asset_p, variant="ios")
            except Exception as exc:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Probe failed for {fn}: {exc}") from exc
            if codec != i_decl.get("codec"):
                raise GateCheckError(5, f"{root_label}/{fn}", f"Codec mismatch for {fn}: expected {i_decl.get('codec')}, got {codec}")
            if not (Fraction(0) < duration_frac < Fraction(29, 1)):
                raise GateCheckError(5, f"{root_label}/{fn}", f"Probed unrounded duration {duration_frac} not strictly between 0 and 29.0 s")
            rounded_sec = format_duration_seconds(duration_frac)
            if i_decl["durationSeconds"] != rounded_sec:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Duration mismatch: manifest {i_decl['durationSeconds']} vs probed {rounded_sec}")
        elif fn in comp_map:
            c_decl = comp_map[fn]
            if actual_size != c_decl["sizeBytes"]:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Companion size mismatch: expected {c_decl['sizeBytes']}, got {actual_size}")
            if actual_sha != c_decl["sha256"]:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Companion sha256 mismatch: expected {c_decl['sha256']}, got {actual_sha}")
            if c_decl.get("sourcePath"):
                src_notice_p = ctx.sources_root / c_decl["sourcePath"]
                if not src_notice_p.is_file() or _is_symlink_or_junction(src_notice_p):
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Companion sourcePath missing or link: {c_decl['sourcePath']}")
                expected_bytes = src_notice_p.read_bytes()
                if asset_p.read_bytes() != expected_bytes:
                    raise GateCheckError(5, f"{root_label}/{fn}", "Companion notice asset bytes mismatch with sourcePath")
            elif fn == "quran-morphology-source.zip":
                try:
                    with zipfile.ZipFile(asset_p, "r") as zf:
                        infolist = zf.infolist()
                        expected_members = sorted(MORPHOLOGY_SOURCE_ARCHIVE_MEMBERS)
                        actual_members = [info.filename for info in infolist]
                        if actual_members != expected_members:
                            raise GateCheckError(5, f"{root_label}/{fn}", f"Morphology source archive member list mismatch: {actual_members} vs {expected_members}")
                        if zf.comment != b"":
                            raise GateCheckError(5, f"{root_label}/{fn}", "Morphology source archive has non-empty zip comment")
                        for info in infolist:
                            if info.date_time != (1980, 1, 1, 0, 0, 0):
                                raise GateCheckError(5, f"{root_label}/{fn}", f"Member {info.filename} timestamp not fixed 1980-01-01")
                            if info.compress_type != zipfile.ZIP_STORED:
                                raise GateCheckError(5, f"{root_label}/{fn}", f"Member {info.filename} not ZIP_STORED")
                            mode = info.external_attr >> 16
                            if mode != 0o100644:
                                raise GateCheckError(5, f"{root_label}/{fn}", f"Member {info.filename} mode not 0100644: {oct(mode)}")
                            src_mem_p = ctx.sources_root / info.filename
                            if not src_mem_p.is_file() or _is_symlink_or_junction(src_mem_p):
                                raise GateCheckError(5, f"{root_label}/{fn}", f"Whitelisted member missing in source tree: {info.filename}")
                            if zf.read(info) != src_mem_p.read_bytes():
                                raise GateCheckError(5, f"{root_label}/{fn}", f"Member content mismatch with source tree: {info.filename}")
                except GateCheckError:
                    raise
                except Exception as exc:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Failed validating morphology source archive: {exc}") from exc
            else:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Unknown companion asset {fn} in descriptor")
def stage_3_manifest_schemas_inventory(ctx: GateContext) -> None:
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        expected_pids = _HADITH_PILOT_PACK_IDS if ctx.profile == "hadith-pilot" else _HADITH_ALL_PACK_IDS
        expected_dbs = sorted([f"{pid}.db" for pid in expected_pids])

        for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
            actual_files: set[str] = set()
            try:
                with os.scandir(root_path) as it:
                    for entry in it:
                        name = entry.name
                        if name.startswith("."):
                            raise GateCheckError(5, f"{root_label}/{name}", "Dotfile not permitted in flat DB root")
                        if _is_symlink_or_junction(entry.path):
                            raise GateCheckError(5, f"{root_label}/{name}", "Symlink or junction not permitted in flat DB root")
                        if entry.is_dir(follow_symlinks=False):
                            raise GateCheckError(5, f"{root_label}/{name}", "Subdirectory not permitted in flat DB root")
                        for suffix in ("-wal", "-shm", "-journal"):
                            if name.endswith(suffix):
                                raise GateCheckError(5, f"{root_label}/{name}", f"Sidecar file {suffix} not permitted")
                        if not entry.is_file(follow_symlinks=False):
                            raise GateCheckError(5, f"{root_label}/{name}", "Non-regular file not permitted in flat DB root")
                        actual_files.add(name)
            except GateCheckError:
                raise
            except Exception as exc:
                raise GateCheckError(5, root_label, f"Error scanning directory: {exc}") from exc

            expected_set = set(expected_dbs)
            if actual_files != expected_set:
                missing = sorted(expected_set - actual_files)
                if missing:
                    raise GateCheckError(5, f"{root_label}/{missing[0]}", f"Expected asset missing: {missing[0]}")
                extra = sorted(actual_files - expected_set)
                if extra:
                    raise GateCheckError(5, f"{root_label}/{extra[0]}", f"Unexpected asset in root: {extra[0]}")

            for db_fn in expected_dbs:
                db_p = root_path / db_fn
                st = db_p.stat()
                if not (0 < st.st_size < 2 * 1024 * 1024 * 1024):
                    raise GateCheckError(5, f"{root_label}/{db_fn}", f"File size {st.st_size} not strictly between 0 and 2 GiB")

            ctx.declared_files_by_root[root_label] = set(expected_dbs)
        return

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data, audio_data = ctx.manifest_data_by_root[root_label]
        cat_bytes = ctx.raw_manifest_bytes[root_label]["catalog.json"]
        audio_bytes = ctx.raw_manifest_bytes[root_label]["audio-manifest.json"]

        # Schema & canonical byte validation
        validate_manifest_schema_and_canonical(
            cat_bytes, cat_data, ctx.catalog_schema_path, f"{root_label}/catalog.json"
        )
        validate_manifest_schema_and_canonical(
            audio_bytes, audio_data, ctx.audio_schema_path, f"{root_label}/audio-manifest.json"
        )

        # Check duplicate pack IDs and lexical pack order
        pack_ids = [p["id"] for p in cat_data["packs"]]
        if len(pack_ids) != len(set(pack_ids)):
            raise GateCheckError(5, f"{root_label}/catalog.json", "Duplicate pack ID declared in catalog")
        if pack_ids != sorted(pack_ids):
            raise GateCheckError(5, f"{root_label}/catalog.json", f"Packs must be in lexical order by ID, got {pack_ids}")

        # Check duplicate track slugs
        track_slugs = [tr["slug"] for tr in audio_data["tracks"]]
        if len(track_slugs) != len(set(track_slugs)):
            raise GateCheckError(5, f"{root_label}/audio-manifest.json", "Duplicate track slug declared in audio manifest")
        if ctx.profile == "v0.1.0":
            _validate_v010_manifests(ctx, root_label, root_path, cat_data, audio_data)
            continue

        # Fixed fixture pack identities, tags, timestamps
        if cat_data["releaseTag"] != "content-v0.0.0":
            raise GateCheckError(5, f"{root_label}/catalog.json", f"Expected releaseTag 'content-v0.0.0', got {cat_data['releaseTag']!r}")
        if cat_data["generatedAt"] != "2026-01-01T00:00:00Z":
            raise GateCheckError(5, f"{root_label}/catalog.json", f"Expected generatedAt '2026-01-01T00:00:00Z', got {cat_data['generatedAt']!r}")
        if audio_data["releaseTag"] != "content-v0.0.0":
            raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Expected releaseTag 'content-v0.0.0', got {audio_data['releaseTag']!r}")
        if audio_data["audioManifestVersion"] != 1:
            raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Expected audioManifestVersion 1, got {audio_data['audioManifestVersion']!r}")
        if audio_data["packId"] != "audio-adhan-core":
            raise GateCheckError(5, f"{root_label}/audio-manifest.json", "audio-manifest packId must be 'audio-adhan-core'")
        # Reconcile pack inventory against fixture expectations
        expected_pack_ids = set(ctx.expectations["packs"].keys())
        actual_pack_ids = set(pack_ids)
        if actual_pack_ids != expected_pack_ids:
            raise GateCheckError(
                5,
                f"{root_label}/catalog.json",
                f"Pack inventory mismatch: expected {sorted(expected_pack_ids)}, got {sorted(actual_pack_ids)}",
            )

        declared_files: set[str] = {"audio-manifest.json", "catalog.json"}
        db_asset_map: dict[str, dict[str, Any]] = {}
        # Validate packs
        for p in cat_data["packs"]:
            pid = p["id"]
            if p["version"] != "0.0.0":
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} version must be '0.0.0'")
            if pid == "audio-adhan-core":
                exp_cat, exp_fmt = "audio", "audio"
            elif pid == "hadith-fixture":
                exp_cat, exp_fmt = "hadith", "sqlite"
            else:
                exp_cat, exp_fmt = "quran", "sqlite"
            if p.get("category") != exp_cat:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} category must be {exp_cat!r}, got {p.get('category')!r}")
            if p.get("format") != exp_fmt:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} format must be {exp_fmt!r}, got {p.get('format')!r}")

            if pid in ("quran-wbw-en", "hadith-fixture"):
                expected_langs = ["ar", "en"]
            elif pid == "quran-tr-en-fixture":
                expected_langs = ["en"]
            else:
                expected_langs = ["ar"]
            if p["languages"] != expected_langs:
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} languages must be {expected_langs}, got {p['languages']}")
            if pid == "quran-core":
                if p.get("required") is not True:
                    raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} must have required: true")
            else:
                if p.get("required") is not False:
                    raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} must have required: false")

            if p["licenseRef"] != f"sources/licenses/{pid}.json":
                raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} licenseRef mismatch")

            if p["format"] == "sqlite":
                fn = f"{pid}.db"
                if fn in declared_files:
                    raise GateCheckError(5, f"{root_label}/catalog.json", f"Duplicate asset filename declared: {fn}")
                declared_files.add(fn)
                db_asset_map[fn] = p
                if p["url"] != f"https://example.org/content-v0.0.0/{fn}":
                    raise GateCheckError(5, f"{root_label}/catalog.json", f"Pack {pid} URL mismatch")
                validate_https_url(p["url"])
            elif p["format"] == "audio":
                if p.get("audioManifestRef") != "audio-manifest.json":
                    raise GateCheckError(5, f"{root_label}/catalog.json", "Audio pack must have audioManifestRef: 'audio-manifest.json'")

        # Validate tracks
        full_audio_map: dict[str, dict[str, Any]] = {}
        ios_audio_map: dict[str, dict[str, Any]] = {}
        for tr in audio_data["tracks"]:
            slug = tr["slug"]
            if slug not in ctx.expectations.get("audioSlugs", []):
                raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Unknown audio slug: {slug}")
            if tr.get("reciter") != "Synthetic tone":
                raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Track reciter must be 'Synthetic tone', got {tr.get('reciter')!r}")

            for v, exp_ext in (("full", "m4a"), ("ios", "caf")):
                v_info = tr[v]
                fn = v_info.get("filename")
                exp_fn = f"adhan-{slug}-full.m4a" if v == "full" else f"adhan-{slug}-ios-notif.caf"
                if fn != exp_fn:
                    raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Track {slug} {v} filename must be {exp_fn}, got {fn!r}")
                if type(v_info.get("durationSeconds")) is not float:
                    raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Track {slug} {v} durationSeconds must be float, got {type(v_info.get('durationSeconds')).__name__}")
                if fn in declared_files:
                    raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Duplicate asset filename declared: {fn}")
                declared_files.add(fn)
                if v == "full":
                    full_audio_map[fn] = v_info
                else:
                    ios_audio_map[fn] = v_info
                if v_info["url"] != f"https://example.org/content-v0.0.0/{fn}":
                    raise GateCheckError(5, f"{root_label}/audio-manifest.json", f"Audio track {v} URL mismatch: {v_info['url']}")
                validate_https_url(v_info["url"])

        # Limits: maximum 1,000 attachments including manifests
        if len(declared_files) > 1000:
            raise GateCheckError(5, f"{root_label}", f"Attachment count {len(declared_files)} exceeds 1,000 limit")

        ctx.declared_files_by_root[root_label] = declared_files

        # Flat directory inventory pass: check extra files, sidecars, nonregular/links in sorted order
        actual_entries = sorted(root_path.iterdir(), key=lambda p: p.name)
        for entry in actual_entries:
            name = entry.name
            if name.endswith(("-wal", "-shm", "-journal")):
                raise GateCheckError(3, f"{root_label}/{name}", f"SQLite sidecar file exists: {name}")
            if _is_symlink_or_junction(entry):
                raise GateCheckError(5, f"{root_label}/{name}", f"Entry {name} is a link or junction")
            if not entry.is_file():
                raise GateCheckError(5, f"{root_label}/{name}", f"Entry {name} is not a regular file")
            if name not in declared_files:
                raise GateCheckError(5, f"{root_label}/{name}", f"Undeclared extra file in release directory: {name}")

        # Hash and probe assets in lexical order per stage
        release_assets = sorted(declared_files - {"audio-manifest.json", "catalog.json"})
        for fn in release_assets:
            asset_p = root_path / fn
            if not asset_p.is_file() or _is_symlink_or_junction(asset_p):
                raise GateCheckError(5, f"{root_label}/{fn}", f"Declared asset {fn} missing or link")
            st = asset_p.stat()
            if st.st_size >= 2 * 1024 * 1024 * 1024:
                raise GateCheckError(5, f"{root_label}/{fn}", f"Asset {fn} exceeds 2 GiB limit ({st.st_size} bytes)")

            if fn in db_asset_map:
                p = db_asset_map[fn]
                if st.st_size != p["sizeBytes"]:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Size mismatch for {fn}: expected {p['sizeBytes']}, got {st.st_size}")
                actual_sha = _recompute_sha256(asset_p)
                if actual_sha != p["sha256"]:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"SHA-256 mismatch for {fn}: expected {p['sha256']}, got {actual_sha}")

            elif fn in full_audio_map:
                v_full = full_audio_map[fn]
                if st.st_size != v_full["sizeBytes"]:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Size mismatch for {fn}: expected {v_full['sizeBytes']}, got {st.st_size}")
                actual_sha = _recompute_sha256(asset_p)
                if actual_sha != v_full["sha256"]:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"SHA-256 mismatch for {fn}: expected {v_full['sha256']}, got {actual_sha}")
                if v_full.get("codec") != "aac":
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Full audio codec must be 'aac', got {v_full.get('codec')!r}")

            elif fn in ios_audio_map:
                v_ios = ios_audio_map[fn]
                if st.st_size != v_ios["sizeBytes"]:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Size mismatch for {fn}: expected {v_ios['sizeBytes']}, got {st.st_size}")
                actual_sha = _recompute_sha256(asset_p)
                if actual_sha != v_ios["sha256"]:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"SHA-256 mismatch for {fn}: expected {v_ios['sha256']}, got {actual_sha}")

                try:
                    codec, duration_frac = probe_asset(asset_p, variant="ios")
                except Exception as exc:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Audio probe failed on {fn}: {exc}") from exc

                if codec != v_ios["codec"]:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Codec mismatch for {fn}: expected {v_ios['codec']}, got {codec}")
                if codec != "adpcm_ima_qt":
                    raise GateCheckError(5, f"{root_label}/{fn}", f"iOS audio codec must be 'adpcm_ima_qt', got {codec}")

                # Check unrounded duration: 0 < duration < 29.0
                if not (Fraction(0) < duration_frac < Fraction(29, 1)):
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Probed unrounded duration {duration_frac} not strictly between 0 and 29.0 s")

                rounded_sec = format_duration_seconds(duration_frac)
                if v_ios["durationSeconds"] != rounded_sec:
                    raise GateCheckError(5, f"{root_label}/{fn}", f"Duration mismatch: manifest {v_ios['durationSeconds']} vs probed {rounded_sec}")
def stage_4_source_lock_clearance(ctx: GateContext) -> None:
    if ctx.profile == "v0.1.0":
        entries, reg, lics = validate_release_sources(ctx.sources_root, local_review=ctx.local_review)
        ctx.lock_entries = entries
        ctx.registry_data = reg
        ctx.license_records = lics
        return
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        sources_dir = ctx.sources_root / "sources"
        raw_root = ctx.sources_root / "raw-inputs"
        reg_path = sources_dir / "registry.json"
        lock_path = sources_dir / "manifest.lock.json"

        if not raw_root.is_dir() or _is_symlink_or_junction(raw_root):
            raise GateCheckError(7, "raw-inputs", "raw-inputs root missing or link")
        if not reg_path.is_file() or _is_symlink_or_junction(reg_path):
            raise GateCheckError(7, "sources/registry.json", "sources/registry.json missing or link")
        if not lock_path.is_file() or _is_symlink_or_junction(lock_path):
            raise GateCheckError(7, "sources/manifest.lock.json", "sources/manifest.lock.json missing or link")

        book_slugs = ("bukhari", "muslim") if ctx.profile == "hadith-pilot" else tuple(_HADITH_LOCAL_ORACLE.keys())
        expected_pids = [f"hadith-{slug}" for slug in book_slugs]

        all_entries: list[LockEntry] = []
        for pid in expected_pids:
            try:
                p_entries = verify_lock(reg_path, raw_root, lock_path, pack_id=pid)
                all_entries.extend(p_entries)
            except Exception as exc:
                raise GateCheckError(7, f"lock verification for {pid}", f"Lock verification failed: {exc}") from exc

        ctx.lock_entries = tuple(all_entries)

        try:
            registry_data = parse_strict_json(reg_path.read_text(encoding="utf-8"))
            ctx.registry_data = registry_data
        except Exception as exc:
            raise GateCheckError(7, "sources/registry.json", f"Failed parsing registry: {exc}") from exc

        for pid in expected_pids:
            rel_lic = f"sources/licenses/{pid}.json"
            slug = pid.removeprefix("hadith-")
            pack_entries = tuple(e for e in all_entries if e.path.startswith(f"hadith/{slug}."))
            try:
                load_sqlite_source_metadata(ctx.sources_root, pid, pack_entries)
            except Exception as exc:
                raise GateCheckError(7, rel_lic, f"License validation failed: {exc}") from exc

            lic_file = ctx.sources_root / rel_lic
            try:
                lic_data = parse_strict_json(lic_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise GateCheckError(7, rel_lic, f"Failed parsing license JSON: {exc}") from exc

            if lic_data.get("status") != "pending":
                raise GateCheckError(7, rel_lic, f"License status must be 'pending', got {lic_data.get('status')!r}")

            ctx.license_records[pid] = lic_data

        print(f"Clearance pending (local verification only): {', '.join(expected_pids)}")
        return
    sources_dir = ctx.sources_root / "sources"
    raw_root = ctx.sources_root / "raw-inputs"
    reg_path = sources_dir / "registry.json"
    lock_path = sources_dir / "manifest.lock.json"

    if not raw_root.is_dir() or _is_symlink_or_junction(raw_root):
        raise GateCheckError(7, "raw-inputs", "raw-inputs root missing or link")
    if not reg_path.is_file() or _is_symlink_or_junction(reg_path):
        raise GateCheckError(7, "sources/registry.json", "sources/registry.json missing or link")
    if not lock_path.is_file() or _is_symlink_or_junction(lock_path):
        raise GateCheckError(7, "sources/manifest.lock.json", "sources/manifest.lock.json missing or link")

    actual_dirs = sorted([d.name for d in raw_root.iterdir() if d.is_dir()])
    if actual_dirs != ["audio", "hadith", "qul", "sqlite"]:
        raise GateCheckError(7, "raw-inputs", f"Unexpected source tree directories: {actual_dirs}")
    audio_dirs = sorted([d.name for d in (raw_root / "audio").iterdir() if d.is_dir()])
    if audio_dirs != ["adhan"]:
        raise GateCheckError(7, "raw-inputs/audio", f"Unexpected audio tree directories: {audio_dirs}")

    try:
        ctx.lock_entries = verify_lock(reg_path, raw_root, lock_path)
    except Exception as exc:
        raise GateCheckError(7, "lock verification", f"Lock verification failed: {exc}") from exc

    try:
        ctx.registry_data = parse_strict_json(reg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GateCheckError(7, "sources/registry.json", f"Failed parsing registry: {exc}") from exc

    expected_reg_paths = set(_FIXTURE_PATH_PACK_MAP.keys())
    actual_reg_paths = set(ctx.registry_data.keys())
    if actual_reg_paths != expected_reg_paths:
        raise GateCheckError(
            7,
            "sources/registry.json",
            f"Registry path set mismatch: expected {sorted(expected_reg_paths)}, got {sorted(actual_reg_paths)}",
        )

    for expected_path, expected_packs in _FIXTURE_PATH_PACK_MAP.items():
        entry = ctx.registry_data[expected_path]
        if tuple(entry.get("packIds", [])) != expected_packs:
            raise GateCheckError(7, "sources/registry.json", f"Pack IDs mismatch for {expected_path}")
        if entry.get("sourceUrl") != f"https://example.org/fixture/{expected_path}":
            raise GateCheckError(7, "sources/registry.json", f"Source URL mismatch for {expected_path}")
        if entry.get("sourceVersion") != "1":
            raise GateCheckError(7, "sources/registry.json", f"Source version mismatch for {expected_path}")

    cand_cat_data = ctx.manifest_data_by_root["candidate"][0]
    for p in cand_cat_data["packs"]:
        pack_id = p["id"]
        lic_file = sources_dir / "licenses" / f"{pack_id}.json"
        if not lic_file.is_file() or _is_symlink_or_junction(lic_file):
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", f"License record missing or link for pack {pack_id}")
        try:
            lic_data = parse_strict_json(lic_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", f"Malformed license record: {exc}") from exc

        if not isinstance(lic_data, dict):
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "License record must be a JSON object")

        expected_lic_keys = {"packId", "licenseSpdx", "primaryPath", "status", "reviewedBy", "sources"}
        if set(lic_data.keys()) != expected_lic_keys:
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "License record keys mismatch")

        if lic_data.get("licenseSpdx") != "CC0-1.0":
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "License SPDX must be CC0-1.0 for fixture")
        if lic_data.get("status") != "fixture":
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "Fixture license status must be 'fixture'")
        if lic_data.get("reviewedBy") is not None:
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "Fixture reviewedBy must be null")

        sources_list = lic_data.get("sources")
        if not isinstance(sources_list, list):
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "Sources must be a list")

        expected_paths = sorted([path for path, packs in _FIXTURE_PATH_PACK_MAP.items() if pack_id in packs])
        if lic_data.get("primaryPath") != expected_paths[0]:
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", f"primaryPath mismatch: expected {expected_paths[0]}")

        actual_source_paths = [s.get("path") for s in sources_list if isinstance(s, dict)]
        if actual_source_paths != expected_paths:
            raise GateCheckError(7, f"sources/licenses/{pack_id}.json", f"Sources list must be sorted and match expected locked paths: {expected_paths}")

        for src in sources_list:
            if not isinstance(src, dict):
                raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "Source entry must be a JSON object")

            src_path = src.get("path")
            reg_entry = ctx.registry_data.get(src_path, {})

            if pack_id == "audio-adhan-core":
                expected_src_keys = {"attribution", "clipStartMs", "name", "notice", "path", "permittedUses", "reciter", "rightsEvidence", "slug", "sourceUrl", "sourceVersion"}
            else:
                expected_src_keys = {"attribution", "name", "notice", "path", "permittedUses", "rightsEvidence", "sourceUrl", "sourceVersion"}

            if set(src.keys()) != expected_src_keys:
                raise GateCheckError(7, f"sources/licenses/{pack_id}.json", f"Source record keys mismatch for {src_path}")

            if pack_id == "audio-adhan-core":
                if src.get("slug") != "fixture" or src.get("reciter") != "Synthetic tone":
                    raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "Invalid audio source clearance fields")
                clip_start = src.get("clipStartMs")
                if type(clip_start) is not int or clip_start != 0:
                    raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "clipStartMs must be integer 0")

            if src.get("sourceUrl") != reg_entry.get("sourceUrl"):
                raise GateCheckError(7, f"sources/licenses/{pack_id}.json", f"Source URL mismatch for {src_path}")
            if src.get("sourceVersion") != reg_entry.get("sourceVersion"):
                raise GateCheckError(7, f"sources/licenses/{pack_id}.json", f"Source version mismatch for {src_path}")
            if src.get("rightsEvidence") != ["https://example.org/fixture/synthetic"]:
                raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "Invalid rightsEvidence")
            if src.get("notice") != "Synthetic fixture" or src.get("attribution") != "Synthetic fixture":
                raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "Invalid notice/attribution")
            if src.get("permittedUses") != []:
                raise GateCheckError(7, f"sources/licenses/{pack_id}.json", "Fixture permittedUses must be empty")

def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def _verify_single_db_schema_and_integrity(
    ctx: GateContext,
    root_label: str,
    db_fn: str,
    db_path: Path,
    pack_id: str,
    common_meta_sql: str,
) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = db_path.parent / (db_path.name + suffix)
        if sidecar.exists() or _is_symlink_or_junction(sidecar):
            raise GateCheckError(3, f"{root_label}/{db_fn}", f"SQLite sidecar file exists: {sidecar.name}")

    conn = _open_ro_db(db_path)
    try:
        cur = conn.cursor()
        res_int = cur.execute("PRAGMA integrity_check;").fetchall()
        if res_int != [("ok",)] and res_int != [(b"ok",)]:
            raise GateCheckError(3, f"{root_label}/{db_fn}", f"integrity_check failed: {res_int}")

        res_fk = cur.execute("PRAGMA foreign_key_check;").fetchall()
        if res_fk:
            raise GateCheckError(3, f"{root_label}/{db_fn}", f"foreign_key_check failed with violations: {res_fk}")

        res_enc = cur.execute("PRAGMA encoding;").fetchone()
        if res_enc[0] not in ("UTF-8", b"UTF-8"):
            raise GateCheckError(3, f"{root_label}/{db_fn}", f"encoding must be UTF-8, got {res_enc[0]}")

        res_ps = cur.execute("PRAGMA page_size;").fetchone()
        if res_ps[0] != 4096:
            raise GateCheckError(3, f"{root_label}/{db_fn}", f"page_size must be 4096, got {res_ps[0]}")

        res_uv = cur.execute("PRAGMA user_version;").fetchone()
        if res_uv[0] != 1:
            raise GateCheckError(3, f"{root_label}/{db_fn}", f"user_version must be 1, got {res_uv[0]}")

        res_jm = cur.execute("PRAGMA journal_mode;").fetchone()
        if str(res_jm[0]).lower() not in ("delete", "b'delete'"):
            raise GateCheckError(3, f"{root_label}/{db_fn}", f"journal_mode must be DELETE, got {res_jm[0]}")

        # Reference schema comparison
        schema_fn = _PACK_SCHEMA_MAP.get(pack_id)
        if not schema_fn:
            raise GateCheckError(3, f"{root_label}/{db_fn}", f"No schema DDL mapped for {pack_id}")
        schema_sql = (ctx.specs_sql_dir / schema_fn).read_text(encoding="utf-8")

        ref_conn = sqlite3.connect(":memory:")
        try:
            ref_conn.execute("PRAGMA foreign_keys = ON;")
            ref_conn.executescript(common_meta_sql)
            ref_conn.executescript(schema_sql)
            ref_conn.execute("ANALYZE;")
            ref_cur = ref_conn.cursor()

            cur.execute("PRAGMA table_list;")
            actual_shadow = {r[1] for r in cur.fetchall() if len(r) > 2 and r[2] == "shadow"}
            ref_cur.execute("PRAGMA table_list;")
            ref_shadow = {r[1] for r in ref_cur.fetchall() if len(r) > 2 and r[2] == "shadow"}

            cur.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name;")
            actual_entities = [
                (str(r[0]), str(r[1]), str(r[2]), (str(r[3]) if r[3] is not None else None))
                for r in cur.fetchall()
                if str(r[1]) not in actual_shadow and str(r[2]) not in actual_shadow
            ]

            ref_cur.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name;")
            ref_entities = [
                (str(r[0]), str(r[1]), str(r[2]), (str(r[3]) if r[3] is not None else None))
                for r in ref_cur.fetchall()
                if str(r[1]) not in ref_shadow and str(r[2]) not in ref_shadow
            ]

            if actual_entities != ref_entities:
                raise GateCheckError(3, f"{root_label}/{db_fn}", f"Schema entities mismatch: actual {actual_entities} vs ref {ref_entities}")

            for _, tbl_name, _, _ in [e for e in ref_entities if e[0] == "table"]:
                cur.execute(f"PRAGMA table_info({tbl_name});")
                act_ti = [(r[1], str(r[2]).upper(), r[3], r[4], r[5]) for r in cur.fetchall()]
                ref_cur.execute(f"PRAGMA table_info({tbl_name});")
                ref_ti = [(r[1], str(r[2]).upper(), r[3], r[4], r[5]) for r in ref_cur.fetchall()]
                if act_ti != ref_ti:
                    raise GateCheckError(3, f"{root_label}/{db_fn}", f"table_info mismatch on {tbl_name}")

                cur.execute(f"PRAGMA foreign_key_list({tbl_name});")
                act_fk = cur.fetchall()
                ref_cur.execute(f"PRAGMA foreign_key_list({tbl_name});")
                ref_fk = ref_cur.fetchall()
                if act_fk != ref_fk:
                    raise GateCheckError(3, f"{root_label}/{db_fn}", f"foreign_key_list mismatch on {tbl_name}")
        finally:
            ref_conn.close()
    finally:
        conn.close()


def stage_5_sqlite_schema_integrity(ctx: GateContext) -> None:
    common_meta_sql = (ctx.specs_sql_dir / "common_meta.sql").read_text(encoding="utf-8")

    if ctx.profile in ("hadith-pilot", "hadith-local"):
        for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
            for db_fn in sorted(ctx.declared_files_by_root[root_label]):
                pack_id = db_fn.removesuffix(".db")
                _verify_single_db_schema_and_integrity(
                    ctx, root_label, db_fn, root_path / db_fn, pack_id, common_meta_sql
                )
        return

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        sqlite_packs = sorted([p for p in cat_data["packs"] if p["format"] == "sqlite"], key=lambda p: p["id"])

        for p in sqlite_packs:
            pack_id = p["id"]
            db_fn = f"{pack_id}.db"
            _verify_single_db_schema_and_integrity(
                ctx, root_label, db_fn, root_path / db_fn, pack_id, common_meta_sql
            )

def stage_6_sqlite_metadata(ctx: GateContext) -> None:
    sources_dir = ctx.sources_root / "sources"
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
            for db_fn in sorted(ctx.declared_files_by_root[root_label]):
                pack_id = db_fn.removesuffix(".db")
                slug = pack_id.removeprefix("hadith-")
                db_path = root_path / db_fn
                conn = _open_ro_db(db_path)
                conn.text_factory = bytes
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT key, value FROM pack_meta;")
                    meta_dict: dict[str, str] = {}
                    for r0, r1 in cur.fetchall():
                        try:
                            k_str = r0.decode("utf-8") if isinstance(r0, bytes) else str(r0)
                            v_str = r1.decode("utf-8") if isinstance(r1, bytes) else str(r1)
                        except UnicodeDecodeError as exc:
                            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Malformed UTF-8 in pack_meta: {exc}") from exc
                        if "\ufffd" in k_str or "\ufffd" in v_str:
                            raise GateCheckError(2, f"{root_label}/{db_fn}", "pack_meta contains replacement character U+FFFD")
                        for s in (k_str, v_str):
                            for ch in s:
                                if 0xD800 <= ord(ch) <= 0xDFFF:
                                    raise GateCheckError(2, f"{root_label}/{db_fn}", f"Lone surrogate in pack_meta U+{ord(ch):04X}")
                        meta_dict[k_str] = v_str

                    cur.execute("SELECT metric, count, notes FROM coverage_report ORDER BY metric;")
                    cov_rows = cur.fetchall()
                    cov_dict: dict[str, int] = {}
                    for m_b, cnt_b, notes_b in cov_rows:
                        m_str = m_b.decode("utf-8") if isinstance(m_b, bytes) else str(m_b)
                        if notes_b is not None:
                            raise GateCheckError(7, f"{root_label}/{db_fn}", f"coverage_report notes must be NULL for {m_str}")
                        cov_dict[m_str] = cnt_b

                    expected_metrics = {"expected_total", "ar_present", "en_present", "en_missing"}
                    if set(cov_dict.keys()) != expected_metrics:
                        raise GateCheckError(7, f"{root_label}/{db_fn}", f"coverage_report metrics mismatch: {sorted(cov_dict.keys())}")

                    en_missing = cov_dict["en_missing"]
                    ar_missing = cov_dict["expected_total"] - cov_dict["ar_present"]
                    expected_completeness = "complete" if (en_missing == 0 and ar_missing == 0) else "partial"

                    if meta_dict.get("pack_id") != pack_id:
                        raise GateCheckError(7, f"{root_label}/{db_fn}", f"pack_meta pack_id mismatch: {meta_dict.get('pack_id')}")
                    if meta_dict.get("pack_version") != "0.2.0":
                        raise GateCheckError(7, f"{root_label}/{db_fn}", f"pack_meta pack_version must be '0.2.0', got {meta_dict.get('pack_version')}")
                    if meta_dict.get("schema_version") != "1":
                        raise GateCheckError(7, f"{root_label}/{db_fn}", f"pack_meta schema_version must be '1', got {meta_dict.get('schema_version')}")
                    if meta_dict.get("fts5_enabled") != "true":
                        raise GateCheckError(7, f"{root_label}/{db_fn}", f"pack_meta fts5_enabled must be 'true'")
                    if meta_dict.get("matn_split_verified") != "false":
                        raise GateCheckError(7, f"{root_label}/{db_fn}", f"pack_meta matn_split_verified must be 'false', got {meta_dict.get('matn_split_verified')}")
                    if meta_dict.get("completeness") != expected_completeness:
                        raise GateCheckError(7, f"{root_label}/{db_fn}", f"pack_meta completeness mismatch: expected {expected_completeness}, got {meta_dict.get('completeness')}")

                    pack_entries = tuple(e for e in ctx.lock_entries if e.path.startswith(f"hadith/{slug}."))
                    exp_src_meta = load_sqlite_source_metadata(
                        ctx.sources_root,
                        pack_id,
                        pack_entries,
                        pack_version="0.2.0",
                        completeness=expected_completeness,
                        fts5_enabled=True,
                    )
                    for k in ("license_spdx", "source_name", "source_url", "source_version", "build_input_sha256"):
                        if meta_dict.get(k) != exp_src_meta[k]:
                            raise GateCheckError(7, f"{root_label}/{db_fn}", f"pack_meta {k} mismatch: actual {meta_dict.get(k)!r} vs source {exp_src_meta[k]!r}")
                finally:
                    conn.close()
        return

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        sqlite_packs = sorted([p for p in cat_data["packs"] if p["format"] == "sqlite"], key=lambda p: p["id"])

        for p in sqlite_packs:
            pack_id = p["id"]
            db_fn = f"{pack_id}.db"
            db_path = root_path / db_fn

            conn = _open_ro_db(db_path)
            conn.text_factory = bytes
            try:
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM pack_meta;")
                meta_dict: dict[str, str] = {}
                for r0, r1 in cur.fetchall():
                    try:
                        k_str = r0.decode("utf-8") if isinstance(r0, bytes) else str(r0)
                        v_str = r1.decode("utf-8") if isinstance(r1, bytes) else str(r1)
                    except UnicodeDecodeError as exc:
                        raise GateCheckError(2, f"{root_label}/{db_fn}", f"Malformed UTF-8 in pack_meta: {exc}") from exc
                    if "\ufffd" in k_str or "\ufffd" in v_str:
                        raise GateCheckError(2, f"{root_label}/{db_fn}", "pack_meta contains replacement character U+FFFD")
                    for s in (k_str, v_str):
                        for ch in s:
                            if 0xD800 <= ord(ch) <= 0xDFFF:
                                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Lone surrogate in pack_meta U+{ord(ch):04X}")
                    meta_dict[k_str] = v_str

                mandatory_keys = {
                    "pack_id", "pack_version", "schema_version", "source_name", "source_url",
                    "source_version", "license_spdx", "license_ref", "build_input_sha256",
                    "completeness", "fts5_enabled",
                }
                if pack_id == "hadith-fixture":
                    mandatory_keys.add("matn_split_verified")

                if set(meta_dict.keys()) != mandatory_keys:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", f"pack_meta keys mismatch: {set(meta_dict.keys())} vs {mandatory_keys}")

                if meta_dict["pack_id"] != p["id"]:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta pack_id mismatch")
                if meta_dict["pack_version"] != p["version"]:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta pack_version mismatch")
                if meta_dict["schema_version"] != str(p["schemaVersion"]):
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta schema_version mismatch")
                if meta_dict["license_ref"] != p["licenseRef"]:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta license_ref mismatch")
                if meta_dict["completeness"] != p["completeness"]:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta completeness mismatch")

                expected_fts_val = "true" if p["requiresFts5"] else "false"
                if meta_dict["fts5_enabled"] != expected_fts_val:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta fts5_enabled mismatch with catalog")

                has_fts = len(_PACK_FTS_MAP[pack_id]) > 0
                if (meta_dict["fts5_enabled"] == "true") != has_fts:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta fts5_enabled mismatch with actual schema")

                if ctx.profile == "v0.1.0" and pack_id in ctx.license_records:
                    lic_data = ctx.license_records[pack_id]
                else:
                    lic_file = sources_dir / "licenses" / f"{pack_id}.json"
                    lic_data = parse_strict_json(lic_file.read_text(encoding="utf-8"))
                if meta_dict["license_spdx"] != lic_data["licenseSpdx"]:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta license_spdx mismatch with license record")

                primary_src = [s for s in lic_data["sources"] if s["path"] == lic_data["primaryPath"]][0]
                if meta_dict["source_name"] != primary_src["name"]:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta source_name mismatch")
                if meta_dict["source_url"] != primary_src["sourceUrl"]:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta source_url mismatch")
                if meta_dict["source_version"] != primary_src["sourceVersion"]:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta source_version mismatch")

                pack_lock_paths = {s["path"] for s in lic_data["sources"]}
                pack_lock_entries = tuple(e for e in ctx.lock_entries if e.path in pack_lock_paths)
                expected_hash = hashlib.sha256(canonical_entries_json(pack_lock_entries)).hexdigest().lower()
                if meta_dict["build_input_sha256"] != expected_hash:
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "pack_meta build_input_sha256 mismatch")

                if pack_id == "hadith-fixture" and meta_dict.get("matn_split_verified") != "true":
                    raise GateCheckError(7, f"{root_label}/{db_fn}", "hadith-fixture matn_split_verified must be 'true'")
            finally:
                conn.close()


def _stage_7_record_keys_v010(ctx: GateContext) -> None:
    raw_root = ctx.sources_root / "raw-inputs"
    coords = load_tanzil_coordinates(raw_root / "tanzil" / "quran-data-Tanzil.xml")
    expected_ayah_keys = [f"{s.id}:{a}" for s in coords.surahs for a in range(1, s.ayah_count + 1)]

    corpus_path = raw_root / "corpus" / "quranic-corpus-morphology-0.4.zip"
    corpus_segments = read_corpus_segments(corpus_path, coords.ayah_ids)

    expected_words: list[tuple[int, int, int, int, int, str]] = []
    expected_segments: list[tuple[int, int, int]] = []
    w_idx = 0
    s_idx = 0
    for (s_id, a_id, w_num), seg_iter in itertools.groupby(corpus_segments, key=lambda s: (s.surah, s.ayah, s.word)):
        w_idx += 1
        current_w_id = w_idx
        cur_aid = coords.ayah_ids[f"{s_id}:{a_id}"]
        cur_wk = f"{s_id}:{a_id}:{w_num}"
        expected_words.append((current_w_id, cur_aid, s_id, a_id, w_num, cur_wk))
        for seg in seg_iter:
            s_idx += 1
            expected_segments.append((s_idx, current_w_id, seg.segment))

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        sqlite_packs = sorted([p for p in cat_data["packs"] if p["format"] == "sqlite"], key=lambda p: p["id"])

        for p in sqlite_packs:
            pack_id = p["id"]
            db_fn = f"{pack_id}.db"
            db_path = root_path / db_fn

            conn = _open_ro_db(db_path)
            conn.text_factory = bytes
            try:
                cur = conn.cursor()
                if pack_id == "quran-core":
                    cur.execute("SELECT id, name_ar, name_tname, name_en, revelation_type, revelation_order, ayah_count, ruku_count, start_ayah_id FROM surahs ORDER BY id;")
                    db_surahs = cur.fetchall()
                    if len(db_surahs) != len(coords.surahs):
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"surahs row count mismatch: expected {len(coords.surahs)}, got {len(db_surahs)}")
                    for r, exp_s in zip(db_surahs, coords.surahs):
                        r_dec = (
                            r[0],
                            r[1].decode("utf-8") if isinstance(r[1], bytes) else r[1],
                            r[2].decode("utf-8") if isinstance(r[2], bytes) else r[2],
                            r[3].decode("utf-8") if isinstance(r[3], bytes) else r[3],
                            r[4].decode("utf-8") if isinstance(r[4], bytes) else r[4],
                            r[5], r[6], r[7], r[8]
                        )
                        if r_dec != tuple(exp_s):
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"surahs data mismatch for surah {exp_s.id}: {r_dec} vs {tuple(exp_s)}")

                    cur.execute("SELECT id, surah_id, ayah_number, verse_key, juz, hizb_quarter, manzil, ruku, page_madani, sajdah_type FROM ayahs ORDER BY id;")
                    db_ayahs = cur.fetchall()
                    if len(db_ayahs) != 6236:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"ayahs count mismatch: expected 6236, got {len(db_ayahs)}")
                    for r in db_ayahs:
                        aid = r[0]
                        sid = r[1]
                        an = r[2]
                        vk = r[3].decode("utf-8") if isinstance(r[3], bytes) else r[3]
                        exp_vk = expected_ayah_keys[aid - 1]
                        if vk != exp_vk or f"{sid}:{an}" != exp_vk:
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"Coordinate component mismatch at ayah {aid}")
                        exp_bounds = coords.boundaries.get(aid)
                        if exp_bounds is not None:
                            juz, hizb, manzil, ruku, page = exp_bounds
                            if (r[4], r[5], r[6], r[7], r[8]) != (juz, hizb, manzil, ruku, page):
                                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Boundary mismatch at ayah {aid}")
                        s_type = r[9].decode("utf-8") if isinstance(r[9], bytes) else r[9]
                        exp_sajdah = coords.sajdah_types.get(aid)
                        if s_type != exp_sajdah:
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"Sajdah mismatch at ayah {aid}: {s_type} vs {exp_sajdah}")

                elif pack_id == "quran-scripts-extended":
                    cur.execute("SELECT ayah_id, verse_key FROM ayah_scripts ORDER BY ayah_id;")
                    rows = cur.fetchall()
                    if len(rows) != 6236:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"ayah_scripts row count mismatch: expected 6236, got {len(rows)}")
                    for idx, r in enumerate(rows, start=1):
                        aid = r[0]
                        vk = r[1].decode("utf-8") if isinstance(r[1], bytes) else r[1]
                        exp_vk = expected_ayah_keys[idx - 1]
                        if aid != idx or vk != exp_vk:
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"ayah_scripts coordinate mismatch at row {idx}")

                elif pack_id == "quran-tr-en-pickthall":
                    cur.execute("SELECT ayah_id, surah_id, ayah_number, verse_key FROM translations ORDER BY ayah_id;")
                    rows = cur.fetchall()
                    if len(rows) != 6236:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"translations row count mismatch: expected 6236, got {len(rows)}")
                    for idx, r in enumerate(rows, start=1):
                        aid, sid, an = r[0], r[1], r[2]
                        vk = r[3].decode("utf-8") if isinstance(r[3], bytes) else r[3]
                        exp_vk = expected_ayah_keys[idx - 1]
                        if aid != idx or vk != exp_vk or f"{sid}:{an}" != exp_vk:
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"translations coordinate component mismatch at {idx}")

                elif pack_id == "quran-tafsir-ar-jalalayn":
                    cur.execute("SELECT ayah_id, verse_key, entry_id FROM tafsir_ayah_map ORDER BY ayah_id;")
                    rows = cur.fetchall()
                    if len(rows) != 6236:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"tafsir_ayah_map row count mismatch: expected 6236, got {len(rows)}")
                    for idx, r in enumerate(rows, start=1):
                        aid = r[0]
                        vk = r[1].decode("utf-8") if isinstance(r[1], bytes) else r[1]
                        exp_vk = expected_ayah_keys[idx - 1]
                        if aid != idx or vk != exp_vk:
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"tafsir_ayah_map coordinate mismatch at {idx}")

                elif pack_id == "quran-morphology":
                    cur.execute("SELECT id, ayah_id, surah_id, ayah_number, word_number, word_key FROM words ORDER BY id;")
                    db_words = cur.fetchall()
                    if len(db_words) != len(expected_words):
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"words count mismatch: expected {len(expected_words)}, got {len(db_words)}")
                    for db_w, exp_w in zip(db_words, expected_words):
                        db_wid, db_aid, db_sid, db_an, db_wn = db_w[0], db_w[1], db_w[2], db_w[3], db_w[4]
                        db_wk = db_w[5].decode("utf-8") if isinstance(db_w[5], bytes) else db_w[5]
                        if (db_wid, db_aid, db_sid, db_an, db_wn, db_wk) != exp_w:
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"word key/coordinate mismatch at word {db_wid}")

                    cur.execute("SELECT id, word_id, segment_number FROM morphology_segments ORDER BY id;")
                    db_segs = cur.fetchall()
                    if len(db_segs) != len(expected_segments):
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"morphology_segments count mismatch: expected {len(expected_segments)}, got {len(db_segs)}")
                    for db_s, exp_s in zip(db_segs, expected_segments):
                        if (db_s[0], db_s[1], db_s[2]) != exp_s:
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"morphology_segment key/parent mismatch at segment {db_s[0]}")
            finally:
                conn.close()

def stage_7_record_keys(ctx: GateContext) -> None:
    if ctx.profile == "v0.1.0":
        _stage_7_record_keys_v010(ctx)
        return
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
            for db_fn in sorted(ctx.declared_files_by_root[root_label]):
                pack_id = db_fn.removesuffix(".db")
                slug = pack_id.removeprefix("hadith-")
                db_path = root_path / db_fn
                conn = _open_ro_db(db_path)
                try:
                    cur = conn.cursor()
                    act_keys = _fetch_validated_keys(cur, "hadiths", "id", "id", root_label, db_fn, is_int=True)
                    hadith_json = load_locked_json(ctx, f"hadith/{slug}.json")
                    locked_pks = sorted([r["id"] for r in hadith_json["hadiths"]])
                    if act_keys != locked_pks:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"Primary key mismatch in hadiths: {len(act_keys)} vs {len(locked_pks)}")
                    act_books = _fetch_validated_keys(cur, "books", "id", "id", root_label, db_fn, is_int=True)
                    if act_books != sorted([b["id"] for b in hadith_json["books"]]):
                        raise GateCheckError(1, f"{root_label}/{db_fn}", "books keys mismatch in hadith")
                    act_chaps = _fetch_validated_keys(cur, "chapters", "id", "id", root_label, db_fn, is_int=True)
                    if act_chaps != sorted([c["id"] for c in hadith_json.get("chapters", [])]):
                        raise GateCheckError(1, f"{root_label}/{db_fn}", "chapters keys mismatch in hadith")
                finally:
                    conn.close()
        return

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        sqlite_packs = sorted([p for p in cat_data["packs"] if p["format"] == "sqlite"], key=lambda p: p["id"])

        for p in sqlite_packs:
            pack_id = p["id"]
            db_fn = f"{pack_id}.db"
            db_path = root_path / db_fn

            expected_keys = ctx.expectations["packs"][pack_id]["keys"]
            conn = _open_ro_db(db_path)
            conn.text_factory = bytes
            try:
                cur = conn.cursor()
                if pack_id == "quran-core":
                    act_keys = _fetch_validated_keys(cur, "ayahs", "verse_key", "id", root_label, db_fn)
                    core_json = load_locked_json(ctx, "sqlite/quran-core.json")
                    locked_pks = sorted([r["verse_key"] for r in core_json["ayahs"]])
                    surah_ids = _fetch_validated_keys(cur, "surahs", "id", "id", root_label, db_fn, is_int=True)
                    if surah_ids != [s["id"] for s in core_json["surahs"]]:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", "surahs keys mismatch in quran-core")
                elif pack_id == "quran-scripts-extended":
                    act_keys = _fetch_validated_keys(cur, "ayah_scripts", "verse_key", "ayah_id", root_label, db_fn)
                    ext_json = load_locked_json(ctx, "sqlite/quran-scripts-extended.json")
                    locked_pks = sorted([r["verse_key"] for r in ext_json["ayah_scripts"]])
                elif pack_id == "quran-tr-en-fixture":
                    act_keys = _fetch_validated_keys(cur, "translations", "verse_key", "ayah_id", root_label, db_fn)
                    tr_json = load_locked_json(ctx, "sqlite/quran-tr-en-fixture.json")
                    locked_pks = sorted([r["verse_key"] for r in tr_json["translations"]])
                elif pack_id == "quran-tafsir-ar-fixture":
                    act_keys = _fetch_validated_keys(cur, "tafsir_ayah_map", "verse_key", "ayah_id", root_label, db_fn)
                    tafsir_json = load_locked_json(ctx, "qul/tafsir-ar-fixture.json")
                    locked_pks = sorted([vk for entry in tafsir_json for vk in entry["verse_keys"]])
                elif pack_id == "quran-morphology":
                    act_keys = _fetch_validated_keys(cur, "words", "word_key", "id", root_label, db_fn)
                    morph_json = load_locked_json(ctx, "sqlite/quran-morphology.json")
                    locked_pks = sorted([r["word_key"] for r in morph_json["words"]])
                    for sub_tbl in ("roots", "lemmas", "verb_concordance", "morphology_segments"):
                        act_sub = _fetch_validated_keys(cur, sub_tbl, "id", "id", root_label, db_fn, is_int=True)
                        exp_sub = [r["id"] for r in morph_json[sub_tbl]]
                        if act_sub != exp_sub:
                            raise GateCheckError(1, f"{root_label}/{db_fn}", f"{sub_tbl} keys mismatch in morphology")
                elif pack_id == "quran-wbw-en":
                    act_keys = _fetch_validated_keys(cur, "word_translations", "word_key", "id", root_label, db_fn)
                    wbw_json = load_locked_json(ctx, "sqlite/quran-wbw-en.json")
                    locked_pks = sorted([r["word_key"] for r in wbw_json["word_translations"]])
                elif pack_id == "hadith-fixture":
                    act_keys = _fetch_validated_keys(cur, "hadiths", "id", "id", root_label, db_fn, is_int=True)
                    hadith_json = load_locked_json(ctx, "hadith/fixture.json")
                    locked_pks = sorted([r["id"] for r in hadith_json["hadiths"]])
                    act_books = _fetch_validated_keys(cur, "books", "id", "id", root_label, db_fn, is_int=True)
                    if act_books != [b["id"] for b in hadith_json["books"]]:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", "books keys mismatch in hadith")
                    act_chaps = _fetch_validated_keys(cur, "chapters", "id", "id", root_label, db_fn, is_int=True)
                    if act_chaps != [c["id"] for c in hadith_json["chapters"]]:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", "chapters keys mismatch in hadith")
                else:
                    act_keys = []
                    locked_pks = []

                if act_keys != expected_keys:
                    raise GateCheckError(1, f"{root_label}/{db_fn}", f"Record keys mismatch: expected {expected_keys}, got {act_keys}")
                if act_keys != locked_pks:
                    raise GateCheckError(1, f"{root_label}/{db_fn}", f"Record keys mismatch against locked inputs: expected {locked_pks}, got {act_keys}")
            finally:
                conn.close()


def _stage_8_text_equivalence_v010_pack(
    ctx: GateContext,
    cur: sqlite3.Cursor,
    pack_id: str,
    root_label: str,
    db_fn: str,
) -> None:
    coords = load_tanzil_coordinates(ctx.sources_root / "raw-inputs" / "tanzil" / "quran-data-Tanzil.xml")
    if pack_id == "quran-core":
        uthmani_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "quran-uthmani.xml", coords, locked_path="tanzil/quran-uthmani.xml")
        clean_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "quran-simple-clean.xml", coords, locked_path="tanzil/quran-simple-clean.xml")
        cur.execute("SELECT verse_key, text_uthmani, text_simple_clean, text_ar_search FROM ayahs ORDER BY id;")
        for r in cur.fetchall():
            vk = r[0].decode("utf-8") if isinstance(r[0], bytes) else r[0]
            u_t = r[1].decode("utf-8") if isinstance(r[1], bytes) else r[1]
            c_t = r[2].decode("utf-8") if isinstance(r[2], bytes) else r[2]
            s_t = r[3].decode("utf-8") if isinstance(r[3], bytes) else r[3]
            if u_t != uthmani_text[vk]:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"text_uthmani mismatch at {vk}")
            if c_t != clean_text[vk]:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"text_simple_clean mismatch at {vk}")
            if s_t != normalize_arabic(c_t):
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"text_ar_search mismatch at {vk}")

    elif pack_id == "quran-scripts-extended":
        uth_min_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "quran-uthmani-min.xml", coords, locked_path="tanzil/quran-uthmani-min.xml")
        simple_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "quran-simple.xml", coords, locked_path="tanzil/quran-simple.xml")
        plain_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "quran-simple-plain.xml", coords, locked_path="tanzil/quran-simple-plain.xml")
        min_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "quran-simple-min.xml", coords, locked_path="tanzil/quran-simple-min.xml")

        cur.execute("SELECT verse_key, text_uthmani_min, text_simple, text_simple_plain, text_simple_min FROM ayah_scripts ORDER BY ayah_id;")
        for r in cur.fetchall():
            vk = r[0].decode("utf-8") if isinstance(r[0], bytes) else r[0]
            if (r[1].decode("utf-8") if isinstance(r[1], bytes) else r[1]) != uth_min_text[vk]:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"text_uthmani_min mismatch at {vk}")
            if (r[2].decode("utf-8") if isinstance(r[2], bytes) else r[2]) != simple_text[vk]:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"text_simple mismatch at {vk}")
            if (r[3].decode("utf-8") if isinstance(r[3], bytes) else r[3]) != plain_text[vk]:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"text_simple_plain mismatch at {vk}")
            if (r[4].decode("utf-8") if isinstance(r[4], bytes) else r[4]) != min_text[vk]:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"text_simple_min mismatch at {vk}")

    elif pack_id == "quran-tr-en-pickthall":
        tr_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "en.pickthall.xml", coords, locked_path="tanzil/en.pickthall.xml")
        cur.execute("SELECT verse_key, text FROM translations ORDER BY ayah_id;")
        for r in cur.fetchall():
            vk = r[0].decode("utf-8") if isinstance(r[0], bytes) else r[0]
            txt = r[1].decode("utf-8") if isinstance(r[1], bytes) else r[1]
            if txt != tr_text[vk]:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"translations text mismatch at {vk}")

    elif pack_id == "quran-tafsir-ar-jalalayn":
        tafsir_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "ar.jalalayn.xml", coords, locked_path="tanzil/ar.jalalayn.xml")
        cur.execute("SELECT m.verse_key, e.text, e.text_search FROM tafsir_ayah_map m JOIN tafsir_entries e ON m.entry_id = e.id ORDER BY m.ayah_id;")
        for r in cur.fetchall():
            vk = r[0].decode("utf-8") if isinstance(r[0], bytes) else r[0]
            txt = r[1].decode("utf-8") if isinstance(r[1], bytes) else r[1]
            ts = r[2].decode("utf-8") if isinstance(r[2], bytes) else r[2]
            if txt != tafsir_text[vk]:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"tafsir text mismatch at {vk}")
            if ts != project_tafsir_text(txt, text_format="plain", language="ar"):
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"tafsir text_search mismatch at {vk}")

    elif pack_id == "quran-morphology":
        corpus_path = ctx.sources_root / "raw-inputs" / "corpus" / "quranic-corpus-morphology-0.4.zip"
        parsed_segments = read_corpus_segments(corpus_path, coords.ayah_ids)

        cur.execute("SELECT id, word_id, segment_number, segment_role, form_buckwalter, form_ar, pos_tag, features_raw FROM morphology_segments ORDER BY id;")
        db_segs = cur.fetchall()
        if len(db_segs) != len(parsed_segments):
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"morphology_segments count mismatch: expected {len(parsed_segments)}, got {len(db_segs)}")

        for db_seg, src_seg in zip(db_segs, parsed_segments):
            (seg_id, w_id, s_num, s_role, fb, fa, pos, feat) = [
                v.decode("utf-8") if isinstance(v, bytes) else v for v in db_seg
            ]
            if s_num != src_seg.segment:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"segment_number mismatch at {seg_id}")
            if s_role != src_seg.role:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"segment_role mismatch at {seg_id}")
            if fb != src_seg.form_buckwalter:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"form_buckwalter mismatch at {seg_id}")
            if fa != src_seg.form_ar:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"form_ar mismatch at {seg_id}")
            if pos != src_seg.pos_tag:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"pos_tag mismatch at {seg_id}")
            if feat != src_seg.features_raw:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"features_raw mismatch at {seg_id}")

        expected_words_text: list[tuple[str, str, str]] = []
        for (s_id, a_id, w_num), seg_iter in itertools.groupby(parsed_segments, key=lambda s: (s.surah, s.ayah, s.word)):
            segs = list(seg_iter)
            uthmani = "".join(s.form_ar for s in segs)
            clean = normalize_arabic(uthmani)
            wk = f"{s_id}:{a_id}:{w_num}"
            expected_words_text.append((wk, uthmani, clean))

        cur.execute("SELECT id, word_key, word_ar_uthmani, word_ar_clean FROM words ORDER BY id;")
        db_words = cur.fetchall()
        if len(db_words) != len(expected_words_text):
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"words count mismatch: expected {len(expected_words_text)}, got {len(db_words)}")
        for db_w, (exp_wk, exp_uthmani, exp_clean) in zip(db_words, expected_words_text):
            wid = db_w[0]
            db_wk = db_w[1].decode("utf-8") if isinstance(db_w[1], bytes) else db_w[1]
            db_uthmani = db_w[2].decode("utf-8") if isinstance(db_w[2], bytes) else db_w[2]
            db_clean = db_w[3].decode("utf-8") if isinstance(db_w[3], bytes) else db_w[3]
            if db_wk != exp_wk:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"word_key mismatch at word {wid}: {db_wk} vs {exp_wk}")
            if db_uthmani != exp_uthmani:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"word_ar_uthmani mismatch at word {wid}")
            if db_clean != exp_clean:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"word_ar_clean mismatch at word {wid}")

def _verify_hadith_db_text_and_source(
    ctx: GateContext,
    cur: sqlite3.Cursor,
    pack_id: str,
    slug: str,
    root_label: str,
    db_fn: str,
) -> None:
    hadith_json = load_locked_json(ctx, f"hadith/{slug}.json")
    hadith_schema_data = parse_strict_json(ctx.hadith_input_schema_path.read_text(encoding="utf-8"))
    v = StrictDraft202012Validator(hadith_schema_data)
    try:
        v.validate(hadith_json)
    except ValidationError as exc:
        raise GateCheckError(2, f"raw-inputs/hadith/{slug}.json", f"Hadith input schema validation failed: {exc.message}")

    h_ids = [h["id"] for h in hadith_json["hadiths"]]
    if len(h_ids) != len(set(h_ids)):
        raise GateCheckError(2, f"raw-inputs/hadith/{slug}.json", "Duplicate hadith ID in raw input")
    h_urns = [h["urn"] for h in hadith_json["hadiths"] if h.get("urn") is not None]
    if len(h_urns) != len(set(h_urns)):
        raise GateCheckError(2, f"raw-inputs/hadith/{slug}.json", "Duplicate hadith URN in raw input")

    chap_books = {c["id"]: c["bookId"] for c in hadith_json.get("chapters", [])}
    for h in hadith_json["hadiths"]:
        c_id = h.get("chapterId")
        if c_id is not None:
            if c_id not in chap_books or chap_books[c_id] != h["bookId"]:
                raise GateCheckError(2, f"raw-inputs/hadith/{slug}.json", f"Hadith {h['id']} chapter {c_id} does not belong to book {h['bookId']}")

    splits_path = ctx.sources_root / "raw-inputs" / "hadith" / f"{slug}.splits.json"
    splits_data = parse_strict_json(splits_path.read_text(encoding="utf-8")) if splits_path.is_file() else {}

    cur.execute(
        "SELECT id, book_id, chapter_id, hadith_number, urn, arabic_full, english_full, "
        "text_split_mode, arabic_sanad, arabic_matn, arabic_matn_clean, english_narrator, "
        "english_body, grade_ar, grade_en, reference_json, is_ar_missing, is_en_missing, arabic_full_clean "
        "FROM hadiths ORDER BY id;"
    )
    db_hadiths = cur.fetchall()
    if len(db_hadiths) != len(hadith_json["hadiths"]):
        raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith count mismatch: {len(db_hadiths)} vs {len(hadith_json['hadiths'])}")
    sorted_raw_hadiths = sorted(hadith_json["hadiths"], key=lambda x: x["id"])
    for db_h, raw_h in zip(db_hadiths, sorted_raw_hadiths):
        (
            h_id, b_id, c_id, h_num, urn, ar_full, en_full,
            split_mode, ar_sanad, ar_matn, ar_matn_clean,
            en_narrator, en_body, g_ar, g_en, ref_json,
            is_ar_miss, is_en_miss, ar_full_clean
        ) = [val.decode("utf-8") if isinstance(val, bytes) else val for val in db_h]

        if h_id != raw_h["id"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith id mismatch: {h_id} vs {raw_h['id']}")
        if b_id != raw_h["bookId"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} book_id mismatch")
        if c_id != raw_h["chapterId"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} chapter_id mismatch")
        if str(h_num) != str(raw_h["hadithNumber"]):
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} hadith_number mismatch")
        if urn != raw_h["urn"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} urn mismatch")
        if ar_full != raw_h["arabicFull"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} arabic_full mismatch")
        if en_full != raw_h["englishFull"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} english_full mismatch")
        if split_mode != raw_h["textSplitMode"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} text_split_mode mismatch")
        if en_narrator != raw_h["englishNarrator"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} english_narrator mismatch")
        if en_body != raw_h["englishBody"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} english_body mismatch")
        if g_ar != raw_h["gradeAr"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} grade_ar mismatch")
        if g_en != raw_h["gradeEn"]:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} grade_en mismatch")

        exp_ref_json = (
            json.dumps(raw_h["reference"], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if raw_h.get("reference") is not None else None
        )
        if ref_json != exp_ref_json:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} reference_json mismatch")

        exp_is_ar = 1 if raw_h["arabicFull"] is None else 0
        exp_is_en = 1 if raw_h["englishFull"] is None else 0
        if is_ar_miss != exp_is_ar:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} is_ar_missing mismatch")
        if is_en_miss != exp_is_en:
            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Hadith {h_id} is_en_missing mismatch")

        if split_mode == "unsplit_raw":
            if ar_sanad is not None or ar_matn is not None or ar_matn_clean is not None:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Unsplit Hadith record {h_id} must have NULL split fields")
            exp_clean = normalize_arabic(ar_full) if ar_full is not None else None
            if ar_full_clean != exp_clean:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"arabic_full_clean search projection mismatch for Hadith {h_id}")
        elif split_mode == "source_verified":
            h_id_str = str(h_id)
            if h_id_str not in splits_data:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"source_verified Hadith {h_id} missing split evidence in splits file")
            split_ev = splits_data[h_id_str]
            if not isinstance(split_ev, dict) or "sourceRef" not in split_ev:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Invalid split evidence shape for Hadith {h_id}")
            if ar_sanad != split_ev.get("arabicSanad") or ar_sanad != raw_h.get("arabicSanad"):
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"arabic_sanad mismatch for Hadith {h_id}")
            if ar_matn != split_ev.get("arabicMatn") or ar_matn != raw_h.get("arabicMatn"):
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"arabic_matn mismatch for Hadith {h_id}")
            if normalize_arabic(ar_matn) != ar_matn_clean:
                raise GateCheckError(2, f"{root_label}/{db_fn}", f"arabic_matn_clean search projection mismatch for Hadith {h_id}")

    _verify_table_rows_by_pk(
        cur, "books", ["id", "number", "name_ar", "name_en"], "id", hadith_json["books"], root_label, db_fn,
        projections={"number": lambda r: str(r["number"]), "name_ar": lambda r: r.get("nameAr"), "name_en": lambda r: r.get("nameEn")}
    )
    _verify_table_rows_by_pk(
        cur, "chapters", ["id", "book_id", "number", "title_ar", "title_en"], "id", hadith_json.get("chapters", []), root_label, db_fn,
        projections={"book_id": lambda r: r["bookId"], "number": lambda r: str(r["number"]) if r.get("number") is not None else None, "title_ar": lambda r: r.get("titleAr"), "title_en": lambda r: r.get("titleEn")}
    )

def stage_8_text_equivalence(ctx: GateContext) -> None:
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
            for db_fn in sorted(ctx.declared_files_by_root[root_label]):
                pack_id = db_fn.removesuffix(".db")
                slug = pack_id.removeprefix("hadith-")
                db_path = root_path / db_fn
                conn = _open_ro_db(db_path)
                conn.text_factory = bytes
                try:
                    cur = conn.cursor()
                    cur.execute("PRAGMA table_list;")
                    shadows = {r[1] for r in cur.fetchall() if len(r) > 2 and r[2] == "shadow"}
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
                    tables = [
                        (r[0].decode() if isinstance(r[0], bytes) else r[0])
                        for r in cur.fetchall()
                        if (r[0].decode() if isinstance(r[0], bytes) else r[0]) not in shadows
                        and not (r[0].decode() if isinstance(r[0], bytes) else r[0]).endswith("_fts")
                    ]
                    for tbl in tables:
                        if not re.fullmatch(r"^[a-zA-Z0-9_]+$", tbl):
                            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Invalid table name: {tbl!r}")
                        cur.execute(f'PRAGMA table_info("{tbl}");')
                        text_columns = [
                            (r[1].decode() if isinstance(r[1], bytes) else r[1], r[3])
                            for r in cur.fetchall()
                            if "TEXT" in (r[2].decode() if isinstance(r[2], bytes) else r[2]).upper()
                        ]
                        for col_name, notnull in text_columns:
                            if not re.fullmatch(r"^[a-zA-Z0-9_]+$", col_name):
                                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Invalid column name: {col_name!r}")
                            cur.execute(f'SELECT rowid, typeof("{col_name}"), CAST("{col_name}" AS BLOB) FROM "{tbl}" WHERE "{col_name}" IS NOT NULL;')
                            for row in cur.fetchall():
                                row_id, col_type, col_bytes = row[0], row[1], row[2]
                                type_str = col_type.decode() if isinstance(col_type, bytes) else str(col_type)
                                if type_str.lower() != "text":
                                    raise GateCheckError(2, f"{root_label}/{db_fn}", f"Non-TEXT storage class {type_str!r} in TEXT column {tbl}.{col_name} at rowid {row_id}")
                                if col_bytes is None:
                                    continue
                                try:
                                    decoded = col_bytes.decode("utf-8")
                                except UnicodeDecodeError as exc:
                                    raise GateCheckError(2, f"{root_label}/{db_fn}", f"Malformed UTF-8 in {tbl}.{col_name} at rowid {row_id}: {exc}") from exc
                                if "\ufffd" in decoded or b"\xef\xbf\xbd" in col_bytes:
                                    raise GateCheckError(2, f"{root_label}/{db_fn}", f"U+FFFD replacement character in {tbl}.{col_name} at rowid {row_id}")
                                for ch in decoded:
                                    if 0xD800 <= ord(ch) <= 0xDFFF:
                                        raise GateCheckError(2, f"{root_label}/{db_fn}", f"Lone surrogate U+{ord(ch):04X} in {tbl}.{col_name} at rowid {row_id}")
                                if notnull and not decoded.strip():
                                    raise GateCheckError(2, f"{root_label}/{db_fn}", f"Empty text in NOT NULL column {tbl}.{col_name} at rowid {row_id}")

                    _verify_hadith_db_text_and_source(ctx, cur, pack_id, slug, root_label, db_fn)
                finally:
                    conn.close()
        return

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        sqlite_packs = sorted([p for p in cat_data["packs"] if p["format"] == "sqlite"], key=lambda p: p["id"])

        for p in sqlite_packs:
            pack_id = p["id"]
            db_fn = f"{pack_id}.db"
            db_path = root_path / db_fn

            conn = _open_ro_db(db_path)
            conn.text_factory = bytes
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA table_list;")
                shadows = {r[1] for r in cur.fetchall() if len(r) > 2 and r[2] == "shadow"}
                cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
                tables = [
                    (r[0].decode() if isinstance(r[0], bytes) else r[0])
                    for r in cur.fetchall()
                    if (r[0].decode() if isinstance(r[0], bytes) else r[0]) not in shadows
                    and not (r[0].decode() if isinstance(r[0], bytes) else r[0]).endswith("_fts")
                ]

                for tbl in tables:
                    if not re.fullmatch(r"^[a-zA-Z0-9_]+$", tbl):
                        raise GateCheckError(2, f"{root_label}/{db_fn}", f"Invalid table name: {tbl!r}")
                    cur.execute(f'PRAGMA table_info("{tbl}");')
                    text_columns = [
                        (r[1].decode() if isinstance(r[1], bytes) else r[1], r[3])
                        for r in cur.fetchall()
                        if "TEXT" in (r[2].decode() if isinstance(r[2], bytes) else r[2]).upper()
                    ]
                    for col_name, notnull in text_columns:
                        if not re.fullmatch(r"^[a-zA-Z0-9_]+$", col_name):
                            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Invalid column name: {col_name!r}")
                        cur.execute(f'SELECT rowid, typeof("{col_name}"), CAST("{col_name}" AS BLOB) FROM "{tbl}" WHERE "{col_name}" IS NOT NULL;')
                        for row in cur.fetchall():
                            row_id, col_type, col_bytes = row[0], row[1], row[2]
                            type_str = col_type.decode() if isinstance(col_type, bytes) else str(col_type)
                            if type_str.lower() != "text":
                                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Non-TEXT storage class {type_str!r} in TEXT column {tbl}.{col_name} at rowid {row_id}")
                            if col_bytes is None:
                                continue
                            try:
                                decoded = col_bytes.decode("utf-8")
                            except UnicodeDecodeError as exc:
                                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Malformed UTF-8 in {tbl}.{col_name} at rowid {row_id}: {exc}") from exc
                            if "\ufffd" in decoded or b"\xef\xbf\xbd" in col_bytes:
                                raise GateCheckError(2, f"{root_label}/{db_fn}", f"U+FFFD replacement character in {tbl}.{col_name} at rowid {row_id}")
                            for ch in decoded:
                                if 0xD800 <= ord(ch) <= 0xDFFF:
                                    raise GateCheckError(2, f"{root_label}/{db_fn}", f"Lone surrogate U+{ord(ch):04X} in {tbl}.{col_name} at rowid {row_id}")
                            if notnull and not decoded.strip():
                                if tbl == "morphology_segments" and col_name in ("form_buckwalter", "form_ar"):
                                    continue
                                raise GateCheckError(2, f"{root_label}/{db_fn}", f"Empty text in NOT NULL column {tbl}.{col_name} at rowid {row_id}")

                if ctx.profile == "v0.1.0":
                    _stage_8_text_equivalence_v010_pack(ctx, cur, pack_id, root_label, db_fn)
                    continue
                if pack_id == "quran-core":
                    core_json = load_locked_json(ctx, "sqlite/quran-core.json")
                    _verify_table_rows_by_pk(
                        cur, "surahs",
                        ["id", "name_ar", "name_tname", "name_en", "revelation_type", "revelation_order", "ayah_count", "ruku_count", "start_ayah_id"],
                        "id", core_json["surahs"], root_label, db_fn
                    )
                    _verify_table_rows_by_pk(
                        cur, "ayahs",
                        ["id", "surah_id", "ayah_number", "verse_key", "text_uthmani", "text_simple_clean", "text_ar_search", "juz", "hizb_quarter", "manzil", "ruku", "page_madani", "sajdah_type"],
                        "id", core_json["ayahs"], root_label, db_fn,
                        projections={"text_ar_search": lambda r: normalize_arabic(r["text_simple_clean"])}
                    )
                elif pack_id == "quran-scripts-extended":
                    ext_json = load_locked_json(ctx, "sqlite/quran-scripts-extended.json")
                    _verify_table_rows_by_pk(
                        cur, "ayah_scripts",
                        ["ayah_id", "verse_key", "text_uthmani_min", "text_simple", "text_simple_plain", "text_simple_min"],
                        "ayah_id", ext_json["ayah_scripts"], root_label, db_fn
                    )
                elif pack_id == "quran-tr-en-fixture":
                    tr_json = load_locked_json(ctx, "sqlite/quran-tr-en-fixture.json")
                    _verify_table_rows_by_pk(
                        cur, "translations",
                        ["ayah_id", "surah_id", "ayah_number", "verse_key", "text"],
                        "ayah_id", tr_json["translations"], root_label, db_fn
                    )
                elif pack_id == "quran-tafsir-ar-fixture":
                    tafsir_json = load_locked_json(ctx, "qul/tafsir-ar-fixture.json")
                    cur.execute("SELECT id, text, text_format, text_search FROM tafsir_entries ORDER BY start_ayah_id;")
                    t_rows = cur.fetchall()
                    if len(t_rows) != len(tafsir_json):
                        raise GateCheckError(2, f"{root_label}/{db_fn}", f"tafsir_entries count mismatch: {len(t_rows)} vs {len(tafsir_json)}")
                    for (e_id, db_text_b, db_fmt_b, db_search_b), exp_entry in zip(t_rows, tafsir_json):
                        db_t = db_text_b.decode("utf-8") if isinstance(db_text_b, bytes) else db_text_b
                        db_f = db_fmt_b.decode("utf-8") if isinstance(db_fmt_b, bytes) else db_fmt_b
                        db_s = db_search_b.decode("utf-8") if isinstance(db_search_b, bytes) else db_search_b
                        if db_t != exp_entry["text"]:
                            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Tafsir text mismatch in entry {e_id}")
                        if db_f != exp_entry["format"]:
                            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Tafsir text_format mismatch in entry {e_id}")
                        expected_proj = project_tafsir_text(exp_entry["text"], text_format=exp_entry["format"], language="ar")
                        if db_s != expected_proj:
                            raise GateCheckError(2, f"{root_label}/{db_fn}", f"Tafsir search projection mismatch in entry {e_id}")
                elif pack_id == "quran-morphology":
                    morph_json = load_locked_json(ctx, "sqlite/quran-morphology.json")
                    _verify_table_rows_by_pk(
                        cur, "words",
                        ["id", "ayah_id", "surah_id", "ayah_number", "word_number", "word_key",
                         "word_ar_uthmani", "word_ar_clean", "primary_pos", "is_verb",
                         "root_id", "lemma_id", "verb_concordance_id", "verb_form", "verb_tense", "verb_voice"],
                        "id", morph_json["words"], root_label, db_fn,
                        projections={"word_ar_clean": lambda r: normalize_arabic(r["word_ar_uthmani"])}
                    )
                    _verify_table_rows_by_pk(cur, "roots",
                        ["id", "root_buckwalter", "root_ar", "root_ar_spaced", "total_occurrences",
                         "verb_occurrences", "noun_occurrences", "frequency_rank"],
                        "id", morph_json["roots"], root_label, db_fn)
                    _verify_table_rows_by_pk(cur, "lemmas",
                        ["id", "lemma_buckwalter", "lemma_ar", "root_id", "primary_pos",
                         "occurrence_count", "frequency_rank"],
                        "id", morph_json["lemmas"], root_label, db_fn)
                    _verify_table_rows_by_pk(cur, "verb_concordance",
                        ["id", "root_id", "lemma_id", "verb_ar", "verb_buckwalter", "root_ar",
                         "verb_form", "verb_form_roman", "total_count", "perf_count", "impf_count",
                         "impv_count", "active_count", "passive_count", "frequency_rank"],
                        "id", morph_json["verb_concordance"], root_label, db_fn)
                    _verify_table_rows_by_pk(cur, "morphology_segments",
                        ["id", "word_id", "segment_number", "segment_role", "form_buckwalter",
                         "form_ar", "pos_tag", "features_raw"],
                        "id", morph_json["morphology_segments"], root_label, db_fn)
                elif pack_id == "quran-wbw-en":
                    wbw_json = load_locked_json(ctx, "sqlite/quran-wbw-en.json")
                    _verify_table_rows_by_pk(
                        cur, "word_translations",
                        ["id", "ayah_id", "word_number", "word_key", "text_uthmani", "translation", "transliteration"],
                        "id", wbw_json["word_translations"], root_label, db_fn
                    )
                elif pack_id == "hadith-fixture":
                    _verify_hadith_db_text_and_source(ctx, cur, pack_id, "fixture", root_label, db_fn)
            finally:
                conn.close()


def stage_9_coverage(ctx: GateContext) -> None:
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
            for db_fn in sorted(ctx.declared_files_by_root[root_label]):
                pack_id = db_fn.removesuffix(".db")
                slug = pack_id.removeprefix("hadith-")
                oracle = _HADITH_LOCAL_ORACLE.get(slug)
                if not oracle:
                    raise GateCheckError(1, f"{root_label}/{db_fn}", f"Unknown Hadith pack slug {slug}")
                (exp_total, exp_ar_pres, exp_en_pres, exp_en_miss, exp_both) = oracle["expected_counts"]

                db_path = root_path / db_fn
                conn = _open_ro_db(db_path)
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT metric, count FROM coverage_report;")
                    cov = dict(cur.fetchall())

                    if cov.get("expected_total") != exp_total:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"expected_total mismatch: expected {exp_total}, got {cov.get('expected_total')}")
                    if cov.get("ar_present") != exp_ar_pres:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"ar_present mismatch: expected {exp_ar_pres}, got {cov.get('ar_present')}")
                    if cov.get("en_present") != exp_en_pres:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"en_present mismatch: expected {exp_en_pres}, got {cov.get('en_present')}")
                    if cov.get("en_missing") != exp_en_miss:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"en_missing mismatch: expected {exp_en_miss}, got {cov.get('en_missing')}")

                    cur.execute("SELECT COUNT(*), COUNT(arabic_full), COUNT(english_full), SUM(is_en_missing) FROM hadiths;")
                    (act_total, act_ar, act_en, act_en_miss) = cur.fetchone()
                    act_en_miss = act_en_miss if act_en_miss is not None else 0

                    if act_total != exp_total:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"hadiths count mismatch: expected {exp_total}, got {act_total}")
                    if act_ar != exp_ar_pres:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"hadiths arabic_full present mismatch: expected {exp_ar_pres}, got {act_ar}")
                    if act_en != exp_en_pres:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"hadiths english_full present mismatch: expected {exp_en_pres}, got {act_en}")
                    if act_en_miss != exp_en_miss:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"hadiths is_en_missing sum mismatch: expected {exp_en_miss}, got {act_en_miss}")

                    cur.execute("SELECT COUNT(*) FROM hadiths WHERE arabic_full IS NOT NULL AND english_full IS NOT NULL;")
                    act_both = cur.fetchone()[0]
                    if act_both != exp_both:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"hadiths both present mismatch: expected {exp_both}, got {act_both}")
                finally:
                    conn.close()
        return

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        sqlite_packs = sorted([p for p in cat_data["packs"] if p["format"] == "sqlite"], key=lambda p: p["id"])

        for p in sqlite_packs:
            pack_id = p["id"]
            db_fn = f"{pack_id}.db"
            db_path = root_path / db_fn

            conn = _open_ro_db(db_path)
            conn.text_factory = bytes
            try:
                cur = conn.cursor()
                if pack_id == "quran-core":
                    cur.execute("SELECT text_uthmani, text_simple_clean FROM ayahs;")
                    rows = cur.fetchall()
                    tot = len(ctx.get_tanzil_coordinates().ayah_ids) if ctx.profile == "v0.1.0" else len(rows)
                    ar_cnt = sum(
                        1 for r in rows
                        if (r[0].decode("utf-8") if isinstance(r[0], bytes) else str(r[0])).strip()
                        and (r[1].decode("utf-8") if isinstance(r[1], bytes) else str(r[1])).strip()
                    )
                    computed_coverage = {"expected_total": tot, "ar_present": ar_cnt}
                    avail = ar_cnt
                elif pack_id == "quran-scripts-extended":
                    cur.execute("SELECT text_uthmani_min, text_simple, text_simple_plain, text_simple_min FROM ayah_scripts;")
                    rows = cur.fetchall()
                    tot = len(ctx.get_tanzil_coordinates().ayah_ids) if ctx.profile == "v0.1.0" else len(rows)
                    ar_cnt = sum(
                        1 for r in rows
                        if all((col.decode("utf-8") if isinstance(col, bytes) else str(col)).strip() for col in r)
                    )
                    computed_coverage = {"expected_total": tot, "ar_present": ar_cnt}
                    avail = ar_cnt
                elif pack_id in ("quran-tr-en-fixture", "quran-tr-en-pickthall"):
                    cur.execute("SELECT text FROM translations;")
                    rows = cur.fetchall()
                    tot = len(ctx.get_tanzil_coordinates().ayah_ids) if ctx.profile == "v0.1.0" else len(rows)
                    en_cnt = sum(
                        1 for r in rows
                        if r[0] and (r[0].decode("utf-8") if isinstance(r[0], bytes) else str(r[0])).strip()
                    )
                    computed_coverage = {"expected_total": tot, "en_present": en_cnt, "en_missing": tot - en_cnt}
                    avail = en_cnt
                elif pack_id in ("quran-tafsir-ar-fixture", "quran-tafsir-ar-jalalayn"):
                    cur.execute(
                        "SELECT e.text, e.text_format "
                        "FROM tafsir_ayah_map m "
                        "JOIN tafsir_entries e ON m.entry_id = e.id "
                        "ORDER BY m.ayah_id;"
                    )
                    rows = cur.fetchall()
                    tot = len(ctx.get_tanzil_coordinates().ayah_ids) if ctx.profile == "v0.1.0" else len(rows)
                    ar_cnt = 0
                    for r in rows:
                        raw_t = r[0].decode("utf-8") if isinstance(r[0], bytes) else str(r[0])
                        fmt = r[1].decode("utf-8") if isinstance(r[1], bytes) else str(r[1])
                        plain = project_tafsir_text(raw_t, text_format=fmt, language="ar")
                        if plain.strip():
                            ar_cnt += 1
                    computed_coverage = {"expected_total": tot, "ar_present": ar_cnt}
                    avail = ar_cnt
                elif pack_id == "quran-morphology":
                    cur.execute("SELECT word_ar_uthmani, word_ar_clean FROM words;")
                    rows = cur.fetchall()
                    if ctx.profile == "v0.1.0":
                        segments = ctx.get_corpus_segments()
                        tot = len({(s.surah, s.ayah, s.word) for s in segments})
                    else:
                        tot = len(rows)
                    ar_cnt = sum(
                        1 for r in rows
                        if r[0] and (r[0].decode("utf-8") if isinstance(r[0], bytes) else str(r[0])).strip()
                        and r[1] and (r[1].decode("utf-8") if isinstance(r[1], bytes) else str(r[1])).strip()
                    )
                    computed_coverage = {"expected_total": tot, "ar_present": ar_cnt}
                    avail = ar_cnt
                elif pack_id == "quran-wbw-en":
                    cur.execute("SELECT text_uthmani, translation FROM word_translations;")
                    rows = cur.fetchall()
                    tot = len(rows)
                    ar_cnt = sum(
                        1 for r in rows
                        if r[0] and (r[0].decode("utf-8") if isinstance(r[0], bytes) else str(r[0])).strip()
                    )
                    en_cnt = sum(
                        1 for r in rows
                        if r[1] and (r[1].decode("utf-8") if isinstance(r[1], bytes) else str(r[1])).strip()
                    )
                    computed_coverage = {"expected_total": tot, "ar_present": ar_cnt, "en_present": en_cnt, "en_missing": tot - en_cnt}
                    avail = sum(
                        1 for r in rows
                        if r[0] and (r[0].decode("utf-8") if isinstance(r[0], bytes) else str(r[0])).strip()
                        and r[1] and (r[1].decode("utf-8") if isinstance(r[1], bytes) else str(r[1])).strip()
                    )
                elif pack_id == "hadith-fixture":
                    cur.execute("SELECT arabic_full, english_full FROM hadiths;")
                    rows = cur.fetchall()
                    tot = len(rows)
                    ar_cnt = sum(
                        1 for r in rows
                        if r[0] and (r[0].decode("utf-8") if isinstance(r[0], bytes) else str(r[0])).strip()
                    )
                    en_cnt = sum(
                        1 for r in rows
                        if r[1] and (r[1].decode("utf-8") if isinstance(r[1], bytes) else str(r[1])).strip()
                    )
                    computed_coverage = {"expected_total": tot, "ar_present": ar_cnt, "en_present": en_cnt, "en_missing": tot - en_cnt}
                    avail = sum(
                        1 for r in rows
                        if r[0] and (r[0].decode("utf-8") if isinstance(r[0], bytes) else str(r[0])).strip()
                        and r[1] and (r[1].decode("utf-8") if isinstance(r[1], bytes) else str(r[1])).strip()
                    )
                else:
                    computed_coverage = {}
                    avail = 0

                cur.execute("SELECT metric, count, notes FROM coverage_report;")
                actual_coverage = {}
                for r in cur.fetchall():
                    metric = r[0].decode() if isinstance(r[0], bytes) else str(r[0])
                    cnt = r[1]
                    notes = r[2]
                    if notes is not None:
                        raise GateCheckError(1, f"{root_label}/{db_fn}", f"coverage_report notes must be NULL, got {notes!r} for metric {metric}")
                    actual_coverage[metric] = cnt
                if actual_coverage != computed_coverage:
                    raise GateCheckError(1, f"{root_label}/{db_fn}", f"coverage_report mismatch with computed content: {actual_coverage} vs {computed_coverage}")

                if ctx.profile == "v0.1.0":
                    if pack_id in ("quran-core", "quran-scripts-extended", "quran-tafsir-ar-jalalayn"):
                        expected_cov = {"expected_total": 6236, "ar_present": 6236}
                    elif pack_id == "quran-tr-en-pickthall":
                        expected_cov = {"expected_total": 6236, "en_present": 6236, "en_missing": 0}
                    elif pack_id == "quran-morphology":
                        expected_cov = {"expected_total": 77429, "ar_present": 77429}
                    else:
                        expected_cov = {}
                else:
                    expected_cov = ctx.expectations["packs"][pack_id]["coverage"]
                if actual_coverage != expected_cov:
                    raise GateCheckError(1, f"{root_label}/{db_fn}", f"Coverage mismatch against expectations: expected {expected_cov}, got {actual_coverage}")

                cat_pack = [item for item in cat_data["packs"] if item["id"] == pack_id][0]
                exp_tot = computed_coverage["expected_total"]
                if cat_pack["coverage"]["expected"] != exp_tot:
                    raise GateCheckError(1, f"{root_label}/{db_fn}", f"Catalog coverage.expected mismatch for {pack_id}")

                if cat_pack["coverage"]["available"] != avail:
                    raise GateCheckError(1, f"{root_label}/{db_fn}", f"Catalog coverage.available mismatch for {pack_id}: expected {avail}, got {cat_pack['coverage']['available']}")

                expected_completeness = "complete" if avail == exp_tot else "partial"
                if cat_pack["completeness"] != expected_completeness:
                    raise GateCheckError(1, f"{root_label}/{db_fn}", f"Catalog completeness mismatch for {pack_id}: expected {expected_completeness}, got {cat_pack['completeness']}")
            finally:
                conn.close()


def _run_fts_temp_integrity_check(
    db_path: Path,
    fts_tables: tuple[str, ...],
    root_label: str,
    db_fn: str,
    pack_id: str,
) -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        temp_db_path = Path(tf.name)
    try:
        shutil.copyfile(db_path, temp_db_path)
        tconn = sqlite3.connect(temp_db_path)
        try:
            tcur = tconn.cursor()
            for fts_tbl in fts_tables:
                if fts_tbl not in _PACK_FTS_MAP[pack_id]:
                    raise GateCheckError(4, f"{root_label}/{db_fn}", f"Non-allowlisted FTS table {fts_tbl}")
                try:
                    tcur.execute(f"INSERT INTO {fts_tbl}({fts_tbl}, rank) VALUES('integrity-check', 1);")
                except Exception as exc:
                    raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS integrity-check failed on {fts_tbl}: {exc}") from exc
        finally:
            tconn.close()
    finally:
        if temp_db_path.exists():
            try:
                temp_db_path.unlink()
            except OSError:
                pass


def _run_fts_hadith_local(ctx: GateContext, root_label: str, db_fn: str, db_path: Path, slug: str) -> None:
    pack_id = f"hadith-{slug}"
    fts_tables = ("hadiths_fts", "hadiths_full_fts")
    _run_fts_temp_integrity_check(db_path, fts_tables, root_label, db_fn, pack_id)

    oracle = _HADITH_LOCAL_ORACLE.get(slug)
    if not oracle:
        raise GateCheckError(4, f"{root_label}/{db_fn}", f"Unknown Hadith pack slug {slug}")

    conn = _open_ro_db(db_path)
    try:
        cur = conn.cursor()

        # Token coverage checks using fts5vocab
        cur.execute("CREATE VIRTUAL TABLE temp.vocab_full USING fts5vocab(main, 'hadiths_full_fts', 'instance');")
        doc_set_full = {r[0] for r in cur.execute("SELECT DISTINCT doc FROM temp.vocab_full;").fetchall()}
        cur.execute("DROP TABLE temp.vocab_full;")

        cur.execute("SELECT id FROM hadiths WHERE arabic_full IS NOT NULL;")
        exp_ar_docs = {r[0] for r in cur.fetchall()}
        if doc_set_full != exp_ar_docs:
            raise GateCheckError(4, f"{root_label}/{db_fn}", f"fts5vocab hadiths_full_fts doc coverage mismatch: {len(doc_set_full)} vs {len(exp_ar_docs)}")

        cur.execute("CREATE VIRTUAL TABLE temp.vocab_primary USING fts5vocab(main, 'hadiths_fts', 'instance');")
        doc_set_en = {r[0] for r in cur.execute("SELECT DISTINCT doc FROM temp.vocab_primary WHERE col = 'english_body';").fetchall()}
        cur.execute("DROP TABLE temp.vocab_primary;")

        cur.execute("SELECT id FROM hadiths WHERE english_body IS NOT NULL;")
        exp_en_docs = {r[0] for r in cur.fetchall()}
        if doc_set_en != exp_en_docs:
            raise GateCheckError(4, f"{root_label}/{db_fn}", f"fts5vocab hadiths_fts english_body doc coverage mismatch: {len(doc_set_en)} vs {len(exp_en_docs)}")

        # Smoke queries
        row_id = oracle["id"]
        ar_literal = oracle["ar_literal"]
        en_literal = oracle["en_literal"]

        # 1. Secondary index: diacritized Arabic (normalized before quoting)
        ar_esc1 = normalize_arabic(ar_literal).replace('"', '""')
        q1 = f'"{ar_esc1}"'
        cur.execute("SELECT rowid FROM hadiths_full_fts WHERE hadiths_full_fts MATCH ? AND rowid = ? ORDER BY rowid;", (q1, row_id))
        if [r[0] for r in cur.fetchall()] != [row_id]:
            raise GateCheckError(4, f"{root_label}/{db_fn}", f"Secondary FTS diacritized Arabic query failed for {slug}")

        # 2. Secondary index: normalized Arabic
        norm_ar = normalize_arabic(ar_literal)
        ar_esc2 = normalize_arabic(norm_ar).replace('"', '""')
        q2 = f'"{ar_esc2}"'
        cur.execute("SELECT rowid FROM hadiths_full_fts WHERE hadiths_full_fts MATCH ? AND rowid = ? ORDER BY rowid;", (q2, row_id))
        if [r[0] for r in cur.fetchall()] != [row_id]:
            raise GateCheckError(4, f"{root_label}/{db_fn}", f"Secondary FTS normalized Arabic query failed for {slug}")

        # 3. Primary index: query same Arabic on arabic_matn_clean -> expect 0 rows
        q_matn = f'arabic_matn_clean : "{ar_esc1}"'
        cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ? AND rowid = ? ORDER BY rowid;", (q_matn, row_id))
        if cur.fetchall():
            raise GateCheckError(4, f"{root_label}/{db_fn}", f"Primary FTS arabic_matn_clean unexpectedly matched unsplit Arabic for {slug}")

        # 4. Primary index: query listed English phrase on english_body
        if en_literal != "none":
            en_esc = en_literal.replace('"', '""')
            q_en = f'english_body : "{en_esc}"'
            cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ? AND rowid = ? ORDER BY rowid;", (q_en, row_id))
            if [r[0] for r in cur.fetchall()] != [row_id]:
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"Primary FTS english_body query failed for {slug}")
        else:
            q_mercy = 'english_body : "mercy"'
            cur.execute("SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ? AND rowid = ? ORDER BY rowid;", (q_mercy, row_id))
            if cur.fetchall():
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"Primary FTS english_body unexpectedly matched 'mercy' for {slug}")

        # 5. Both indexes: query "zzzzfixturemissing" -> expect 0 rows
        for fts_tbl in ("hadiths_full_fts", "hadiths_fts"):
            cur.execute(f"SELECT rowid FROM {fts_tbl} WHERE {fts_tbl} MATCH ? AND rowid = ? ORDER BY rowid;", ('"zzzzfixturemissing"', row_id))
            if cur.fetchall():
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS query for negative literal matched in {fts_tbl} for {slug}")
    finally:
        conn.close()


def stage_10_fts(ctx: GateContext) -> None:
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
            for db_fn in sorted(ctx.declared_files_by_root[root_label]):
                slug = db_fn.removesuffix(".db").removeprefix("hadith-")
                _run_fts_hadith_local(ctx, root_label, db_fn, root_path / db_fn, slug)
        return

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        sqlite_packs = sorted([p for p in cat_data["packs"] if p["format"] == "sqlite"], key=lambda p: p["id"])

        for p in sqlite_packs:
            pack_id = p["id"]
            db_fn = f"{pack_id}.db"
            db_path = root_path / db_fn
            fts_tables = _PACK_FTS_MAP[pack_id]
            if not fts_tables:
                continue

            _run_fts_temp_integrity_check(db_path, fts_tables, root_label, db_fn, pack_id)
            if ctx.profile == "v0.1.0":
                _run_fts_queries_v010(ctx, root_label, db_fn, db_path, pack_id)
            else:
                # Execute declared FTS queries from expectations
                fts_specs = ctx.expectations["packs"][pack_id].get("fts", [])
                conn = _open_ro_db(db_path)
                try:
                    cur = conn.cursor()
                    for q_spec in fts_specs:
                        tbl = q_spec["table"]
                        if tbl not in _PACK_FTS_MAP[pack_id]:
                            raise GateCheckError(4, f"{root_label}/{db_fn}", f"Non-allowlisted FTS query table {tbl}")
                        query_str = q_spec["query"]
                        expected_row_ids = q_spec["rowIds"]

                        effective_query = query_str
                        if tbl in _ARABIC_FTS_TABLES and _ARABIC_CHAR_RE.search(query_str):
                            effective_query = normalize_arabic(query_str)

                        if not effective_query.strip():
                            actual_row_ids: list[int] = []
                        else:
                            escaped_query = effective_query.replace('"', '""')
                            quoted_phrase = f'"{escaped_query}"'
                            cur.execute(f"SELECT rowid FROM {tbl} WHERE {tbl} MATCH ? ORDER BY rowid;", [quoted_phrase])
                            actual_row_ids = [r[0] for r in cur.fetchall()]

                        if actual_row_ids != expected_row_ids:
                            raise GateCheckError(
                                4,
                                f"{root_label}/{db_fn}",
                                f"FTS query {query_str!r} on {tbl} returned {actual_row_ids}, expected {expected_row_ids}",
                            )
                finally:
                    conn.close()


def _run_fts_queries_v010(ctx: GateContext, root_label: str, db_fn: str, db_path: Path, pack_id: str) -> None:
    queries: list[tuple[str, str, list[int]]] = []
    if pack_id == "quran-core":
        queries = [
            ("ayahs_fts", "رَبِّ العالمين", [2]),
            ("ayahs_fts", "رب العالمين", [2]),
            ("ayahs_fts", "zzzzfixturemissing", []),
        ]
    elif pack_id == "quran-tafsir-ar-jalalayn":
        queries = [
            ("tafsir_fts", "رَبِّ العالمين", [2]),
            ("tafsir_fts", "رب العالمين", [2]),
            ("tafsir_fts", "zzzzfixturemissing", []),
        ]
    elif pack_id == "quran-tr-en-pickthall":
        queries = [
            ("translations_fts", "Lord of the Worlds", [2]),
            ("translations_fts", "zzzzfixturemissing", []),
        ]

    if not queries:
        return

    conn = _open_ro_db(db_path)
    try:
        cur = conn.cursor()
        for tbl, query_str, expected_row_ids in queries:
            if tbl not in _PACK_FTS_MAP[pack_id]:
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"Non-allowlisted FTS query table {tbl}")

            effective_query = query_str
            if tbl in _ARABIC_FTS_TABLES and _ARABIC_CHAR_RE.search(query_str):
                effective_query = normalize_arabic(query_str)

            if not effective_query.strip():
                actual_row_ids: list[int] = []
            else:
                escaped_query = effective_query.replace('"', '""')
                quoted_phrase = f'"{escaped_query}"'
                cur.execute(f"SELECT rowid FROM {tbl} WHERE {tbl} MATCH ? AND rowid = 2 ORDER BY rowid;", [quoted_phrase])
                actual_row_ids = [r[0] for r in cur.fetchall()]

            if actual_row_ids != expected_row_ids:
                raise GateCheckError(
                    4,
                    f"{root_label}/{db_fn}",
                    f"FTS query {query_str!r} on {tbl} returned {actual_row_ids}, expected {expected_row_ids}",
                )
    finally:
        conn.close()


def _stage_11_tafsir_v010(ctx: GateContext) -> None:
    coords = load_tanzil_coordinates(ctx.sources_root / "raw-inputs" / "tanzil" / "quran-data-Tanzil.xml")
    jalalayn_text = load_tanzil_text(ctx.sources_root / "raw-inputs" / "tanzil" / "ar.jalalayn.xml", coords, locked_path="tanzil/ar.jalalayn.xml")
    ayah_keys = [item[0] for item in sorted(coords.ayah_ids.items(), key=lambda x: x[1])]
    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        if "quran-tafsir-ar-jalalayn" not in [p["id"] for p in cat_data["packs"]]:
            continue

        db_fn = "quran-tafsir-ar-jalalayn.db"
        db_path = root_path / db_fn

        conn = _open_ro_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, group_key, start_ayah_id, end_ayah_id, text, text_format, text_search FROM tafsir_entries ORDER BY start_ayah_id;")
            entries = cur.fetchall()
            if len(entries) != 6236:
                raise GateCheckError(9, f"{root_label}/{db_fn}", f"tafsir_entries count mismatch: expected 6236, got {len(entries)}")

            prev_end = 0
            for idx, r in enumerate(entries):
                e_id, group_key, s_ayah, e_ayah, text, fmt, search_t = r
                vk = ayah_keys[idx]
                exp_aid = idx + 1
                group_key_str = group_key.decode("utf-8") if isinstance(group_key, bytes) else group_key
                fmt_str = fmt.decode("utf-8") if isinstance(fmt, bytes) else fmt
                text_str = text.decode("utf-8") if isinstance(text, bytes) else text
                search_str = search_t.decode("utf-8") if isinstance(search_t, bytes) else search_t

                if e_id != exp_aid or s_ayah != exp_aid or e_ayah != exp_aid:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} interval mismatch: expected {exp_aid}..{exp_aid}, got {s_ayah}..{e_ayah}")
                if group_key_str != vk:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} group_key mismatch: expected {vk}, got {group_key_str}")
                if s_ayah <= prev_end:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir interval overlap at entry {e_id}")
                prev_end = e_ayah

                if fmt_str != "plain":
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} format mismatch: expected plain, got {fmt_str}")
                if text_str != jalalayn_text[vk]:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} text mismatch against source")
                if search_str != project_tafsir_text(text_str, text_format="plain", language="ar"):
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} search projection mismatch")

            cur.execute("SELECT ayah_id, verse_key, entry_id FROM tafsir_ayah_map ORDER BY ayah_id;")
            map_rows = cur.fetchall()
            if len(map_rows) != 6236:
                raise GateCheckError(9, f"{root_label}/{db_fn}", f"tafsir_ayah_map count mismatch: expected 6236, got {len(map_rows)}")

            for idx, (aid, vk_b, entry_id) in enumerate(map_rows):
                exp_aid = idx + 1
                vk_str = vk_b.decode("utf-8") if isinstance(vk_b, bytes) else vk_b
                exp_vk = ayah_keys[idx]
                if aid != exp_aid:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"tafsir_ayah_map ayah_id mismatch at {idx}: expected {exp_aid}, got {aid}")
                if vk_str != exp_vk:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"tafsir_ayah_map verse_key mismatch at {idx}: expected {exp_vk}, got {vk_str}")
                if entry_id != exp_aid:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"tafsir_ayah_map entry_id mismatch at {idx}: expected {exp_aid}, got {entry_id}")
        finally:
            conn.close()


def stage_11_tafsir(ctx: GateContext) -> None:
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        return
    if ctx.profile == "v0.1.0":
        _stage_11_tafsir_v010(ctx)
        return
    raw_root = ctx.sources_root / "raw-inputs"
    locked_tafsir = parse_strict_json((raw_root / "qul" / "tafsir-ar-fixture.json").read_text(encoding="utf-8"))

    for root_label, root_path in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        cat_data = ctx.manifest_data_by_root[root_label][0]
        if "quran-tafsir-ar-fixture" not in [p["id"] for p in cat_data["packs"]]:
            continue

        db_fn = "quran-tafsir-ar-fixture.db"
        db_path = root_path / db_fn

        conn = _open_ro_db(db_path)
        try:
            cur = conn.cursor()
            cur.execute("SELECT id, group_key, start_ayah_id, end_ayah_id, text, text_format, text_search FROM tafsir_entries ORDER BY start_ayah_id;")
            entries = cur.fetchall()

            if len(entries) != len(locked_tafsir):
                raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entries count mismatch against locked input: {len(entries)} vs {len(locked_tafsir)}")

            prev_end = 0
            entry_dict: dict[int, tuple[int, int]] = {}

            for idx, (e_id, group_key, s_ayah, e_ayah, text, fmt, search_t) in enumerate(entries):
                # Contract: entry id = start_ayah_id (not sequential counter)
                if e_id != s_ayah:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry id must equal start_ayah_id, got id={e_id} vs start_ayah_id={s_ayah}")

                # Derive exact canonical group_key and start/end ayah IDs from locked group (fixture known surah 1 with 1:A -> A)
                locked_item = locked_tafsir[idx]
                v_keys = locked_item.get("verse_keys") if isinstance(locked_item, dict) else None
                if not v_keys or not isinstance(v_keys, list):
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} locked input missing verse_keys")

                surah_ids: list[int] = []
                verse_nums: list[int] = []
                for vk in v_keys:
                    if not isinstance(vk, str):
                        raise GateCheckError(9, f"{root_label}/{db_fn}", f"Invalid verse_key in locked input: {vk!r}")
                    m_vk = re.fullmatch(r"^([1-9][0-9]*):([1-9][0-9]*)$", vk)
                    if not m_vk:
                        raise GateCheckError(9, f"{root_label}/{db_fn}", f"Invalid canonical verse_key grammar in locked input: {vk!r}")
                    surah_ids.append(int(m_vk.group(1)))
                    verse_nums.append(int(m_vk.group(2)))

                if len(set(surah_ids)) != 1:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Cross-surah Tafsir group not allowed in entry {e_id}")

                surah_id = surah_ids[0]
                start_v = verse_nums[0]
                end_v = verse_nums[-1]
                if verse_nums != list(range(start_v, start_v + len(verse_nums))):
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir group verse_keys must be consecutive and ordered in entry {e_id}")

                exp_group_key = f"{surah_id}:{start_v}" if len(v_keys) == 1 else f"{surah_id}:{start_v}-{end_v}"
                exp_start_ayah = start_v
                exp_end_ayah = end_v

                group_key_str = group_key.decode("utf-8") if isinstance(group_key, bytes) else group_key
                if group_key_str != exp_group_key:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} group_key mismatch: expected {exp_group_key!r}, got {group_key_str!r}")
                if s_ayah != exp_start_ayah:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} start_ayah_id mismatch: expected {exp_start_ayah}, got {s_ayah}")
                if e_ayah != exp_end_ayah:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} end_ayah_id mismatch: expected {exp_end_ayah}, got {e_ayah}")

                if s_ayah > e_ayah:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} start_ayah_id > end_ayah_id ({s_ayah} > {e_ayah})")
                if s_ayah <= prev_end:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir interval overlap at entry {e_id}")
                if prev_end > 0 and s_ayah > prev_end + 1:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir interval gap before entry {e_id}")
                prev_end = e_ayah

                if text != locked_item["text"] or fmt != locked_item["format"]:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir entry {e_id} text/format mismatch against locked input")
                entry_dict[e_id] = (s_ayah, e_ayah)

            cur.execute("SELECT ayah_id, verse_key, entry_id FROM tafsir_ayah_map ORDER BY ayah_id;")
            map_rows = cur.fetchall()
            expected_map_count = sum(len(item["verse_keys"]) for item in locked_tafsir)
            if len(map_rows) != expected_map_count:
                raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir map must cover {expected_map_count} ayahs, got {len(map_rows)}")

            # Build set of locked verse_keys and their entry start_ayah_id
            locked_vk_to_entry_id: dict[str, int] = {}
            for locked_item in locked_tafsir:
                first_vk = locked_item["verse_keys"][0]
                entry_start = int(first_vk.split(":")[1])
                for vk in locked_item["verse_keys"]:
                    locked_vk_to_entry_id[vk] = entry_start

            for ayah_id, verse_key, entry_id in map_rows:
                verse_key_str = verse_key.decode("utf-8") if isinstance(verse_key, bytes) else verse_key
                if entry_id not in entry_dict:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir map points to non-existent entry {entry_id}")
                s_ayah, e_ayah = entry_dict[entry_id]
                if not (s_ayah <= ayah_id <= e_ayah):
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir map pointer mismatch: ayah {ayah_id} outside entry {entry_id} interval [{s_ayah}, {e_ayah}]")
                if verse_key_str not in locked_vk_to_entry_id:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir map verse_key {verse_key_str!r} not found in locked input")
                if locked_vk_to_entry_id[verse_key_str] != entry_id:
                    raise GateCheckError(9, f"{root_label}/{db_fn}", f"Tafsir map verse_key {verse_key_str!r} points to wrong entry {entry_id}")
        finally:
            conn.close()


def stage_12_cross_root_equality(ctx: GateContext) -> None:
    cand_files = ctx.declared_files_by_root["candidate"]

    cand_inodes: dict[tuple[int, int], str] = {}
    for fn in sorted(cand_files):
        c_file = ctx.candidate / fn
        st = c_file.stat()
        cand_inodes[(st.st_dev, st.st_ino)] = fn

    for fn in sorted(cand_files):
        c_file = ctx.candidate / fn
        r_file = ctx.rebuild / fn

        if not r_file.is_file() or _is_symlink_or_junction(r_file):
            raise GateCheckError(6, fn, f"Rebuild asset {fn} missing or link")

        r_st = r_file.stat()
        r_key = (r_st.st_dev, r_st.st_ino)
        if r_key in cand_inodes:
            raise GateCheckError(6, fn, f"Cross-root file inode alias detected for asset {fn} (matches candidate {cand_inodes[r_key]})")
        c_size = c_file.stat().st_size
        r_size = r_file.stat().st_size
        if c_size != r_size:
            raise GateCheckError(6, fn, f"Size difference in {fn} between candidate and rebuild: {c_size} vs {r_size}")

        with open(c_file, "rb") as f1, open(r_file, "rb") as f2:
            while True:
                b1 = f1.read(65536)
                b2 = f2.read(65536)
                if b1 != b2:
                    raise GateCheckError(6, fn, f"Distributable byte difference in {fn} between candidate and rebuild")
                if not b1:
                    break


_STAGE_HANDLERS = [
    stage_0_preflight,
    stage_1_root_identity,
    stage_2_manifest_text,
    stage_3_manifest_schemas_inventory,
    stage_4_source_lock_clearance,
    stage_5_sqlite_schema_integrity,
    stage_6_sqlite_metadata,
    stage_7_record_keys,
    stage_8_text_equivalence,
    stage_9_coverage,
    stage_10_fts,
    stage_11_tafsir,
    stage_12_cross_root_equality,
]


def run_checks(
    *,
    profile: str,
    candidate: Path,
    rebuild: Path,
    sources_root: Path,
    expectations: dict[str, Any] | None = None,
    local_review: bool = False,
) -> None:
    if profile not in ("fixture", "v0.1.0", "hadith-pilot", "hadith-local"):
        raise ValueError(f"Unsupported profile: {profile!r}")
    if local_review and profile != "v0.1.0":
        raise ValueError("local_review is only permitted for profile 'v0.1.0'")
    if profile == "fixture" and expectations is None:
        raise ValueError("expectations is required for fixture profile")
    if profile in ("v0.1.0", "hadith-pilot", "hadith-local") and expectations is not None:
        raise ValueError(f"expectations is not permitted for {profile} profile")

    repo_root = Path(__file__).resolve().parents[2]
    specs_dir = repo_root / "specs"

    ctx = GateContext(
        profile=profile,
        candidate=candidate,
        rebuild=rebuild,
        sources_root=sources_root,
        expectations=expectations or {},
        specs_dir=specs_dir,
        local_review=local_review,
    )

    for stage_idx, (stage_name, check_num) in enumerate(_STAGES):
        handler = _STAGE_HANDLERS[stage_idx]
        try:
            handler(ctx)
        except GateCheckError as exc:
            _fail(stage_idx, exc.check_num, exc.asset, exc.reason)

    print("Check #1: Passed (inventory, keys, coverage)")
    print("Check #2: Passed (text encoding, projections, split evidence)")
    print("Check #3: Passed (SQLite integrity, foreign keys, schema)")
    print("Check #4: Passed (FTS integrity and query verification)")
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        print("Check #5: Passed (flat DB asset inventory and sizes)")
    else:
        print("Check #5: Passed (manifest schemas, asset verification, probes)")
    print("Check #6: Passed (root identity and cross-root byte equality)")
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        expected_pids = _HADITH_PILOT_PACK_IDS if ctx.profile == "hadith-pilot" else _HADITH_ALL_PACK_IDS
        print(f"Clearance pending (local verification only): {', '.join(expected_pids)}")
    elif ctx.profile == "v0.1.0" and ctx.local_review:
        pending_packs = sorted([
            pid for pid, rec in ctx.license_records.items()
            if rec.get("status") == "pending"
        ])
        if pending_packs:
            print(f"Clearance pending (local verification only): {', '.join(pending_packs)}")
        else:
            print("Check #7: Passed (source lock, licenses, metadata reconciliation)")
    else:
        print("Check #7: Passed (source lock, licenses, metadata reconciliation)")
    print("Check 8: Not applicable (no production QAC/QUL pair).")
    if ctx.profile in ("hadith-pilot", "hadith-local"):
        print("Check 9: Not applicable (no Tafsir packs in Hadith profile).")
    else:
        print("Check #9: Passed (Tafsir intervals and map pointers)")
    print("All quality gate checks passed successfully.")


def validate_expectations_oracle(exp_data: Any, exp_path: Path) -> dict[str, Any]:
    if not isinstance(exp_data, dict):
        raise ValueError("Root must be a JSON object")
    if set(exp_data.keys()) != {"packs", "audioSlugs"}:
        raise ValueError("Root object must contain exactly 'packs' and 'audioSlugs'")
    if exp_data["audioSlugs"] != ["fixture"]:
        raise ValueError("'audioSlugs' must be exactly ['fixture']")

    packs_obj = exp_data["packs"]
    if not isinstance(packs_obj, dict):
        raise ValueError("'packs' must be a JSON object")

    expected_pids = {
        "quran-core", "quran-scripts-extended", "quran-tr-en-fixture",
        "quran-tafsir-ar-fixture", "quran-morphology", "quran-wbw-en",
        "hadith-fixture", "audio-adhan-core",
    }
    if set(packs_obj.keys()) != expected_pids:
        raise ValueError(f"'packs' keys mismatch: expected {sorted(expected_pids)}, got {sorted(packs_obj.keys())}")

    if packs_obj["audio-adhan-core"] != {}:
        raise ValueError("'audio-adhan-core' in packs must be an empty object")

    for pid in expected_pids - {"audio-adhan-core"}:
        p_oracle = packs_obj[pid]
        if not isinstance(p_oracle, dict):
            raise ValueError(f"Oracle for pack {pid} must be an object")
        if set(p_oracle.keys()) != {"keys", "coverage", "fts"}:
            raise ValueError(f"Pack {pid} oracle must contain exactly 'keys', 'coverage', 'fts'")

        keys_list = p_oracle["keys"]
        if not isinstance(keys_list, list) or len(keys_list) == 0:
            raise ValueError(f"Pack {pid} 'keys' must be a non-empty array")
        if pid == "hadith-fixture":
            for k in keys_list:
                if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
                    raise ValueError(f"Pack {pid} keys must be positive integers")
        else:
            # Verse packs: "S:A" (two positive decimal components, no leading zeros)
            # Word packs (morphology/wbw): "S:A:W" (three components)
            _word_packs = {"quran-morphology", "quran-wbw-en"}
            _comp_re = re.compile(r"^([1-9][0-9]*):([1-9][0-9]*)(?::([1-9][0-9]*))?$")
            is_word_pack = pid in _word_packs
            for k in keys_list:
                if not isinstance(k, str) or not k:
                    raise ValueError(f"Pack {pid} keys must be non-empty strings")
                m = _comp_re.fullmatch(k)
                if not m:
                    raise ValueError(f"Pack {pid} key {k!r} has invalid format (no leading zeros, positive integers required)")
                if is_word_pack and m.group(3) is None:
                    raise ValueError(f"Pack {pid} (word pack) key {k!r} must have three components S:A:W")
                if not is_word_pack and m.group(3) is not None:
                    raise ValueError(f"Pack {pid} (verse pack) key {k!r} must have exactly two components S:A")
        # Verify keys are sorted ascending (numeric for verse/word keys, natural for strings)
        if pid == "hadith-fixture":
            if keys_list != sorted(keys_list) or len(keys_list) != len(set(keys_list)):
                raise ValueError(f"Pack {pid} keys must be sorted ascending without duplicates")
        elif pid not in {"audio-adhan-core"}:
            def _numeric_key(k: str) -> tuple:
                return tuple(int(x) for x in k.split(":"))
            if keys_list != sorted(keys_list, key=_numeric_key):
                raise ValueError(f"Pack {pid} keys must be sorted in ascending numeric order without duplicates")
            if len(keys_list) != len(set(keys_list)):
                raise ValueError(f"Pack {pid} keys must be unique")

        cov = p_oracle["coverage"]
        if not isinstance(cov, dict) or len(cov) == 0:
            raise ValueError(f"Pack {pid} 'coverage' must be a non-empty object")
        for m, cnt in cov.items():
            if not isinstance(cnt, int) or isinstance(cnt, bool) or cnt < 0:
                raise ValueError(f"Coverage metric {m} in pack {pid} must be a non-negative integer")
        if "expected_total" not in cov:
            raise ValueError(f"Pack {pid} coverage must include 'expected_total'")

        fts_list = p_oracle["fts"]
        if not isinstance(fts_list, list):
            raise ValueError(f"Pack {pid} 'fts' must be an array")
        declared_fts = _PACK_FTS_MAP[pid]
        if not declared_fts:
            if len(fts_list) != 0:
                raise ValueError(f"Pack {pid} has no FTS tables; 'fts' array must be empty")
        else:
            if len(fts_list) == 0:
                raise ValueError(f"Pack {pid} declares FTS; 'fts' array must not be empty")
            has_pos = False
            has_neg = False
            for q_spec in fts_list:
                if not isinstance(q_spec, dict) or set(q_spec.keys()) != {"table", "query", "rowIds"}:
                    raise ValueError(f"Pack {pid} FTS query must have exactly 'table', 'query', 'rowIds'")
                if q_spec["table"] not in declared_fts:
                    raise ValueError(f"Pack {pid} FTS query table {q_spec['table']!r} not in declared FTS tables {declared_fts}")
                if not isinstance(q_spec["query"], str) or not q_spec["query"]:
                    raise ValueError(f"Pack {pid} FTS query must be non-empty string")
                rids = q_spec["rowIds"]
                if not isinstance(rids, list):
                    raise ValueError(f"Pack {pid} FTS rowIds must be an array")
                for rid in rids:
                    if not isinstance(rid, int) or isinstance(rid, bool):
                        raise ValueError(f"Pack {pid} FTS rowIds must be integers")
                if rids != sorted(list(set(rids))):
                    raise ValueError(f"Pack {pid} FTS rowIds must be sorted without duplicates")
                if len(rids) > 0:
                    has_pos = True
                else:
                    has_neg = True
            if not has_pos:
                raise ValueError(f"Pack {pid} FTS queries must include at least one positive control")
            if not has_neg:
                raise ValueError(f"Pack {pid} FTS queries must include at least one negative control")

    return exp_data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic quality gate validator", add_help=False)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--rebuild", required=True)
    parser.add_argument("--sources-root", required=True)
    parser.add_argument("--expectations", required=False, default=None)

    try:
        args, unknown = parser.parse_known_args(argv)
        if unknown:
            sys.stderr.write(f"CLI error: unrecognized arguments {unknown}\n")
            return 2
    except Exception as exc:
        sys.stderr.write(f"CLI error: {exc}\n")
        return 2

    profile = args.profile
    if profile not in ("fixture", "v0.1.0", "hadith-pilot", "hadith-local"):
        sys.stderr.write(
            f"CLI error: unsupported profile {profile!r}; must be 'fixture', 'v0.1.0', 'hadith-pilot', or 'hadith-local'\n"
        )
        return 2

    if profile == "fixture":
        if args.expectations is None:
            sys.stderr.write("CLI error: --expectations is required for fixture profile\n")
            return 2

        exp_path = Path(args.expectations)
        if not exp_path.is_file() or _is_symlink_or_junction(exp_path):
            sys.stderr.write(f"CLI error: expectations file not found or invalid: {exp_path}\n")
            return 2

        try:
            exp_text = exp_path.read_text(encoding="utf-8")
            parsed_exp = parse_strict_json(exp_text)
            expectations = validate_expectations_oracle(parsed_exp, exp_path)
        except Exception as exc:
            sys.stderr.write(f"Malformed expectations file {exp_path}: {exc}\n")
            return 2
    else:
        if args.expectations is not None:
            sys.stderr.write(f"CLI error: --expectations is rejected for {profile} profile\n")
            return 2
        expectations = None

    candidate = Path(args.candidate)
    rebuild = Path(args.rebuild)
    sources_root = Path(args.sources_root)

    if not candidate.exists() or not candidate.is_dir():
        sys.stderr.write(f"CLI error: candidate directory does not exist: {candidate}\n")
        return 2
    if not rebuild.exists() or not rebuild.is_dir():
        sys.stderr.write(f"CLI error: rebuild directory does not exist: {rebuild}\n")
        return 2
    if not sources_root.exists() or not sources_root.is_dir():
        sys.stderr.write(f"CLI error: sources-root directory does not exist: {sources_root}\n")
        return 2

    try:
        run_checks(
            profile=profile,
            candidate=candidate,
            rebuild=rebuild,
            sources_root=sources_root,
            expectations=expectations,
            local_review=False,
        )
        return 0
    except GateCheckError as exc:
        sys.stderr.write(f"Check #{exc.check_num} failed on {exc.asset}: {exc.reason}\n")
        sys.stderr.write("Check 8: Not applicable (no production QAC/QUL pair).\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
