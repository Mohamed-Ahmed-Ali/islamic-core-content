"""Deterministic quality gate validator for Phase 3 local QUL integration."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

from pipelines.common.lock_inputs import (
    LockEntry,
    parse_and_validate_lock,
    parse_and_validate_registry,
    parse_strict_json,
    verify_lock,
    _is_symlink_or_junction,
)
from pipelines.common.pack_sources import load_sqlite_source_metadata
from pipelines.common.stage_qul_inputs import audit_all_wbw_inputs
from pipelines.common.tanzil_coordinates import load_tanzil_coordinates
from pipelines.validators.word_alignment import validate_word_alignment


class GateCheckError(Exception):
    def __init__(self, check_num: int, asset: str, reason: str) -> None:
        super().__init__(f"Check #{check_num} failed on {asset}: {reason}")
        self.check_num = check_num
        self.asset = asset
        self.reason = reason


PHASE3_DB_PACKS: tuple[str, ...] = (
    "quran-core",
    "quran-morphology",
    "quran-wbw-en",
    "quran-tafsir-ar-ibn-kathir",
    "quran-tafsir-ar-tabari",
    "quran-tafsir-ar-qurtubi",
    "quran-tafsir-ar-wasit",
    "quran-tafsir-ar-razi",
    "quran-tafsir-en-ibn-kathir",
    "quran-tr-en-sahih",
    "quran-mushaf-madani-v2",
    "quran-mushaf-indopak-15",
)

PHASE3_RECITATION_PACKS: tuple[str, ...] = (
    "audio-recitation-abdul-basit-mujawwad",
    "audio-recitation-abdul-basit-murattal",
    "audio-recitation-al-husary-muallim",
    "audio-recitation-al-husary-mujawwad",
    "audio-recitation-al-husary-murattal",
    "audio-recitation-al-minshawi-murattal",
    "audio-recitation-al-tablawi-murattal",
)

PHASE3_ALL_PACKS: tuple[str, ...] = PHASE3_DB_PACKS + PHASE3_RECITATION_PACKS

PHASE3_EXPECTED_ARTIFACTS: frozenset[str] = frozenset(
    [f"{pid}.db" for pid in PHASE3_DB_PACKS] + [f"{pid}.json" for pid in PHASE3_RECITATION_PACKS]
)

PHASE3_QUL_LOCKED_PATHS: tuple[str, ...] = (
    "qul/wbw/english-wbw-translation.json.zip",
    "qul/wbw/english-wbw-translation.db.zip",
    "qul/wbw/english-wbw-transliteration.json.zip",
    "qul/wbw/english-wbw-transliteration.db.zip",
    "qul/tafsir/ar-tafsir-ibn-kathir.json.zip",
    "qul/tafsir/ar-tafsir-al-tabari.json.zip",
    "qul/tafsir/ar-tafseer-al-qurtubi.json.zip",
    "qul/tafsir/ar-tafsir-al-wasit.json.zip",
    "qul/tafsir/tafsir-al-razi.json.zip",
    "qul/tafsir/en-tafisr-ibn-kathir.json.zip",
    "qul/translation/en-sahih-international-with-footnote-tags.json.zip",
    "qul/mushaf/qpc-v2-15-lines.db.zip",
    "qul/mushaf/qudratullah-indopak-15-lines.db.zip",
    "qul/mushaf/qpc-hafs-tajweed.json.zip",
    "qul/recitation/ayah-recitation-abdul-basit-abdul-samad-mujawwad-hafs-949.json.zip",
    "qul/recitation/ayah-recitation-abdul-basit-abdul-samad-murattal-hafs-950.json.zip",
    "qul/recitation/ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-955.json.zip",
    "qul/recitation/ayah-recitation-mahmoud-khalil-al-husary-mujawwad-hafs-956.json.zip",
    "qul/recitation/ayah-recitation-mahmoud-khalil-al-husary-murattal-hafs-957-Muallim.json.zip",
    "qul/recitation/ayah-recitation-muhammad-siddiq-al-minshawi-murattal-hafs-959.json.zip",
    "qul/recitation/ayah-recitation-mohamed-al-tablawi-recitation-murattal-hafs-73.json.zip",
)

EXPECTED_TAFSIR_GROUPS: dict[str, int] = {
    "quran-tafsir-ar-ibn-kathir": 1911,
    "quran-tafsir-ar-tabari": 3636,
    "quran-tafsir-ar-qurtubi": 3261,
    "quran-tafsir-ar-wasit": 6108,
    "quran-tafsir-ar-razi": 2969,
    "quran-tafsir-en-ibn-kathir": 1902,
}


@dataclass(frozen=True)
class RecitationManifestInterface:
    pack_ids: tuple[str, ...]
    render_manifest: Callable[[Path, str], bytes]


def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database file not found: {db_path}")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def verify_phase3_root_identity(candidate: Path, rebuild: Path) -> None:
    """Check #6: Ensure roots do not share hardlinks or duplicate inodes across or within roots."""
    cand_inodes: dict[tuple[int, int], str] = {}
    if candidate.exists() and candidate.is_dir():
        for entry in candidate.iterdir():
            if entry.is_file() and not _is_symlink_or_junction(entry):
                st = entry.stat()
                key = (st.st_dev, st.st_ino)
                if key in cand_inodes:
                    raise GateCheckError(
                        6,
                        f"candidate/{entry.name}",
                        f"Duplicate inode or hardlink within candidate: {entry.name} shares inode with {cand_inodes[key]}",
                    )
                cand_inodes[key] = entry.name

    reb_inodes: dict[tuple[int, int], str] = {}
    if rebuild.exists() and rebuild.is_dir():
        for entry in rebuild.iterdir():
            if entry.is_file() and not _is_symlink_or_junction(entry):
                st = entry.stat()
                key = (st.st_dev, st.st_ino)
                if key in reb_inodes:
                    raise GateCheckError(
                        6,
                        f"rebuild/{entry.name}",
                        f"Duplicate inode or hardlink within rebuild: {entry.name} shares inode with {reb_inodes[key]}",
                    )
                if key in cand_inodes:
                    raise GateCheckError(
                        6,
                        f"rebuild/{entry.name}",
                        f"Duplicate inode or hardlink across candidate and rebuild: {entry.name} matches candidate/{cand_inodes[key]}",
                    )
                reb_inodes[key] = entry.name


def scan_phase3_inventory(root_label: str, root_path: Path) -> set[str]:
    """Check #5: Verify flat root containing exactly the 19 declared files with valid size."""
    actual_files: set[str] = set()
    try:
        with os.scandir(root_path) as it:
            for entry in it:
                name = entry.name
                if name.startswith("."):
                    raise GateCheckError(5, f"{root_label}/{name}", "Dotfile not permitted in root")
                if _is_symlink_or_junction(entry.path):
                    raise GateCheckError(5, f"{root_label}/{name}", "Symlink or junction not permitted in root")
                if entry.is_dir(follow_symlinks=False):
                    raise GateCheckError(5, f"{root_label}/{name}", "Subdirectory not permitted in flat root")
                for suffix in ("-wal", "-shm", "-journal"):
                    if name.endswith(suffix):
                        raise GateCheckError(5, f"{root_label}/{name}", f"Sidecar file {suffix} not permitted")
                if not entry.is_file(follow_symlinks=False):
                    raise GateCheckError(5, f"{root_label}/{name}", "Non-regular file not permitted in root")
                actual_files.add(name)
    except GateCheckError:
        raise
    except Exception as exc:
        raise GateCheckError(5, root_label, f"Error scanning directory: {exc}") from exc

    expected = set(PHASE3_EXPECTED_ARTIFACTS)
    if actual_files != expected:
        missing = sorted(expected - actual_files)
        if missing:
            raise GateCheckError(5, f"{root_label}/{missing[0]}", f"Expected asset missing: {missing[0]}")
        extra = sorted(actual_files - expected)
        if extra:
            raise GateCheckError(5, f"{root_label}/{extra[0]}", f"Unexpected asset in root: {extra[0]}")

    for fn in sorted(actual_files):
        p = root_path / fn
        st = p.stat()
        if not (0 < st.st_size < 2 * 1024 * 1024 * 1024):
            raise GateCheckError(5, f"{root_label}/{fn}", f"File size {st.st_size} not strictly between 0 and 2 GiB")

    return actual_files


def _get_expected_primary_path(pid: str) -> str:
    if pid == "quran-core":
        return "tanzil/quran-uthmani.xml"
    if pid == "quran-morphology":
        return "corpus/quranic-corpus-morphology-0.4.zip"
    if pid == "quran-wbw-en":
        return "qul/wbw/english-wbw-translation.json.zip"
    if pid == "quran-tr-en-sahih":
        return "qul/translation/en-sahih-international-with-footnote-tags.json.zip"
    if pid == "quran-mushaf-madani-v2":
        return "qul/mushaf/qpc-v2-15-lines.db.zip"
    if pid == "quran-mushaf-indopak-15":
        return "qul/mushaf/qudratullah-indopak-15-lines.db.zip"
    if pid.startswith("quran-tafsir-"):
        tafsir_map = {
            "quran-tafsir-ar-ibn-kathir": "qul/tafsir/ar-tafsir-ibn-kathir.json.zip",
            "quran-tafsir-ar-tabari": "qul/tafsir/ar-tafsir-al-tabari.json.zip",
            "quran-tafsir-ar-qurtubi": "qul/tafsir/ar-tafseer-al-qurtubi.json.zip",
            "quran-tafsir-ar-wasit": "qul/tafsir/ar-tafsir-al-wasit.json.zip",
            "quran-tafsir-ar-razi": "qul/tafsir/tafsir-al-razi.json.zip",
            "quran-tafsir-en-ibn-kathir": "qul/tafsir/en-tafisr-ibn-kathir.json.zip",
        }
        if pid in tafsir_map:
            return tafsir_map[pid]
        raise ValueError(f"Unknown tafsir pack {pid}")
    if pid.startswith("audio-recitation-"):
        from pipelines.builders.build_recitation_manifests import RECITATION_PACKS
        if pid in RECITATION_PACKS:
            return RECITATION_PACKS[pid]["archive_path"]
        raise ValueError(f"Unknown recitation pack {pid}")
    raise ValueError(f"Unknown pack ID {pid}")


def verify_phase3_source_lock_and_clearance(sources_root: Path) -> None:
    """Check #7: Verify canonical source lock, WBW inputs parity, and unreviewed pending licenses."""
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
        reg_data = parse_and_validate_registry(reg_path)
    except Exception as exc:
        raise GateCheckError(7, "sources/registry.json", f"Failed parsing registry: {exc}") from exc

    qul_in_reg = {p for p in reg_data if p.startswith("qul/")}
    if qul_in_reg != set(PHASE3_QUL_LOCKED_PATHS):
        raise GateCheckError(
            7,
            "sources/registry.json",
            f"Registry QUL paths mismatch: expected {sorted(PHASE3_QUL_LOCKED_PATHS)}, got {sorted(qul_in_reg)}",
        )

    try:
        lock_entries = parse_and_validate_lock(lock_path)
    except Exception as exc:
        raise GateCheckError(7, "sources/manifest.lock.json", f"Failed parsing lock file: {exc}") from exc

    lock_by_path: dict[str, LockEntry] = {entry.path: entry for entry in lock_entries}
    qul_in_lock = {p for p in lock_by_path if p.startswith("qul/")}
    if qul_in_lock != set(PHASE3_QUL_LOCKED_PATHS):
        raise GateCheckError(
            7,
            "sources/manifest.lock.json",
            f"Lock QUL paths mismatch: expected {sorted(PHASE3_QUL_LOCKED_PATHS)}, got {sorted(qul_in_lock)}",
        )

    # Verify all 21 QUL locked archive versions equal sha256:<digest>
    for qul_path in PHASE3_QUL_LOCKED_PATHS:
        if qul_path not in reg_data:
            raise GateCheckError(7, "sources/registry.json", f"Expected QUL path {qul_path} missing in registry")
        reg_entry = reg_data[qul_path]
        lock_entry = lock_by_path.get(qul_path)
        if not lock_entry:
            raise GateCheckError(7, "sources/manifest.lock.json", f"Expected QUL path {qul_path} missing in lock")
        expected_ver = f"sha256:{lock_entry.sha256}"
        if lock_entry.source_version != expected_ver:
            raise GateCheckError(
                7,
                f"sources/manifest.lock.json:{qul_path}",
                f"Lock source_version {lock_entry.source_version!r} does not equal sha256:{lock_entry.sha256!r}",
            )
        if reg_entry.get("sourceVersion") != expected_ver:
            raise GateCheckError(
                7,
                f"sources/registry.json:{qul_path}",
                f"Registry sourceVersion {reg_entry.get('sourceVersion')!r} does not equal lock hash {expected_ver!r}",
            )

    # Verify source lock across all 19 packs
    for pid in PHASE3_ALL_PACKS:
        try:
            verify_lock(reg_path, raw_root, lock_path, pack_id=pid)
        except Exception as exc:
            raise GateCheckError(7, f"lock verification for {pid}", f"Lock verification failed: {exc}") from exc

    # Audit WBW SQLite vs JSON parity
    try:
        audit_all_wbw_inputs(raw_root)
    except Exception as exc:
        raise GateCheckError(7, "raw-inputs/qul/wbw", f"WBW SQLite/JSON audit failed: {exc}") from exc

    # Verify licenses
    licenses_dir = sources_dir / "licenses"
    for pid in PHASE3_ALL_PACKS:
        lic_file = licenses_dir / f"{pid}.json"
        rel_lic = f"sources/licenses/{pid}.json"
        if not lic_file.is_file() or _is_symlink_or_junction(lic_file):
            raise GateCheckError(7, rel_lic, f"License file missing or link: {rel_lic}")
        try:
            lic_data = parse_strict_json(lic_file.read_text(encoding="utf-8"))
        except Exception as exc:
            raise GateCheckError(7, rel_lic, f"Failed parsing license JSON: {exc}") from exc

        if not isinstance(lic_data, dict):
            raise GateCheckError(7, rel_lic, f"License root must be a JSON object: {rel_lic}")

        expected_primary = _get_expected_primary_path(pid)
        pack_entries = tuple(lock_by_path[p] for p in sorted(reg_data) if pid in reg_data[p]["packIds"])

        try:
            load_sqlite_source_metadata(
                sources_root,
                pid,
                pack_entries,
                primary_path=expected_primary,
                pack_version="0.3.0" if (pid.startswith("quran-") or pid.startswith("audio-")) else "0.1.0",
            )
        except Exception as exc:
            raise GateCheckError(7, rel_lic, f"License metadata validation failed: {exc}") from exc

        if lic_data.get("status") != "pending":
            raise GateCheckError(7, rel_lic, f"License status must be 'pending', got {lic_data.get('status')!r}")
        if lic_data.get("reviewedBy") is not None:
            raise GateCheckError(7, rel_lic, f"reviewedBy must be null, got {lic_data.get('reviewedBy')!r}")
        if lic_data.get("primaryPath") != expected_primary:
            raise GateCheckError(7, rel_lic, f"primaryPath must be {expected_primary!r}, got {lic_data.get('primaryPath')!r}")

        # Specific QUL license constraints (excluding unchanged base packs quran-core and quran-morphology)
        if pid not in ("quran-core", "quran-morphology"):
            if lic_data.get("licenseSpdx") != "LicenseRef-QUL-Unreviewed":
                raise GateCheckError(
                    7,
                    rel_lic,
                    f"licenseSpdx must be 'LicenseRef-QUL-Unreviewed', got {lic_data.get('licenseSpdx')!r}",
                )
            sources_list = lic_data.get("sources")
            if not isinstance(sources_list, list) or not sources_list:
                raise GateCheckError(7, rel_lic, "sources list must be a non-empty list")
            for s_entry in sources_list:
                if not isinstance(s_entry, dict):
                    raise GateCheckError(7, rel_lic, "Source entry must be a JSON object")
                if s_entry.get("path", "").startswith("qul/"):
                    if s_entry.get("permittedUses") != []:
                        raise GateCheckError(7, rel_lic, "QUL unreviewed permittedUses must be empty")
                    if not s_entry.get("notice"):
                        raise GateCheckError(7, rel_lic, "QUL source notice must not be empty")
                    if not s_entry.get("attribution"):
                        raise GateCheckError(7, rel_lic, "QUL source attribution must not be empty")
                    if not s_entry.get("rightsEvidence"):
                        raise GateCheckError(7, rel_lic, "QUL source rightsEvidence must not be empty")

    print(f"Clearance pending (local verification only): {', '.join(PHASE3_ALL_PACKS)}")


def build_phase3_reference_db(sources_root: Path, ref_dir: Path, pack_id: str) -> Path:
    """Build a single trusted reference database directly from verified sources."""
    if pack_id == "quran-core":
        from pipelines.builders.build_quran_core import build_quran_core
        build_quran_core(sources_root, ref_dir)
    elif pack_id == "quran-morphology":
        from pipelines.builders.build_quran_morphology import build_quran_morphology
        build_quran_morphology(sources_root, ref_dir)
    elif pack_id == "quran-wbw-en":
        from pipelines.builders.build_quran_wbw import build_quran_wbw
        build_quran_wbw(sources_root, ref_dir)
    elif pack_id.startswith("quran-tafsir-"):
        from pipelines.builders.build_quran_tafsir import build_quran_tafsir
        build_quran_tafsir(sources_root, ref_dir, pack_id=pack_id)
    elif pack_id == "quran-tr-en-sahih":
        from pipelines.builders.build_quran_translation import build_quran_translation
        build_quran_translation(sources_root, ref_dir, pack_id=pack_id)
    elif pack_id.startswith("quran-mushaf-"):
        from pipelines.builders.build_quran_mushaf import build_quran_mushaf
        build_quran_mushaf(sources_root, ref_dir, pack_id=pack_id)
    else:
        raise ValueError(f"Unknown DB pack ID for reference builder: {pack_id}")

    ref_db = ref_dir / f"{pack_id}.db"
    if not ref_db.is_file():
        raise FileNotFoundError(f"Reference build failed to create {ref_db}")
    return ref_db


def verify_db_content_against_reference(
    cand_db: Path,
    reb_db: Path,
    ref_db: Path,
    pack_id: str,
    db_fn: str,
) -> None:
    """Compare user tables, pack_meta (#7), coverage_report (#1), Tafsir map (#9), and text/data (#2)."""
    with closing(_open_ro_db(ref_db)) as ref_conn:
        ref_tables = [
            r[0] for r in ref_conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' AND name NOT LIKE '%_config%' "
                "ORDER BY name;"
            ).fetchall()
        ]

        # Extract all table schemas and rows from reference
        ref_table_data: dict[str, list[tuple[Any, ...]]] = {}
        for tbl in ref_tables:
            col_info = ref_conn.execute(f"PRAGMA table_info({tbl});").fetchall()
            col_names = [r[1] for r in col_info]
            pk_cols = [r[1] for r in col_info if r[5] > 0]
            order_clause = f"ORDER BY {', '.join(pk_cols)}" if pk_cols else "ORDER BY rowid"
            select_cols = ", ".join(f"typeof({c}), {c}" for c in col_names)
            query = f"SELECT {select_cols} FROM {tbl} {order_clause};"
            ref_table_data[tbl] = ref_conn.execute(query).fetchall()

    for root_label, db_path in (("candidate", cand_db), ("rebuild", reb_db)):
        with closing(_open_ro_db(db_path)) as conn:
            cur_tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' AND name NOT LIKE '%_config%' "
                    "ORDER BY name;"
                ).fetchall()
            ]
            if cur_tables != ref_tables:
                raise GateCheckError(
                    3,
                    f"{root_label}/{db_fn}",
                    f"User tables mismatch: got {cur_tables}, expected {ref_tables}",
                )

            for tbl in ref_tables:
                col_info = conn.execute(f"PRAGMA table_info({tbl});").fetchall()
                col_names = [r[1] for r in col_info]
                pk_cols = [r[1] for r in col_info if r[5] > 0]
                order_clause = f"ORDER BY {', '.join(pk_cols)}" if pk_cols else "ORDER BY rowid"
                select_cols = ", ".join(f"typeof({c}), {c}" for c in col_names)
                query = f"SELECT {select_cols} FROM {tbl} {order_clause};"
                try:
                    rows = conn.execute(query).fetchall()
                except (sqlite3.Error, UnicodeError) as exc:
                    raise GateCheckError(
                        2,
                        f"{root_label}/{db_fn}",
                        f"Corrupt or malformed database text/content in {tbl}: {exc}",
                    ) from exc
                exp_rows = ref_table_data[tbl]

                if rows != exp_rows:
                    if tbl == "pack_meta":
                        raise GateCheckError(
                            7,
                            f"{root_label}/{db_fn}",
                            f"Metadata mismatch in pack_meta: got {rows}, expected {exp_rows}",
                        )
                    elif tbl == "coverage_report":
                        raise GateCheckError(
                            1,
                            f"{root_label}/{db_fn}",
                            f"Coverage report mismatch in {tbl}: row count got {len(rows)}, expected {len(exp_rows)}",
                        )
                    elif tbl == "tafsir_ayah_map":
                        raise GateCheckError(
                            9,
                            f"{root_label}/{db_fn}",
                            f"Tafsir mapping or pointer mismatch in table {tbl}",
                        )
                    elif tbl == "tafsir_entries":
                        if len(rows) != len(exp_rows):
                            raise GateCheckError(
                                9,
                                f"{root_label}/{db_fn}",
                                f"Tafsir entries interval mismatch in {tbl}: row count got {len(rows)}, expected {len(exp_rows)}",
                            )
                        for idx, (act_r, exp_r) in enumerate(zip(rows, exp_rows)):
                            if act_r != exp_r:
                                if act_r[:8] != exp_r[:8]:
                                    raise GateCheckError(
                                        9,
                                        f"{root_label}/{db_fn}",
                                        f"Tafsir entries interval mismatch in {tbl} at row {idx + 1}",
                                    )
                                else:
                                    raise GateCheckError(
                                        2,
                                        f"{root_label}/{db_fn}",
                                        f"Data content mismatch in table {tbl} at row {idx + 1}: got {act_r!r}, expected {exp_r!r}",
                                    )
                    else:
                        if len(rows) != len(exp_rows):
                            raise GateCheckError(
                                1,
                                f"{root_label}/{db_fn}",
                                f"Row count mismatch in table {tbl}: got {len(rows)}, expected {len(exp_rows)}",
                            )
                        # Find first mismatch
                        for idx, (act_r, exp_r) in enumerate(zip(rows, exp_rows)):
                            if act_r != exp_r:
                                raise GateCheckError(
                                    2,
                                    f"{root_label}/{db_fn}",
                                    f"Data content mismatch in table {tbl} at row {idx + 1}: got {act_r!r}, expected {exp_r!r}",
                                )


def verify_phase3_acceptance_controls(db_path: Path, pack_id: str, root_label: str, db_fn: str) -> None:
    """Check #1: Verify independent acceptance totals for verses, words, pages, lines, footnotes."""
    with closing(_open_ro_db(db_path)) as conn:
        if pack_id == "quran-core":
            cnt = conn.execute("SELECT count(*) FROM ayahs;").fetchone()[0]
            if cnt != 6236:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Core verse count mismatch: expected 6236, got {cnt}")

        elif pack_id == "quran-wbw-en":
            cnt = conn.execute("SELECT count(*) FROM word_translations;").fetchone()[0]
            if cnt != 77429:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"WBW word count mismatch: expected 77429, got {cnt}")

        elif pack_id == "quran-mushaf-madani-v2":
            layout = conn.execute("SELECT page_count, lines_per_page FROM layout_info WHERE id=1;").fetchone()
            if not layout or layout[0] != 604 or layout[1] != 15:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Mushaf madani layout mismatch: {layout}")
            lines_cnt = conn.execute("SELECT count(*) FROM page_lines;").fetchone()[0]
            if lines_cnt != 9046:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Mushaf madani lines mismatch: expected 9046, got {lines_cnt}")
            tajweed_cnt = conn.execute("SELECT count(*) FROM tajweed_ayahs;").fetchone()[0]
            if tajweed_cnt != 6236:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Tajweed verse count mismatch: expected 6236, got {tajweed_cnt}")

        elif pack_id == "quran-mushaf-indopak-15":
            layout = conn.execute("SELECT page_count, lines_per_page FROM layout_info WHERE id=1;").fetchone()
            if not layout or layout[0] != 610 or layout[1] != 15:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Mushaf indopak layout mismatch: {layout}")
            lines_cnt = conn.execute("SELECT count(*) FROM page_lines;").fetchone()[0]
            if lines_cnt != 9131:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Mushaf indopak lines mismatch: expected 9131, got {lines_cnt}")
            tajweed_cnt = conn.execute("SELECT count(*) FROM tajweed_ayahs;").fetchone()[0]
            if tajweed_cnt != 6236:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Tajweed verse count mismatch: expected 6236, got {tajweed_cnt}")

        elif pack_id in EXPECTED_TAFSIR_GROUPS:
            exp_groups = EXPECTED_TAFSIR_GROUPS[pack_id]
            group_cnt = conn.execute("SELECT count(*) FROM tafsir_entries;").fetchone()[0]
            if group_cnt != exp_groups:
                raise GateCheckError(
                    1,
                    f"{root_label}/{db_fn}",
                    f"Tafsir group count mismatch: expected {exp_groups}, got {group_cnt}",
                )
            map_cnt = conn.execute("SELECT count(DISTINCT ayah_id) FROM tafsir_ayah_map;").fetchone()[0]
            if map_cnt != 6236:
                raise GateCheckError(
                    1,
                    f"{root_label}/{db_fn}",
                    f"Tafsir mapped ayah count mismatch: expected 6236, got {map_cnt}",
                )
            if pack_id == "quran-tafsir-ar-qurtubi":
                cov_row = conn.execute("SELECT count FROM coverage_report WHERE metric='ar_present';").fetchone()
                if not cov_row or cov_row[0] != 6234:
                    raise GateCheckError(
                        1,
                        f"{root_label}/{db_fn}",
                        f"Qurtubi ar_present coverage mismatch: expected 6234, got {cov_row}",
                    )
                comp_row = conn.execute("SELECT value FROM pack_meta WHERE key='completeness';").fetchone()
                if not comp_row or comp_row[0] != "partial":
                    raise GateCheckError(
                        7,
                        f"{root_label}/{db_fn}",
                        f"Qurtubi completeness mismatch: expected 'partial', got {comp_row}",
                    )

        elif pack_id == "quran-tr-en-sahih":
            tr_cnt = conn.execute("SELECT count(*) FROM translations;").fetchone()[0]
            if tr_cnt != 6236:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Saheeh translation count mismatch: expected 6236, got {tr_cnt}")
            fn_cnt = conn.execute("SELECT count(*) FROM translation_footnotes;").fetchone()[0]
            if fn_cnt != 1904:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Saheeh footnote count mismatch: expected 1904, got {fn_cnt}")
            fn_ayahs = conn.execute("SELECT count(DISTINCT ayah_id) FROM translation_footnotes;").fetchone()[0]
            if fn_ayahs != 1613:
                raise GateCheckError(1, f"{root_label}/{db_fn}", f"Saheeh footnote ayah count mismatch: expected 1613, got {fn_ayahs}")
            # Known blank footnote 7:143 ID 226798
            # In Tanzil coordinates 7:143 is ayah_id 1104
            blank_fn = conn.execute(
                "SELECT tf.text FROM translation_footnotes tf JOIN translations t ON tf.ayah_id=t.ayah_id "
                "WHERE t.verse_key='7:143' AND tf.footnote_id=226798;"
            ).fetchone()
            if not blank_fn or blank_fn[0] != "":
                raise GateCheckError(
                    2,
                    f"{root_label}/{db_fn}",
                    f"Saheeh blank footnote at 7:143 ID 226798 not preserved as empty string: got {blank_fn}",
                )


def run_phase3_fts_smoke(db_path: Path, pack_id: str, root_label: str, db_fn: str) -> None:
    """Check #4: Execute FTS positive controls at rowid 1 and bound negative query."""
    with closing(_open_ro_db(db_path)) as conn:
        cur = conn.cursor()
        # Verify negative control returns 0 rows on all FTS tables
        fts_tables = [
            r[0] for r in cur.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE '%_fts' ORDER BY name;"
            ).fetchall()
        ]
        for tbl in fts_tables:
            neg_res = cur.execute(f"SELECT rowid FROM {tbl} WHERE {tbl} MATCH ?;", ("zzzzfixturemissing",)).fetchall()
            if neg_res:
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"Negative FTS query matched rows in {tbl}: {neg_res}")

        # Positive controls
        if pack_id == "quran-core":
            res = cur.execute("SELECT rowid FROM ayahs_fts WHERE ayahs_fts MATCH ?;", ("بسم",)).fetchall()
            if not any(r[0] == 1 for r in res):
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS positive query 'بسم' did not match rowid 1: {res}")
        elif pack_id in ("quran-tafsir-ar-ibn-kathir", "quran-tafsir-ar-wasit"):
            res = cur.execute("SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH ?;", ("بسم",)).fetchall()
            if not any(r[0] == 1 for r in res):
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS positive query 'بسم' did not match rowid 1: {res}")
        elif pack_id == "quran-tafsir-ar-tabari":
            res = cur.execute("SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH ?;", ("القول",)).fetchall()
            if not any(r[0] == 1 for r in res):
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS positive query 'القول' did not match rowid 1: {res}")
        elif pack_id == "quran-tafsir-ar-qurtubi":
            res = cur.execute("SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH ?;", ("الكلام",)).fetchall()
            if not any(r[0] == 1 for r in res):
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS positive query 'الكلام' did not match rowid 1: {res}")
        elif pack_id == "quran-tafsir-ar-razi":
            res = cur.execute("SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH ?;", ("الباء",)).fetchall()
            if not any(r[0] == 1 for r in res):
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS positive query 'الباء' did not match rowid 1: {res}")
        elif pack_id == "quran-tafsir-en-ibn-kathir":
            res = cur.execute("SELECT rowid FROM tafsir_fts WHERE tafsir_fts MATCH ?;", ("Meaning",)).fetchall()
            if not any(r[0] == 1 for r in res):
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS positive query 'Meaning' did not match rowid 1: {res}")
        elif pack_id == "quran-tr-en-sahih":
            res = cur.execute("SELECT rowid FROM translations_fts WHERE translations_fts MATCH ?;", ('"Entirely Merciful"',)).fetchall()
            if not any(r[0] == 1 for r in res):
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS positive query 'Entirely Merciful' did not match rowid 1: {res}")
        elif pack_id == "quran-wbw-en":
            target_id_row = cur.execute("SELECT id FROM word_translations WHERE word_key='1:5:2';").fetchone()
            if not target_id_row:
                raise GateCheckError(4, f"{root_label}/{db_fn}", "Target word_key 1:5:2 not found in word_translations")
            target_id = target_id_row[0]
            res = cur.execute("SELECT rowid FROM word_translations_fts WHERE word_translations_fts MATCH ?;", ('"we worship"',)).fetchall()
            if not any(r[0] == target_id for r in res):
                raise GateCheckError(4, f"{root_label}/{db_fn}", f"FTS positive query 'we worship' did not match rowid {target_id}: {res}")
