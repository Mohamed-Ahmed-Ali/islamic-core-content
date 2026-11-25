"""Quran translation pack builder CLI and pipeline."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
from typing import Any
from pipelines.common.qul_input import read_zip_strict_json
from pipelines.common.quran_pack import load_quran_pack, write_quran_pack
from pipelines.common.tanzil_coordinates import (
    load_tanzil_coordinates,
    load_tanzil_text,
)
from pipelines.toolchain.check_runtime import verify_runtime

DEFAULT_PACK_ID = "quran-tr-en-pickthall"
PACK_ID = DEFAULT_PACK_ID

SUPPORTED_TRANSLATION_PACKS: dict[str, dict[str, Any]] = {
    "quran-tr-en-pickthall": {
        "kind": "tanzil",
        "source_path": "tanzil/en.pickthall.xml",
        "expected_paths": frozenset({"tanzil/quran-data-Tanzil.xml", "tanzil/en.pickthall.xml"}),
        "schema_file": "quran_translation.v1.sql",
    },
    "quran-tr-en-sahih": {
        "kind": "qul",
        "source_path": "qul/translation/en-sahih-international-with-footnote-tags.json.zip",
        "expected_paths": frozenset({
            "tanzil/quran-data-Tanzil.xml",
            "qul/translation/en-sahih-international-with-footnote-tags.json.zip",
        }),
        "schema_file": "quran_translation_sahih.v1.sql",
    },
}
SUPPORTED_PACK_IDS: tuple[str, ...] = tuple(SUPPORTED_TRANSLATION_PACKS.keys())
EXPECTED_PATHS = SUPPORTED_TRANSLATION_PACKS[DEFAULT_PACK_ID]["expected_paths"]
_POSITIVE_DECIMAL_KEY_RE = re.compile(r"^[1-9][0-9]*\Z")


class SahihVerseHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self._in_sup: bool = False
        self._text_search_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "sup":
            if self._in_sup:
                raise ValueError("Nested <sup> tags are not allowed in Saheeh translation HTML")
            attrs_dict = dict(attrs)
            if "foot_note" not in attrs_dict:
                raise ValueError("<sup> tag missing required 'foot_note' attribute")
            fn_id = attrs_dict["foot_note"]
            if fn_id is None or not _POSITIVE_DECIMAL_KEY_RE.fullmatch(fn_id):
                raise ValueError(f"Invalid footnote ID in <sup> tag: {fn_id!r}")
            self.refs.append(fn_id)
            self._in_sup = True
        else:
            raise ValueError(f"Unexpected HTML tag in Saheeh translation verse: <{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "sup":
            if not self._in_sup:
                raise ValueError("Closing </sup> tag without matching opening tag")
            self._in_sup = False
        else:
            raise ValueError(f"Unexpected closing HTML tag: </{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._in_sup:
            self._text_search_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._in_sup:
            raise ValueError("Unclosed <sup> tag at end of verse HTML")

    @property
    def text_search(self) -> str:
        return "".join(self._text_search_parts)


@dataclass(frozen=True)
class SahihTranslationPayload:
    translations_text: dict[str, str]
    translations_text_search: dict[str, str]
    footnotes: list[tuple[str, int, str]]
    blank_footnotes: tuple[str, ...] = ()
def parse_sahih_translation_data(
    data: Any,
    expected_verse_keys: set[str],
) -> SahihTranslationPayload:
    """Validate Saheeh JSON payload and return parsed texts, search projection, and footnotes."""
    if not isinstance(data, dict):
        raise ValueError(f"Expected Saheeh JSON to be an object/dict, got {type(data).__name__}")

    actual_keys = set(data.keys())
    if actual_keys != expected_verse_keys:
        missing = sorted(expected_verse_keys - actual_keys)
        extra = sorted(actual_keys - expected_verse_keys)
        raise ValueError(
            f"Saheeh verse keys do not match expected Tanzil verse keys. Missing: {missing[:5]}, Extra: {extra[:5]}"
        )

    translations_text: dict[str, str] = {}
    translations_text_search: dict[str, str] = {}
    footnotes: list[tuple[str, int, str]] = []
    blank_footnotes: list[str] = []
    for vkey, val in data.items():
        if not isinstance(val, dict):
            raise ValueError(f"Verse {vkey!r} value must be a dict, got {type(val).__name__}")
        if set(val.keys()) != {"t", "f"}:
            raise ValueError(f"Verse {vkey!r} object must contain exact keys {{'t', 'f'}}, got {sorted(val.keys())}")

        t = val["t"]
        if not isinstance(t, str) or not t.strip():
            raise ValueError(f"Verse {vkey!r} key 't' must be a nonblank string, got {t!r}")

        f = val["f"]
        if not isinstance(f, dict):
            raise ValueError(f"Verse {vkey!r} key 'f' must be a dict/object, got {type(f).__name__}")

        parser = SahihVerseHtmlParser()
        parser.feed(t)
        parser.close()

        if len(parser.refs) != len(set(parser.refs)):
            raise ValueError(f"Verse {vkey!r} contains duplicate footnote references: {parser.refs}")

        verse_footnotes: dict[str, str] = {}
        for fk, fv in f.items():
            if not isinstance(fk, str) or not _POSITIVE_DECIMAL_KEY_RE.fullmatch(fk):
                raise ValueError(
                    f"Verse {vkey!r} footnote key {fk!r} is not a positive canonical decimal string"
                )
            if not isinstance(fv, str):
                raise ValueError(
                    f"Verse {vkey!r} footnote value for key {fk!r} must be a string, got {type(fv).__name__}"
                )
            if not fv:
                if vkey == "7:143" and fk == "226798":
                    blank_footnotes.append(f"{vkey}:{fk}")
                else:
                    raise ValueError(
                        f"Verse {vkey!r} footnote {fk!r} has blank body, which is only allowed at verse 7:143 footnote 226798"
                    )
            elif not fv.strip():
                raise ValueError(
                    f"Verse {vkey!r} footnote {fk!r} has whitespace-only body"
                )
            verse_footnotes[fk] = fv

        ref_set = set(parser.refs)
        f_keys_set = set(f.keys())
        if ref_set != f_keys_set:
            missing_refs = sorted(ref_set - f_keys_set)
            missing_bodies = sorted(f_keys_set - ref_set)
            raise ValueError(
                f"Footnote reference and body mismatch in verse {vkey!r}. "
                f"References without body: {missing_refs}, bodies without reference: {missing_bodies}"
            )
        translations_text[vkey] = t
        translations_text_search[vkey] = parser.text_search

        for ref_id in parser.refs:
            footnotes.append((vkey, int(ref_id), verse_footnotes[ref_id]))

    return SahihTranslationPayload(
        translations_text=translations_text,
        translations_text_search=translations_text_search,
        footnotes=footnotes,
        blank_footnotes=tuple(blank_footnotes),
    )

def build_quran_translation(
    sources_root: Path,
    output_dir: Path,
    *,
    pack_id: str = DEFAULT_PACK_ID,
) -> None:
    """Build selected translation SQLite pack deterministically."""
    verify_runtime()

    if pack_id not in SUPPORTED_TRANSLATION_PACKS:
        raise ValueError(f"Unknown or unsupported translation pack ID: {pack_id!r}")

    cfg = SUPPORTED_TRANSLATION_PACKS[pack_id]
    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    raw_root, metadata = load_quran_pack(
        sources_root,
        pack_id,
        cfg["expected_paths"],
        fts_enabled=True,
    )

    meta_file = raw_root / "tanzil" / "quran-data-Tanzil.xml"
    coords = load_tanzil_coordinates(meta_file)

    sahih_payload: SahihTranslationPayload | None = None
    if cfg["kind"] == "tanzil":
        pickthall_file = raw_root / cfg["source_path"]
        translations_text = load_tanzil_text(
            pickthall_file,
            coords,
            locked_path=cfg["source_path"],
        )
    elif cfg["kind"] == "qul":
        zip_path = raw_root / cfg["source_path"]
        data = read_zip_strict_json(zip_path)
        sahih_payload = parse_sahih_translation_data(data, set(coords.ayah_ids.keys()))
        translations_text = sahih_payload.translations_text
    else:
        raise ValueError(f"Unknown pack kind: {cfg['kind']!r}")

    coverage: dict[str, tuple[int, str | None]] = {
        "expected_total": (6236, None),
        "en_present": (6236, None),
        "en_missing": (0, None),
    }
    if pack_id == "quran-tr-en-sahih":
        assert sahih_payload is not None
        if sahih_payload.blank_footnotes:
            coverage["source_blank_footnotes"] = (
                len(sahih_payload.blank_footnotes),
                ",".join(sahih_payload.blank_footnotes),
            )

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "sql" / cfg["schema_file"]
    output_path = output_dir / f"{pack_id}.db"

    with write_quran_pack(
        output_path,
        schema_path,
        metadata,
        coverage,
        fts_tables=("translations_fts",),
    ) as conn:
        if pack_id == "quran-tr-en-sahih":
            assert sahih_payload is not None
            def _sahih_param_stream():
                for s in coords.surahs:
                    for aya_num in range(1, s.ayah_count + 1):
                        vkey = f"{s.id}:{aya_num}"
                        aid = coords.ayah_ids[vkey]
                        yield (
                            aid,
                            s.id,
                            aya_num,
                            vkey,
                            sahih_payload.translations_text[vkey],
                            sahih_payload.translations_text_search[vkey],
                        )

            conn.executemany(
                "INSERT INTO translations ("
                "ayah_id, surah_id, ayah_number, verse_key, text, text_search"
                ") VALUES (?, ?, ?, ?, ?, ?);",
                _sahih_param_stream(),
            )

            sorted_footnote_params = [
                (coords.ayah_ids[vkey], fn_id, fn_text)
                for vkey, fn_id, fn_text in sahih_payload.footnotes
            ]
            sorted_footnote_params.sort(key=lambda r: (r[0], r[1]))

            conn.executemany(
                "INSERT INTO translation_footnotes ("
                "ayah_id, footnote_id, text"
                ") VALUES (?, ?, ?);",
                sorted_footnote_params,
            )
        else:
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
    parser = argparse.ArgumentParser(description="Build Quran translation SQLite pack.")
    parser.add_argument("--sources-root", required=True, type=Path, help="Root directory containing sources/ and raw-inputs/")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory where <pack-id>.db will be written")
    parser.add_argument(
        "--pack",
        default=DEFAULT_PACK_ID,
        choices=SUPPORTED_PACK_IDS,
        help=f"Translation pack ID to build (default: {DEFAULT_PACK_ID})",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_quran_translation(args.sources_root, args.output_dir, pack_id=args.pack)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building {args.pack}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
