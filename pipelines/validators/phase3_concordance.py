"""Phase 3 concordance validator and indexed cross-pack proof query CLI."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any

PROOF_SQL = (
    "SELECT v.verb_buckwalter,v.total_count,mw.word_key,wt.translation,a.text_uthmani "
    "FROM m.words AS mw "
    "JOIN m.verb_concordance AS v ON v.id=mw.verb_concordance_id "
    "JOIN w.word_translations AS wt ON wt.word_key=mw.word_key "
    "JOIN main.ayahs AS a ON a.id=mw.ayah_id "
    "WHERE mw.word_key=?;"
)

SPECIAL_WBW_GLOSSES: dict[str, str] = {
    "2:181:4": "he (has) heard [it]",
    "8:6:5": "was made clear",
    "13:37:9": "came to you",
}


def validate_phase3_concordance(root: Path | str) -> None:
    """Validate Phase 3 concordance point lookups, timing benchmark, and special glosses."""
    root = Path(root)
    core_path = root / "quran-core.db"
    morph_path = root / "quran-morphology.db"
    wbw_path = root / "quran-wbw-en.db"

    for p, name in ((core_path, "quran-core.db"), (morph_path, "quran-morphology.db"), (wbw_path, "quran-wbw-en.db")):
        if not p.is_file():
            raise FileNotFoundError(f"Required database not found: {name} in {root}")

    core_uri = f"{core_path.resolve().as_uri()}?mode=ro"
    morph_uri = f"{morph_path.resolve().as_uri()}?mode=ro"
    wbw_uri = f"{wbw_path.resolve().as_uri()}?mode=ro"

    conn = sqlite3.connect(core_uri, uri=True)
    try:
        conn.execute("PRAGMA trusted_schema = OFF;")
        conn.execute("ATTACH DATABASE ? AS m;", (morph_uri,))
        conn.execute("ATTACH DATABASE ? AS w;", (wbw_uri,))

        # 1. Assert query plan
        plan_rows = conn.execute("EXPLAIN QUERY PLAN " + PROOF_SQL, ("1:5:2",)).fetchall()
        plan_details = [row[3] for row in plan_rows]

        for d in plan_details:
            if "SCAN" in d:
                raise ValueError(f"Query plan contains scan: {d}")

        if not any(
            re.search(r"SEARCH (?:TABLE )?mw USING (?:COVERING )?INDEX .*\(word_key=\?\)", d)
            for d in plan_details
        ):
            raise ValueError(f"Query plan missing indexed word_key lookup on mw: {plan_details}")

        if not any(
            re.search(r"SEARCH (?:TABLE )?wt USING (?:COVERING )?INDEX .*\(word_key=\?\)", d)
            for d in plan_details
        ):
            raise ValueError(f"Query plan missing indexed word_key lookup on wt: {plan_details}")

        if not any(
            re.search(r"SEARCH (?:TABLE )?v USING INTEGER PRIMARY KEY", d)
            for d in plan_details
        ):
            raise ValueError(f"Query plan missing integer PK lookup on v: {plan_details}")

        if not any(
            re.search(r"SEARCH (?:TABLE )?a USING INTEGER PRIMARY KEY", d)
            for d in plan_details
        ):
            raise ValueError(f"Query plan missing integer PK lookup on a: {plan_details}")

        # 2. Warmup
        conn.execute(PROOF_SQL, ("1:5:2",)).fetchall()

        # 3. Benchmark 20 iterations
        samples_ns: list[int] = []
        last_rows: list[tuple[Any, ...]] = []
        for _ in range(20):
            t0 = time.perf_counter_ns()
            cur = conn.execute(PROOF_SQL, ("1:5:2",))
            last_rows = cur.fetchall()
            t1 = time.perf_counter_ns()
            samples_ns.append(t1 - t0)

        for idx, s_ns in enumerate(samples_ns, start=1):
            s_ms = s_ns / 1_000_000.0
            if s_ms >= 10.0:
                raise ValueError(f"Query benchmark sample {idx} exceeded 10ms: {s_ms:.3f} ms ({s_ns} ns)")

        max_ns = max(samples_ns)
        max_ms = max_ns / 1_000_000.0
        print(f"phase3_concordance: {len(samples_ns)} samples, max={max_ms:.3f} ms ({max_ns} ns)")

        # 4. Verify exact row
        if len(last_rows) != 1:
            raise ValueError(f"Expected exactly 1 row for word_key '1:5:2', got {len(last_rows)}")

        verb_buckwalter, total_count, word_key, translation, text_uthmani = last_rows[0]

        if verb_buckwalter != "Eabada":
            raise ValueError(f"Expected verb_buckwalter 'Eabada', got {verb_buckwalter!r}")

        if word_key != "1:5:2":
            raise ValueError(f"Expected word_key '1:5:2', got {word_key!r}")

        if translation != "we worship":
            raise ValueError(f"Expected translation 'we worship', got {translation!r}")

        if text_uthmani != "إِيَّاكَ نَعْبُدُ وَإِيَّاكَ نَسْتَعِينُ":
            raise ValueError(f"Expected text_uthmani 'إِيَّاكَ نَعْبُدُ وَإِيَّاكَ نَسْتَعِينُ', got {text_uthmani!r}")

        # Independent count of matching verb_concordance_id in m.words
        cur_v_id = conn.execute(
            "SELECT verb_concordance_id FROM m.words WHERE word_key = ?;",
            ("1:5:2",),
        ).fetchone()
        if cur_v_id is None or cur_v_id[0] is None:
            raise ValueError("Word '1:5:2' has no verb_concordance_id in m.words")
        v_id = cur_v_id[0]
        ind_count_row = conn.execute(
            "SELECT COUNT(*) FROM m.words WHERE verb_concordance_id = ?;",
            (v_id,),
        ).fetchone()
        ind_count = ind_count_row[0] if ind_count_row else 0
        if total_count != ind_count:
            raise ValueError(
                f"verb count mismatch: v.total_count={total_count} != independent count in m.words={ind_count}"
            )

        # 5. Assert special WBW glosses
        for skey, expected_trans in SPECIAL_WBW_GLOSSES.items():
            r = conn.execute(
                "SELECT translation FROM w.word_translations WHERE word_key = ?;",
                (skey,),
            ).fetchall()
            if not r:
                raise ValueError(f"Missing special WBW word_key {skey}")
            if r[0][0] != expected_trans:
                raise ValueError(
                    f"Special gloss mismatch for {skey}: expected {expected_trans!r}, got {r[0][0]!r}"
                )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Phase 3 concordance validation."""
    parser = argparse.ArgumentParser(
        description="Validate Phase 3 concordance point lookups, timing benchmark, and special glosses."
    )
    parser.add_argument(
        "root",
        type=Path,
        help="Root directory containing quran-core.db, quran-morphology.db, and quran-wbw-en.db",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        validate_phase3_concordance(args.root)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
