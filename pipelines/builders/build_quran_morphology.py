"""Quran morphology pack builder CLI and pipeline."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping
import zipfile

from pipelines.common.lock_inputs import (
    LockEntry,
    canonical_entries_json,
    parse_strict_json,
    validate_https_url,
    validate_safe_relative_path,
    verify_exact_file_on_disk,
    verify_lock,
)
from pipelines.common.search_text import normalize_arabic
from pipelines.common.sqlite_utils import (
    finalize_pack,
    open_pack,
    write_pack_metadata,
)
from pipelines.common.tanzil_coordinates import load_tanzil_coordinates
from pipelines.toolchain.check_runtime import verify_runtime

PACK_ID = "quran-morphology"
EXPECTED_PATHS = frozenset({
    "corpus/quranic-corpus-morphology-0.4.zip",
    "tanzil/quran-data-Tanzil.xml",
})

_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")

_VALID_PERMITTED_USES = frozenset({
    "public_redistribution",
    "commercial_use",
    "sqlite_conversion",
    "search_projection",
    "transcode",
    "clip",
    "fade",
    "loudness_normalization",
})

# Literal ASCII-to-Arabic transliteration pairs from §6.5
_TRANSLITERATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("'", "\u0621"),
    (">", "\u0623"),
    ("&", "\u0624"),
    ("<", "\u0625"),
    ("}", "\u0626"),
    ("A", "\u0627"),
    ("b", "\u0628"),
    ("p", "\u0629"),
    ("t", "\u062A"),
    ("v", "\u062B"),
    ("j", "\u062C"),
    ("H", "\u062D"),
    ("x", "\u062E"),
    ("d", "\u062F"),
    ("*", "\u0630"),
    ("r", "\u0631"),
    ("z", "\u0632"),
    ("s", "\u0633"),
    ("$", "\u0634"),
    ("S", "\u0635"),
    ("D", "\u0636"),
    ("T", "\u0637"),
    ("Z", "\u0638"),
    ("E", "\u0639"),
    ("g", "\u063A"),
    ("_", "\u0640"),
    ("f", "\u0641"),
    ("q", "\u0642"),
    ("k", "\u0643"),
    ("l", "\u0644"),
    ("m", "\u0645"),
    ("n", "\u0646"),
    ("h", "\u0647"),
    ("w", "\u0648"),
    ("Y", "\u0649"),
    ("y", "\u064A"),
    ("F", "\u064B"),
    ("N", "\u064C"),
    ("K", "\u064D"),
    ("a", "\u064E"),
    ("u", "\u064F"),
    ("i", "\u0650"),
    ("~", "\u0651"),
    ("o", "\u0652"),
    ("^", "\u0653"),
    ("#", "\u0654"),
    ("`", "\u0670"),
    ("{", "\u0671"),
    (":", "\u06DC"),
    ("@", "\u06DF"),
    ('"', "\u06E0"),
    ("[", "\u06E2"),
    (";", "\u06E3"),
    (",", "\u06E5"),
    (".", "\u06E6"),
    ("!", "\u06E8"),
    ("-", "\u06EA"),
    ("+", "\u06EB"),
    ("%", "\u06EC"),
    ("]", "\u06ED"),
    (" ", " "),
)

_TRANSLITERATION_MAP: dict[str, str] = dict(_TRANSLITERATION_PAIRS)

_ROMAN_FORMS: dict[int, str] = {
    1: "I",
    2: "II",
    3: "III",
    4: "IV",
    5: "V",
    6: "VI",
    7: "VII",
    8: "VIII",
    9: "IX",
    10: "X",
    11: "XI",
    12: "XII",
}

_FORM_ROMAN_TO_INT: dict[str, int] = {
    f"({roman})": num for num, roman in _ROMAN_FORMS.items()
}

_VALID_PERSON_REGEX = re.compile(r"^(1S|1P|2D|[23][FM][DPS])$")
_VALID_TENSES = frozenset({"PERF", "IMPF", "IMPV"})
_VALID_VOICES = frozenset({"ACT", "PASS"})
_VALID_MOODS = frozenset({"MOOD:JUS", "MOOD:SUBJ"})
_VALID_SPECIALS = frozenset({"SP:kaAd", "SP:kaAn"})



def transliterate_buckwalter(text: str, *, is_lemma: bool = False) -> str:
    """Transliterate Buckwalter ASCII string to Arabic according to §6.5 table."""
    if is_lemma and text.endswith("2"):
        text = text[:-1]
    res: list[str] = []
    for ch in text:
        if ch in _TRANSLITERATION_MAP:
            res.append(_TRANSLITERATION_MAP[ch])
        else:
            raise ValueError(f"Unmapped Buckwalter character: {ch!r} in {text!r}")
    return "".join(res)


@dataclass(frozen=True)
class CorpusSegment:
    surah: int
    ayah: int
    word: int
    segment: int
    form_buckwalter: str
    form_ar: str
    pos_tag: str
    features_raw: str
    role: str


def _parse_verb_features(features_raw: str, tag: str) -> tuple[str, str, int, str, str]:
    """Parse verb features according to §6.5 closed grammar.

    Returns: (root_buckwalter, lemma_buckwalter, verb_form, verb_tense, verb_voice)
    """
    tokens = features_raw.split("|")
    if len(tokens) < 2 or tokens[0] != "STEM" or tokens[1] != "POS:V":
        raise ValueError(f"Verb stem must start with STEM|POS:V, got {features_raw!r}")

    lemmas: list[str] = []
    roots: list[str] = []
    tenses: list[str] = []
    forms: list[int] = []
    voices: list[str] = []
    persons: list[str] = []
    moods: list[str] = []
    specials: list[str] = []

    for tok in tokens[2:]:
        if not tok:
            raise ValueError(f"Empty token in verb features: {features_raw!r}")
        if tok.startswith("LEM:"):
            lem_val = tok[4:]
            if not lem_val:
                raise ValueError(f"Empty LEM value in {features_raw!r}")
            lemmas.append(lem_val)
        elif tok.startswith("ROOT:"):
            root_val = tok[5:]
            if not root_val:
                raise ValueError(f"Empty ROOT value in {features_raw!r}")
            roots.append(root_val)
        elif tok in _VALID_TENSES:
            tenses.append(tok)
        elif tok in _FORM_ROMAN_TO_INT:
            forms.append(_FORM_ROMAN_TO_INT[tok])
        elif tok in _VALID_VOICES:
            voices.append(tok)
        elif _VALID_PERSON_REGEX.match(tok):
            persons.append(tok)
        elif tok in _VALID_MOODS:
            moods.append(tok)
        elif tok in _VALID_SPECIALS:
            specials.append(tok)
        else:
            raise ValueError(f"Unknown or out-of-grammar verb token {tok!r} in {features_raw!r}")

    if len(lemmas) != 1:
        raise ValueError(f"Verbs require exactly one LEM:..., found {len(lemmas)} in {features_raw!r}")
    if len(roots) != 1:
        raise ValueError(f"Verbs require exactly one ROOT:..., found {len(roots)} in {features_raw!r}")
    if len(tenses) != 1:
        raise ValueError(f"Verbs require exactly one tense, found {len(tenses)} in {features_raw!r}")
    if len(forms) > 1:
        raise ValueError(f"Duplicate form tokens in {features_raw!r}")
    if len(voices) > 1:
        raise ValueError(f"Duplicate voice tokens in {features_raw!r}")
    if len(persons) > 1:
        raise ValueError(f"Duplicate person tokens in {features_raw!r}")
    if len(moods) > 1:
        raise ValueError(f"Duplicate mood tokens in {features_raw!r}")
    if len(specials) > 1:
        raise ValueError(f"Duplicate special tokens in {features_raw!r}")

    root_val = roots[0]
    lem_val = lemmas[0]
    tense_val = tenses[0]
    form_val = forms[0] if forms else 1
    voice_val = voices[0] if voices else "ACT"

    # Validate characters in root and lemma
    transliterate_buckwalter(root_val)
    transliterate_buckwalter(lem_val, is_lemma=True)

    return root_val, lem_val, form_val, tense_val, voice_val


def _parse_nonverb_stem(features_raw: str, tag: str) -> tuple[str | None, str | None]:
    """Parse non-verb stem features.

    Returns: (root_buckwalter | None, lemma_buckwalter | None)
    """
    tokens = features_raw.split("|")
    lemmas: list[str] = []
    roots: list[str] = []

    for tok in tokens[1:]:
        if tok.startswith("POS:"):
            pos_val = tok[4:]
            if pos_val != tag:
                raise ValueError(f"Stem POS:{pos_val} does not agree with TAG={tag} in {features_raw!r}")
        elif tok.startswith("LEM:"):
            lem_val = tok[4:]
            if not lem_val:
                raise ValueError(f"Empty LEM: in {features_raw!r}")
            lemmas.append(lem_val)
        elif tok.startswith("ROOT:"):
            root_val = tok[5:]
            if not root_val:
                raise ValueError(f"Empty ROOT: in {features_raw!r}")
            roots.append(root_val)

    if len(roots) > 1:
        raise ValueError(f"Duplicate ROOT in stem features: {features_raw!r}")
    if len(lemmas) > 1:
        raise ValueError(f"Duplicate LEM in stem features: {features_raw!r}")

    root_val = roots[0] if roots else None
    lem_val = lemmas[0] if lemmas else None

    if root_val is not None:
        transliterate_buckwalter(root_val)
    if lem_val is not None:
        transliterate_buckwalter(lem_val, is_lemma=True)

    return root_val, lem_val

def read_corpus_segments(path: Path, ayah_ids: Mapping[str, int]) -> tuple[CorpusSegment, ...]:
    """Parse and validate Quranic Arabic Corpus morphology segments from ZIP member.

    Consumes raw member without external extraction, verifies coordinates against
    Tanzil ayah_ids, checks for gaps/duplicates, and returns parsed segments in
    numeric coordinate order.
    """
    path = Path(path)
    if not path.is_file() or os.path.islink(path):
        raise ValueError(f"Corpus path must be a regular file: {path}")

    with zipfile.ZipFile(path, "r") as zf:
        infolist = zf.infolist()
        if len(infolist) != 1:
            raise ValueError(f"Expected exactly 1 regular member in {path}, got {len(infolist)}")
        info = infolist[0]
        expected_member = "quranic-corpus-morphology-0.4.txt"
        if info.filename != expected_member or getattr(info, "orig_filename", info.filename) != expected_member:
            raise ValueError(f"Unexpected ZIP entry name: {info.filename!r}")
        if info.is_dir() or (info.external_attr & 0x10) != 0:
            raise ValueError("Corpus ZIP member is a directory")

        # Check mode if non-zero
        mode = info.external_attr >> 16
        type_bits = mode & 0o170000
        if type_bits != 0 and type_bits != 0o100000:
            raise ValueError(f"Corpus ZIP member is explicitly non-regular: mode {oct(mode)}")

        raw_bytes = zf.read(info.filename)

    if b"\r" in raw_bytes.replace(b"\r\n", b""):
        raise ValueError("Corpus member contains lone CR")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Corpus text is not valid UTF-8: {exc}") from exc

    if "\ufffd" in text:
        raise ValueError("Corpus text contains replacement character U+FFFD")

    # Split strictly on LF / CRLF line endings, avoiding Unicode line separators
    clean_text = text[:-2] if text.endswith("\r\n") else (text[:-1] if text.endswith("\n") else text)
    lines = [line[:-1] if line.endswith("\r") else line for line in clean_text.split("\n")] if clean_text else []
    header_idx = -1
    for idx, line in enumerate(lines):
        if line == "LOCATION\tFORM\tTAG\tFEATURES":
            header_idx = idx
            break
        if line and not line.startswith("#"):
            raise ValueError(f"Unexpected preamble line: {line!r}")

    if header_idx < 0:
        raise ValueError("Missing tab-separated header: LOCATION\tFORM\tTAG\tFEATURES")

    data_lines = lines[header_idx + 1 :]
    if not data_lines:
        raise ValueError("Corpus has no segment records")

    segments: list[CorpusSegment] = []
    seen_coords: set[tuple[int, int, int, int]] = set()
    coord_pattern = re.compile(r"^\(([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*)\)$")

    for line_num, line in enumerate(data_lines, start=header_idx + 2):
        line_str = line
        if not line_str:
            raise ValueError(f"Line {line_num}: empty/blank row in data body")

        parts = line_str.split("\t")
        if len(parts) != 4:
            raise ValueError(f"Line {line_num} does not have exactly 4 columns: {line_str!r}")

        loc_str, form_raw, tag_raw, feat_raw = parts
        if not tag_raw or not tag_raw.strip():
            raise ValueError(f"Line {line_num}: empty or blank TAG in {line_str!r}")
        if form_raw != "" and not form_raw.strip():
            raise ValueError(f"Line {line_num}: blank whitespace FORM in {line_str!r}")

        match = coord_pattern.match(loc_str)
        if not match:
            raise ValueError(f"Line {line_num}: malformed coordinate {loc_str!r}")

        surah = int(match.group(1))
        ayah = int(match.group(2))
        word = int(match.group(3))
        seg_num = int(match.group(4))

        verse_key = f"{surah}:{ayah}"
        if verse_key not in ayah_ids:
            raise ValueError(f"Line {line_num}: unknown verse {verse_key} not in Tanzil coordinates")

        coord = (surah, ayah, word, seg_num)
        if coord in seen_coords:
            raise ValueError(f"Line {line_num}: duplicate coordinate {loc_str}")
        seen_coords.add(coord)

        form_ar = transliterate_buckwalter(form_raw)
        feat_tokens = feat_raw.split("|")
        role = feat_tokens[0]
        if role not in ("PREFIX", "STEM", "SUFFIX"):
            raise ValueError(f"Line {line_num}: invalid segment role {role!r} in {feat_raw}")

        if role in ("PREFIX", "SUFFIX"):
            for tok in feat_tokens[1:]:
                if tok.startswith(("ROOT:", "LEM:", "POS:")):
                    raise ValueError(f"Line {line_num}: affix segment cannot have ROOT, LEM, or POS: {feat_raw}")
        elif role == "STEM":
            roots = [tok for tok in feat_tokens if tok.startswith("ROOT:")]
            lemmas = [tok for tok in feat_tokens if tok.startswith("LEM:")]
            poses = [tok for tok in feat_tokens if tok.startswith("POS:")]
            if len(roots) > 1:
                raise ValueError(f"Line {line_num}: duplicate ROOT in stem: {feat_raw}")
            if len(lemmas) > 1:
                raise ValueError(f"Line {line_num}: duplicate LEM in stem: {feat_raw}")
            if len(poses) > 1:
                raise ValueError(f"Line {line_num}: duplicate POS in stem: {feat_raw}")
            if poses:
                pos_val = poses[0][4:]
                if pos_val != tag_raw:
                    raise ValueError(f"Line {line_num}: Stem POS:{pos_val} does not agree with TAG={tag_raw} in {feat_raw}")

        if tag_raw == "V" or (role == "STEM" and any(tok.startswith("POS:V") for tok in feat_tokens)):
            if role != "STEM" or tag_raw != "V":
                raise ValueError(f"Line {line_num}: Verb must be STEM with TAG=V: {feat_raw}")
            _parse_verb_features(feat_raw, tag_raw)
        elif role == "STEM":
            _parse_nonverb_stem(feat_raw, tag_raw)
        segments.append(
            CorpusSegment(
                surah=surah,
                ayah=ayah,
                word=word,
                segment=seg_num,
                form_buckwalter=form_raw,
                form_ar=form_ar,
                pos_tag=tag_raw,
                features_raw=feat_raw,
                role=role,
            )
        )

    # Sort segments by numeric coordinate order
    segments.sort(key=lambda s: (s.surah, s.ayah, s.word, s.segment))

    # Verify contiguous numbering with no gaps
    # Group by (surah, ayah) to verify word numbers start at 1 and have no gaps
    words_by_ayah: dict[tuple[int, int], set[int]] = defaultdict(set)
    segs_by_word: dict[tuple[int, int, int], set[int]] = defaultdict(set)

    for s in segments:
        words_by_ayah[(s.surah, s.ayah)].add(s.word)
        segs_by_word[(s.surah, s.ayah, s.word)].add(s.segment)

    for (s_id, a_id), word_nums in words_by_ayah.items():
        expected_words = set(range(1, len(word_nums) + 1))
        if word_nums != expected_words:
            raise ValueError(
                f"Word numbers for ayah {s_id}:{a_id} must start at 1 with no gaps; got {sorted(word_nums)}"
            )

    for (s_id, a_id, w_num), seg_nums in segs_by_word.items():
        expected_segs = set(range(1, len(seg_nums) + 1))
        if seg_nums != expected_segs:
            raise ValueError(
                f"Segment numbers for word {s_id}:{a_id}:{w_num} must start at 1 with no gaps; got {sorted(seg_nums)}"
            )

    return tuple(segments)


def load_morphology_license(sources_root: Path) -> dict[str, Any]:
    """Validate and return sources/licenses/quran-morphology.json against locked inputs."""
    sources_root = Path(sources_root)
    registry_path = sources_root / "sources" / "registry.json"
    raw_root = sources_root / "raw-inputs"
    lock_path = sources_root / "sources" / "manifest.lock.json"
    lock_entries = verify_lock(registry_path, raw_root, lock_path, pack_id=PACK_ID)
    return _load_morphology_license(sources_root, lock_entries)


def _load_morphology_license(
    sources_root: Path,
    lock_entries: tuple[LockEntry, ...],
) -> dict[str, Any]:
    """Validate and return sources/licenses/quran-morphology.json using verified lock entries."""
    sources_root = Path(sources_root)
    locked_paths = frozenset(e.path for e in lock_entries)
    if locked_paths != EXPECTED_PATHS:
        raise ValueError(
            f"Lock entries for {PACK_ID} must match exact expected paths {sorted(EXPECTED_PATHS)}, got {sorted(locked_paths)}"
        )
    rel_lic_path = f"sources/licenses/{PACK_ID}.json"
    verify_exact_file_on_disk(sources_root, rel_lic_path)
    abs_lic_path = sources_root / rel_lic_path

    raw_lic_bytes = abs_lic_path.read_bytes()
    try:
        lic_text = raw_lic_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"License file {rel_lic_path} is not valid UTF-8: {exc}") from exc

    lic_data: dict[str, Any] = parse_strict_json(lic_text)
    if not isinstance(lic_data, dict):
        raise ValueError(f"License root must be a JSON object: {rel_lic_path}")

    expected_lic_keys = {"packId", "licenseSpdx", "primaryPath", "status", "reviewedBy", "sources"}
    if set(lic_data.keys()) != expected_lic_keys:
        raise ValueError(
            f"License file {rel_lic_path} keys mismatch: expected {sorted(expected_lic_keys)}, got {sorted(lic_data.keys())}"
        )

    if lic_data["packId"] != PACK_ID:
        raise ValueError(f"packId mismatch in {rel_lic_path}: expected {PACK_ID!r}, got {lic_data['packId']!r}")

    lic_spdx = lic_data["licenseSpdx"]
    if lic_spdx != "GPL-3.0":
        raise ValueError(f"licenseSpdx must be 'GPL-3.0' in {rel_lic_path}, got {lic_spdx!r}")

    primary_path = lic_data["primaryPath"]
    if primary_path != "corpus/quranic-corpus-morphology-0.4.zip":
        raise ValueError(f"primaryPath must be 'corpus/quranic-corpus-morphology-0.4.zip', got {primary_path!r}")

    status = lic_data["status"]
    if status not in ("approved", "pending"):
        raise ValueError(f"License status must be 'approved' or 'pending', got {status!r}")

    reviewed_by = lic_data["reviewedBy"]
    if status == "pending":
        if reviewed_by is not None:
            raise ValueError(f"reviewedBy must be null when status is 'pending' in {rel_lic_path}")
    elif status == "approved":
        if not isinstance(reviewed_by, str) or not reviewed_by.strip():
            raise ValueError(f"reviewedBy must be non-empty string when status is 'approved' in {rel_lic_path}")

    sources_list = lic_data["sources"]
    if not isinstance(sources_list, list):
        raise ValueError(f"sources must be a list in {rel_lic_path}")

    expected_paths_sorted = sorted(EXPECTED_PATHS)
    actual_paths = [s.get("path") for s in sources_list if isinstance(s, dict)]
    if actual_paths != expected_paths_sorted:
        raise ValueError(
            f"License sources list must be sorted and match expected locked paths: expected {expected_paths_sorted}, got {actual_paths}"
        )

    lock_by_path = {e.path: e for e in lock_entries}
    expected_src_keys = {
        "attribution",
        "name",
        "notice",
        "path",
        "permittedUses",
        "rightsEvidence",
        "sourceUrl",
        "sourceVersion",
    }

    for src in sources_list:
        if not isinstance(src, dict):
            raise ValueError(f"Source entry must be an object in {rel_lic_path}")
        if set(src.keys()) != expected_src_keys:
            raise ValueError(f"Source keys mismatch in {rel_lic_path} for {src.get('path')}: got {sorted(src.keys())}")

        p = src["path"]
        if p not in lock_by_path:
            raise ValueError(f"Source path {p!r} not found in verified lock")
        lock_e = lock_by_path[p]

        if src["sourceUrl"] != lock_e.source_url:
            raise ValueError(
                f"sourceUrl mismatch for {p}: license has {src['sourceUrl']!r}, lock has {lock_e.source_url!r}"
            )
        if src["sourceVersion"] != lock_e.source_version:
            raise ValueError(
                f"sourceVersion mismatch for {p}: license has {src['sourceVersion']!r}, lock has {lock_e.source_version!r}"
            )

        if not isinstance(src["name"], str) or not src["name"].strip():
            raise ValueError(f"Empty source name in {rel_lic_path} for {p}")
        if not isinstance(src["sourceUrl"], str) or not src["sourceUrl"].startswith("https://"):
            raise ValueError(f"Invalid sourceUrl in {rel_lic_path} for {p}")
        validate_https_url(src["sourceUrl"])
        if not isinstance(src["sourceVersion"], str) or not src["sourceVersion"].strip():
            raise ValueError(f"Empty sourceVersion in {rel_lic_path} for {p}")

        if not isinstance(src["notice"], str):
            raise ValueError(f"notice must be string in {rel_lic_path} for {p}")
        if not isinstance(src["attribution"], str):
            raise ValueError(f"attribution must be string in {rel_lic_path} for {p}")

        if status == "approved":
            if not src["notice"].strip():
                raise ValueError(f"notice must be non-empty string in approved {rel_lic_path} for {p}")
            if not src["attribution"].strip():
                raise ValueError(f"attribution must be non-empty string in approved {rel_lic_path} for {p}")

        if not isinstance(src["rightsEvidence"], list):
            raise ValueError(f"rightsEvidence must be a list in {rel_lic_path} for {p}")
        for ev in src["rightsEvidence"]:
            if not isinstance(ev, str):
                raise ValueError(f"rightsEvidence entries must be strings in {rel_lic_path} for {p}, got {type(ev).__name__}")
            if not ev.strip():
                raise ValueError(f"Invalid rightsEvidence entry in {rel_lic_path} for {p}")
            if ev.startswith("https://"):
                validate_https_url(ev)
            elif ev.startswith("sources/licenses/evidence/"):
                validate_safe_relative_path(ev)
                verify_exact_file_on_disk(sources_root, ev)
            else:
                raise ValueError(
                    f"rightsEvidence entry must be HTTPS URL or exact-case path under sources/licenses/evidence/: {ev!r}"
                )
        if len(src["rightsEvidence"]) != len(set(src["rightsEvidence"])):
            raise ValueError(f"rightsEvidence contains duplicates in {rel_lic_path} for {p}")

        if status == "approved" and not src["rightsEvidence"]:
            raise ValueError(f"rightsEvidence cannot be empty in approved {rel_lic_path} for {p}")

        if not isinstance(src["permittedUses"], list):
            raise ValueError(f"permittedUses must be a list in {rel_lic_path} for {p}")
        for u in src["permittedUses"]:
            if not isinstance(u, str):
                raise ValueError(f"permittedUses entries must be strings in {rel_lic_path} for {p}, got {type(u).__name__}")
            if u not in _VALID_PERMITTED_USES:
                raise ValueError(f"Invalid permittedUses entry {u!r} in {rel_lic_path} for {p}")
        if len(src["permittedUses"]) != len(set(src["permittedUses"])):
            raise ValueError(f"permittedUses contains duplicates in {rel_lic_path} for {p}")

        if status == "pending" and src["permittedUses"]:
            raise ValueError(f"Pending license record cannot grant permittedUses in {rel_lic_path} for {p}")
        if status == "approved" and not src["permittedUses"]:
            raise ValueError(f"Empty permittedUses in approved {rel_lic_path} for {p}")

    return lic_data



@dataclass
class _WordSummary:
    surah: int
    ayah: int
    word_number: int
    word_key: str
    word_ar_uthmani: str
    word_ar_clean: str
    primary_pos: str
    is_verb: int
    root_buckwalter: str | None
    lemma_buckwalter: str | None
    verb_form: int | None
    verb_tense: str | None
    verb_voice: str | None
    segments: list[CorpusSegment]


def build_quran_morphology(sources_root: Path, output_dir: Path) -> None:
    """Build quran-morphology.db deterministically."""
    verify_runtime()

    sources_root = Path(sources_root)
    output_dir = Path(output_dir)

    raw_root = sources_root / "raw-inputs"
    registry_path = sources_root / "sources" / "registry.json"
    lock_path = sources_root / "sources" / "manifest.lock.json"

    # Verify lock against exact morphology inputs
    lock_entries = verify_lock(registry_path, raw_root, lock_path, pack_id=PACK_ID)
    locked_paths = frozenset(e.path for e in lock_entries)
    if locked_paths != EXPECTED_PATHS:
        raise ValueError(f"Locked paths mismatch for {PACK_ID}: expected {sorted(EXPECTED_PATHS)}, got {sorted(locked_paths)}")

    # Load and validate license record
    lic_data = _load_morphology_license(sources_root, lock_entries)
    primary_lock = [e for e in lock_entries if e.path == lic_data["primaryPath"]][0]
    primary_src = [s for s in lic_data["sources"] if s["path"] == lic_data["primaryPath"]][0]

    # Preflight destination
    output_path = output_dir / f"{PACK_ID}.db"
    if os.path.lexists(output_path) or output_path.exists():
        raise FileExistsError(f"Output database file already exists: {output_path}")

    for suffix in _SIDECAR_SUFFIXES:
        sidecar = output_path.with_name(output_path.name + suffix)
        if os.path.lexists(sidecar) or sidecar.exists():
            raise FileExistsError(f"SQLite sidecar file already exists: {sidecar}")

    # Load Tanzil coordinates
    meta_file = raw_root / "tanzil" / "quran-data-Tanzil.xml"
    coords = load_tanzil_coordinates(meta_file)

    # Read Corpus segments
    corpus_file = raw_root / "corpus" / "quranic-corpus-morphology-0.4.zip"
    all_segments = read_corpus_segments(corpus_file, coords.ayah_ids)

    # Group segments by word using itertools.groupby
    word_summaries: list[_WordSummary] = []
    for (s_id, a_id, w_num), seg_iter in itertools.groupby(
        all_segments, key=lambda s: (s.surah, s.ayah, s.word)
    ):
        segs = list(seg_iter)
        uthmani_text = "".join(s.form_ar for s in segs)
        clean_text = normalize_arabic(uthmani_text)
        word_key = f"{s_id}:{a_id}:{w_num}"

        # Choose stem: earliest verb stem if present, otherwise earliest STEM;
        # if no stem, use first segment tag and no root/lemma
        verb_stems = [s for s in segs if s.role == "STEM" and s.pos_tag == "V"]
        all_stems = [s for s in segs if s.role == "STEM"]

        if verb_stems:
            selected = verb_stems[0]
            root_b, lem_b, v_form, v_tense, v_voice = _parse_verb_features(selected.features_raw, selected.pos_tag)
            summary = _WordSummary(
                surah=s_id,
                ayah=a_id,
                word_number=w_num,
                word_key=word_key,
                word_ar_uthmani=uthmani_text,
                word_ar_clean=clean_text,
                primary_pos="V",
                is_verb=1,
                root_buckwalter=root_b,
                lemma_buckwalter=lem_b,
                verb_form=v_form,
                verb_tense=v_tense,
                verb_voice=v_voice,
                segments=segs,
            )
        elif all_stems:
            selected = all_stems[0]
            root_b, lem_b = _parse_nonverb_stem(selected.features_raw, selected.pos_tag)
            summary = _WordSummary(
                surah=s_id,
                ayah=a_id,
                word_number=w_num,
                word_key=word_key,
                word_ar_uthmani=uthmani_text,
                word_ar_clean=clean_text,
                primary_pos=selected.pos_tag,
                is_verb=0,
                root_buckwalter=root_b,
                lemma_buckwalter=lem_b,
                verb_form=None,
                verb_tense=None,
                verb_voice=None,
                segments=segs,
            )
        else:
            first_seg = segs[0]
            summary = _WordSummary(
                surah=s_id,
                ayah=a_id,
                word_number=w_num,
                word_key=word_key,
                word_ar_uthmani=uthmani_text,
                word_ar_clean=clean_text,
                primary_pos=first_seg.pos_tag,
                is_verb=0,
                root_buckwalter=None,
                lemma_buckwalter=None,
                verb_form=None,
                verb_tense=None,
                verb_voice=None,
                segments=segs,
            )

        word_summaries.append(summary)

    # 1. Process roots
    # Group roots by raw root; count selected words
    roots_total: dict[str, int] = Counter()
    roots_verbs: dict[str, int] = Counter()
    roots_nouns: dict[str, int] = Counter()

    for w in word_summaries:
        if w.root_buckwalter is not None:
            r = w.root_buckwalter
            roots_total[r] += 1
            if w.primary_pos == "V":
                roots_verbs[r] += 1
            elif w.primary_pos in ("N", "PN"):
                roots_nouns[r] += 1

    unique_roots_sorted = sorted(roots_total.keys())
    # Assign IDs in ascending raw root order
    root_id_map: dict[str, int] = {r: idx + 1 for idx, r in enumerate(unique_roots_sorted)}

    # Ranks: descending total count then raw root ascending
    roots_ranked = sorted(unique_roots_sorted, key=lambda r: (-roots_total[r], r))
    root_rank_map: dict[str, int] = {r: idx + 1 for idx, r in enumerate(roots_ranked)}

    roots_table_rows: list[tuple[Any, ...]] = []
    for r in unique_roots_sorted:
        r_id = root_id_map[r]
        r_ar = transliterate_buckwalter(r)
        r_ar_spaced = " ".join(r_ar)
        roots_table_rows.append((
            r_id,
            r,
            r_ar,
            r_ar_spaced,
            roots_total[r],
            roots_verbs[r],
            roots_nouns[r],
            root_rank_map[r],
        ))

    # 2. Process lemmas
    # Group lemmas by raw lemma; for each choose most frequent (root, primary_pos)
    lemma_totals: dict[str, int] = Counter()
    lemma_pairs: dict[str, Counter[tuple[str | None, str]]] = defaultdict(Counter)

    for w in word_summaries:
        if w.lemma_buckwalter is not None:
            lem = w.lemma_buckwalter
            lemma_totals[lem] += 1
            lemma_pairs[lem][(w.root_buckwalter, w.primary_pos)] += 1

    unique_lemmas_sorted = sorted(lemma_totals.keys())
    lemma_id_map: dict[str, int] = {lem: idx + 1 for idx, lem in enumerate(unique_lemmas_sorted)}

    lemmas_ranked = sorted(unique_lemmas_sorted, key=lambda lem: (-lemma_totals[lem], lem))
    lemma_rank_map: dict[str, int] = {lem: idx + 1 for idx, lem in enumerate(lemmas_ranked)}

    lemmas_table_rows: list[tuple[Any, ...]] = []
    for lem in unique_lemmas_sorted:
        lem_id = lemma_id_map[lem]
        lem_ar = transliterate_buckwalter(lem, is_lemma=True)
        # Winner among (root, pos) pairs:
        # ties: null root first, then raw root and POS in ascending binary order
        candidates = list(lemma_pairs[lem].items())
        candidates.sort(
            key=lambda item: (
                -item[1],
                0 if item[0][0] is None else 1,
                "" if item[0][0] is None else item[0][0],
                item[0][1],
            )
        )
        winning_pair = candidates[0][0]
        best_root, best_pos = winning_pair
        root_fk = root_id_map[best_root] if best_root is not None else None

        lemmas_table_rows.append((
            lem_id,
            lem,
            lem_ar,
            root_fk,
            best_pos,
            lemma_totals[lem],
            lemma_rank_map[lem],
        ))

    # 3. Process verb concordance
    # Group by (raw root, numeric form, raw lemma)
    verb_groups: dict[tuple[str, int, str], list[_WordSummary]] = defaultdict(list)
    for w in word_summaries:
        if w.is_verb:
            assert w.root_buckwalter is not None
            assert w.verb_form is not None
            assert w.lemma_buckwalter is not None
            key = (w.root_buckwalter, w.verb_form, w.lemma_buckwalter)
            verb_groups[key].append(w)

    unique_concordance_sorted = sorted(verb_groups.keys())
    concordance_id_map: dict[tuple[str, int, str], int] = {
        key: idx + 1 for idx, key in enumerate(unique_concordance_sorted)
    }

    # Concordance rank: descending total count then natural key ascending
    concordance_ranked = sorted(
        unique_concordance_sorted,
        key=lambda k: (-len(verb_groups[k]), k[0], k[1], k[2]),
    )
    concordance_rank_map: dict[tuple[str, int, str], int] = {
        key: idx + 1 for idx, key in enumerate(concordance_ranked)
    }

    verb_concordance_rows: list[tuple[Any, ...]] = []
    for key in unique_concordance_sorted:
        c_id = concordance_id_map[key]
        raw_r, v_form, raw_lem = key
        r_id = root_id_map[raw_r]
        lem_id = lemma_id_map[raw_lem]
        verb_ar = transliterate_buckwalter(raw_lem, is_lemma=True)
        root_ar = transliterate_buckwalter(raw_r)
        v_roman = _ROMAN_FORMS[v_form]

        words_in_grp = verb_groups[key]
        t_count = len(words_in_grp)
        perf_count = sum(1 for w in words_in_grp if w.verb_tense == "PERF")
        impf_count = sum(1 for w in words_in_grp if w.verb_tense == "IMPF")
        impv_count = sum(1 for w in words_in_grp if w.verb_tense == "IMPV")
        act_count = sum(1 for w in words_in_grp if w.verb_voice == "ACT")
        pass_count = sum(1 for w in words_in_grp if w.verb_voice == "PASS")
        f_rank = concordance_rank_map[key]

        verb_concordance_rows.append((
            c_id,
            r_id,
            lem_id,
            verb_ar,
            raw_lem,
            root_ar,
            v_form,
            v_roman,
            t_count,
            perf_count,
            impf_count,
            impv_count,
            act_count,
            pass_count,
            f_rank,
        ))

    # 4. Prepare words and morphology_segments rows
    words_rows: list[tuple[Any, ...]] = []
    segments_rows: list[tuple[Any, ...]] = []
    seg_global_id = 1
    ar_present_count = 0

    for word_idx, w in enumerate(word_summaries, start=1):
        ayah_id = coords.ayah_ids[f"{w.surah}:{w.ayah}"]
        r_fk = root_id_map[w.root_buckwalter] if w.root_buckwalter is not None else None
        lem_fk = lemma_id_map[w.lemma_buckwalter] if w.lemma_buckwalter is not None else None

        if w.is_verb:
            assert w.root_buckwalter is not None
            assert w.verb_form is not None
            assert w.lemma_buckwalter is not None
            v_key = (w.root_buckwalter, w.verb_form, w.lemma_buckwalter)
            v_conc_fk: int | None = concordance_id_map[v_key]
        else:
            v_conc_fk = None

        if bool(w.word_ar_uthmani.strip()) and bool(w.word_ar_clean.strip()):
            ar_present_count += 1

        words_rows.append((
            word_idx,
            ayah_id,
            w.surah,
            w.ayah,
            w.word_number,
            w.word_key,
            w.word_ar_uthmani,
            w.word_ar_clean,
            w.primary_pos,
            w.is_verb,
            r_fk,
            lem_fk,
            v_conc_fk,
            w.verb_form,
            w.verb_tense,
            w.verb_voice,
        ))

        for s in w.segments:
            segments_rows.append((
                seg_global_id,
                word_idx,
                s.segment,
                s.role,
                s.form_buckwalter,
                s.form_ar,
                s.pos_tag,
                s.features_raw,
            ))
            seg_global_id += 1

    # Metadata & Coverage
    ar_present_count = sum(
        1 for w in word_summaries if w.word_ar_uthmani.strip() and w.word_ar_clean.strip()
    )
    expected_total = len(word_summaries)
    completeness = (
        "complete"
        if expected_total > 0 and ar_present_count == expected_total
        else "partial"
    )

    input_sha256 = hashlib.sha256(canonical_entries_json(lock_entries)).hexdigest().lower()

    metadata: dict[str, str] = {
        "pack_id": PACK_ID,
        "pack_version": "0.1.0",
        "schema_version": "1",
        "source_name": primary_src["name"],
        "source_url": primary_lock.source_url,
        "source_version": primary_lock.source_version,
        "license_spdx": lic_data["licenseSpdx"],
        "license_ref": f"sources/licenses/{PACK_ID}.json",
        "build_input_sha256": input_sha256,
        "completeness": completeness,
        "fts5_enabled": "false",
    }

    coverage: dict[str, tuple[int, str | None]] = {
        "ar_present": (ar_present_count, None),
        "expected_total": (expected_total, None),
    }

    repo_root = Path(__file__).resolve().parents[2]
    schema_path = repo_root / "specs" / "sql" / "quran_morphology.v1.sql"

    conn: sqlite3.Connection | None = None
    created = False
    try:
        conn = open_pack(output_path, schema_path)
        created = True
        conn.execute("BEGIN;")

        conn.executemany(
            "INSERT INTO roots ("
            "id, root_buckwalter, root_ar, root_ar_spaced, total_occurrences, "
            "verb_occurrences, noun_occurrences, frequency_rank"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            roots_table_rows,
        )

        conn.executemany(
            "INSERT INTO lemmas ("
            "id, lemma_buckwalter, lemma_ar, root_id, primary_pos, occurrence_count, frequency_rank"
            ") VALUES (?, ?, ?, ?, ?, ?, ?);",
            lemmas_table_rows,
        )

        conn.executemany(
            "INSERT INTO verb_concordance ("
            "id, root_id, lemma_id, verb_ar, verb_buckwalter, root_ar, verb_form, "
            "verb_form_roman, total_count, perf_count, impf_count, impv_count, "
            "active_count, passive_count, frequency_rank"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            verb_concordance_rows,
        )

        conn.executemany(
            "INSERT INTO words ("
            "id, ayah_id, surah_id, ayah_number, word_number, word_key, "
            "word_ar_uthmani, word_ar_clean, primary_pos, is_verb, root_id, "
            "lemma_id, verb_concordance_id, verb_form, verb_tense, verb_voice"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            words_rows,
        )

        conn.executemany(
            "INSERT INTO morphology_segments ("
            "id, word_id, segment_number, segment_role, form_buckwalter, form_ar, pos_tag, features_raw"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?);",
            segments_rows,
        )

        write_pack_metadata(conn, metadata, coverage)
        finalize_pack(conn, fts_tables=())
    except Exception:
        if conn is not None:
            try:
                conn.close()
            finally:
                conn = None
        if created:
            if output_path.exists():
                output_path.unlink()
            for suffix in _SIDECAR_SUFFIXES:
                sidecar = output_path.with_name(output_path.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        raise
    finally:
        if conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build quran-morphology SQLite pack.")
    parser.add_argument("--sources-root", required=True, type=Path, help="Root directory containing sources/ and raw-inputs/")
    parser.add_argument("--output-dir", required=True, type=Path, help="Output directory where quran-morphology.db will be written")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        build_quran_morphology(args.sources_root, args.output_dir)
        return 0
    except Exception as exc:
        sys.stderr.write(f"Error building quran-morphology: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
