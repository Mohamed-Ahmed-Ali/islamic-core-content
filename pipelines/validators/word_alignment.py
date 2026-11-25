"""Deterministic cross-pack word alignment validator between core, morphology, and WBW packs."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sqlite3


def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database file not found: {db_path}")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def validate_word_alignment(
    core_path: Path,
    morphology_path: Path,
    wbw_path: Path,
) -> None:
    """Validate word alignment between core ayahs, morphology words, and WBW words.

    Performs symmetric set comparison on (ayah_id, word_number, word_key) over core ayahs.
    Raises ValueError with per-ayah diagnostics in numeric order if any mismatch is found.
    Returns None when aligned.
    """
    core_path = Path(core_path)
    morphology_path = Path(morphology_path)
    wbw_path = Path(wbw_path)

    core_conn = _open_ro_db(core_path)
    try:
        morph_conn = _open_ro_db(morphology_path)
        try:
            wbw_conn = _open_ro_db(wbw_path)
            try:
                # 1. Read canonical ayah inventory from core
                cur_core = core_conn.cursor()
                cur_core.execute("SELECT id, verse_key FROM ayahs ORDER BY id ASC;")
                core_rows = cur_core.fetchall()
                if not core_rows:
                    raise ValueError("Core database contains no ayahs")
                core_ayahs: dict[int, str] = {row[0]: row[1] for row in core_rows}

                # 2. Read lexical coordinates from morphology
                cur_morph = morph_conn.cursor()
                cur_morph.execute(
                    "SELECT ayah_id, word_number, word_key FROM words ORDER BY ayah_id ASC, word_number ASC;"
                )
                morph_rows = cur_morph.fetchall()

                # 3. Read lexical coordinates from WBW
                cur_wbw = wbw_conn.cursor()
                cur_wbw.execute(
                    "SELECT ayah_id, word_number, word_key FROM word_translations ORDER BY ayah_id ASC, word_number ASC;"
                )
                wbw_rows = cur_wbw.fetchall()
            finally:
                wbw_conn.close()
        finally:
            morph_conn.close()
    finally:
        core_conn.close()

    # Group coordinates by ayah_id
    morph_by_ayah: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for a_id, w_num, w_key in morph_rows:
        morph_by_ayah[a_id].append((w_num, w_key))

    wbw_by_ayah: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for a_id, w_num, w_key in wbw_rows:
        wbw_by_ayah[a_id].append((w_num, w_key))

    # All ayahs to inspect: union of core_ayahs and any unexpected ayah_id in morph/wbw
    all_ayah_ids = sorted(set(core_ayahs.keys()) | set(morph_by_ayah.keys()) | set(wbw_by_ayah.keys()))

    mismatches: list[str] = []

    for ayah_id in all_ayah_ids:
        verse_key = core_ayahs.get(ayah_id, f"unknown:{ayah_id}")
        qac_words = morph_by_ayah.get(ayah_id, [])
        qul_words = wbw_by_ayah.get(ayah_id, [])

        qac_set = set(qac_words)
        qul_set = set(qul_words)

        if qac_set == qul_set and ayah_id in core_ayahs:
            continue

        qac_count = len(qac_words)
        qul_count = len(qul_words)
        delta = qul_count - qac_count

        missing_coords = qac_set - qul_set
        extra_coords = qul_set - qac_set

        missing_ordinals = sorted({w for w, _ in missing_coords})
        extra_ordinals = sorted({w for w, _ in extra_coords})

        diag = (
            f"Ayah {ayah_id} ({verse_key}): QAC={qac_count} QUL={qul_count} delta={delta}; "
            f"missing ordinals: {missing_ordinals}, extra ordinals: {extra_ordinals}"
        )
        mismatches.append(diag)

    if mismatches:
        raise ValueError(
            f"Word alignment mismatch across {len(mismatches)} ayah(s):\n"
            + "\n".join(mismatches)
        )
