"""Deterministic release builder for Phase 3 local QUL integration."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from pipelines.common.lock_inputs import _is_symlink_or_junction
from pipelines.toolchain.check_runtime import verify_runtime
from pipelines.validators.phase3_local import (
    PHASE3_DB_PACKS,
    PHASE3_EXPECTED_ARTIFACTS,
    PHASE3_RECITATION_PACKS,
)


def build_phase3_local(
    sources_root: Path | str,
    output_dir: Path | str,
) -> None:
    """Build all Phase 3 local artifacts deterministically into a fresh, empty output root."""
    verify_runtime()

    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    if not sources_root.is_dir() or _is_symlink_or_junction(sources_root):
        raise ValueError(f"sources_root does not exist or is a link: {sources_root}")

    if _is_symlink_or_junction(output_dir):
        raise ValueError(f"output_dir must not be a symlink or junction: {output_dir}")

    if output_dir.exists():
        if not output_dir.is_dir():
            raise ValueError(f"output_dir exists but is not a directory: {output_dir}")
        if any(output_dir.iterdir()):
            raise ValueError(f"output_dir must be empty: {output_dir}")
    else:
        output_dir.mkdir(parents=True, exist_ok=False)

    # 1. Quran Core
    from pipelines.builders.build_quran_core import build_quran_core
    build_quran_core(sources_root, output_dir)

    # 2. Quran Morphology
    from pipelines.builders.build_quran_morphology import build_quran_morphology
    build_quran_morphology(sources_root, output_dir)

    # 3. Quran WBW English
    from pipelines.builders.build_quran_wbw import build_quran_wbw
    build_quran_wbw(sources_root, output_dir)

    # 4. Six QUL Commentary Editions
    from pipelines.builders.build_quran_tafsir import build_quran_tafsir
    for pid in (
        "quran-tafsir-ar-ibn-kathir",
        "quran-tafsir-ar-tabari",
        "quran-tafsir-ar-qurtubi",
        "quran-tafsir-ar-wasit",
        "quran-tafsir-ar-razi",
        "quran-tafsir-en-ibn-kathir",
    ):
        build_quran_tafsir(sources_root, output_dir, pack_id=pid)

    # 5. Saheeh International Translation with Footnotes
    from pipelines.builders.build_quran_translation import build_quran_translation
    build_quran_translation(sources_root, output_dir, pack_id="quran-tr-en-sahih")

    # 6. Two Mushaf Layout and Tajweed Packs
    from pipelines.builders.build_quran_mushaf import build_quran_mushaf
    for pid in ("quran-mushaf-madani-v2", "quran-mushaf-indopak-15"):
        build_quran_mushaf(sources_root, output_dir, pack_id=pid)

    # 7. Seven Recitation Timing Manifests
    from pipelines.builders.build_recitation_manifests import build_recitation_manifests
    build_recitation_manifests(sources_root, output_dir)

    # Final verification of all 19 expected artifacts
    missing_artifacts = [
        fn for fn in sorted(PHASE3_EXPECTED_ARTIFACTS) if not (output_dir / fn).is_file()
    ]
    if missing_artifacts:
        raise RuntimeError(f"Missing expected artifacts after build: {missing_artifacts}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic Phase 3 local release orchestrator")
    parser.add_argument("--sources-root", required=True, type=Path, help="Path to sources root containing sources/ and raw-inputs/")
    parser.add_argument("--output-dir", required=True, type=Path, help="Path to fresh, empty output directory")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_phase3_local(args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building Phase 3 local packs: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
