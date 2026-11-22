# `islamic-core-content` — Implementation Plan

> **Goal:** Ship verified Islamic data packs, starting with the first audited Quran and Adhan release, then expanding to bilingual Hadith, QUL datasets, and developer SDKs.

---

## 1. Current Asset Readiness Audit

### Present locally; not yet staged, locked, or license-cleared (Milestone 1 — `v0.1.0`)

| Local material | Target | Release blocker |
| --- | --- | --- |
| Tanzil metadata and six text XMLs | `quran-core.db`, `quran-scripts-extended.db` | Verify exact notices, pinned versions, SHA-256 and redistribution terms. |
| `en.pickthall.xml` (Tanzil header: 2010-09-04) | `quran-tr-en-pickthall.db` | **Not cleared.** Review the actual edition and Tanzil terms. |
| `ar.jalalayn.xml` (Tanzil header: 2010-12-01) | `quran-tafsir-ar-jalalayn.db` | **Not cleared.** Review the actual edition and Tanzil terms. |
| `quranic-corpus-morphology-0.4.zip` | `quran-morphology.db` | Resolve both Corpus and embedded Tanzil obligations and corresponding-source delivery. |
| Six listed Adhan MP3s | `audio-adhan-core` | **None cleared.** Identities suggested by local filenames are not evidence of attribution or rights. |

### In Progress (Milestones 2 & 3 — `v0.2.0` / `v0.3.0`)
* **Hadith Books (Arabic + English):** Currently being collected into standardized per-book **JSON** files (replacing the older agent DB that dropped English fields).
* **Extended Tafsirs (Arabic + English):** Collecting in unified **JSON** format (from QUL or verified upstream sources).
* **QUL Downloads (JSON):** Word-by-Word (`wbw-en` + others), Tajweed scripts, Mushaf page layouts, and recitation segment timestamps.

---

## 2. Raw Input Staging Guide (For Your Ongoing Collection)

Only the paths below are builder inputs. A clean checkout contains neither `Contant/` nor `raw-inputs/`. For local staging, copy exactly the nine listed Tanzil XML basenames from flat `Contant/` to `raw-inputs/tanzil/` without renaming (`Contant/tanzil/` is not a source path). Copy `Contant/quranic-corpus-morphology-0.4.zip` to `raw-inputs/corpus/` unchanged; use only its `quranic-corpus-morphology-0.4.txt` member, not the loose extraction. Do not glob, use other exports, or commit raw data.

```text
raw-inputs/
├── tanzil/
│   ├── quran-data-Tanzil.xml
│   ├── quran-uthmani.xml
│   ├── quran-simple-clean.xml
│   ├── quran-uthmani-min.xml
│   ├── quran-simple.xml
│   ├── quran-simple-plain.xml
│   ├── quran-simple-min.xml
│   ├── en.pickthall.xml
│   └── ar.jalalayn.xml
├── corpus/
│   └── quranic-corpus-morphology-0.4.zip
├── audio/adhan/
│   ├── abdul-basit-abdul-samad.mp3
│   ├── abdul-basit-abdul-samad-2.mp3
│   ├── mustafa-ismail.mp3
│   ├── mahmoud-khalil-al-hussary.mp3
│   ├── abul-ainain-shuaisha.mp3
│   └── mohamed-refaat.mp3
├── hadith/
│   ├── bukhari.json
│   ├── muslim.json
│   └── ...
└── qul/
    ├── wbw-en.json
    ├── tafsir-ar-ibn-kathir.json
    └── ...
```

Copy each exact filename below from `Contant/Adhan/` to `raw-inputs/audio/adhan/{slug}.mp3`. The same stable slug identifies both outputs.

| Exact local basename | Stable slug |
| --- | --- |
| `Adhan - Abdul Basit Abdul Samad.mp3` | `abdul-basit-abdul-samad` |
| `Adhan - Abdul Basit Abdul Samad 2.mp3` | `abdul-basit-abdul-samad-2` |
| `Adhan - Mustafa Ismail.mp3` | `mustafa-ismail` |
| `Adhan - Mahmoud Khalil Al-Hussary.mp3` | `mahmoud-khalil-al-hussary` |
| `Adhan - Abul Ainain Shu’aisha.mp3` | `abul-ainain-shuaisha` |
| `Adhan - Mohamed Refaat.mp3` | `mohamed-refaat` |

The release profile requires exactly the 16 paths above. Assign `quran-uthmani.xml` and `quran-simple-clean.xml` to `quran-core`; the four alternate script XMLs to `quran-scripts-extended`; `en.pickthall.xml` to `quran-tr-en-pickthall`; `ar.jalalayn.xml` to `quran-tafsir-ar-jalalayn`; the Corpus ZIP to `quran-morphology`; and all six MP3s to `audio-adhan-core`. The Tanzil metadata XML belongs to all five SQLite packs (canonical coordinates). Document actual source identity and edition in `sources/registry.json`.

On a fresh release runner, download from the authorized locked HTTPS URLs except the explicit `bundlePaths` in `sources/release-v0.1.0.json`. For that subset only, obtain `content-v0.1.0-authorized-inputs.zip` from access-controlled storage using `CONTENT_AUTHORIZED_INPUT_BUNDLE_URL` in the protected release environment. Require the bundle's member set to equal `bundlePaths` before extraction; reject duplicate/case-colliding, linked and unsafe entries. Stage each file at its exact lock path; reject absent, mismatched or unapproved inputs before building or publishing. `Contant/` is never a CI acquisition route.

### v1 Hadith input (`raw-inputs/hadith/{book}.json`)

One JSON object per collection with exactly three arrays: `books` (nonempty), `chapters` (may be empty), and `hadiths` (nonempty). All fields in the example are required; positive integers identify rows within each array. `chapterId` is a positive integer or `null`. `urn` and all textual values except `books[].number` and `hadiths[].hadithNumber` are nonempty strings or `null`; those two numbers are required nonempty strings, not integers. `reference` is a required object with exactly `inBook` and `uscMsa` (each nullable nonempty string). `textSplitMode` is the enum `unsplit_raw` or `source_verified`, never null. Both split fields must be null for `unsplit_raw`; `source_verified` requires nonblank `arabicFull`, `arabicSanad` and `arabicMatn` and evidence of the upstream split. No heuristic or regex splitting. Full text, English components, grades and both reference values are retained unchanged.

IDs (`id`, `bookId`, and any non-null `chapterId`) must fit SQLite signed 64-bit positive integers: `1`–`9223372036854775807`. Reject `9223372036854775808` in schema validation.

```json
{
  "books":[{"id":1,"number":"1","nameAr":"كتاب","nameEn":null}],
  "chapters":[{"id":1,"bookId":1,"number":null,"titleAr":null,"titleEn":null}],
  "hadiths":[{"id":1,"bookId":1,"chapterId":1,"hadithNumber":"1a","urn":null,"arabicFull":"رَبِّ","englishFull":null,"englishNarrator":null,"englishBody":null,"gradeAr":null,"gradeEn":null,"reference":{"inBook":"Book 1, Hadith 1","uscMsa":null},"textSplitMode":"unsplit_raw","arabicSanad":null,"arabicMatn":null}]
}
```

Book/chapter/record IDs are the DB IDs; each non-null `urn` is unique in the collection. `bookId` must resolve; a non-null `chapterId` must resolve to a chapter in the same book. Missing flags equal `1` exactly when the respective full text is null. Store `reference` as a compact UTF-8 JSON object with keys in lexical order. Test the explicit-null example and a verified bilingual case; reject omitted `englishFull`, undeclared fields, duplicate IDs/non-null URNs, unresolved or cross-book references, or a supplied split under `unsplit_raw`. Shape errors fail schema validation; cross-record checks are semantic.

If any row is `source_verified`, also lock `hadith/{book}.splits.json` to the same pack. This is provenance evidence, not a second builder input dialect: an object keyed by canonical decimal record ID, one entry per verified row and none for unsplit rows. Each value has exactly `sourceRef` (nonblank edition-specific reference), `arabicSanad` and `arabicMatn` (nonblank upstream values). Check #2 matches split fields exactly to this independent transcription of the upstream split; reject missing, extra or mismatched evidence. Phase 0 uses only a synthetic split record.

### v1 grouped Tafsir input (`raw-inputs/qul/tafsir-{lang}-{slug}.json`)

A nonempty JSON array. Each object has exactly `verse_keys` (nonempty array of non-null strings), `text` (nonblank string), and `format` (`plain` or `html`). No nullable or optional fields. Verse keys have form `S:A` with positive decimal integers and no leading zeros. Validate against the canonical verse inventory; within each group keys are strictly ascending consecutive verses in one surah, and groups are ordered by numeric first verse and cannot overlap. The exact union must equal the profile's expected key set (`ARCHITECTURE.md` §6.4 and §9).

```json
[
  {"verse_keys":["1:1"],"text":"رَبِّ","format":"plain"},
  {"verse_keys":["1:2","1:3"],"text":"<p>مثال</p>","format":"html"}
]
```

Accept this singleton/range pair against an oracle of `1:1`–`1:3`; reject wrong types or formats at schema validation, and reject unknown keys, gaps, overlaps, descending keys, cross-surah groups and map rows outside their entry at semantic validation. NDJSON is upstream only; both schemas reject it as builder input.

---

## 3. Phased Execution Roadmap

### Phase 0: Repository Bootstrap & Contracts
**Objective:** Establish the skeleton, contracts and a synthetic-only gate. No production pack builders, inputs, registry/lock, source clearance, release manifests or tag workflow are in Phase 0.

* [x] **Bootstrap and pin tooling.** Initialize Git, root `LICENSE` (MIT for repository code), and `.gitignore` (`/Contant/`, `/raw-inputs/`, `/dist/`, `*.db`, `*.m4a`, `*.caf`). Before staging, require all six paths printed by `git check-ignore -- Contant/example raw-inputs/example dist/example example.db example.m4a example.caf`. Track `ARCHITECTURE.md` and `PLAN.md` with exact case. Scope: `pipelines/toolchain/Dockerfile`, `pipelines/toolchain/identity.json`, `pipelines/toolchain/check_runtime.py`, `.dockerignore` and `requirements-test.txt`. Use the reference toolchains in `ARCHITECTURE.md` §5. `.dockerignore` permits only the files used by the image build, not local data.
  * `requirements-test.txt` pins `pytest==8.3.5`, `iniconfig==2.0.0`, `packaging==24.2`, `pluggy==1.5.0`, `colorama==0.4.6` (Windows only), `jsonschema==4.23.0`, `attrs==25.3.0`, `jsonschema-specifications==2024.10.1`, `referencing==0.36.2` and `rpds-py==0.24.0`. Install platform wheels with recorded SHA-256 hashes (`python -m pip install --only-binary=:all: --require-hashes -r requirements-test.txt`). Use `Draft202012Validator` plus explicit standard-library HTTPS/UTC checks; no validator format extras. The media probe is the pinned ffprobe executable, not a Python package.
  * `identity.json` is keyed by `linux-x64` and `windows-x64`. Each value has `artifacts` (object keyed by artifact basename, values exactly `{url, sha256}`), `pythonVersion`, `sqliteVersion`, `sqliteSourceId`, `sqliteCompileOptions` (sorted string array), `ffmpegVersion` and `ffprobeVersion` (complete `-version` stdout, normalized to LF). Obtain CPython source/installer from the official [3.13.3 release](https://www.python.org/downloads/release/python-3133/), verify the source release signature and Windows installer Authenticode. For [SQLite's amalgamation](https://sqlite.org/2025/sqlite-amalgamation-3490100.zip), verify `sqlite3.c` against the [3.49.1 release SHA3-256](https://sqlite.org/releaselog/3_49_1.html). Use the [FFmpeg source archive](https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz) and verify its upstream `.asc` signature. Hash those verified archives and the official GyanD ZIP from §5 at bootstrap; subsequent jobs use the recorded SHA-256, never learn it from a new download. Use PyPI's published hashes for the exact wheels.
  * `python -m pipelines.toolchain.check_runtime` exits nonzero on identity or capability mismatch; it also runs before fixture/pack writes and gate validation. Use the active pinned Python (`sys.executable`) and resolve `ffmpeg` and `ffprobe` from the pinned toolchain directories prepended to `PATH`; reuse those resolved absolute paths. Do not fall back after a mismatch. Linux bootstrap: `docker build --platform linux/amd64 -t content-reference -f pipelines/toolchain/Dockerfile .`; run the PR verification steps below in a single `docker run --rm -v "$PWD:/repo:ro" -w /repo content-reference` session with a writable temp root. Windows installs the pinned interpreter, ZIP and hashed wheel set first, then runs the same steps natively.
* [x] **Freeze contracts before implementation.** Use `ARCHITECTURE.md` §§3.1, 5–7, 9 for lock, helper, DDL, catalog, audio and gate behavior; use §2 here for input shapes. Do not let fixtures act as an alternate schema.
* [x] **Write schemas and synthetic fixtures.** Scope: `specs/sql/common_meta.sql` plus `quran_core.v1.sql`, `quran_scripts_extended.v1.sql`, `quran_translation.v1.sql`, `quran_tafsir.v1.sql`, `quran_morphology.v1.sql`, `quran_wbw.v1.sql`, `hadith_book.v1.sql` in the same directory. Each pack SQL follows common metadata in a fresh database. JSON Schema files: `specs/catalog.schema.json`, `specs/audio-manifest.schema.json`, `specs/inputs/hadith-input.schema.json`, `specs/inputs/tafsir-input.schema.json`. Test files: `tests/__init__.py`, `tests/test_contracts.py`, `tests/test_quality_gate.py`, and `tests/fixtures/` (text-only; includes `__init__.py`, `build.py`, `expectations.json` and synthetic input examples). No tracked DBs or audio.
* [x] **Implement shared helpers.** Scope: `pipelines/common/lock_inputs.py`, `pipelines/common/sqlite_utils.py`, `pipelines/common/search_text.py`, `pipelines/common/audio_probe.py`, and package `__init__.py` files for `pipelines/`, `pipelines/common/`, `pipelines/validators/`. Implement the §3.1 CLI and §5 APIs in `ARCHITECTURE.md`. Share `normalize_arabic(text: str) -> str` and `project_tafsir_text(text: str, *, text_format: str, language: str) -> str` in `search_text.py` using that document's §6; expose its §7.2 probe API in `audio_probe.py`. Only synthetic locks are created. Prove exact-path/hash verification, no rewrite on verify, foreign keys on every writer, FTS rebuild/optimize, and final `ANALYZE`/`VACUUM`.
* [x] **Implement the gate and PR CI.** Scope: `pipelines/validators/quality_gate.py` and `.github/workflows/verify-pr.yml`. Implement the `fixture` entry point and applicable checks first; wire the `v0.1.0` profile in Task 1.7. CI verifies exact-case contract/fixture paths on Linux, uses the pinned reference environment and also validates logical fixture outcomes on Windows. It never invokes Phase 1 builders or needs production acquisition or publish credentials.

PR verification, in order: verify `git ls-files --error-unmatch -- PLAN.md ARCHITECTURE.md tests/fixtures/build.py tests/fixtures/expectations.json` on the Linux host and verify the exact SQL/schema/input paths listed here; then in the selected runtime, from repo root, execute these commands with `TMP` substituted by the same absolute OS temp directory throughout (Linux `mktemp -d`; Windows an unused directory inside `$env:RUNNER_TEMP`). Stop at the first nonzero exit. On a read-only container mount use `PYTHONDONTWRITEBYTECODE=1` and disable the pytest cache.

```sh
python -m pipelines.toolchain.check_runtime
python -m pytest -q -p no:cacheprovider
python -m tests.fixtures.build --root TMP
python -m pipelines.validators.quality_gate --profile fixture --candidate TMP/candidate --rebuild TMP/rebuild --sources-root TMP/source --expectations tests/fixtures/expectations.json
```

**Fixture protocol:** `python -m tests.fixtures.build --root TMP` creates fresh `TMP/candidate`, `TMP/rebuild` and `TMP/source`. Under `TMP/source/raw-inputs/`, use only the paths below. Author one synthetic registry/lock and the synthetic license records in `TMP/source/sources/`, using §3.1–3.2 of `ARCHITECTURE.md`. Every raw path belongs only to the listed pack; set `sourceUrl` to `https://example.org/fixture/{path}`, `sourceVersion` to `1` as a string, and primary source `name` to `Synthetic fixture`. Generate only temporary binaries.

| Raw path(s) | Format and membership |
| --- | --- |
| `sqlite/{pack-id}.json` | For `quran-core`, `quran-scripts-extended`, `quran-tr-en-fixture`, `quran-morphology`, and `quran-wbw-en`: copy the same paths from `tests/fixtures/inputs/`. Each JSON object maps content-table names (no metadata/FTS) to arrays of row objects keyed by SQL column. Supply SQL column values directly except derived search columns; derive those with `search_text.py`. |
| `hadith/fixture.json` | Copy `tests/fixtures/inputs/hadith/fixture.json`, the §2 canonical JSON, to build `hadith-fixture`. Copy fields to the snake_case columns, set flags and project verified Matn as specified. |
| `hadith/fixture.splits.json` | Copy `tests/fixtures/inputs/hadith/fixture.splits.json` to `hadith-fixture`; independent synthetic split evidence for ID `2` only. The primary path is `hadith/fixture.json`. |
| `qul/tafsir-ar-fixture.json` | Copy `tests/fixtures/inputs/qul/tafsir-ar-fixture.json` (§2 example) to build `quran-tafsir-ar-fixture` with `1:A → A` for `1:1`–`1:3`. |
| `audio/adhan/fixture.wav` | `audio-adhan-core` master. Generate 2 s, signed 16-bit little-endian PCM, mono, 32 kHz, with 16 samples at `4096` and 16 at `-4096` repeated for a 1 kHz tone. Name the track `Synthetic tone`, slug `fixture`, clip start `0`. Encode both §7.2 variants from the tone directly without production loudness normalization; use the same codec, container and final probe rules. |

Append this second record to the Hadith `hadiths` array in the §2 example:

```json
{"id":2,"bookId":1,"chapterId":1,"hadithNumber":"1b","urn":null,"arabicFull":"إسناد رَبِّ","englishFull":"mercy","englishNarrator":null,"englishBody":"mercy","gradeAr":null,"gradeEn":null,"reference":{"inBook":null,"uscMsa":null},"textSplitMode":"source_verified","arabicSanad":"إسناد","arabicMatn":"رَبِّ"}
```

The independent synthetic evidence file contains `{"2":{"sourceRef":"Synthetic fixture record 2","arabicSanad":"إسناد","arabicMatn":"رَبِّ"}}`; it authorizes the fixture assertion only. The unsplit row never gains a Matn or Arabic search projection.

For each build, read the same locked inputs. Supply fixture table records in forward order to the first build and reverse order to the second **before** sorting by primary key and inserting. #2 compares retained text/markup with the locked source by table and primary key (Hadith by source record ID; Tafsir by first key in each input group). The fixture builder can write manifests in temp roots using actual output facts; no Phase 1 manifest generator is used. Use tag `content-v0.0.0`, pack version `0.0.0`, timestamp `2026-01-01T00:00:00Z`, and URL base `https://example.org/content-v0.0.0/`. Use `["ar","en"]` for WBW and Hadith, `["en"]` for the translation and `["ar"]` for all others; only the core pack sets `required: true`.

`tests/fixtures/expectations.json` is independent of build output. Its root has `packs` (object keyed by pack ID), and `audioSlugs` (array `["fixture"]`). Each SQLite pack entry has `keys` (ascending canonical coverage keys; Hadith IDs are integers, all others are strings), `coverage` (required §5 metrics as a metric-to-integer object), and `fts` (array of `{table, query, rowIds}` objects; `query` is a literal string and `rowIds` a sorted integer array, empty for a negative control; packs without FTS use `[]`). `audio-adhan-core` has an empty object. Use `quran-core`, `quran-scripts-extended`, `quran-tr-en-fixture`, `quran-tafsir-ar-fixture`, `quran-morphology`, `quran-wbw-en`, `hadith-fixture` and `audio-adhan-core`. Verse fixtures cover surah 1, `1:1`–`1:3`; word fixtures cover `1:1:1` and `1:1:2`; Hadith IDs `1` (unsplit, Arabic present, English absent) and `2` (verified, bilingual) give `expected_total=2`, `ar_present=2`, `en_present=1`, `en_missing=1`. Other fixtures are complete. For the core, Tafsir and Hadith Arabic index put `رَبِّ` only in row 1, 1 and 2 respectively (Hadith row 1 is not indexed in Arabic); test `رَبِّ` and `رب` against those IDs and `كتب` against none. For the translation, WBW and Hadith English index put `mercy` only in row 1, 1 and 2 respectively; `absent` must yield none. Grouped Tafsir uses the §2 example; include more than one SQL row per populated test table where applicable.

For fixture #1 also compare the full primary-key sets for core `surahs` and morphology `roots`, `lemmas`, `words`, `morphology_segments` and `verb_concordance` to the corresponding locked row files, plus Hadith `books` and `chapters` to the locked canonical input. Tafsir entry intervals/IDs and map are compared in #9 against the grouped input. Test #2 rejects a decoded lone surrogate, escaped U+FFFD, a BLOB in a TEXT column and a mismatched verified split.

**Definition of Done:** From repo root run `python -m pytest -q`, the fixture recipe above in an OS temp directory and `python -m pipelines.validators.quality_gate --profile fixture --candidate TMP/candidate --rebuild TMP/rebuild --sources-root TMP/source --expectations tests/fixtures/expectations.json`. A valid pair returns `0` with #8 not applicable; its mixed Hadith pack has `matn_split_verified='true'`. Verify failures for malformed UTF-8 or U+FFFD (#2), invalid DB schema or foreign key (#3), empty FTS index with populated content (#4), changed checksum, invalid manifest schema, wrong CAF codec, a valid 29.0 s CAF at 32 kHz, and an omitted file declared in a manifest (#5), actual distributable byte difference while both roots separately pass (#6), missing license (#7), and Tafsir interval/map mismatch (#9). Refresh affected hashes for mutations not testing #5, and assert each relevant check/asset is reported nonzero. Test wrong-case, duplicate/case-colliding, missing, traversal, symlink and mismatch at the lock boundary. Windows runs the same two-build fixture gate on its pinned local stack, with no comparison against Linux bytes.

For the #6-only negative case, append one valid empty MP4 `free` box (`0000000866726565` hex) to `adhan-fixture-full.m4a` in `B` and update that manifest's `sizeBytes` and `sha256`. Both roots pass their own checks but differ in distributable bytes. Also reject the same physical root supplied twice (#6), floating `1.0` for integer version fields (#5), and a malformed expectations file (exit `2`). Verify canonical literal non-ASCII UTF-8 and `2.0`/`1.5` duration spellings per `ARCHITECTURE.md` §7.

---

### Phase 1: `v0.1.0` — Five Quran packs and core Adhan audio

**Entry:** Phase 0 DoD passes. Stage and inventory the exact §2 assets, author and review `sources/registry.json` and `sources/manifest.lock.json`, and establish a fresh-runner acquisition route. Use `ARCHITECTURE.md` §§3.1–3.2 for lock and reviewed clearance; pending records permit only authorized local review, not release CI. Pickthall, Jalalayn and all six masters are not cleared. Do not silently substitute another edition, recording or smaller inventory.

Unless qualified, builder section references below point to `ARCHITECTURE.md`. In Task 1.1, own the shared `pipelines/common/tanzil_coordinates.py` used by all five SQLite builders and the release gate. Run each production builder as `python -m pipelines.builders.<module> --sources-root S --output-dir OUT`; it verifies the lock entries for its own pack, reads under `S/raw-inputs/` and writes only its named final assets into `OUT`, refusing existing output files. The release runner separately verifies the complete lock and approval set before any builder runs.

* [x] **Task 1.1 (`quran-core.db`):** Implement `pipelines/builders/build_quran_core.py` with §6.1 key/boundary rules and `ayahs_fts`. Check all 114 surahs and 6,236 IDs; specifically `1:7 → 7`, `2:1 → 8`, and `114:6 → 6236`. Test boundaries, exact-match sajdah, separate/non-shipped `bismillah`, unsorted inputs and duplicate-with-omission rejection. Record the actual Tanzil notices for core.
* [ ] **Task 1.2 (`quran-scripts-extended.db`):** Implement `pipelines/builders/build_quran_scripts_extended.py` with the four named scripts and shared metadata. Join by canonical keys, not row position; verify the exact key sets and original text. Review all source notices.
* [ ] **Task 1.3 (`quran-tr-en-pickthall.db`):** Implement `pipelines/builders/build_quran_translation.py` with §6.1 keys, preserved XML text, `translations_fts`, and the exact edition's narrowly scoped leading-notice handling. Approve the edition's record only with evidence covering the intended redistribution and transformation.
* [ ] **Task 1.4 (`quran-tafsir-ar-jalalayn.db`):** Implement `pipelines/builders/build_quran_tafsir.py`. For release, use §6.1 XML handling and one `plain` entry per Jalalayn ayah. Test grouped QUL JSON with synthetic fixtures only in Phase 1; `S:A` singleton, `S:A-B` inclusive same-surah range, no gaps inside a group, no overlap, exact expected union and one map row per ayah. Reject cross-surah intervals and map rows outside their entry. Clear the actual Jalalayn edition before release.
* [ ] **Task 1.5 (`quran-morphology.db` — isolated `GPL-3.0`):** Implement `pipelines/builders/build_quran_morphology.py` from the named ZIP member using §6.5 selection, transliteration, grouping and ranks. Test the fixed Form I active/passive fixture, a multi-stem word and malformed markers. The `(root, form, lemma)` breakdown counts canonical words, not affixes; the upstream reference groups by root and form. Preserve both notices and authorize corresponding-source delivery under §3.2; no Corpus-derived content goes into core or another pack.
* [ ] **Task 1.6 (`audio-adhan-core`):** Implement `pipelines/builders/build_audio_adhan.py` with §5/§7.2's pinned FFmpeg/ffprobe build, safe subprocess arguments, normalization, clipping and final-file probes. Review each recording's source URL, identity, attribution, clip start and rights for both renditions; write the track review facts to `sources/licenses/audio-adhan-core.json`. Own both encoded assets and their probe facts, not release manifests. Require CAF `ima4`/`adpcm_ima_qt`, encode target ≤28.5 s and strict checked duration `<29.0 s`.
* [ ] **Task 1.7 (Release Pipeline):** Run `python -m pipelines.release.generate_catalog --sources-root S --asset-root OUT` for each independent build. Only this renderer writes the two release manifests; use the reviewed timestamp, release URLs and finalized asset facts. Own the companion ZIP and verbatim notice copies (§§3.2, 7.1). Create `.github/workflows/release-packs.yml` and the `v0.1.0` gate profile; build and probe in the pinned Linux §5 image.

**Inventory:** `quran-core.db`, `quran-scripts-extended.db`, `quran-tr-en-pickthall.db`, `quran-tafsir-ar-jalalayn.db`, `quran-morphology.db`; exactly two files per §2 slug (`adhan-{slug}-full.m4a`, `adhan-{slug}-ios-notif.caf`); `catalog.json`, `audio-manifest.json` (19 pack/manifest files total), plus `quran-morphology-source.zip` and any separately required notices listed in `sources/release-v0.1.0.json`. No other attachments. Enforce each `<2 GiB` and a maximum of 1,000.

1. **Prepare and tag:** Review the exact source and license files to be tagged. From those same bytes and the verified locked inputs, assemble candidate files and companions; record companion hashes in `sources/release-v0.1.0.json` and generate canonical manifests with final URLs. Commit `catalog/catalog.json`, `catalog/audio-manifest.json`, the release descriptor, approved rights evidence and lock/registry to that reviewed commit; tag it `content-v0.1.0`. No companion or pack bytes go in Git.
2. **Verify from tag, before uploads:** From a clean checkout `S` of that tag acquire inputs under `S/raw-inputs/` per §2 and verify every expected size/hash and approval. Rebuild into two empty roots `A` and `B`; in each manifest render probe both audio variants and refuse mismatches before writing. Compare both manifest pairs to the tracked bytes before invoking `python -m pipelines.validators.quality_gate --profile v0.1.0 --candidate A --rebuild B --sources-root S`. No upload on input, approval, probe or gate failure; no threshold overrides. PR CI receives no production credentials.
3. **Validate draft, then publish:** Attach the validated inventory only to a draft release for that tag. Fetch via authenticated draft asset downloads, verify hashes, exact attachment set and manifest bytes, open the core DB read-only and run the pinned FTS query below. Publish only if all draft checks pass. From a clean consumer with no checkout or private credentials, fetch public manifests, assets and rights companions using `https://github.com/{owner}/{repo}/releases/download/content-v0.1.0/{filename}` and repeat the inventory, hash and read-only checks. Draft downloads use asset IDs, not the still-unpublished URLs.

**Definition of Done:** All six packs pass applicable #1–#7 and #9, #8 is explicitly not applicable, both clean builds and the tagged manifests agree, and every source/edition and rendition has reviewed clearance. A fresh consumer can download, hash and query the release: the core FTS phrase `رب العالمين` restricted to `rowid=2` returns row 2 (`1:2`); use `mode=ro` and check `user_version=1`. Do not complete Phase 1 or publish a partial set if any pack or companion fails. A substitute or a reduced inventory requires an explicit spec change.

---

### Phase 2: Milestone `v0.2.0` — Bilingual Hadith Packs
**Objective:** Fix the missing English fields from prior builds and ship audited, bilingual Hadith SQLite databases.
* [ ] **Task 2.1 (Ingestion & Audit):** Validate each collection's `raw-inputs/hadith/{book}.json` against `specs/inputs/hadith-input.schema.json`; handle `{book}.splits.json` separately as the §2 evidence contract.
* [ ] **Task 2.2 (Builder):** Implement `build_hadith_book.py`:
  * Preserve `arabic_full`, `english_full`, `english_narrator`, `english_body`, grades, and structured references.
  * Strictly enforce `text_split_mode` (`source_verified` vs. `unsplit_raw`); zero heuristic or regex splitting.
  * Compute `is_ar_missing`, `is_en_missing`, and exact `coverage_report` metrics.
  * Populate `hadiths_fts` strictly per the Matn/English indexing policy.
* [ ] **Task 2.3 (License Verification):** Document exact source URL, edition, translator, and Sunnah.com redistribution terms in `sources/licenses/hadith-{book}.json`.
* [ ] **Task 2.4 (Pilot & Rollout):** Ship `hadith-bukhari.db` and `hadith-muslim.db` first, then expand to remaining Kutub al-Sittah (`abudawud`, `tirmidhi`, `nasai`, `ibnmajah`) and Nawawi/Riyad us-Salihin.

**Definition of Done:** Every Hadith pack accurately reports Arabic and English coverage, passes FTS5 tests, never falsely declares `"completeness": "complete"` when translations are missing, and passes bit-for-bit determinism.

---

### Phase 3: Milestone `v0.3.0` — QUL Integration (WBW, Tajweed, Extended Tafsirs & Recitations)
**Objective:** Layer QUL's rich JSON datasets onto the canonical `surah:ayah:word` coordinate system while auditing per-resource licenses.
* [ ] **Task 3.1 (QUL License Audit):** Audit each selected QUL dataset individually (WBW English, modern English translation such as Saheeh International, Ibn Kathir AR/EN, Tafsir Al-Muyassar, Mushaf layouts, Tajweed) and record redistribution status in `sources/licenses/`. Remember: QUL's GitHub `MIT` license covers its software, not every hosted dataset.
* [ ] **Task 3.2 (`quran-wbw-en.db`):** Implement `build_quran_wbw.py` ingesting QUL WBW JSON keyed by `word_key` (`surah:ayah:word`) so it joins cleanly with `quran-morphology.db` via `ATTACH DATABASE`.
* [ ] **Task 3.3 (Quality Gate #8 — Word Alignment):** Implement cross-pack validator verifying word counts per ayah between `quran-morphology.db` (QAC 0.4) and `quran-wbw-en.db` (QUL), deterministically normalizing non-word waqf/pause tokens and logging any deltas in `coverage_report`.
* [ ] **Task 3.4 (Extended Tafsirs & Modern Translations):** Compile collected QUL Tafsir JSONs (`ar-ibn-kathir`, `en-ibn-kathir`, `ar-muyassar`, `ar-saadi`) and `en.sahih` using existing builders.
* [ ] **Task 3.5 (Tajweed & Mushaf Layouts):** Finalize `quran_mushaf.v1.sql` and build `quran-mushaf-{layout}.db` (line-by-line Madani/IndoPak page mapping + Tajweed markup).
* [ ] **Task 3.6 (Recitation Audio Manifests):** Define an explicit new manifest contract for QUL reciter metadata and ayah/word timing; do not add recitation fields to the closed Adhan v1 schema. Keep multi-GB audio libraries external and timing packs lightweight.
* [ ] **Task 3.7 (Global Adhan Expansion):** Add license-cleared Makkah, Madinah, Al-Aqsa, and Fajr Adhans to `audio-adhan-core`.

**Definition of Done:** A cross-database `ATTACH` query combining `quran-core.db` + `quran-morphology.db` + `quran-wbw-en.db` returns verb frequency concordance with English word meanings and full ayah text in under 10 ms.

---

### Phase 4: Milestone `v1.0.0` — Developer SDK & First Consumer Integration
**Objective:** Make integration effortless for your React Native / Expo app and external TypeScript developers without coupling the data repo to any app.
* [ ] **Task 4.1 (`packages/core-sdk`):** Build a zero-UI TypeScript package providing:
  * Typed `Catalog` and `AudioManifest` parsers with runtime schema validation.
  * Diff engine (`getAvailableUpdates(remoteCatalog, installedPacks)`).
  * Transport-agnostic download, `sizeBytes`, and `SHA-256` verification interface.
* [ ] **Task 4.2 (`packages/expo-adapter`):** Build an optional Expo helper implementing:
  * Atomic install flow: temporary download -> size & SHA-256 verification -> `PRAGMA quick_check` & `user_version` check -> atomic rename into local SQLite directory.
  * `user.db` `installed_packs` registry helper (keeping user state strictly separate from content `.db` files).
  * iOS `.caf` Adhan installation helper targeting `/Library/Sounds`.
* [ ] **Task 4.3 (Consumer Smoke Test):** Integrate `@islamic-core/expo-adapter` into your first consumer app and verify FTS5 (`enableFTS: true`) plus iOS/Android Adhan playback on physical devices.

**Definition of Done:** First consumer app installs, verifies, updates, and queries packs end-to-end with zero hardcoded content inside the app repository.

---

### Phase 5: Open-Source Launch & Sustainability
* [ ] Write `README.md` with quick-start SQL snippets (Python, Swift, Kotlin, Flutter, Expo) and a transparent per-pack license table.
* [ ] Write `CONTRIBUTING.md` explaining how community contributors can propose new translations, tafsirs, or Hadith books via PR + license audit.
* [ ] Evaluate optional `.db.gz` or `.zip` compressed release assets (`v1.1+`) with automated Android and iOS decompression verification.
* [ ] Tag and publish **`v1.0.0`** as a stable public release.

---

## 4. Key Risks & Mitigations

| Risk | Impact | Mitigation |
| :--- | :--- | :--- |
| **QUL Dataset Copyright Mismatch** | Publishing a restricted translation/tafsir under an open-source release violates copyright. | Mandatory per-pack license audit in `sources/licenses/` before CI allows release; never assume QUL's repo `MIT` license covers hosted data. |
| **QAC License Isolation** | Mixing Corpus data into core would combine content with different obligations. | Keep the morphology DB physically separate; preserve both notices, verify approved transforms and deliver corresponding source before publication. |
| **Phase 1 Rights Not Cleared** | Local copies of the two Tanzil editions and six recordings do not establish redistribution rights. | #7 requires approved edition/track evidence and permitted uses; no full-release approval without every record. |
| **QAC vs. QUL Word Count Drift** | Pause signs counted as tokens in one source break `word_key` joins. | Quality Gate #8 validates every ayah's word alignment during CI and strips or maps non-word tokens deterministically. |
| **Unsplit Hadith Search Usability** | If Sunnah.com lacks verified `sanad`/`matn` separation, `arabic_matn_clean` stays `NULL`. | Keep `text_split_mode = 'unsplit_raw'` strictly; decide in Phase 2 whether to expose a clearly named secondary index (`hadiths_full_fts`) for unsplit books rather than polluting `hadiths_fts`. |
| **iOS Notification Silent Fallback** | Apple's limit is `<30 s` and iOS supports multiple codecs; neither is the project acceptance rule. | Quality Gate #5 probes the final CAF container, `ima4`/`adpcm_ima_qt`, and duration `<29.0 s`; the encode target is ≤28.5 s. |
| **GitHub Asset & Repo Limits** | Raw inputs and build outputs bloat Git; over-limit assets cannot be uploaded. | Ignore raw/generated inputs and enforce `<2 GiB` per attachment and at most 1,000 attachments in #5; keep large Quran recitations external. |