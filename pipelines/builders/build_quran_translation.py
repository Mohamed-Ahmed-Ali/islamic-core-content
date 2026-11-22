"""Quran Pickthall translation pack builder CLI and pipeline."""

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

PACK_ID = "quran-tr-en-pickthall"
EXPECTED_PATHS = frozenset({
    "tanzil/quran-data-Tanzil.xml",
    "tanzil/en.pickthall.xml",
})


def build_quran_translation(sources_root: Path, output_dir: Path) -> None:
    """Build quran-tr-en-pickthall.db deterministically."""
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
    pickthall_file = raw_root / "tanzil" / "en.pickthall.xml"

    coords = load_tanzil_coordinates(meta_file)
    translations_text = load_tanzil_text(
        pickthall_file,
        coords,
        locked_path="tanzil/en.pickthall.xml",
    )

    coverage: dict[str, tuple[int, str | None]] = {
        "expected_total": (6236, None),
        "en_present": (6236, None),
        "en_missing": (0, None),
    }

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "sql" / "quran_translation.v1.sql"
    output_path = output_dir / "quran-tr-en-pickthall.db"

    with write_quran_pack(
        output_path,
        schema_path,
        metadata,
        coverage,
        fts_tables=("translations_fts",),
    ) as conn:
        def _translation_param_stream():
            for s in coords.surahs:
                for aya_num in range(1, s.ayah_count + 1):
                    vkey = f"{s.id}:{aya_num}"
                    aid = coords.ayah_ids[vkey]
                    t_text = translations_text[vkey]
                    yield (
                        aid,
                        s.id,
                        aya_num,
                        vkey,
                        t_text,
                    )

        conn.executemany(
            "INSERT INTO translations ("
            "ayah_id, surah_id, ayah_number, verse_key, text"
            ") VALUES (?, ?, ?, ?, ?);",
            _translation_param_stream(),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build quran-tr-en-pickthall SQLite pack.")
    parser.add_argument("--sources-root", required=True, type=Path, help="Root directory containing sources/ and raw-inputs/")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory where quran-tr-en-pickthall.db will be written")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_quran_translation(args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building quran-tr-en-pickthall: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
