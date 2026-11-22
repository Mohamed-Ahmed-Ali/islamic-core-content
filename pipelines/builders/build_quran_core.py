"""Quran core pack builder CLI and pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from pipelines.common.quran_pack import load_quran_pack, write_quran_pack
from pipelines.common.search_text import normalize_arabic
from pipelines.common.tanzil_coordinates import (
    load_tanzil_coordinates,
    load_tanzil_text,
)
from pipelines.toolchain.check_runtime import verify_runtime

PACK_ID = "quran-core"
EXPECTED_PATHS = frozenset({
    "tanzil/quran-data-Tanzil.xml",
    "tanzil/quran-uthmani.xml",
    "tanzil/quran-simple-clean.xml",
})


def build_quran_core(sources_root: Path, output_dir: Path) -> None:
    """Build quran-core.db deterministically."""
    verify_runtime()

    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    raw_root, metadata = load_quran_pack(
        sources_root,
        PACK_ID,
        EXPECTED_PATHS,
        fts_enabled=True,
    )

    meta_file = raw_root / "tanzil" / "quran-data-Tanzil.xml"
    uth_file = raw_root / "tanzil" / "quran-uthmani.xml"
    clean_file = raw_root / "tanzil" / "quran-simple-clean.xml"

    coords = load_tanzil_coordinates(meta_file)
    uthmani_text = load_tanzil_text(uth_file, coords, locked_path="tanzil/quran-uthmani.xml")
    clean_text = load_tanzil_text(clean_file, coords, locked_path="tanzil/quran-simple-clean.xml")

    coverage: dict[str, tuple[int, str | None]] = {
        "expected_total": (6236, None),
        "ar_present": (6236, None),
    }

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "sql" / "quran_core.v1.sql"
    output_path = output_dir / "quran-core.db"

    with write_quran_pack(
        output_path,
        schema_path,
        metadata,
        coverage,
        fts_tables=("ayahs_fts",),
    ) as conn:
        # Insert surahs in ascending PK order directly from ordered Surah NamedTuples
        conn.executemany(
            "INSERT INTO surahs ("
            "id, name_ar, name_tname, name_en, revelation_type, revelation_order, ayah_count, ruku_count, start_ayah_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            coords.surahs,
        )

        # Stream ayah parameters in ascending PK order instead of accumulating
        def _ayah_param_stream():
            for s in coords.surahs:
                for aya_num in range(1, s.ayah_count + 1):
                    vkey = f"{s.id}:{aya_num}"
                    aid = coords.ayah_ids[vkey]
                    t_uth = uthmani_text[vkey]
                    t_clean = clean_text[vkey]
                    t_search = normalize_arabic(t_clean)
                    juz, hizb, manzil, ruku, page = coords.boundaries[aid]
                    sajda = coords.sajdah_types.get(aid)
                    yield (
                        aid,
                        s.id,
                        aya_num,
                        vkey,
                        t_uth,
                        t_clean,
                        t_search,
                        juz,
                        hizb,
                        manzil,
                        ruku,
                        page,
                        sajda,
                    )

        conn.executemany(
            "INSERT INTO ayahs ("
            "id, surah_id, ayah_number, verse_key, text_uthmani, text_simple_clean, text_ar_search, "
            "juz, hizb_quarter, manzil, ruku, page_madani, sajdah_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            _ayah_param_stream(),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build quran-core SQLite pack.")
    parser.add_argument("--sources-root", required=True, type=Path, help="Root directory containing sources/ and raw-inputs/")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory where quran-core.db will be written")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_quran_core(args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building quran-core: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
