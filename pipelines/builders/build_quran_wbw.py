"""Quran English word-by-word (WBW) pack builder CLI and pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata

from pipelines.common.qul_input import read_zip_strict_json
from pipelines.common.quran_pack import load_quran_pack, write_quran_pack
from pipelines.common.tanzil_coordinates import TanzilCoordinates, load_tanzil_coordinates
from pipelines.toolchain.check_runtime import verify_runtime

PACK_ID = "quran-wbw-en"
PACK_VERSION = "0.3.0"
EXPECTED_TOTAL_WORDS = 77429
EXPECTED_PATHS = frozenset({
    "qul/mushaf/qpc-hafs-tajweed.json.zip",
    "qul/wbw/english-wbw-translation.db.zip",
    "qul/wbw/english-wbw-translation.json.zip",
    "qul/wbw/english-wbw-transliteration.db.zip",
    "qul/wbw/english-wbw-transliteration.json.zip",
    "tanzil/quran-data-Tanzil.xml",
})

_COORD_RE = re.compile(r"^([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*)$")
_TAG_RE = re.compile(r"<(/?[^>]+)>")
_VALID_OPEN_RE = re.compile(
    r"^rule\s+class=(?:\"[a-z_]+\"|'[a-z_]+'|[a-z_]+)(?:\s+data-bs-original-title=\"\"\s+title=\"\")?$"
)
_MARKER_RE = re.compile(r"^\([0-9]+\)$")

SPECIAL_GAPS: dict[str, int] = {
    "2:181": 4,
    "8:6": 5,
    "13:37": 9,
}

LEXICAL_COMBINE_RULES: dict[str, tuple[int, tuple[str, str]]] = {
    "2:181": (2, ("بعد", "ما")),
    "8:6": (3, ("بعد", "ما")),
    "13:37": (7, ("بعد", "ما")),
    "37:130": (2, ("إل", "ياسين")),
}


def clean_tajweed_markup(text: str, vkey: str = "") -> str:
    """Validate balanced QPC Tajweed <rule> tags (depth <= 2) and strip delimiters."""
    depth = 0
    pos = 0
    result: list[str] = []
    for m in _TAG_RE.finditer(text):
        tag_content = m.group(1)
        tag_start, tag_end = m.span()
        result.append(text[pos:tag_start])
        pos = tag_end

        if tag_content == "/rule":
            depth -= 1
            if depth < 0:
                raise ValueError(f"Unbalanced closing tag </rule> in {vkey}")
        elif _VALID_OPEN_RE.match(tag_content):
            depth += 1
            if depth > 2:
                raise ValueError(f"Rule nesting exceeds maximum depth 2 in {vkey}: depth={depth}")
        else:
            raise ValueError(f"Invalid tag <{tag_content}> in {vkey}")

    result.append(text[pos:])
    if depth != 0:
        raise ValueError(f"Unbalanced tags at end of {vkey}: depth={depth}")

    cleaned = "".join(result)
    if "<" in cleaned or ">" in cleaned:
        raise ValueError(f"Remaining angle brackets in {vkey}")
    return cleaned


def _strip_tatweel_and_marks(s: str) -> str:
    """Remove tatweel and nonspacing/enclosing marks for base letter verification."""
    res: list[str] = []
    for c in s:
        if c == "\u0640":
            continue
        if unicodedata.category(c) in ("Mn", "Me"):
            continue
        res.append(c)
    return "".join(res)


def _parse_terminal_ayah_number(token: str) -> int | None:
    """Parse ASCII or Arabic-Indic terminal ayah number; return None if not pure digits."""
    d: list[str] = []
    for c in token:
        if "0" <= c <= "9":
            d.append(c)
        elif "\u0660" <= c <= "\u0669":
            d.append(str(ord(c) - ord("\u0660")))
        else:
            return None
    return int("".join(d)) if d else None


def _is_standalone_annotation(token: str) -> bool:
    """Return True if token consists solely of Quranic pause/annotation marks or joining controls."""
    if not token:
        return False
    return all(
        ord(c) in range(0x06D6, 0x06ED + 1) or c in ("\u200c", "\u200d")
        for c in token
    )


def load_qpc_hafs_tajweed_verses(
    zip_path: Path | str,
    expected_ayah_ids: dict[str, int] | list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load and strictly validate markup-preserving QPC Hafs Tajweed verse records."""
    data = read_zip_strict_json(zip_path)
    if not isinstance(data, dict):
        raise ValueError(f"Expected object in QPC Tajweed archive: {type(data)}")

    if expected_ayah_ids is not None:
        if isinstance(expected_ayah_ids, dict):
            expected_keys = set(expected_ayah_ids.keys())
        else:
            expected_keys = set(expected_ayah_ids)
        actual_keys = set(data.keys())
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)[:5]
            extra = sorted(actual_keys - expected_keys)[:5]
            raise ValueError(
                f"QPC Tajweed verse set mismatch: "
                f"missing={missing}, extra={extra} "
                f"(total expected={len(expected_keys)}, got={len(actual_keys)})"
            )

    result: dict[str, dict[str, Any]] = {}
    expected_fields = {"id", "verse_key", "surah", "ayah", "text"}

    for vkey, rec in data.items():
        if not isinstance(rec, dict):
            raise ValueError(f"Invalid record at {vkey}: {type(rec)}")
        if set(rec.keys()) != expected_fields:
            raise ValueError(f"QPC record fields mismatch in {vkey}: {set(rec.keys())} != {expected_fields}")

        aid = rec["id"]
        if not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0:
            raise ValueError(f"Invalid id in QPC record at {vkey}: {aid!r}")

        if isinstance(expected_ayah_ids, dict):
            expected_aid = expected_ayah_ids[vkey]
            if aid != expected_aid:
                raise ValueError(f"QPC id mismatch at {vkey}: got {aid}, expected {expected_aid}")

        if rec["verse_key"] != vkey:
            raise ValueError(f"QPC verse_key mismatch: record has {rec['verse_key']!r}, outer key is {vkey!r}")

        parts = vkey.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid verse key format in QPC data: {vkey!r}")
        try:
            s_num, a_num = int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError(f"Non-integer verse key in QPC data: {vkey!r}")
        if s_num <= 0 or a_num <= 0:
            raise ValueError(f"Non-positive verse coordinate in QPC data: {vkey!r}")
        if str(s_num) != parts[0] or str(a_num) != parts[1]:
            raise ValueError(
                f"Non-canonical verse key in QPC data (leading zeros or extra chars): {vkey!r}"
            )

        surah = rec["surah"]
        ayah = rec["ayah"]
        if not isinstance(surah, int) or isinstance(surah, bool) or surah != s_num:
            raise ValueError(f"Invalid integer type or mismatched surah in QPC record {vkey}: {surah!r} != {s_num}")
        if not isinstance(ayah, int) or isinstance(ayah, bool) or ayah != a_num:
            raise ValueError(f"Invalid integer type or mismatched ayah in QPC record {vkey}: {ayah!r} != {a_num}")

        text = rec["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Missing, blank, or non-string text in QPC record {vkey}: {text!r}")

        result[vkey] = rec

    return result


def load_qpc_hafs_tajweed_words(
    zip_path: Path | str,
    ayah_keys: list[str] | dict[str, int] | None = None,
) -> dict[str, list[str]]:
    """Load, strip rule tags, and split Arabic tokens from QPC Hafs Tajweed JSON archive."""
    verses = load_qpc_hafs_tajweed_verses(zip_path, expected_ayah_ids=ayah_keys)
    result: dict[str, list[str]] = {}

    for vkey, rec in verses.items():
        clean_text = clean_tajweed_markup(rec["text"], vkey)
        tokens = clean_text.split()
        if not tokens:
            raise ValueError(f"Empty tokens in {vkey}")

        a_num = rec["ayah"]

        # Omit terminal decimal token if and only if its integer value == ayah number
        last_parsed = _parse_terminal_ayah_number(tokens[-1])
        if last_parsed is not None:
            if last_parsed != a_num:
                raise ValueError(
                    f"Terminal decimal mismatch in {vkey}: parsed {last_parsed} != ayah {a_num}"
                )
            tokens = tokens[:-1]

        # Reject any decimal lexical leaks in remaining tokens
        for t in tokens:
            if _parse_terminal_ayah_number(t) is not None:
                raise ValueError(f"Decimal lexical leak in {vkey}: token {t!r}")

        # Omit standalone tokens composed solely of Quranic pause/annotation marks or joining controls
        tokens = [t for t in tokens if not _is_standalone_annotation(t)]
        if not tokens:
            raise ValueError(f"No tokens remaining in {vkey} after removing annotations")

        # Apply audited lexical rules
        if vkey in LEXICAL_COMBINE_RULES:
            idx, (exp_b1, exp_b2) = LEXICAL_COMBINE_RULES[vkey]
            if len(tokens) < idx + 2:
                raise ValueError(f"Insufficient tokens in {vkey}: {len(tokens)}")
            b1 = _strip_tatweel_and_marks(tokens[idx])
            b2 = _strip_tatweel_and_marks(tokens[idx + 1])
            if b1 != exp_b1 or b2 != exp_b2:
                raise ValueError(
                    f"Lexical rule {vkey} failed base letter check: {b1!r}, {b2!r} != {exp_b1!r}, {exp_b2!r}"
                )
            tokens = tokens[:idx] + [f"{tokens[idx]} {tokens[idx + 1]}"] + tokens[idx + 2:]

        result[vkey] = tokens

    return result


def load_qul_wbw_meanings(
    zip_path: Path | str,
    ayah_keys: list[str] | set[str] | None = None,
) -> dict[str, list[tuple[int, str]]]:
    """Load and validate English word-by-word meanings from QUL JSON archive."""
    data = read_zip_strict_json(zip_path)
    if not isinstance(data, dict):
        raise ValueError(f"Expected object in WBW translation archive: {type(data)}")

    by_ayah: dict[str, dict[int, str]] = {}
    for key_str, gloss in data.items():
        if not isinstance(key_str, str) or not isinstance(gloss, str):
            raise ValueError(f"Non-string key or value in WBW meanings: {(key_str, gloss)!r}")
        if not gloss.strip():
            raise ValueError(f"Blank gloss in WBW meanings at {key_str}: {gloss!r}")

        m = _COORD_RE.match(key_str)
        if not m:
            raise ValueError(f"Noncanonical coordinate format in WBW meanings: {key_str!r}")

        s_num, a_num, w_num = int(m.group(1)), int(m.group(2)), int(m.group(3))
        vkey = f"{s_num}:{a_num}"
        pos_dict = by_ayah.setdefault(vkey, {})
        if w_num in pos_dict:
            raise ValueError(f"Duplicate coordinate in WBW meanings: {key_str!r}")
        pos_dict[w_num] = gloss

    if ayah_keys is not None:
        expected_set = set(ayah_keys)
        actual_set = set(by_ayah.keys())
        if actual_set != expected_set:
            missing = sorted(expected_set - actual_set)[:5]
            extra = sorted(actual_set - expected_set)[:5]
            raise ValueError(
                f"WBW meanings verse set mismatch: "
                f"missing={missing}, extra={extra} "
                f"(total expected={len(expected_set)}, got={len(actual_set)})"
            )
        keys_to_process = list(ayah_keys)
    else:
        keys_to_process = list(by_ayah.keys())

    result: dict[str, list[tuple[int, str]]] = {}

    for vkey in keys_to_process:
        pos_dict = by_ayah[vkey]
        positions = sorted(pos_dict.keys())
        if len(positions) < 2:
            raise ValueError(f"Too few positions in {vkey}: {positions}")

        s_num, a_num = map(int, vkey.split(":"))

        last_pos = positions[-1]
        last_val = pos_dict[last_pos]
        expected_marker = f"({a_num})"
        if last_val != expected_marker:
            raise ValueError(
                f"Missing or invalid terminal ayah marker at {vkey}:{last_pos}: "
                f"expected {expected_marker!r}, got {last_val!r}"
            )

        lexical_positions = positions[:-1]
        n_lexical = len(lexical_positions)
        omitted = SPECIAL_GAPS.get(vkey)

        if omitted is not None:
            expected_lex = [p for p in range(1, n_lexical + 2) if p != omitted]
            expected_marker_pos = n_lexical + 2
        else:
            expected_lex = list(range(1, n_lexical + 1))
            expected_marker_pos = n_lexical + 1

        if last_pos != expected_marker_pos:
            raise ValueError(
                f"Word positions in {vkey}: terminal marker position mismatch: got {last_pos}, expected {expected_marker_pos}"
            )
        if lexical_positions != expected_lex:
            raise ValueError(
                f"Word positions in {vkey} do not match expected lexical positions: "
                f"got {lexical_positions}, expected {expected_lex}"
            )

        for p in lexical_positions:
            val = pos_dict[p]
            if _MARKER_RE.match(val):
                raise ValueError(f"Marker-looking nonlexical row in {vkey}:{p}: {val!r}")

        result[vkey] = [(new_w, pos_dict[p]) for new_w, p in enumerate(expected_lex, start=1)]

    return result


def load_qul_wbw_pronunciations(
    zip_path: Path | str,
    ayah_keys: list[str] | set[str] | dict[str, int] | None = None,
    *,
    expected_counts: dict[str, int] | None = None,
) -> dict[str, dict[int, str | None]]:
    """Load and map English transliterations to canonical word numbers."""
    data = read_zip_strict_json(zip_path)
    if not isinstance(data, dict):
        raise ValueError(f"Expected object in WBW transliteration archive: {type(data)}")

    raw_by_ayah: dict[str, dict[int, str]] = {}
    for key_str, pron in data.items():
        if not isinstance(key_str, str) or not isinstance(pron, str):
            raise ValueError(f"Non-string key or value in WBW transliteration: {(key_str, pron)!r}")
        if not pron.strip():
            raise ValueError(f"Blank pronunciation in WBW transliteration at {key_str}: {pron!r}")

        m = _COORD_RE.match(key_str)
        if not m:
            raise ValueError(f"Noncanonical coordinate in WBW transliteration: {key_str!r}")

        s_num, a_num, w_num = int(m.group(1)), int(m.group(2)), int(m.group(3))
        vkey = f"{s_num}:{a_num}"
        pos_dict = raw_by_ayah.setdefault(vkey, {})
        if w_num in pos_dict:
            raise ValueError(f"Duplicate coordinate in WBW transliteration: {key_str!r}")
        pos_dict[w_num] = pron

    if isinstance(ayah_keys, dict) and expected_counts is None:
        expected_counts = ayah_keys
        target_keys = list(ayah_keys.keys())
    elif ayah_keys is not None:
        target_keys = list(ayah_keys)
    else:
        target_keys = list(raw_by_ayah.keys())

    actual_set = set(raw_by_ayah.keys())
    expected_set = set(target_keys)
    if actual_set != expected_set:
        missing = sorted(expected_set - actual_set)[:5]
        extra = sorted(actual_set - expected_set)[:5]
        raise ValueError(
            f"WBW transliteration verse set mismatch: "
            f"missing={missing}, extra={extra} "
            f"(total expected={len(expected_set)}, got={len(actual_set)})"
        )

    result: dict[str, dict[int, str | None]] = {}

    for vkey in target_keys:
        raw_dict = raw_by_ayah[vkey]
        if expected_counts and vkey in expected_counts:
            n_words = expected_counts[vkey]
        elif vkey == "2:181":
            n_words = 13
        elif vkey == "8:6":
            n_words = 11
        elif vkey == "13:37":
            n_words = 19
        elif vkey == "36:52":
            n_words = 12
        else:
            n_words = len(raw_dict)

        if vkey == "2:181":
            exp_raw = {1, 2, 3, 5} | {j + 2 for j in range(5, n_words + 1)}
            if set(raw_dict.keys()) != exp_raw:
                raise ValueError(f"Pronunciation raw keys mismatch in 2:181: {set(raw_dict.keys())} != {exp_raw}")
            if raw_dict[5] != raw_dict[3]:
                raise ValueError("Expected duplicate pronunciation slot 5 to match slot 3 in 2:181")
            canon_map: dict[int, str | None] = {1: raw_dict[1], 2: raw_dict[2], 3: raw_dict[3], 4: None}
            for j in range(5, n_words + 1):
                canon_map[j] = raw_dict[j + 2]
        elif vkey == "8:6":
            exp_raw = {1, 2, 3, 4, 6} | {j + 2 for j in range(5, 11)}
            if set(raw_dict.keys()) != exp_raw:
                raise ValueError(f"Pronunciation raw keys mismatch in 8:6: {set(raw_dict.keys())} != {exp_raw}")
            if raw_dict[6] != raw_dict[4]:
                raise ValueError("Expected duplicate pronunciation slot 6 to match slot 4 in 8:6")
            canon_map = {1: raw_dict[1], 2: raw_dict[2], 3: raw_dict[3], 4: raw_dict[4]}
            for j in range(5, 11):
                canon_map[j] = raw_dict[j + 2]
            canon_map[11] = None
        elif vkey == "13:37":
            exp_raw = set(range(1, 9)) | {j + 1 for j in range(9, n_words + 1)}
            if set(raw_dict.keys()) != exp_raw:
                raise ValueError(f"Pronunciation raw keys mismatch in 13:37: {set(raw_dict.keys())} != {exp_raw}")
            canon_map = {j: raw_dict[j] for j in range(1, 9)}
            for j in range(9, n_words + 1):
                canon_map[j] = raw_dict[j + 1]
        elif vkey == "36:52":
            exp_raw = set(range(1, n_words + 1)) - {7}
            if set(raw_dict.keys()) != exp_raw:
                raise ValueError(f"Pronunciation raw keys mismatch in 36:52: {set(raw_dict.keys())} != {exp_raw}")
            canon_map = {j: raw_dict[j] for j in range(1, 7)}
            canon_map[7] = None
            for j in range(8, n_words + 1):
                canon_map[j] = raw_dict[j]
        else:
            exp_raw = set(range(1, n_words + 1))
            if set(raw_dict.keys()) != exp_raw:
                raise ValueError(
                    f"Pronunciation raw keys mismatch in {vkey}: got {sorted(raw_dict.keys())}, expected {sorted(exp_raw)}"
                )
            canon_map = {j: raw_dict[j] for j in range(1, n_words + 1)}

        result[vkey] = canon_map

    return result


def write_wbw_pack(
    output_path: Path,
    schema_path: Path,
    metadata: dict[str, str],
    surah_ayah_sequence: list[tuple[int, int, int]],
    ar_words_by_ayah: dict[str, list[str]],
    meanings_by_ayah: dict[str, list[tuple[int, str]]],
    pron_by_ayah: dict[str, dict[int, str | None]],
    expected_total_words: int = EXPECTED_TOTAL_WORDS,
) -> None:
    """Write verified WBW pack SQLite database with integrity and FTS indexing."""
    total_words = 0
    rekeyed_ayahs: list[str] = []
    raw_deltas: dict[str, tuple[int, str]] = {}
    for s_id, a_num, _aid in surah_ayah_sequence:
        vkey = f"{s_id}:{a_num}"
        ar_words = ar_words_by_ayah[vkey]
        glosses = meanings_by_ayah[vkey]
        if len(ar_words) != len(glosses):
            raise ValueError(
                f"Word count mismatch at {vkey}: Arabic tokens {len(ar_words)} != English glosses {len(glosses)}"
            )
        total_words += len(ar_words)
        if vkey in SPECIAL_GAPS:
            rekeyed_ayahs.append(vkey)
        n_norm = len(glosses)
        raw_deltas[f"alignment_raw_delta:{vkey}"] = (
            1,
            f"QUL raw={n_norm + 1}; normalized={n_norm}; delta=-1",
        )

    if total_words != expected_total_words:
        raise ValueError(
            f"Total word count mismatch: got {total_words}, expected {expected_total_words}"
        )

    coverage: dict[str, tuple[int, str | None]] = {
        "expected_total": (expected_total_words, None),
        "ar_present": (expected_total_words, None),
        "en_present": (expected_total_words, None),
        "en_missing": (0, None),
        "alignment_markers_removed": (len(surah_ayah_sequence), None),
        "alignment_rekeyed_ayahs": (
            len(rekeyed_ayahs),
            ",".join(rekeyed_ayahs) if rekeyed_ayahs else None,
        ),
    }
    coverage.update(raw_deltas)

    with write_quran_pack(
        output_path,
        schema_path,
        metadata,
        coverage,
        fts_tables=("word_translations_fts",),
    ) as conn:
        def _word_translation_param_stream():
            current_id = 0
            for s_id, a_num, aid in surah_ayah_sequence:
                vkey = f"{s_id}:{a_num}"
                ar_words = ar_words_by_ayah[vkey]
                gloss_entries = meanings_by_ayah[vkey]
                pron_map = pron_by_ayah[vkey]

                for (w_num, gloss), ar_text in zip(gloss_entries, ar_words, strict=True):
                    current_id += 1
                    word_key = f"{vkey}:{w_num}"
                    translit = pron_map.get(w_num)
                    yield (
                        current_id,
                        aid,
                        w_num,
                        word_key,
                        ar_text,
                        gloss,
                        translit,
                    )

        conn.executemany(
            "INSERT INTO word_translations ("
            "id, ayah_id, word_number, word_key, text_uthmani, translation, transliteration"
            ") VALUES (?, ?, ?, ?, ?, ?, ?);",
            _word_translation_param_stream(),
        )


def build_quran_wbw(sources_root: Path, output_dir: Path) -> None:
    """Build quran-wbw-en.db deterministically from locked QUL inputs."""
    verify_runtime()

    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    raw_root, metadata = load_quran_pack(
        sources_root,
        PACK_ID,
        EXPECTED_PATHS,
        fts_enabled=True,
        pack_version=PACK_VERSION,
        completeness="complete",
    )

    tanzil_xml = raw_root / "tanzil" / "quran-data-Tanzil.xml"
    coords = load_tanzil_coordinates(tanzil_xml)

    qpc_zip = raw_root / "qul" / "mushaf" / "qpc-hafs-tajweed.json.zip"
    meanings_zip = raw_root / "qul" / "wbw" / "english-wbw-translation.json.zip"
    pron_zip = raw_root / "qul" / "wbw" / "english-wbw-transliteration.json.zip"

    all_ayah_keys: list[str] = []
    surah_ayah_sequence: list[tuple[int, int, int]] = []
    for s in coords.surahs:
        for a in range(1, s.ayah_count + 1):
            vkey = f"{s.id}:{a}"
            all_ayah_keys.append(vkey)
            surah_ayah_sequence.append((s.id, a, coords.ayah_ids[vkey]))

    ar_words_by_ayah = load_qpc_hafs_tajweed_words(qpc_zip, coords.ayah_ids)
    meanings_by_ayah = load_qul_wbw_meanings(meanings_zip, all_ayah_keys)
    gloss_counts = {vkey: len(meanings_by_ayah[vkey]) for vkey in all_ayah_keys}
    pron_by_ayah = load_qul_wbw_pronunciations(pron_zip, all_ayah_keys, expected_counts=gloss_counts)

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "sql" / "quran_wbw.v1.sql"
    output_path = output_dir / "quran-wbw-en.db"

    write_wbw_pack(
        output_path,
        schema_path,
        metadata,
        surah_ayah_sequence,
        ar_words_by_ayah,
        meanings_by_ayah,
        pron_by_ayah,
        expected_total_words=EXPECTED_TOTAL_WORDS,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build quran-wbw-en SQLite pack.")
    parser.add_argument(
        "--sources-root",
        required=True,
        type=Path,
        help="Root directory containing raw-inputs, sources/registry.json, and sources/licenses",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Output directory to place quran-wbw-en.db",
    )

    args = parser.parse_args(argv)

    if not args.sources_root.is_dir():
        print(f"Error: Sources root does not exist: {args.sources_root}", file=sys.stderr)
        return 2
    if not args.output_dir.is_dir():
        print(f"Error: Output directory does not exist: {args.output_dir}", file=sys.stderr)
        return 2

    try:
        build_quran_wbw(args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        print(f"Error: Failed to build quran-wbw-en: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
