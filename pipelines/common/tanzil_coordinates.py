"""Tanzil XML coordinates parser and scripture text loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import NamedTuple
import xml.etree.ElementTree as ET

_CANONICAL_POSITIVE_INT_RE = re.compile(r"^[1-9][0-9]*$")
_CANONICAL_NONNEGATIVE_INT_RE = re.compile(r"^(0|[1-9][0-9]*)$")


class Surah(NamedTuple):
    id: int
    name_ar: str
    name_tname: str
    name_en: str
    revelation_type: str
    revelation_order: int
    ayah_count: int
    ruku_count: int
    start_ayah_id: int


@dataclass(frozen=True)
class TanzilCoordinates:
    surahs: tuple[Surah, ...]
    ayah_ids: dict[str, int]
    boundaries: dict[int, tuple[int, int, int, int, int]]
    sajdah_types: dict[int, str]


class _StrictTreeBuilder(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError(f"DOCTYPE / DTD declaration not permitted: {name}")


def _parse_canonical_int(val: str, *, allow_zero: bool = False, field_name: str = "integer") -> int:
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a string, got {type(val)}")
    regex = _CANONICAL_NONNEGATIVE_INT_RE if allow_zero else _CANONICAL_POSITIVE_INT_RE
    if not regex.fullmatch(val):
        raise ValueError(f"Non-canonical {field_name} integer literal: {val!r}")
    return int(val)


_XML_DECL_ENCODING_RE = re.compile(r"""^<\?xml\s+[^>]*?encoding\s*=\s*(['"])(.*?)\1""", re.IGNORECASE | re.DOTALL)

_EDITION_NOTICE_ID_LINES: dict[str, bytes] = {
    "tanzil/en.pickthall.xml": b"#  ID: en.pickthall",
    "tanzil/ar.jalalayn.xml": b"#  ID: ar.jalalayn",
}

_XML_DECL_STRICT_RE = re.compile(
    rb"""^<\?xml[ \t\r\n]+version[ \t\r\n]*=[ \t\r\n]*(['"])1\.[0-9]+\1"""
    rb"""(?:[ \t\r\n]+encoding[ \t\r\n]*=[ \t\r\n]*(['"])([A-Za-z0-9._-]+)\2)?"""
    rb"""(?:[ \t\r\n]+standalone[ \t\r\n]*=[ \t\r\n]*(['"])(?:yes|no)\4)?"""
    rb"""[ \t\r\n]*\?>"""
)


def _strip_edition_leading_notice(
    raw_bytes: bytes,
    *,
    locked_path: str,
    context: str = "",
) -> bytes:
    """Validate and strip the single leading notice comment for Pickthall or Jalalayn."""
    m = _XML_DECL_STRICT_RE.match(raw_bytes)
    if not m:
        raise ValueError(f"Valid XML declaration required at start of edition XML {context}")

    decl_end = m.end()
    idx = decl_end
    while idx < len(raw_bytes) and raw_bytes[idx : idx + 1] in (b" ", b"\t", b"\r", b"\n"):
        idx += 1

    if not raw_bytes.startswith(b"<!--", idx):
        raise ValueError(
            f"Expected leading notice comment block immediately following XML declaration and whitespace in {context}"
        )

    comm_start = idx
    comm_end = raw_bytes.find(b"-->", comm_start)
    if comm_end == -1:
        raise ValueError(f"Unterminated leading notice comment block in {context}")
    comm_end += 3

    notice_bytes = raw_bytes[comm_start:comm_end]
    if b"<!--" in notice_bytes[4:]:
        raise ValueError(f"Nested comment opener '<!--' in leading notice block in {context}")

    try:
        notice_text = notice_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Leading notice is not valid UTF-8 {context}: {exc}") from exc
    if "\ufffd" in notice_text:
        raise ValueError(f"Replacement character U+FFFD detected in leading notice {context}")

    for ch in notice_text:
        cp = ord(ch)
        if cp == 0x9 or cp == 0xA or cp == 0xD or (0x20 <= cp <= 0xD7FF) or (0xE000 <= cp < 0xFFFD) or (0x10000 <= cp <= 0x10FFFF):
            continue
        raise ValueError(f"Invalid XML character in leading notice {context}")

    expected_id_line = _EDITION_NOTICE_ID_LINES[locked_path]
    lines = re.split(rb"\r\n|\r|\n", notice_bytes)
    found_expected = False
    for line in lines:
        if line.startswith(b"#  ID:"):
            if line != expected_id_line:
                raise ValueError(
                    f"Wrong edition in leading notice in {context}"
                )
            found_expected = True

    if not found_expected:
        raise ValueError(
            f"Missing expected edition line in leading notice in {context}"
        )

    return raw_bytes[:comm_start] + raw_bytes[comm_end:]


def _check_string_for_fffd(text: str, context: str = "") -> None:
    if "\ufffd" in text:
        raise ValueError(f"Replacement character U+FFFD detected {context}")


def _parse_strict_xml_from_bytes(raw_bytes: bytes, context: str = "") -> ET.Element:
    if raw_bytes.startswith(b"<?xml"):
        decl_end = raw_bytes.find(b"?>")
        if decl_end != -1:
            decl_header = raw_bytes[: decl_end + 2].decode("latin1", errors="ignore")
            m = _XML_DECL_ENCODING_RE.match(decl_header)
            if m:
                decl_enc = m.group(2).strip().lower()
                if decl_enc not in ("utf-8", "utf8"):
                    raise ValueError(f"Conflicting XML declaration encoding {decl_enc!r} in {context}; strict UTF-8 required")

    try:
        decoded_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"XML is not valid UTF-8 {context}: {exc}") from exc

    _check_string_for_fffd(decoded_text, f"in raw text {context}")

    parser = ET.XMLParser(target=_StrictTreeBuilder(), encoding="utf-8")
    try:
        parser.feed(raw_bytes)
        root = parser.close()
    except Exception as exc:
        raise ValueError(f"XML parse error {context}: {exc}") from exc

    # Recursively check decoded element tags, attributes, and text
    for elem in root.iter():
        if elem.tag:
            _check_string_for_fffd(elem.tag, f"in tag {elem.tag!r} {context}")
        if elem.text:
            _check_string_for_fffd(elem.text, f"in text of tag {elem.tag!r} {context}")
        for attr_k, attr_v in elem.attrib.items():
            _check_string_for_fffd(attr_k, f"in attribute key {attr_k!r} of {elem.tag!r} {context}")
            _check_string_for_fffd(attr_v, f"in attribute {attr_k!r} of {elem.tag!r} {context}")

    return root


def load_tanzil_coordinates(path: Path) -> TanzilCoordinates:
    """Parse Tanzil metadata XML and construct canonical TanzilCoordinates."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Tanzil metadata file not found: {path}")

    root = _parse_strict_xml_from_bytes(path.read_bytes(), context=str(path))
    if root.tag != "quran":
        raise ValueError(f"Expected root tag 'quran', got {root.tag!r}")
    if root.text and root.text.strip():
        raise ValueError(f"Stray text in metadata root: {root.text.strip()!r}")

    expected_root_attrs = {"type", "version", "copyright", "license"}
    if set(root.attrib.keys()) != expected_root_attrs:
        raise ValueError(f"Metadata root attributes mismatch: expected {expected_root_attrs}, got {set(root.attrib.keys())}")
    for k in expected_root_attrs:
        if not root.attrib[k].strip():
            raise ValueError(f"Empty metadata root attribute: {k!r}")

    expected_sections = ["suras", "juzs", "hizbs", "manzils", "rukus", "pages", "sajdas"]
    section_map: dict[str, ET.Element] = {}
    for child in root:
        tag = child.tag
        if tag in section_map:
            raise ValueError(f"Duplicate section {tag!r} in metadata")
        if tag not in expected_sections:
            raise ValueError(f"Unexpected section {tag!r} in metadata")
        if child.text and child.text.strip():
            raise ValueError(f"Stray text in metadata section {tag!r}: {child.text.strip()!r}")
        if child.tail and child.tail.strip():
            raise ValueError(f"Stray tail after metadata section {tag!r}: {child.tail.strip()!r}")
        section_map[tag] = child

    if set(section_map.keys()) != set(expected_sections):
        missing = set(expected_sections) - set(section_map.keys())
        raise ValueError(f"Missing required sections in metadata: {missing}")

    # Validate section attributes (optional 'alias' only)
    for tag, elem in section_map.items():
        extra_attrs = set(elem.attrib.keys()) - {"alias"}
        if extra_attrs:
            raise ValueError(f"Unexpected attributes on section {tag!r}: {extra_attrs}")

    # 1. Parse surahs
    suras_elem = section_map["suras"]
    parsed_surahs: list[Surah] = []
    seen_sura_indices: set[int] = set()

    for child in suras_elem:
        if child.tag != "sura":
            raise ValueError(f"Expected tag 'sura', got {child.tag!r}")
        if len(child) != 0:
            raise ValueError("Unexpected child elements under sura in metadata")
        if child.text and child.text.strip():
            raise ValueError(f"Stray text in sura element in metadata: {child.text.strip()!r}")
        if child.tail and child.tail.strip():
            raise ValueError(f"Stray tail after sura element in metadata: {child.tail.strip()!r}")
        expected_sura_attrs = {"index", "ayas", "start", "name", "tname", "ename", "type", "order", "rukus"}
        if set(child.attrib.keys()) != expected_sura_attrs:
            raise ValueError(f"Sura attributes mismatch: got {set(child.attrib.keys())}")

        s_idx = _parse_canonical_int(child.attrib["index"], field_name="sura.index")
        if s_idx in seen_sura_indices:
            raise ValueError(f"Duplicate sura index: {s_idx}")
        seen_sura_indices.add(s_idx)

        ayas = _parse_canonical_int(child.attrib["ayas"], field_name="sura.ayas")
        start = _parse_canonical_int(child.attrib["start"], allow_zero=True, field_name="sura.start")
        order = _parse_canonical_int(child.attrib["order"], field_name="sura.order")
        rukus = _parse_canonical_int(child.attrib["rukus"], field_name="sura.rukus")

        rev_type = child.attrib["type"]
        if rev_type not in ("Meccan", "Medinan"):
            raise ValueError(f"Invalid revelation type: {rev_type!r}")

        name_ar = child.attrib["name"]
        name_tname = child.attrib["tname"]
        name_en = child.attrib["ename"]
        for n_label, n_val in (("name", name_ar), ("tname", name_tname), ("ename", name_en)):
            if not n_val.strip():
                raise ValueError(f"Empty sura {n_label} at index {s_idx}")

        start_ayah_id = start + 1
        parsed_surahs.append(Surah(
            id=s_idx,
            name_ar=name_ar,
            name_tname=name_tname,
            name_en=name_en,
            revelation_type=rev_type,
            revelation_order=order,
            ayah_count=ayas,
            ruku_count=rukus,
            start_ayah_id=start_ayah_id,
        ))

    if len(parsed_surahs) != 114:
        raise ValueError(f"Expected exactly 114 surahs, found {len(parsed_surahs)}")

    # Sort surahs strictly by numeric ID
    parsed_surahs.sort(key=lambda s: s.id)
    if [s.id for s in parsed_surahs] != list(range(1, 115)):
        raise ValueError("Surah indices must be exactly 1..114 contiguous")

    # Verify zero-based prefix sums and total verse count BEFORE constructing ayah_ids
    expected_start = 0
    for s in parsed_surahs:
        actual_start = s.start_ayah_id - 1
        if actual_start != expected_start:
            raise ValueError(
                f"Surah {s.id} start mismatch: expected {expected_start}, got {actual_start}"
            )
        expected_start += s.ayah_count
        if expected_start > 6236:
            raise ValueError(f"Surah prefix sum exceeds 6236 at surah {s.id}: {expected_start}")

    if expected_start != 6236:
        raise ValueError(f"Total verse count mismatch: expected 6236, got {expected_start}")

    # Build canonical ayah_ids map from verified prefix sums
    ayah_ids: dict[str, int] = {}
    for s in parsed_surahs:
        start_offset = s.start_ayah_id - 1
        for aya_num in range(1, s.ayah_count + 1):
            ayah_ids[f"{s.id}:{aya_num}"] = start_offset + aya_num

    assert len(ayah_ids) == 6236

    # 2. Helper to parse and sweep boundary sections
    def _parse_boundary_section(
        elem: ET.Element,
        expected_tag: str,
        expected_count: int,
        section_name: str,
    ) -> list[tuple[int, int]]:
        """Parse boundary elements and return list of (index, global_ayah_id)."""
        items: list[tuple[int, int]] = []
        seen_indices: set[int] = set()

        for child in elem:
            if child.tag != expected_tag:
                raise ValueError(f"Expected tag {expected_tag!r} in {section_name}, got {child.tag!r}")
            if len(child) != 0:
                raise ValueError(f"Unexpected child elements in {section_name}")
            if child.text and child.text.strip():
                raise ValueError(f"Stray text in {section_name}")
            if child.tail and child.tail.strip():
                raise ValueError(f"Stray tail in {section_name}")
            if set(child.attrib.keys()) != {"index", "sura", "aya"}:
                raise ValueError(f"Invalid attributes on {expected_tag!r}: {set(child.attrib.keys())}")
            b_idx = _parse_canonical_int(child.attrib["index"], field_name=f"{section_name}.index")
            if b_idx in seen_indices:
                raise ValueError(f"Duplicate index {b_idx} in {section_name}")
            seen_indices.add(b_idx)

            sura = _parse_canonical_int(child.attrib["sura"], field_name=f"{section_name}.sura")
            aya = _parse_canonical_int(child.attrib["aya"], field_name=f"{section_name}.aya")
            coord = f"{sura}:{aya}"
            if coord not in ayah_ids:
                raise ValueError(f"Unknown coordinate {coord!r} in {section_name} at index {b_idx}")

            items.append((b_idx, ayah_ids[coord]))

        if len(items) != expected_count:
            raise ValueError(f"Expected {expected_count} entries in {section_name}, got {len(items)}")

        items.sort(key=lambda x: x[0])
        if [x[0] for x in items] != list(range(1, expected_count + 1)):
            raise ValueError(f"Indices in {section_name} must be contiguous 1..{expected_count}")

        # First entry must start at ayah 1 (1:1)
        if items[0][1] != 1:
            raise ValueError(f"{section_name} first entry must start at ayah 1 (1:1), got global ayah {items[0][1]}")

        # Must be strictly increasing by global ayah ID
        for i in range(1, len(items)):
            if items[i][1] <= items[i - 1][1]:
                raise ValueError(
                    f"{section_name} start global IDs must be strictly increasing: "
                    f"index {items[i][0]} (id {items[i][1]}) <= index {items[i-1][0]} (id {items[i-1][1]})"
                )

        return items

    juz_starts = _parse_boundary_section(section_map["juzs"], "juz", 30, "juzs")
    hizb_starts = _parse_boundary_section(section_map["hizbs"], "quarter", 240, "hizbs")
    manzil_starts = _parse_boundary_section(section_map["manzils"], "manzil", 7, "manzils")
    ruku_starts = _parse_boundary_section(section_map["rukus"], "ruku", 556, "rukus")
    page_starts = _parse_boundary_section(section_map["pages"], "page", 604, "pages")

    # Sweep boundaries across all 6236 ayahs
    boundaries: dict[int, tuple[int, int, int, int, int]] = {}
    p_juz = 0
    p_hizb = 0
    p_manzil = 0
    p_ruku = 0
    p_page = 0

    for aid in range(1, 6237):
        while p_juz + 1 < len(juz_starts) and juz_starts[p_juz + 1][1] <= aid:
            p_juz += 1
        while p_hizb + 1 < len(hizb_starts) and hizb_starts[p_hizb + 1][1] <= aid:
            p_hizb += 1
        while p_manzil + 1 < len(manzil_starts) and manzil_starts[p_manzil + 1][1] <= aid:
            p_manzil += 1
        while p_ruku + 1 < len(ruku_starts) and ruku_starts[p_ruku + 1][1] <= aid:
            p_ruku += 1
        while p_page + 1 < len(page_starts) and page_starts[p_page + 1][1] <= aid:
            p_page += 1

        boundaries[aid] = (
            juz_starts[p_juz][0],
            hizb_starts[p_hizb][0],
            manzil_starts[p_manzil][0],
            ruku_starts[p_ruku][0],
            page_starts[p_page][0],
        )

    # 3. Parse sajdahs
    sajdas_elem = section_map["sajdas"]
    sajdah_types: dict[int, str] = {}
    seen_sajda_indices: set[int] = set()

    for child in sajdas_elem:
        if child.tag != "sajda":
            raise ValueError(f"Expected tag 'sajda', got {child.tag!r}")
        if len(child) != 0:
            raise ValueError("Unexpected child elements in sajda")
        if child.text and child.text.strip():
            raise ValueError("Stray text in sajda")
        if child.tail and child.tail.strip():
            raise ValueError("Stray tail in sajda")
        if set(child.attrib.keys()) != {"index", "sura", "aya", "type"}:
            raise ValueError(f"Invalid attributes on 'sajda': {set(child.attrib.keys())}")
        s_idx = _parse_canonical_int(child.attrib["index"], field_name="sajda.index")
        if s_idx in seen_sajda_indices:
            raise ValueError(f"Duplicate sajda index: {s_idx}")
        seen_sajda_indices.add(s_idx)

        sura = _parse_canonical_int(child.attrib["sura"], field_name="sajda.sura")
        aya = _parse_canonical_int(child.attrib["aya"], field_name="sajda.aya")
        coord = f"{sura}:{aya}"
        if coord not in ayah_ids:
            raise ValueError(f"Unknown coordinate {coord!r} in sajda index {s_idx}")

        aid = ayah_ids[coord]
        if aid in sajdah_types:
            raise ValueError(f"Duplicate sajdah coordinate {coord!r} (ayah id {aid})")

        s_type = child.attrib["type"]
        if s_type not in ("recommended", "obligatory"):
            raise ValueError(f"Invalid sajdah type {s_type!r} at index {s_idx}")

        sajdah_types[aid] = s_type

    if len(sajdah_types) != 15:
        raise ValueError(f"Expected exactly 15 sajdahs, got {len(sajdah_types)}")
    if seen_sajda_indices != set(range(1, 16)):
        raise ValueError(f"Sajda indices must be exactly 1..15, got {sorted(seen_sajda_indices)}")

    return TanzilCoordinates(
        surahs=tuple(parsed_surahs),
        ayah_ids=ayah_ids,
        boundaries=boundaries,
        sajdah_types=sajdah_types,
    )


def load_tanzil_text(
    path: Path,
    coordinates: TanzilCoordinates,
    *,
    locked_path: str,
) -> dict[str, str]:
    """Parse Tanzil scripture XML and map canonical verse keys S:A to text."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Scripture file not found: {path}")

    raw_bytes = path.read_bytes()
    if locked_path in _EDITION_NOTICE_ID_LINES:
        parser_bytes = _strip_edition_leading_notice(
            raw_bytes,
            locked_path=locked_path,
            context=f"{path} ({locked_path})",
        )
    else:
        parser_bytes = raw_bytes

    root = _parse_strict_xml_from_bytes(parser_bytes, context=f"{path} ({locked_path})")
    if root.tag != "quran":
        raise ValueError(f"Expected root tag 'quran', got {root.tag!r}")
    if root.attrib:
        raise ValueError(f"Scripture root tag must not have attributes, got {root.attrib}")
    if root.text and root.text.strip():
        raise ValueError(f"Stray text in scripture root: {root.text.strip()!r}")

    result: dict[str, str] = {}
    seen_sura_indices: set[int] = set()

    for sura_elem in root:
        if sura_elem.tag != "sura":
            raise ValueError(f"Expected 'sura' element, got {sura_elem.tag!r}")
        if "index" not in sura_elem.attrib or "name" not in sura_elem.attrib:
            raise ValueError("Missing 'index' or 'name' attribute on scripture sura")
        extra_sura_attrs = set(sura_elem.attrib.keys()) - {"index", "name"}
        if extra_sura_attrs:
            raise ValueError(f"Unexpected attributes on scripture sura: {extra_sura_attrs}")
        if sura_elem.text and sura_elem.text.strip():
            raise ValueError(f"Stray text in scripture sura: {sura_elem.text.strip()!r}")
        if sura_elem.tail and sura_elem.tail.strip():
            raise ValueError(f"Stray tail after scripture sura: {sura_elem.tail.strip()!r}")

        sura_idx = _parse_canonical_int(sura_elem.attrib["index"], field_name="scripture.sura.index")
        if not (1 <= sura_idx <= 114):
            raise ValueError(f"Scripture sura index {sura_idx} out of range 1..114")
        if sura_idx in seen_sura_indices:
            raise ValueError(f"Duplicate or split sura index: {sura_idx}")
        seen_sura_indices.add(sura_idx)

        sura_aya_count = 0
        for aya_elem in sura_elem:
            if aya_elem.tag != "aya":
                raise ValueError(f"Expected 'aya' element, got {aya_elem.tag!r}")
            if len(aya_elem) != 0:
                raise ValueError(f"Unexpected child elements under aya in scripture")
            if aya_elem.text and aya_elem.text.strip():
                raise ValueError(f"Stray text under aya in scripture: {aya_elem.text.strip()!r}")
            if aya_elem.tail and aya_elem.tail.strip():
                raise ValueError(f"Stray tail after aya in scripture: {aya_elem.tail.strip()!r}")

            if "index" not in aya_elem.attrib or "text" not in aya_elem.attrib:
                raise ValueError("Missing 'index' or 'text' on scripture aya")
            extra_aya_attrs = set(aya_elem.attrib.keys()) - {"index", "text", "bismillah"}
            if extra_aya_attrs:
                raise ValueError(f"Unexpected attributes on scripture aya: {extra_aya_attrs}")

            aya_idx = _parse_canonical_int(aya_elem.attrib["index"], field_name="scripture.aya.index")
            text_val = aya_elem.attrib["text"]
            if not text_val.strip():
                raise ValueError(f"Blank aya text for coordinate {sura_idx}:{aya_idx}")

            coord_key = f"{sura_idx}:{aya_idx}"
            if coord_key in result:
                raise ValueError(f"Duplicate aya coordinate in scripture: {coord_key}")
            if coord_key not in coordinates.ayah_ids:
                raise ValueError(f"Unexpected aya coordinate {coord_key!r} not in coordinates")

            result[coord_key] = text_val
            sura_aya_count += 1

        if sura_aya_count == 0:
            raise ValueError(f"Empty sura {sura_idx} in scripture")

    if len(seen_sura_indices) != 114:
        missing_suras = sorted(set(range(1, 115)) - seen_sura_indices)
        raise ValueError(f"Scripture sura inventory incomplete: expected 114 suras, got {len(seen_sura_indices)}, missing: {missing_suras[:5]}")

    if len(result) != len(coordinates.ayah_ids):
        missing = sorted(set(coordinates.ayah_ids.keys()) - set(result.keys()))
        raise ValueError(f"Scripture missing {len(missing)} ayahs, first missing: {missing[:5]}")

    return result
