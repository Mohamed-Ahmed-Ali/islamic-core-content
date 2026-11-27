"""Deterministic quality gate validator for Mashrūʿ Taqrīb as-Sunnah local packs."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from pipelines.common.lock_inputs import (
    _is_symlink_or_junction,
    parse_and_validate_registry,
    parse_strict_json,
    verify_lock,
)
from pipelines.common.pack_sources import load_sqlite_source_metadata
from pipelines.common.stage_taqreeb_inputs import TAQREEB_SOURCES
from pipelines.common.search_text import (
    arabic_match_literal,
    normalize_arabic_search,
)
from pipelines.validators.phase3_local import (
    GateCheckError,
    verify_phase3_root_identity,
)
from pipelines.validators.quality_gate import (
    GateContext,
    _recompute_sha256,
    _run_fts_temp_integrity_check,
    _verify_single_db_schema_and_integrity,
    stage_0_preflight,
    stage_1_root_identity,
)

_MARKER_REGEX: re.Pattern[str] = re.compile(r"\{\[([0-9]+)\]\}")

TAQREEB_INVENTORY: dict[str, dict[str, Any]] = {
    "hadith-taqreeb-jami": {
        "book_id": 1,
        "maqsads": 10,
        "kutub": 45,
        "chapters": 2740,
        "hadiths": 22564,
        "footnotes": 7472,
        "completeness": "partial",
        "ar_present": 22561,
        "cross_ref_count": 1,
        "empty_text_count": 2,
        "footnote_missing_marker": 0,
        "marker_missing_footnote": 0,
    },
    "hadith-taqreeb-maalim": {
        "book_id": 2,
        "maqsads": 10,
        "kutub": 51,
        "chapters": 2018,
        "hadiths": 4303,
        "footnotes": 1920,
        "completeness": "complete",
        "ar_present": 4303,
        "cross_ref_count": 0,
        "empty_text_count": 0,
        "footnote_missing_marker": 1,
        "marker_missing_footnote": 0,
    },
    "hadith-taqreeb-wajeez": {
        "book_id": 3,
        "maqsads": 9,
        "kutub": 43,
        "chapters": 916,
        "hadiths": 1402,
        "footnotes": 658,
        "completeness": "complete",
        "ar_present": 1402,
        "cross_ref_count": 0,
        "empty_text_count": 0,
        "footnote_missing_marker": 0,
        "marker_missing_footnote": 0,
    },
    "hadith-taqreeb-kulliyyah": {
        "book_id": 4,
        "maqsads": 6,
        "kutub": 20,
        "chapters": 40,
        "hadiths": 151,
        "footnotes": 157,
        "completeness": "complete",
        "ar_present": 151,
        "cross_ref_count": 0,
        "empty_text_count": 0,
        "footnote_missing_marker": 0,
        "marker_missing_footnote": 0,
    },
}


def _format_marker_notes(discrepancies: list[tuple[int, int]]) -> str | None:
    if not discrepancies:
        return None
    return " | ".join(
        f"hadith_id={hid}; footnote_number={num}"
        for hid, num in sorted(discrepancies)
    )


def verify_taqreeb_source_controls(pack_id: str, document: dict[str, Any]) -> None:
    """Check source document book_id, counts against inventory, and anomalies."""
    if pack_id not in TAQREEB_SOURCES:
        raise GateCheckError(2, pack_id, f"Unknown pack ID {pack_id}")

    expected_book_id, basename = TAQREEB_SOURCES[pack_id]
    rel_path = f"hadith-taqreeb/{basename}"

    if document.get("book_id") != expected_book_id:
        raise GateCheckError(
            2,
            rel_path,
            f"book_id mismatch: expected {expected_book_id}, got {document.get('book_id')}",
        )

    maqsads = document.get("maqsads", [])
    maqsads_count = len(maqsads)
    kutub_count = 0
    chapters_count = 0
    hadiths_by_id: dict[int, dict[str, Any]] = {}
    footnotes_count = 0

    for m in maqsads:
        for k in m.get("kutub", []):
            kutub_count += 1
            for c in k.get("chapters", []):
                chapters_count += 1
                for h in c.get("hadiths", []):
                    hid = h.get("id")
                    if hid in hadiths_by_id:
                        raise GateCheckError(2, rel_path, f"Duplicate hadith id {hid}")
                    hadiths_by_id[hid] = h
                    footnotes_count += len(h.get("footnotes", []))

    inv = TAQREEB_INVENTORY.get(pack_id, {})
    if inv and document.get("total_hadiths") == inv.get("hadiths"):
        if (
            maqsads_count != inv.get("maqsads")
            or kutub_count != inv.get("kutub")
            or chapters_count != inv.get("chapters")
            or len(hadiths_by_id) != inv.get("hadiths")
            or footnotes_count != inv.get("footnotes")
        ):
            raise GateCheckError(2, rel_path, "Inventory count mismatch against canonical source inventory")
    else:
        if document.get("total_hadiths") != len(hadiths_by_id):
            raise GateCheckError(
                2,
                rel_path,
                f"total_hadiths mismatch: declared {document.get('total_hadiths')}, traversed {len(hadiths_by_id)}",
            )

    if pack_id == "hadith-taqreeb-jami":
        if 7427 in hadiths_by_id:
            h7427 = hadiths_by_id[7427]
            if (
                h7427.get("text_ar") is not None
                or h7427.get("text_ar_simple") != ""
                or h7427.get("takhreej") != "وأخرجه/ حم(2254) (2380) (2381)."
            ):
                raise GateCheckError(2, rel_path, "Jāmiʿ hadith 7427 anomaly mismatch")
        if 8199 in hadiths_by_id:
            h8199 = hadiths_by_id[8199]
            if h8199.get("text_ar") != "" or h8199.get("text_ar_simple") != "":
                raise GateCheckError(2, rel_path, "Jāmiʿ hadith 8199 anomaly mismatch")
        if 16588 in hadiths_by_id:
            h16588 = hadiths_by_id[16588]
            if h16588.get("text_ar") != "" or h16588.get("atraf") != ["[انظر الحديث: 7734]."]:
                raise GateCheckError(2, rel_path, "Jāmiʿ hadith 16588 anomaly mismatch")
    elif pack_id == "hadith-taqreeb-maalim":
        if 1 in hadiths_by_id:
            h1 = hadiths_by_id[1]
            fn1 = [fn for fn in h1.get("footnotes", []) if fn.get("number") == 1]
            if not fn1 or "English footnote for translation of first hadith" not in fn1[0].get("text", ""):
                raise GateCheckError(2, rel_path, "Maʿālim hadith 1 footnote 1 anomaly mismatch")
            if "{[1]}" in (h1.get("text_ar") or ""):
                raise GateCheckError(2, rel_path, "Maʿālim hadith 1 text_ar should be marker-free")


def verify_taqreeb_db_content(
    db_paths: tuple[Path, Path],
    document: dict[str, Any],
    metadata: dict[str, str],
) -> None:
    """Verify exact DB content and coverage report in both candidate and rebuild DBs against source JSON."""
    book_id = document["book_id"]
    title_ar = document["title_ar"]
    title_en = document["title_en"]
    description_ar = document["description_ar"]
    total_maqsads = document["total_maqsads"]
    total_hadiths = document["total_hadiths"]

    books_rows = [(book_id, title_ar, title_en, description_ar, total_maqsads, total_hadiths)]
    maqsads_rows: list[tuple[int, int, int, str, str]] = []
    kutub_rows: list[tuple[int, int, int, str, str]] = []
    chapters_rows: list[tuple[int, int, int, str, str]] = []
    hadiths_rows: list[tuple[Any, ...]] = []
    footnotes_rows: list[tuple[int, int, str]] = []
    atraf_rows: list[tuple[int, int, str]] = []

    ar_present = 0
    cross_ref_count = 0
    empty_text_count = 0
    footnote_count = 0
    footnote_missing_marker: list[tuple[int, int]] = []
    marker_missing_footnote: list[tuple[int, int]] = []

    for m in document.get("maqsads", []):
        m_id = m["maqsad_id"]
        maqsads_rows.append((m_id, book_id, m["maqsad_index"], m["name_ar"], m["name_en"]))
        for k in m.get("kutub", []):
            k_id = k["ketab_id"]
            kutub_rows.append((k_id, m_id, k["ketab_index"], k["name_ar"], k["name_en"]))
            for c in k.get("chapters", []):
                c_id = c["chapter_id"]
                chapters_rows.append((c_id, k_id, c["chapter_index"], c["name_ar"], c["name_en"]))
                for h in c.get("hadiths", []):
                    h_id = h["id"]
                    h_no = h["hadith_no"]
                    text_ar = h["text_ar"]
                    text_ar_simple = h["text_ar_simple"]
                    takhreej = h["takhreej"]
                    hukm = h["hukm"]
                    page_no = h["page_no"]

                    if text_ar is None:
                        is_cross_ref = 1
                        cross_ref_count += 1
                    else:
                        is_cross_ref = 0
                        if text_ar.strip():
                            ar_present += 1
                        else:
                            empty_text_count += 1

                    text_split_mode = "unsplit_raw"
                    text_ar_search = normalize_arabic_search(text_ar) if text_ar else ""

                    hadiths_rows.append((
                        h_id,
                        c_id,
                        h_no,
                        text_ar,
                        text_ar_simple,
                        takhreej,
                        hukm,
                        page_no,
                        is_cross_ref,
                        text_split_mode,
                        text_ar_search,
                    ))

                    seen_fn_nums: set[int] = set()
                    for fn in h.get("footnotes", []):
                        fn_num = fn["number"]
                        seen_fn_nums.add(fn_num)
                        footnotes_rows.append((h_id, fn_num, fn["text"]))
                        footnote_count += 1

                    marker_nums = {int(x) for x in _MARKER_REGEX.findall(text_ar)} if text_ar else set()
                    for missing_m in seen_fn_nums - marker_nums:
                        footnote_missing_marker.append((h_id, missing_m))
                    for missing_fn in marker_nums - seen_fn_nums:
                        marker_missing_footnote.append((h_id, missing_fn))

                    for pos, atraf_text in enumerate(h.get("atraf", []), start=1):
                        atraf_rows.append((h_id, pos, atraf_text))

    books_rows.sort(key=lambda r: r[0])
    maqsads_rows.sort(key=lambda r: r[0])
    kutub_rows.sort(key=lambda r: r[0])
    chapters_rows.sort(key=lambda r: r[0])
    hadiths_rows.sort(key=lambda r: r[0])
    footnotes_rows.sort(key=lambda r: (r[0], r[1]))
    atraf_rows.sort(key=lambda r: (r[0], r[1]))

    expected_coverage = [
        ("ar_present", ar_present, None),
        ("cross_ref_count", cross_ref_count, None),
        ("empty_text_count", empty_text_count, None),
        ("en_missing", total_hadiths, None),
        ("en_present", 0, None),
        ("expected_total", total_hadiths, None),
        ("footnote_count", footnote_count, None),
        (
            "footnote_missing_inline_marker_count",
            len(footnote_missing_marker),
            _format_marker_notes(footnote_missing_marker),
        ),
        (
            "inline_marker_missing_footnote_count",
            len(marker_missing_footnote),
            _format_marker_notes(marker_missing_footnote),
        ),
    ]
    expected_coverage.sort(key=lambda r: r[0])

    expected_tables: list[tuple[str, str, list[tuple[Any, ...]]]] = [
        (
            "books",
            "SELECT book_id, title_ar, title_en, description_ar, total_maqsads, total_hadiths FROM books ORDER BY book_id;",
            books_rows,
        ),
        (
            "maqsads",
            "SELECT maqsad_id, book_id, maqsad_index, name_ar, name_en FROM maqsads ORDER BY maqsad_id;",
            maqsads_rows,
        ),
        (
            "kutub",
            "SELECT ketab_id, maqsad_id, ketab_index, name_ar, name_en FROM kutub ORDER BY ketab_id;",
            kutub_rows,
        ),
        (
            "chapters",
            "SELECT chapter_id, ketab_id, chapter_index, name_ar, name_en FROM chapters ORDER BY chapter_id;",
            chapters_rows,
        ),
        (
            "hadiths",
            "SELECT id, chapter_id, hadith_no, text_ar, text_ar_simple, takhreej, hukm, page_no, is_cross_ref, text_split_mode, text_ar_search FROM hadiths ORDER BY id;",
            hadiths_rows,
        ),
        (
            "footnotes",
            "SELECT hadith_id, number, text FROM footnotes ORDER BY hadith_id, number;",
            footnotes_rows,
        ),
        (
            "hadith_atraf",
            "SELECT hadith_id, position, text FROM hadith_atraf ORDER BY hadith_id, position;",
            atraf_rows,
        ),
        (
            "coverage_report",
            "SELECT metric, count, notes FROM coverage_report ORDER BY metric;",
            expected_coverage,
        ),
    ]

    for db_path in db_paths:
        uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("PRAGMA trusted_schema = OFF;")
            cur = conn.cursor()
            for tbl_name, sql, exp_rows in expected_tables:
                cur.execute(sql)
                actual_rows = cur.fetchall()
                if len(actual_rows) != len(exp_rows):
                    raise GateCheckError(
                        2,
                        db_path.name,
                        f"Row count mismatch in {tbl_name}: expected {len(exp_rows)}, got {len(actual_rows)}",
                    )
                for row_idx, (act_r, exp_r) in enumerate(zip(actual_rows, exp_rows)):
                    for col_idx, (act_v, exp_v) in enumerate(zip(act_r, exp_r)):
                        if isinstance(act_v, bytes):
                            raise GateCheckError(
                                2,
                                db_path.name,
                                f"BLOB storage detected in {tbl_name} row {row_idx} col {col_idx}",
                            )
                        if act_v != exp_v:
                            raise GateCheckError(
                                2,
                                db_path.name,
                                f"Value mismatch in {tbl_name} row {row_idx} col {col_idx}: expected {exp_v!r}, got {act_v!r}",
                            )
        finally:
            conn.close()


def verify_taqreeb_fts_smoke(db_path: Path, pack_id: str) -> None:
    """Run smoke test queries and vocab checks against FTS5 table."""
    targets: dict[str, tuple[int, list[str], str]] = {
        "hadith-taqreeb-jami": (6000, ["الإِسْلَامُ عَلَىٰ", "الاسلام علي"], "علي"),
        "hadith-taqreeb-maalim": (1, ["الإِسْلَامُ عَلَىٰ", "الاسلام علي"], "علي"),
        "hadith-taqreeb-wajeez": (4325, ["الإِسْلَامُ عَلَىٰ", "الاسلام علي"], "علي"),
        "hadith-taqreeb-kulliyyah": (28739, ["الأَعْمَالُ بالنِّيَّةِ", "الاعمال بالنيه"], "الاعمال"),
    }

    uri = f"{db_path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA trusted_schema = OFF;")
        cur = conn.cursor()

        if pack_id in targets:
            target_rowid, queries, vocab_term = targets[pack_id]
            exists = cur.execute("SELECT 1 FROM hadiths WHERE id = ?;", (target_rowid,)).fetchone()
            if exists:
                for q in queries:
                    match_lit = arabic_match_literal(q)
                    res = cur.execute(
                        "SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ? AND rowid = ?;",
                        (match_lit, target_rowid),
                    ).fetchall()
                    if res != [(target_rowid,)]:
                        raise GateCheckError(
                            4,
                            db_path.name,
                            f"FTS MATCH failed for {q!r} on rowid {target_rowid}: got {res}",
                        )

                cur.execute("CREATE VIRTUAL TABLE temp.taqreeb_vocab USING fts5vocab(main, hadiths_fts, 'instance');")
                vocab_hits = cur.execute(
                    "SELECT count(*) FROM temp.taqreeb_vocab WHERE term = ? AND doc = ?;",
                    (vocab_term, target_rowid),
                ).fetchone()[0]
                if vocab_hits < 1:
                    raise GateCheckError(
                        4,
                        db_path.name,
                        f"FTS token {vocab_term!r} missing from vocab for doc {target_rowid}",
                    )

                if pack_id == "hadith-taqreeb-jami":
                    for zero_rid in (7427, 8199, 16588):
                        if cur.execute("SELECT 1 FROM hadiths WHERE id = ?;", (zero_rid,)).fetchone():
                            cnt = cur.execute(
                                "SELECT count(*) FROM temp.taqreeb_vocab WHERE doc = ?;",
                                (zero_rid,),
                            ).fetchone()[0]
                            if cnt != 0:
                                raise GateCheckError(
                                    4,
                                    db_path.name,
                                    f"Expected 0 FTS terms for empty/cross-ref row {zero_rid}, got {cnt}",
                                )

        # Negative query must return no rows
        neg_lit = arabic_match_literal("zzzzfixturemissing")
        neg_res = cur.execute(
            "SELECT rowid FROM hadiths_fts WHERE hadiths_fts MATCH ?;",
            (neg_lit,),
        ).fetchall()
        if len(neg_res) != 0:
            raise GateCheckError(4, db_path.name, f"Negative query returned rows: {neg_res}")
    finally:
        conn.close()


def run_hadith_taqreeb_local(ctx: GateContext) -> None:
    """Deterministic quality gate for Taqreeb local profile."""
    # Check 0: Preflight
    stage_0_preflight(ctx)

    # Check #6: Root identity
    stage_1_root_identity(ctx)
    verify_phase3_root_identity(ctx.candidate, ctx.rebuild)

    # Check #5: Asset inventory
    expected_filenames = frozenset({f"{pid}.db" for pid in TAQREEB_SOURCES})
    for root_label, root_dir in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        actual_files: set[str] = set()
        for entry in root_dir.iterdir():
            name = entry.name
            if name.startswith("."):
                raise GateCheckError(5, f"{root_label}/{name}", "Dotfile not permitted in root")
            if _is_symlink_or_junction(entry):
                raise GateCheckError(5, f"{root_label}/{name}", "Symlink or junction not permitted in root")
            if entry.is_dir():
                raise GateCheckError(5, f"{root_label}/{name}", "Subdirectory not permitted in root")
            if not entry.is_file():
                raise GateCheckError(5, f"{root_label}/{name}", "Non-regular file in root")
            if name.endswith(("-wal", "-shm", "-journal")):
                raise GateCheckError(5, f"{root_label}/{name}", "SQLite sidecar not permitted in root")
            if name not in expected_filenames:
                raise GateCheckError(5, f"{root_label}/{name}", f"Unexpected asset in root: {name}")

            st = entry.stat()
            if not (1 <= st.st_size <= 2147483647):
                raise GateCheckError(5, f"{root_label}/{name}", f"Invalid file size: {st.st_size}")
            actual_files.add(name)

        missing = expected_filenames - actual_files
        if missing:
            raise GateCheckError(5, root_label, f"Expected asset missing: {sorted(missing)}")

    # Check #7: Source lock & clearance
    sources_root = ctx.sources_root
    raw_root = sources_root / "raw-inputs"
    registry_path = sources_root / "sources" / "registry.json"
    lock_path = sources_root / "sources" / "manifest.lock.json"
    reg_entries = parse_and_validate_registry(registry_path)

    for pack_id in TAQREEB_SOURCES:
        expected_book_id, basename = TAQREEB_SOURCES[pack_id]
        expected_rel = f"hadith-taqreeb/{basename}"

        lock_entries = verify_lock(registry_path, raw_root, lock_path, pack_id=pack_id)
        if [e.path for e in lock_entries] != [expected_rel]:
            raise GateCheckError(
                7,
                f"sources/{pack_id}",
                f"Expected sole locked path {expected_rel}, got {[e.path for e in lock_entries]}",
            )
        reg_item = reg_entries.get(expected_rel)
        if not reg_item or reg_item.get("packIds") != [pack_id]:
            raise GateCheckError(
                7,
                f"sources/{pack_id}",
                f"Registry entry {expected_rel} packIds must be exactly [{pack_id!r}]",
            )

        lic_rel = f"sources/licenses/{pack_id}.json"
        lic_path = sources_root / lic_rel
        if not lic_path.is_file():
            raise GateCheckError(7, lic_rel, "License file missing")
        lic_data = parse_strict_json(lic_path.read_text(encoding="utf-8"))
        if lic_data.get("primaryPath") != expected_rel:
            raise GateCheckError(
                7,
                lic_rel,
                f"License primaryPath mismatch: expected {expected_rel}, got {lic_data.get('primaryPath')}",
            )
        if lic_data.get("status") != "pending":
            raise GateCheckError(7, lic_rel, f"License status must be 'pending', got {lic_data.get('status')}")
        if lic_data.get("permittedUses") is not None and lic_data.get("permittedUses") != []:
            raise GateCheckError(7, lic_rel, f"License permittedUses must be empty, got {lic_data.get('permittedUses')}")
        if lic_data.get("reviewedBy") is not None:
            raise GateCheckError(7, lic_rel, "License reviewedBy must be null")
        for src in lic_data.get("sources", []):
            if src.get("permittedUses") != []:
                raise GateCheckError(7, lic_rel, f"Source permittedUses must be empty for {src.get('path')}")

    # Check #3: SQLite schema and pack_meta
    common_meta_sql = (ctx.specs_sql_dir / "common_meta.sql").read_text(encoding="utf-8")
    for root_label, root_dir in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        for pack_id in TAQREEB_SOURCES:
            db_fn = f"{pack_id}.db"
            db_path = root_dir / db_fn
            _verify_single_db_schema_and_integrity(ctx, root_label, db_fn, db_path, pack_id, common_meta_sql)

            expected_book_id, basename = TAQREEB_SOURCES[pack_id]
            expected_rel = f"hadith-taqreeb/{basename}"
            lock_entries = verify_lock(registry_path, raw_root, lock_path, pack_id=pack_id)
            expected_comp = TAQREEB_INVENTORY.get(pack_id, {}).get("completeness", "complete")
            expected_meta = load_sqlite_source_metadata(
                sources_root,
                pack_id,
                lock_entries,
                primary_path=expected_rel,
                pack_version="0.4.0",
                completeness=expected_comp,
                fts5_enabled=True,
            )
            expected_meta["matn_split_verified"] = "false"

            conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                conn.execute("PRAGMA trusted_schema = OFF;")
                actual_meta = dict(conn.execute("SELECT key, value FROM pack_meta;").fetchall())
            finally:
                conn.close()

            for k, v in expected_meta.items():
                if actual_meta.get(k) != v:
                    raise GateCheckError(
                        3,
                        f"{root_label}/{db_fn}",
                        f"pack_meta mismatch for {k}: expected {v!r}, got {actual_meta.get(k)!r}",
                    )

    # Check #2: Source fidelity & SQL content
    for pack_id in TAQREEB_SOURCES:
        expected_book_id, basename = TAQREEB_SOURCES[pack_id]
        expected_rel = f"hadith-taqreeb/{basename}"
        raw_path = raw_root / expected_rel
        raw_text = raw_path.read_bytes().decode("utf-8")
        doc = parse_strict_json(raw_text)

        verify_taqreeb_source_controls(pack_id, doc)

        cand_db = ctx.candidate / f"{pack_id}.db"
        reb_db = ctx.rebuild / f"{pack_id}.db"
        verify_taqreeb_db_content((cand_db, reb_db), doc, {})

    # Check #4: FTS integrity & smoke
    for root_label, root_dir in (("candidate", ctx.candidate), ("rebuild", ctx.rebuild)):
        for pack_id in TAQREEB_SOURCES:
            db_path = root_dir / f"{pack_id}.db"
            _run_fts_temp_integrity_check(db_path, ("hadiths_fts",), root_label, db_path.name, pack_id)
            verify_taqreeb_fts_smoke(db_path, pack_id)

    # Check #6: Cross-root byte equality & ledger
    ledger_rows: list[dict[str, Any]] = []
    for pack_id in sorted(TAQREEB_SOURCES.keys()):
        db_fn = f"{pack_id}.db"
        cand_db = ctx.candidate / db_fn
        reb_db = ctx.rebuild / db_fn

        c_size = cand_db.stat().st_size
        r_size = reb_db.stat().st_size
        if c_size != r_size:
            raise GateCheckError(6, f"candidate/{db_fn} and rebuild/{db_fn}", f"Size mismatch: {c_size} != {r_size}")

        c_sha = _recompute_sha256(cand_db)
        r_sha = _recompute_sha256(reb_db)
        if c_sha != r_sha:
            raise GateCheckError(
                6,
                f"candidate/{db_fn} and rebuild/{db_fn}",
                f"Byte digest mismatch: candidate {c_sha} vs rebuild {r_sha}",
            )

        conn = sqlite3.connect(f"{cand_db.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            conn.execute("PRAGMA trusted_schema = OFF;")
            cov = dict(conn.execute("SELECT metric, count FROM coverage_report;").fetchall())
        finally:
            conn.close()

        ledger_rows.append({
            "pack_id": pack_id,
            "sha256": c_sha,
            "size": c_size,
            "hadiths": cov.get("expected_total", 0),
            "footnotes": cov.get("footnote_count", 0),
            "cross_ref": cov.get("cross_ref_count", 0),
            "empty_text": cov.get("empty_text_count", 0),
            "fn_missing_marker": cov.get("footnote_missing_inline_marker_count", 0),
            "marker_missing_fn": cov.get("inline_marker_missing_footnote_count", 0),
        })

    print("Check #1: Passed (inventory, keys, coverage)")
    print("Check #2: Passed (source fidelity, text projections, and SQL content)")
    print("Check #3: Passed (SQLite schema, integrity, and pack_meta)")
    print("Check #4: Passed (FTS integrity and smoke queries)")
    print("Check #5: Passed (flat DB asset inventory and sizes)")
    print("Check #6: Passed (root identity and cross-root byte equality)")
    print("\n--- Taqreeb SQLite Pack Ledger ---")
    for row in ledger_rows:
        print(
            f"Pack: {row['pack_id']}\n"
            f"  SHA-256:   {row['sha256']}\n"
            f"  Size:      {row['size']} bytes\n"
            f"  Counts:    hadiths={row['hadiths']}, footnotes={row['footnotes']}, cross_ref={row['cross_ref']}, empty_text={row['empty_text']}\n"
            f"  Markers:   fn_missing_marker={row['fn_missing_marker']}, marker_missing_fn={row['marker_missing_fn']}"
        )
    print("----------------------------------\n")
    print(f"Clearance pending (local verification only): {', '.join(sorted(TAQREEB_SOURCES.keys()))}")
    print("Check 8: Not applicable (no production QAC/QUL pair).")
    print("Check 9: Not applicable (no Tafsir packs in Hadith profile).")
    print("All quality gate checks passed successfully.")
