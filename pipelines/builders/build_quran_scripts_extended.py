"""Quran scripts extended pack builder CLI and pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from pipelines.common.quran_pack import load_quran_pack, write_quran_pack
from pipelines.common.tanzil_coordinates import (
    load_tanzil_coordinates,
    load_tanzil_text,
)
from pipelines.toolchain.check_runtime import verify_runtime

PACK_ID = "quran-scripts-extended"
EXPECTED_PATHS = frozenset({
    "tanzil/quran-data-Tanzil.xml",
    "tanzil/quran-uthmani-min.xml",
    "tanzil/quran-simple.xml",
    "tanzil/quran-simple-plain.xml",
    "tanzil/quran-simple-min.xml",
})


def build_quran_scripts_extended(sources_root: Path, output_dir: Path) -> None:
    """Build quran-scripts-extended.db deterministically."""
    verify_runtime()

    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    raw_root, metadata = load_quran_pack(
        sources_root,
        PACK_ID,
        EXPECTED_PATHS,
        fts_enabled=False,
    )

    meta_file = raw_root / "tanzil" / "quran-data-Tanzil.xml"
    uth_min_file = raw_root / "tanzil" / "quran-uthmani-min.xml"
    simple_file = raw_root / "tanzil" / "quran-simple.xml"
    simple_plain_file = raw_root / "tanzil" / "quran-simple-plain.xml"
    simple_min_file = raw_root / "tanzil" / "quran-simple-min.xml"

    coords = load_tanzil_coordinates(meta_file)
    text_uthmani_min = load_tanzil_text(uth_min_file, coords, locked_path="tanzil/quran-uthmani-min.xml")
    text_simple = load_tanzil_text(simple_file, coords, locked_path="tanzil/quran-simple.xml")
    text_simple_plain = load_tanzil_text(simple_plain_file, coords, locked_path="tanzil/quran-simple-plain.xml")
    text_simple_min = load_tanzil_text(simple_min_file, coords, locked_path="tanzil/quran-simple-min.xml")

    coverage: dict[str, tuple[int, str | None]] = {
        "expected_total": (6236, None),
        "ar_present": (6236, None),
    }

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "sql" / "quran_scripts_extended.v1.sql"
    output_path = output_dir / "quran-scripts-extended.db"

    with write_quran_pack(
        output_path,
        schema_path,
        metadata,
        coverage,
        fts_tables=(),
    ) as conn:
        def _script_param_stream():
            for vkey, aid in coords.ayah_ids.items():
                yield (
                    aid,
                    vkey,
                    text_uthmani_min[vkey],
                    text_simple[vkey],
                    text_simple_plain[vkey],
                    text_simple_min[vkey],
                )

        conn.executemany(
            "INSERT INTO ayah_scripts ("
            "ayah_id, verse_key, text_uthmani_min, text_simple, text_simple_plain, text_simple_min"
            ") VALUES (?, ?, ?, ?, ?, ?);",
            _script_param_stream(),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build quran-scripts-extended SQLite pack.")
    parser.add_argument("--sources-root", required=True, type=Path, help="Root directory containing sources/ and raw-inputs/")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory where quran-scripts-extended.db will be written")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_quran_scripts_extended(args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building quran-scripts-extended: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
