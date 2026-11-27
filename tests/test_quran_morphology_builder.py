"""Tests for isolated Quran morphology builder and parser."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import zipfile
import pytest

from pipelines.common.lock_inputs import author_lock, canonical_entries_json
from tests.fixtures.tanzil import (
    SURAH_AYAH_COUNTS,
    build_synthetic_metadata_xml,
)

from pipelines.builders.build_quran_morphology import (
    CorpusSegment,
    _load_morphology_license,
    build_quran_morphology,
    load_morphology_license,
    main,
    read_corpus_segments,
)

DUMMY_CORPUS_NOTICE = b"# PLEASE DO NOT REMOVE OR CHANGE THIS COPYRIGHT BLOCK\n# Corpus Notice\n"
DUMMY_TANZIL_NOTICE = b"# PLEASE DO NOT REMOVE OR CHANGE THIS COPYRIGHT BLOCK\n# Tanzil Notice\n"


def _make_corpus_tsv_content(records: list[str]) -> bytes:
    header = "LOCATION\tFORM\tTAG\tFEATURES\r\n"
    lines = [DUMMY_CORPUS_NOTICE.decode("ascii"), DUMMY_TANZIL_NOTICE.decode("ascii"), header]
    lines.extend(records)
    return "".join(lines).encode("utf-8")


def _make_corpus_zip(tsv_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("quranic-corpus-morphology-0.4.txt", tsv_bytes)
    return buf.getvalue()


def _setup_synthetic_morphology_env(
    root: Path,
    tsv_records: list[str],
    *,
    license_status: str = "pending",
    reviewed_by: str | None = None,
) -> tuple[Path, Path]:
    sources_root = root
    raw_root = root / "raw-inputs"
    corpus_dir = raw_root / "corpus"
    tanzil_dir = raw_root / "tanzil"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    tanzil_dir.mkdir(parents=True, exist_ok=True)

    out_dir = root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Tanzil metadata
    meta_xml = build_synthetic_metadata_xml()
    (tanzil_dir / "quran-data-Tanzil.xml").write_text(meta_xml, encoding="utf-8")

    # 2. Corpus morphology ZIP
    zip_bytes = _make_corpus_zip(_make_corpus_tsv_content(tsv_records))
    (corpus_dir / "quranic-corpus-morphology-0.4.zip").write_bytes(zip_bytes)

    # 3. Registry
    registry_data = {
        "corpus/quranic-corpus-morphology-0.4.zip": {
            "packIds": ["quran-morphology"],
            "sourceUrl": "https://corpus.quran.com/download/",
            "sourceVersion": "0.4",
        },
        "tanzil/quran-data-Tanzil.xml": {
            "packIds": ["quran-morphology"],
            "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml",
            "sourceVersion": "1.0",
        },
    }
    sources_dir = root / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    reg_path = sources_dir / "registry.json"
    reg_path.write_text(json.dumps(registry_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # 4. Lock
    lock_path = sources_dir / "manifest.lock.json"
    author_lock(reg_path, raw_root, lock_path)

    # 5. Evidence files and License
    evidence_dir = sources_dir / "licenses" / "evidence" / "quran-morphology"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "CORPUS-NOTICE.txt").write_bytes(b"CORPUS NOTICE")
    (evidence_dir / "TANZIL-NOTICE.txt").write_bytes(b"TANZIL NOTICE")
    (evidence_dir / "GPL-3.0.txt").write_bytes(b"GPL-3.0 TEXT")
    (evidence_dir / "REBUILD.md").write_text("# Rebuild\n", encoding="utf-8")
    (evidence_dir / ".gitattributes").write_text("* -text\n", encoding="utf-8")

    lic_data = {
        "licenseSpdx": "GPL-3.0",
        "packId": "quran-morphology",
        "primaryPath": "corpus/quranic-corpus-morphology-0.4.zip",
        "reviewedBy": reviewed_by,
        "sources": [
            {
                "attribution": "Kais Dukes / Quranic Arabic Corpus and Tanzil Project",
                "name": "Quranic Arabic Corpus",
                "notice": "See notice files",
                "path": "corpus/quranic-corpus-morphology-0.4.zip",
                "permittedUses": [],
                "rightsEvidence": [
                    "https://corpus.quran.com/license.jsp",
                    "sources/licenses/evidence/quran-morphology/CORPUS-NOTICE.txt",
                    "sources/licenses/evidence/quran-morphology/TANZIL-NOTICE.txt",
                    "sources/licenses/evidence/quran-morphology/GPL-3.0.txt",
                ],
                "sourceUrl": "https://corpus.quran.com/download/",
                "sourceVersion": "0.4",
            },
            {
                "attribution": "Tanzil Project (https://tanzil.net/)",
                "name": "Tanzil Quran Metadata",
                "notice": "<quran type=\"metadata\" version=\"1.0\" copyright=\"(C) 2008-2009 Tanzil.info\" license=\"cc-by\">",
                "path": "tanzil/quran-data-Tanzil.xml",
                "permittedUses": [],
                "rightsEvidence": [
                    "https://tanzil.net/docs/quran_metadata",
                ],
                "sourceUrl": "https://tanzil.net/res/text/metadata/quran-data.xml",
                "sourceVersion": "1.0",
            },
        ],
        "status": license_status,
    }
    lic_file = sources_dir / "licenses" / "quran-morphology.json"
    lic_file.write_text(json.dumps(lic_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return sources_root, out_dir


def _open_ro_db(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA trusted_schema = OFF;")
    return conn


def test_fixed_2_3_7_and_2_11_2_concordance_tie(tmp_path: Path) -> None:
    records = [
        # Ayah 2:3 words 1..6 (non-verbs)
        "(2:3:1:1)\t{l~a*iyna\tREL\tSTEM|POS:REL|LEM:{l~a*iY\r\n",
        "(2:3:2:1)\tbi\tP\tSTEM|POS:P|LEM:bi\r\n",
        "(2:3:3:1)\tgayobi\tN\tSTEM|POS:N|LEM:gayob|ROOT:gyb\r\n",
        "(2:3:4:1)\tbi\tP\tSTEM|POS:P|LEM:bi\r\n",
        "(2:3:5:1)\tS~alaw`pa\tN\tSTEM|POS:N|LEM:Salaw`p|ROOT:Slw\r\n",
        "(2:3:6:1)\tmi\tP\tSTEM|POS:P|LEM:min\r\n",
        # (2:3:7) razaqa + na` + humo (Form I, PERF, ACT)
        "(2:3:7:1)\trazaqo\tV\tSTEM|POS:V|PERF|LEM:razaqa|ROOT:rzq|1P\r\n",
        "(2:3:7:2)\tna`\tPRON\tSUFFIX|PRON:1P\r\n",
        "(2:3:7:3)\thumo\tPRON\tSUFFIX|PRON:3MP\r\n",
        # Ayah 2:11 word 1 (non-verb)
        "(2:11:1:1)\t<i*aA\tT\tSTEM|POS:T|LEM:<i*aA\r\n",
        # (2:11:2) qiyla (Form I, PERF, PASS)
        "(2:11:2:1)\tqiyla\tV\tSTEM|POS:V|PERF|PASS|LEM:qaAla|ROOT:qwl|3MS\r\n",
    ]
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, records)
    build_quran_morphology(s_root, out_dir)

    db_path = out_dir / "quran-morphology.db"
    assert db_path.is_file()

    conn = _open_ro_db(db_path)
    try:
        cur = conn.cursor()
        # Verify verb_concordance
        cur.execute(
            "SELECT id, verb_buckwalter, verb_ar, root_ar, verb_form, verb_form_roman, "
            "total_count, perf_count, impf_count, impv_count, active_count, passive_count, frequency_rank "
            "FROM verb_concordance ORDER BY frequency_rank ASC;"
        )
        conc_rows = cur.fetchall()
        assert len(conc_rows) == 2

        # Tied counts: qwl ranks before rzq
        row_qwl = conc_rows[0]
        assert row_qwl[1] == "qaAla"
        assert row_qwl[4] == 1
        assert row_qwl[5] == "I"
        assert row_qwl[6] == 1  # total_count
        assert row_qwl[7] == 1  # perf_count
        assert row_qwl[10] == 0  # active_count
        assert row_qwl[11] == 1  # passive_count
        assert row_qwl[12] == 1  # frequency_rank = 1

        row_rzq = conc_rows[1]
        assert row_rzq[1] == "razaqa"
        assert row_rzq[4] == 1
        assert row_rzq[5] == "I"
        assert row_rzq[6] == 1  # total_count
        assert row_rzq[7] == 1  # perf_count
        assert row_rzq[10] == 1  # active_count
        assert row_rzq[11] == 0  # passive_count
        assert row_rzq[12] == 2  # frequency_rank = 2

        # Verify verb words
        cur.execute("SELECT word_key, is_verb, verb_form, verb_tense, verb_voice FROM words WHERE is_verb = 1 ORDER BY id ASC;")
        word_rows = cur.fetchall()
        assert len(word_rows) == 2
        assert word_rows[0] == ("2:3:7", 1, 1, "PERF", "ACT")
        assert word_rows[1] == ("2:11:2", 1, 1, "PERF", "PASS")

        # Verify morphology_segments for 2:3:7 and 2:11:2
        cur.execute(
            "SELECT words.word_key, morphology_segments.segment_number, morphology_segments.segment_role, morphology_segments.form_buckwalter "
            "FROM morphology_segments JOIN words ON words.id = morphology_segments.word_id "
            "WHERE words.word_key IN ('2:3:7', '2:11:2') ORDER BY morphology_segments.id ASC;"
        )
        seg_rows = cur.fetchall()
        assert len(seg_rows) == 4
        assert seg_rows[0] == ("2:3:7", 1, "STEM", "razaqo")
        assert seg_rows[1] == ("2:3:7", 2, "SUFFIX", "na`")
        assert seg_rows[2] == ("2:3:7", 3, "SUFFIX", "humo")
        assert seg_rows[3] == ("2:11:2", 1, "STEM", "qiyla")

        # Verify completeness is complete even in synthetic fixture with only 2 words
        meta = dict(cur.execute("SELECT key, value FROM pack_meta;").fetchall())
        assert meta["completeness"] == "complete"
    finally:
        conn.close()


def test_earliest_verb_among_noun_and_two_verbs(tmp_path: Path) -> None:
    records = [
        "(1:1:1:1)\tsomi\tN\tSTEM|POS:N|LEM:{som|ROOT:smw\r\n",
        "(1:1:1:2)\tfaEala\tV\tSTEM|POS:V|PERF|LEM:faEala|ROOT:fEl|3MS\r\n",
        "(1:1:1:3)\tkataba\tV\tSTEM|POS:V|PERF|LEM:kataba|ROOT:ktb|3MS\r\n",
    ]
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, records)
    build_quran_morphology(s_root, out_dir)

    conn = _open_ro_db(out_dir / "quran-morphology.db")
    try:
        cur = conn.cursor()
        cur.execute("SELECT word_key, primary_pos, is_verb, verb_form, verb_tense FROM words;")
        w_row = cur.fetchone()
        assert w_row == ("1:1:1", "V", 1, 1, "PERF")

        # Check that root is fEl and lemma is faEala (the earliest verb), not smw or ktb
        cur.execute("SELECT root_buckwalter FROM roots JOIN words ON words.root_id = roots.id;")
        assert cur.fetchone()[0] == "fEl"
        cur.execute("SELECT lemma_buckwalter FROM lemmas JOIN words ON words.lemma_id = lemmas.id;")
        assert cur.fetchone()[0] == "faEala"

        # Check all 3 segments are retained
        cur.execute("SELECT segment_number, pos_tag FROM morphology_segments ORDER BY id ASC;")
        segs = cur.fetchall()
        assert segs == [(1, "N"), (2, "V"), (3, "V")]
    finally:
        conn.close()


def test_invalid_verb_tokens_v_close_paren_and_pas(tmp_path: Path) -> None:
    # 1. Invalid form V)
    records_v = [
        "(1:1:1:1)\tfaEala\tV\tSTEM|POS:V|PERF|V)|LEM:faEala|ROOT:fEl|3MS\r\n",
    ]
    s_root_1, out_dir_1 = _setup_synthetic_morphology_env(tmp_path / "case1", records_v)
    with pytest.raises(Exception, match=r"(?i)(invalid|grammar|token|form)"):
        build_quran_morphology(s_root_1, out_dir_1)

    # 2. Invalid voice PAS
    records_pas = [
        "(1:1:1:1)\tfaEala\tV\tSTEM|POS:V|PERF|PAS|LEM:faEala|ROOT:fEl|3MS\r\n",
    ]
    s_root_2, out_dir_2 = _setup_synthetic_morphology_env(tmp_path / "case2", records_pas)
    with pytest.raises(Exception, match=r"(?i)(invalid|grammar|token|voice)"):
        build_quran_morphology(s_root_2, out_dir_2)
def test_record_permutation_yields_identical_db_bytes(tmp_path: Path) -> None:
    records_a = [
        "(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n",
        "(1:1:1:2)\tsomi\tN\tSTEM|POS:N|LEM:{som|ROOT:smw\r\n",
        "(1:1:2:1)\t{ll~ahi\tPN\tSTEM|POS:PN|LEM:All~ah|ROOT:Alh\r\n",
    ]
    records_b = [
        "(1:1:2:1)\t{ll~ahi\tPN\tSTEM|POS:PN|LEM:All~ah|ROOT:Alh\r\n",
        "(1:1:1:2)\tsomi\tN\tSTEM|POS:N|LEM:{som|ROOT:smw\r\n",
        "(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n",
    ]

    s_root_a, out_dir_a = _setup_synthetic_morphology_env(tmp_path / "env_a", records_a)
    s_root_b, out_dir_b = _setup_synthetic_morphology_env(tmp_path / "env_b", records_b)

    build_quran_morphology(s_root_a, out_dir_a)
    build_quran_morphology(s_root_b, out_dir_b)

    conn_a = _open_ro_db(out_dir_a / "quran-morphology.db")
    conn_b = _open_ro_db(out_dir_b / "quran-morphology.db")
    try:
        meta_a = dict(conn_a.execute("SELECT key, value FROM pack_meta;").fetchall())
        meta_b = dict(conn_b.execute("SELECT key, value FROM pack_meta;").fetchall())
        # Different input order in locked ZIP yields different true build_input_sha256
        assert meta_a["build_input_sha256"] != meta_b["build_input_sha256"]

        for tbl in ("roots", "lemmas", "verb_concordance", "words", "morphology_segments"):
            rows_a = conn_a.execute(f"SELECT * FROM {tbl} ORDER BY id;").fetchall()
            rows_b = conn_b.execute(f"SELECT * FROM {tbl} ORDER BY id;").fetchall()
            assert rows_a == rows_b, f"Mismatch in semantic table {tbl}"
    finally:
        conn_a.close()
        conn_b.close()

    # Two independent builds on the exact same intentionally shuffled locked input yield 100% identical DB bytes
    out_dir_b_rebuild = tmp_path / "env_b_rebuild"
    build_quran_morphology(s_root_b, out_dir_b_rebuild)
    assert (out_dir_b / "quran-morphology.db").read_bytes() == (out_dir_b_rebuild / "quran-morphology.db").read_bytes()


def test_short_lemma_variant_identity_preserved_display_drops_2(tmp_path: Path) -> None:
    records = [
        "(1:1:1:1)\tmaEa\tN\tSTEM|POS:N|LEM:maE|ROOT:mEy\r\n",
        "(1:1:2:1)\tmaEa\tN\tSTEM|POS:N|LEM:maE2|ROOT:mEy\r\n",
    ]
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, records)
    build_quran_morphology(s_root, out_dir)

    conn = _open_ro_db(out_dir / "quran-morphology.db")
    try:
        cur = conn.cursor()
        cur.execute("SELECT lemma_buckwalter, lemma_ar FROM lemmas ORDER BY id ASC;")
        lemmas = cur.fetchall()
        assert len(lemmas) == 2
        # lemma_buckwalter preserves the trailing 2
        assert lemmas[0][0] == "maE"
        assert lemmas[1][0] == "maE2"
        # Arabic display omits the trailing 2: "مَع"
        assert lemmas[0][1] == "مَع"
        assert lemmas[1][1] == "مَع"
    finally:
        conn.close()


def test_duplicate_compensating_for_missing_segment_rejected(tmp_path: Path) -> None:
    # Word has 3 segments total, but segment 2 is missing and segment 3 appears twice: (1, 3, 3)
    records = [
        "(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n",
        "(1:1:1:3)\tsomi\tN\tSTEM|POS:N|LEM:{som|ROOT:smw\r\n",
        "(1:1:1:3)\tsomi\tN\tSTEM|POS:N|LEM:{som|ROOT:smw\r\n",
    ]
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, records)
    with pytest.raises(Exception, match=r"(?i)(duplicate|gap|coordinate)"):
        build_quran_morphology(s_root, out_dir)


def test_header_only_relocked_member_rejected(tmp_path: Path) -> None:
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, [])
    with pytest.raises(Exception, match=r"(?i)(empty|no segment|records|header)"):
        build_quran_morphology(s_root, out_dir)


def test_blank_form_and_tag_rejected(tmp_path: Path) -> None:
    # Blank form
    records_blank_form = ["(1:1:1:1)\t \tN\tSTEM|POS:N|LEM:{som\r\n"]
    s_root_1, out_dir_1 = _setup_synthetic_morphology_env(tmp_path / "bf", records_blank_form)
    with pytest.raises(Exception, match=r"(?i)(blank|form|empty)"):
        build_quran_morphology(s_root_1, out_dir_1)

    # Blank tag
    records_blank_tag = ["(1:1:1:1)\tsomi\t \tSTEM|POS:N|LEM:{som\r\n"]
    s_root_2, out_dir_2 = _setup_synthetic_morphology_env(tmp_path / "bt", records_blank_tag)
    with pytest.raises(Exception, match=r"(?i)(blank|tag|empty)"):
        build_quran_morphology(s_root_2, out_dir_2)


def test_second_unselected_verb_stem_with_invalid_grammar_rejected(tmp_path: Path) -> None:
    # Word has two verb stems: first is valid, second is unselected with invalid voice PAS.
    # Even though first verb is selected, the second stem must still be validated and rejected.
    records = [
        "(1:1:1:1)\tkataba\tV\tSTEM|POS:V|PERF|LEM:kataba|ROOT:ktb|3MS\r\n",
        "(1:1:1:2)\tfaEala\tV\tSTEM|POS:V|PERF|PAS|LEM:faEala|ROOT:fEl|3MS\r\n",
    ]
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, records)
    with pytest.raises(Exception, match=r"(?i)(invalid|voice|grammar|token)"):
        build_quran_morphology(s_root, out_dir)

def test_license_source_url_and_version_reconciled_with_lock(tmp_path: Path) -> None:
    records = ["(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n"]
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, records)
    lic_path = s_root / "sources" / "licenses" / "quran-morphology.json"
    data = json.loads(lic_path.read_text(encoding="utf-8"))

    # Tamper with sourceUrl of Corpus record
    data["sources"][0]["sourceUrl"] = "https://example.org/not-the-locked-source"
    lic_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match=r"(?i)(mismatch|sourceUrl)"):
        load_morphology_license(s_root)

    with pytest.raises(ValueError, match=r"(?i)(mismatch|sourceUrl)"):
        build_quran_morphology(s_root, out_dir)


def test_pending_and_approved_license_validation(tmp_path: Path) -> None:
    records = ["(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n"]
    s_root, _ = _setup_synthetic_morphology_env(tmp_path, records)
    lic_path = s_root / "sources" / "licenses" / "quran-morphology.json"
    data = json.loads(lic_path.read_text(encoding="utf-8"))

    # 1. Pending record with empty rightsEvidence, notice, attribution is permitted by §3.2
    for src in data["sources"]:
        src["rightsEvidence"] = []
        src["notice"] = ""
        src["attribution"] = ""
        src["permittedUses"] = []
    lic_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    loaded = load_morphology_license(s_root)
    assert loaded["status"] == "pending"

    # 2. Pending record cannot have granted permittedUses
    data["sources"][0]["permittedUses"] = ["sqlite_conversion"]
    lic_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?i)(pending|permittedUses)"):
        load_morphology_license(s_root)

    # 3. Non-string element in permittedUses or rightsEvidence produces useful validation error
    data["sources"][0]["permittedUses"] = [{"bad": "type"}]
    lic_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match=r"(?i)(string|type)"):
        load_morphology_license(s_root)

def test_load_morphology_license(tmp_path: Path) -> None:
    records = ["(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n"]
    s_root, _ = _setup_synthetic_morphology_env(tmp_path, records)

    lic = load_morphology_license(s_root)
    assert lic["packId"] == "quran-morphology"
    assert lic["licenseSpdx"] == "GPL-3.0"
    assert lic["status"] == "pending"
    assert lic["reviewedBy"] is None

    # Status approved without reviewer must fail
    lic_file = s_root / "sources" / "licenses" / "quran-morphology.json"
    data = json.loads(lic_file.read_text(encoding="utf-8"))
    data["status"] = "approved"
    data["reviewedBy"] = None
    lic_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match=r"reviewedBy"):
        load_morphology_license(s_root)


    # _load_morphology_license rejects missing or extra lock paths
    with pytest.raises(ValueError, match=r"exact expected paths"):
        _load_morphology_license(s_root, lock_entries=())

def test_preexisting_output_refused(tmp_path: Path) -> None:
    records = ["(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n"]
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, records)
    existing_db = out_dir / "quran-morphology.db"
    existing_db.write_bytes(b"preexisting")

    with pytest.raises(FileExistsError):
        build_quran_morphology(s_root, out_dir)
    assert existing_db.read_bytes() == b"preexisting"


def test_cli_main(tmp_path: Path) -> None:
    records = ["(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n"]
    s_root, out_dir = _setup_synthetic_morphology_env(tmp_path, records)

    ret = main(["--sources-root", str(s_root), "--output-dir", str(out_dir)])
    assert ret == 0
    assert (out_dir / "quran-morphology.db").is_file()

    # Rerun exits with code 1 due to existing output
    ret2 = main(["--sources-root", str(s_root), "--output-dir", str(out_dir)])
    assert ret2 == 1


def test_read_corpus_segments_contract(tmp_path: Path) -> None:
    records = ["(1:1:1:1)\tbi\tP\tPREFIX|bi+\r\n"]
    s_root, _ = _setup_synthetic_morphology_env(tmp_path, records)
    corpus_zip = s_root / "raw-inputs" / "corpus" / "quranic-corpus-morphology-0.4.zip"
    ayah_ids = {"1:1": 1}

    segments = read_corpus_segments(corpus_zip, ayah_ids)
    assert isinstance(segments, tuple)
    assert len(segments) == 1
    seg = segments[0]
    assert isinstance(seg, CorpusSegment)
    assert seg.surah == 1
    assert seg.ayah == 1
    assert seg.word == 1
    assert seg.segment == 1
    assert seg.form_buckwalter == "bi"
    assert seg.form_ar == "بِ"
    assert seg.pos_tag == "P"
    assert seg.features_raw == "PREFIX|bi+"
    assert seg.role == "PREFIX"

    # Test immutability
    with pytest.raises(Exception):
        seg.surah = 2  # type: ignore[misc]


def test_zip_orig_filename_and_unicode_newlines(tmp_path: Path) -> None:
    # 1. Reject ZIP member when orig_filename does not match expected name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo("quranic-corpus-morphology-0.4.txt")
        info.filename = "quranic-corpus-morphology-0.4.txt\x00evil.txt"
        zf.writestr(info, b"LOCATION\tFORM\tTAG\tFEATURES\r\n")
    zip_path = tmp_path / "tampered.zip"
    zip_path.write_bytes(buf.getvalue())

    with pytest.raises(ValueError, match=r"Unexpected ZIP entry name"):
        read_corpus_segments(zip_path, {"1:1": 1})

    # 2. Unicode line separator (U+2028) is not treated as a record delimiter
    tsv_content = (
        "LOCATION\tFORM\tTAG\tFEATURES\r\n"
        "(1:1:1:1)\tbi\u2028bad\tP\tPREFIX|bi+\r\n"
    ).encode("utf-8")
    zip_buf2 = _make_corpus_zip(tsv_content)
    zip_path2 = tmp_path / "unicode_newline.zip"
    zip_path2.write_bytes(zip_buf2)
    with pytest.raises(ValueError):
        read_corpus_segments(zip_path2, {"1:1": 1})
