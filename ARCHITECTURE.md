# `islamic-core-content` — System Architecture

> **Status:** Draft v1.0 (Baseline Design)  
> **Scope:** Platform-agnostic, free, open-source core Islamic content distribution built on deterministic SQLite databases, standardized audio assets, and a unified JSON catalog.

---

## 1. Vision & Project Boundaries

`islamic-core-content` is an independent, globally reusable data repository designed so any developer—whether building with React Native/Expo, Flutter, Swift, Kotlin, Web/WASM, or backend services—can consume verified Islamic data offline through standard SQLite files and static manifests.

### Explicit Boundaries
* **In Scope:**
  * Upstream source fetchers and cryptographic input lockfiles (`SHA-256`).
  * Canonical SQLite schema specifications (`PRAGMA user_version`) and JSON Schemas.
  * Deterministic, bit-for-bit reproducible SQLite builders and audio transcoding pipelines.
  * License provenance tracking and per-pack coverage reporting.
  * Automated CI Quality Gate (9 checks) and GitHub Releases publishing.
  * Optional, lightweight developer SDKs for catalog parsing and integrity verification.
* **Out of Scope:**
  * UI components, screens, theming, or Mushaf rendering engines.
  * Prayer time calculation algorithms, geolocation, or notification scheduling logic.
  * Audio playback engines or user-specific state (`user.db`, bookmarks, reading history).

### Two-Layer Architecture
1. **Consumable Content Layer (Platform-Agnostic):** Published SQLite `.db` packs, audio assets, `catalog/catalog.json`, and `catalog/audio-manifest.json`. Any app in any language can consume this layer directly without adopting any SDK.
2. **Optional Developer Tooling Layer:** Formal schema definitions (`specs/`), a lightweight TypeScript catalog/verifier library (`packages/core-sdk`), and later an optional Expo adapter (`packages/expo-adapter`).

---

## 2. Repository Structure

Code, schemas, lockfiles, license records, and candidate release manifests live in Git. Local inputs (`Contant/`, `raw-inputs/`), build output (`dist/`), workstation files, and database, archive, and audio packs are excluded by `.gitignore`. PR verification rejects tracked paths that match those rules. `Contant/` is an ignored, local-only staging directory used by staging commands; it is never read directly by artifact builders or used for CI acquisition. `Contant/quran_content/` is a separate legacy local review folder (see `PLAN.md` §2 for disposition), not a sixth import root. Publish built packs only as release assets after clearance.

The tree shows the main interfaces, not every test or toolchain file; `PLAN.md` defines phase-local file scope.

```text
islamic-core-content/
├── ARCHITECTURE.md
├── PLAN.md
├── LICENSE                             # Repo code license (MIT)
├── pytest.ini                          # Scopes discovery strictly to tests/
├── specs/                              # Source of truth for all data contracts
│   ├── catalog.schema.json
│   ├── audio-manifest.schema.json
│   ├── recitation-manifest.schema.json # Recitation timing manifest schema (Phase 3)
│   ├── inputs/
│   │   ├── hadith-input.schema.json    # Validation schema for raw Hadith JSONs
│   │   └── tafsir-input.schema.json    # Validation schema for raw Tafsir JSONs
│   └── sql/
│       ├── common_meta.sql             # Mandatory pack_meta + coverage_report
│       ├── quran_core.v1.sql
│       ├── quran_scripts_extended.v1.sql
│       ├── quran_translation.v1.sql
│       ├── quran_translation_sahih.v1.sql
│       ├── quran_tafsir.v1.sql
│       ├── quran_mushaf.v1.sql
│       ├── quran_morphology.v1.sql
│       ├── quran_wbw.v1.sql
│       ├── hadith_book.v1.sql
│       └── hadith_taqreeb_v1.sql
├── sources/                            # Provenance lockfiles & license registry (no raw dumps)
│   ├── registry.json                   # Explicit pack membership and source identities (50 inputs)
│   ├── manifest.lock.json              # Pinned source URLs, versions, and input SHA-256s
│   ├── adhan-local-intake.json         # Standalone inventory for private local Adhan masters
│   └── licenses/                       # Per-pack redistribution & attribution metadata (36 records)
│       ├── quran-core.json
│       ├── ...
│       └── hadith-taqreeb-wajeez.json
├── pipelines/                          # Python build & QA engine; reference runtime in §5
│   ├── common/                         # SQLite determinism helpers, search normalization, staging
│   │   ├── search_text.py              # Shared Arabic FTS normalization & literal query match
│   │   ├── stage_hadith_inputs.py      # Staging for canonical Hadith JSONs
│   │   ├── stage_qul_inputs.py         # Staging for QUL WBW, Tafsir, Mushaf, Recitation
│   │   └── stage_taqreeb_inputs.py     # Staging for Mashrūʿ Taqrīb Realm JSONs
│   ├── builders/
│   │   ├── build_quran_core.py
│   │   ├── build_quran_scripts_extended.py
│   │   ├── build_quran_translation.py
│   │   ├── build_quran_tafsir.py
│   │   ├── build_quran_morphology.py   # Roots, Lemmas, Verb Concordance, Segments
│   │   ├── build_quran_wbw.py          # QUL Word-by-Word meanings
│   │   ├── build_quran_mushaf.py       # Madani & IndoPak Mushaf layout packs
│   │   ├── build_recitation_manifests.py # Recitation timing manifests
│   │   ├── build_hadith_book.py        # Nine canonical Hadith books
│   │   ├── hadith_taqreeb_builder.py   # Four Mashrūʿ Taqrīb packs
│   │   ├── build_audio_adhan.py        # ffmpeg/ffprobe pipeline (.m4a + iOS .caf)
│   │   └── build_audio_adhan_local.py  # Local private Adhan staging & builder
│   ├── validators/
│   │   ├── quality_gate.py             # Profile- and pack-specific gate (§9)
│   │   └── phase3_concordance.py       # Phase 3 ATTACH concordance validator
│   └── release/
│       ├── build_phase3_local.py       # Deterministic Phase 3 local release orchestrator
│       ├── release_packs.py            # Acquisition, clearance verification, and companion packager
│       └── generate_catalog.py         # Emits v0.1.0 candidate catalog.json & audio-manifest.json
├── catalog/                            # Versioned candidate release manifests committed to Git
│   ├── catalog.json
│   └── audio-manifest.json
├── Contant/                            # GITIGNORED local import only; never a builder/CI path
│   ├── adhan/                          # Original Adhan audio masters
│   ├── audit/                          # Local verification records (not builder inputs)
│   ├── hadith/                         # Upstream Hadith repositories and archives
│   ├── qul/                            # wbw/, tafsir/, translation/, mushaf/, recitation/
│   ├── quran/                          # Tanzil exports, Corpus archive, and other Quran inputs
│   └── quran_content/                  # Legacy local review folder (see PLAN.md §2; not staged)
├── raw-inputs/                         # GITIGNORED local staging directory
└── .github/workflows/
    ├── verify-pr.yml                   # Runs schema tests & quality gate on PRs
    └── release-packs.yml               # Dual-build determinism check + release clearance
```

---

## 3. Upstream Sources, Formats & Licensing Governance

Reformatting upstream data into SQLite does **not** erase its original license or attribution requirements. Every pack must be isolated by license and accompanied by a verified `sources/licenses/{pack-id}.json` file.

| Domain | Upstream Source | Canonical Raw Format | License & Isolation Rule |
| --- | --- | --- | --- |
| **Quran Core & Scripts** | Tanzil (`quran-data`, `uthmani`, `simple-clean`, extended scripts) | **XML** | Verify the notices of the exact files; preserve licensed Quran text unchanged and include attribution and notices. |
| **Baseline Translation & Tafsir** | Tanzil (`en.pickthall`, `ar.jalalayn`) | **XML** | Neither edition is cleared. [Tanzil translation terms](https://tanzil.net/trans/) limit use to noncommercial purposes absent permission; age of the original work does not clear the downloaded edition or its intended redistribution. |
| **Tajweed, WBW, Mushaf Layouts, Extended Tafsirs, Audio Segments** | Quranic Universal Library — QUL (`qul.tarteel.ai`) | **JSON** (preferred over QUL's prebuilt SQLite to enforce our unified schema) | **Critical Rule:** QUL's GitHub `MIT` license covers its Ruby on Rails CMS software, **not** every dataset hosted on QUL. Individual translations, tafsirs, and recitations remain the property of their respective authors/publishers. Each QUL-sourced pack must undergo an individual license check before release. |
| **Roots, Morphology & Verb Concordance** | Quranic Arabic Corpus v0.4 (`corpus.quran.com`) | **TSV (inside ZIP)** | Resolve both the Corpus GPL and the embedded Tanzil notices before shipping; isolate in `quran-morphology.db`. |
| **Hadith Collections (AR + EN)** | Sunnah.com & audited collections | **JSON** for builders | Document edition, translator and redistribution terms per book in `sources/licenses/`. |
| **Adhan Audio** | Two independent inventories: six Phase 1 candidate masters (`audio-adhan-core`, `PLAN.md` §2) and 25 private local intake masters (50 derivatives, `sources/adhan-local-intake.json`, `PLAN.md` §2/Task 3.7) | **MP3** to **M4A + CAF** | Neither inventory is cleared for public redistribution (`status: "pending"`). Require rights and attribution evidence for each master and both derivatives before release. |

### Input Format Standard

* The v1 Hadith and grouped Tafsir builders accept only the UTF-8 JSON shapes in `PLAN.md` §2. NDJSON can be an upstream format, converted explicitly to canonical JSON before validation; it is not a second builder dialect.
* **XML** is retained solely for Tanzil downloads because it is Tanzil's canonical format.
* **CSV** is prohibited for Hadith and Tafsir inputs due to escaping fragility with Arabic/English punctuation and markup.

### 3.1 Locked inputs

`sources/registry.json` is the explicit authoring inventory: a JSON object keyed by exact raw-root-relative path. Each value has exactly `sourceUrl` (absolute HTTPS provenance URL), `sourceVersion` (nonempty edition/version string), and `packIds` (nonempty array of distinct pack IDs, lexically sorted). Multi-pack entries are hashed once and attributed to every listed pack. Never discover inputs by scanning or infer provenance from names. Hadith registry URLs are provenance; `sha256` always hashes staged canonical JSON. For Malik, the URL points to the base edition, and `sourceVersion` content-addresses the selected local composite with the original byte hash. Neither remote raw URL nor old ZIP replaces the locked canonical files. No change to fixture or v0.1.0 acquisition.

`sources/manifest.lock.json` contains exactly `lockVersion` (integer `1`) and `inputs` (array). Each entry contains, in order, `path`, `sourceUrl`, `sourceVersion`, `sizeBytes` (nonnegative integer), and `sha256` (64 lowercase hex digits over raw bytes). Sort entries lexically by `path`. Serialize in fixed key order, compact JSON (no separator whitespace), literal UTF-8 (no BOM), and one trailing LF. Apply the same serialization to pack subsets (§5). Reject unknown/duplicate keys, unsupported versions, mismatched registry fields or entry sets, and noncanonical lock bytes.

Paths use `/`, no empty, `.` or `..` components, and only ASCII alphanumerics plus `.`, `_`, `-` in each component. Reject absolute/drive paths, Windows device names and components ending in a dot, case collisions, symlinks or junctions at any component, and nonregular/missing files. Verify spelling by directory entry even on case-insensitive filesystems. Do not consume unlisted files. Verify size and hash.

From the repository root, `python -m pipelines.common.lock_inputs --registry REGISTRY --raw-root RAW --output LOCK` authors the lock; adding `--verify` reads `LOCK` as the expected lock and exits nonzero on any mismatch without writing. CI uses verify only. The shared production registry and lock started in Phase 1 and include later phases (covering 50 locked inputs across Phases 1–4); the private Adhan intake has its own standalone inventory (`sources/adhan-local-intake.json`). Phase 0 uses temporary inputs and a synthetic registry.

Verification optionally accepts `--pack PACK_ID` (only with `--verify`). Both routes use `verify_lock(registry_path: Path, raw_root: Path, lock_path: Path, *, pack_id: str | None = None) -> tuple[LockEntry, ...]` in `lock_inputs.py`. Always validate the entire registry/lock and their correspondence; with a pack selector, verify raw files and return entries only for that known pack, in lock order. Without it, verify and return all entries. `LockEntry` is the §3.1 v1 entry shape. Unknown pack, malformed inventory or mismatched selected bytes fail; no rewrite.

### 3.2 Clearance records

Each exact-case `sources/licenses/{id}.json` contains `packId`, `licenseSpdx` (applicable SPDX expression or recorded `LicenseRef-*`), `primaryPath` (one source's locked path), `status` (`approved`, `pending`, `rejected`, or `fixture`), `reviewedBy` (nonempty reviewer name for `approved`, else `null`), and `sources` (one object per locked input for this pack, sorted by path). Each source has `path`, `name`, `sourceUrl`, `sourceVersion`, `rightsEvidence` (array of HTTPS URLs or exact-case paths under `sources/licenses/evidence/` in the tagged tree), `notice` and `attribution` (verbatim notice and required credit), and `permittedUses` (distinct values from `public_redistribution`, `commercial_use`, `sqlite_conversion`, `search_projection`, `transcode`, `clip`, `fade`, `loudness_normalization`). `sourceUrl`/`sourceVersion` match the lock. For audio each source also has `slug`, `reciter`, and `clipStartMs` (nonnegative integer) and matches a single track. All named fields required, no unknown fields; only `reviewedBy` may be null. Approved records require nonblank strings and nonempty arrays; pending/rejected records may leave `rightsEvidence`, `notice`, `attribution`, and `permittedUses` empty. Fixture records follow the literals below and are forbidden in release profiles.

#7 checks that a reviewed release record is `approved`, names all locked sources, and permits this project's public redistribution (including commercial consumer use) and the transformations actually used. Require `sqlite_conversion` for SQLite, `search_projection` for packs with FTS or derived normalized text, and `transcode`, `clip`, `fade`, `loudness_normalization` for audio. Keep evidence and actual conditions in the tagged tree (no private credentials); a reviewer must resolve the rights before approving. A record's existence or a reachable URL alone is not approval.

Fixture records have `status: "fixture"`, `reviewedBy: null`, `licenseSpdx: "CC0-1.0"`, `rightsEvidence: ["https://example.org/fixture/synthetic"]`, `notice` and `attribution` both `"Synthetic fixture"`, and `permittedUses: []`. #7 validates these literal fields, the same path/lock/provenance relationships and per-track metadata, but does not require a release approval or fetch fixture evidence.

> **Current Rights & Clearance Baseline:** Exactly 36 tracked per-pack license records in `sources/licenses/` are currently `"status": "pending"` (6 Phase 1 + 9 canonical Hadith + 17 QUL + 4 Taqrīb); the separate `sources/adhan-local-intake.json` is also `"status": "pending"`, but is an intake inventory, not one of those 36 per-pack license files. Local quality gates accept pending provenance for offline verification, whereas the strict release workflow requires approved evidence. QUL Rails CMS software license (`MIT`) does not cover its hosted datasets; `quran-morphology.db` remains strictly isolated under `GPL-3.0`. No local gate pass, committed candidate manifests, or source hashes imply publication rights. The checked-in v0.1.0 candidate manifests predate the changed Arabic index bytes and do not prove a strict HEAD rebuild matches them.

For morphology, preserve the two upstream notice blocks and resolve the terms of the embedded Quran text separately from [Corpus GPLv3](https://corpus.quran.com/license.jsp). If distribution of the DB or its corresponding source is not cleared under both notices, block v0.1.0. For equivalent access, attach `quran-morphology-source.zip` to the same release and link the tagged license record and archive hash next to the DB. Keep the archive out of Git.

The archive contains only the following root-relative members (brace lists enumerate files, not glob patterns). Use ZIP_STORED, lexical member order, no directory entries/comments/extra fields, timestamp `1980-01-01T00:00:00`, Unix regular-file mode `0644` and no archive comment. All members use authorized locked bytes or exact tagged file bytes; exclude manifests and the release input descriptor so its committed archive hash is not self-referential.

* `raw-inputs/corpus/quranic-corpus-morphology-0.4.zip`, `raw-inputs/tanzil/quran-data-Tanzil.xml`
* `ARCHITECTURE.md`, `LICENSE`, `requirements-test.txt`, `.dockerignore`, `sources/{registry.json,manifest.lock.json}`
* `sources/licenses/quran-morphology.json`, `sources/licenses/evidence/quran-morphology/{CORPUS-NOTICE.txt,TANZIL-NOTICE.txt,GPL-3.0.txt,REBUILD.md}`
* `specs/sql/{common_meta.sql,quran_morphology.v1.sql}`
* `pipelines/__init__.py`, `pipelines/toolchain/{Dockerfile,identity.json,check_runtime.py}`, `pipelines/builders/build_quran_morphology.py`, `pipelines/common/{__init__.py,lock_inputs.py,sqlite_utils.py,search_text.py,tanzil_coordinates.py}`

`REBUILD.md` documents the §5 image bootstrap, `python -m pipelines.common.lock_inputs --registry sources/registry.json --raw-root raw-inputs --output sources/manifest.lock.json --verify --pack quran-morphology`, then `python -m pipelines.builders.build_quran_morphology --sources-root . --output-dir dist/morphology` from the archive root. No inputs for other packs are needed.

---

## 4. Modular Pack Architecture

Data is designed as granular, independently downloadable packs so apps only download what they need once cleared for release, keeping licenses cleanly separated. See [PLAN.md §1](PLAN.md#1-current-asset-readiness-audit) for the current milestone implementation and clearance status summary across all packs.

| Pack ID | Format | Compression (v1) | Source | License Isolation |
| --- | --- | --- | --- | --- |
| `quran-core` | SQLite | `none` | Tanzil XML | Exact-file notices and reviewed transformation rights |
| `quran-scripts-extended` | SQLite | `none` | Tanzil XML (4 alt scripts) | Exact-file notices and reviewed transformation rights |
| `quran-tr-en-pickthall` | SQLite | `none` | Tanzil XML | Not cleared for v0.1.0 |
| `quran-tr-en-sahih` | SQLite | `none` | QUL Saheeh International JSON with verse HTML and footnotes | Pending; local only |
| `quran-tafsir-ar-jalalayn` | SQLite | `none` | Tanzil XML | Not cleared for v0.1.0 |
| `quran-tafsir-{lang}-{slug}` | SQLite | `none` | QUL JSON | Audited per-pack |
| `quran-wbw-{lang}` | SQLite | `none` | QUL JSON | Audited per-pack |
| `quran-mushaf-{layout}` | SQLite | `none` | QUL SQLite page lines + QPC Hafs Tajweed JSON | Audited per-pack |
| `quran-morphology` | SQLite | `none` | Quranic Arabic Corpus 0.4 | `GPL-3.0` (isolated) |
| `hadith-{book}` | SQLite | `none` | Nine canonical editions (see `PLAN.md` §2: 7 Sunnah.com snapshots, Malik composite, Ahmad Open-Hadith-Data CSV) | Audited per-pack |
| `hadith-taqreeb-{suffix}` | SQLite | `none` | Ṣāliḥ ash-Shāmī JSON | Pending; local only |
| `audio-adhan-core` | M4A + CAF | Native audio | Six Adhan masters | None cleared for v0.1.0 |
| `audio-recitation-{edition}` | JSON timing manifest | `none` | QUL ayah-by-ayah JSON | Pending; remote audio URLs only |

Al-Qurtubi maps all 6,236 verses, including the two source-empty entries at `34:54` and `59:19`. It preserves the source HTML and reports `ar_present=6234` with partial completeness. Saheeh stores verse HTML and note bodies separately; its search index excludes footnote markers and note text.

> **Compression Strategy:** Post-clearance release v1 is designed to publish uncompressed `.db` files for zero-friction direct download on all mobile runtimes. Optional `.db.gz` or `.zip` variants may be added in a later minor release once verified across Android and iOS extraction pipelines.

---

## 5. SQLite Standard & Deterministic Build Contract

Every generated `.db` file must satisfy these settings. Byte-for-byte reproducibility is measured within the pinned reference environment below, not across operating systems:

```sql
PRAGMA encoding = 'UTF-8';
PRAGMA page_size = 4096;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;  -- No dangling -wal/-shm sidecar files
PRAGMA user_version = 1;       -- Schema version integer (matches catalog.json)

```

### Reference environment

Linux x86-64, `debian:12.11-slim@sha256:cea2634840f5a87503d8210e4df97b9f23a2acd67ff860a76c133d963032f866`, GCC 12.2.0 from the Debian `20250814T000000Z` Bookworm and Bookworm security snapshots. Use `LC_ALL=C.UTF-8` and `TZ=UTC`. Build CPython **3.13.3** (`Python-3.13.3.tar.xz`, default configure, no PGO/LTO); link `_sqlite3` explicitly against SQLite **3.49.1** (`sqlite-amalgamation-3490100.zip`, `-O2 -fPIC -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_MATH_FUNCTIONS -DSQLITE_THREADSAFE=1`, no other SQLite defines). Build FFmpeg and ffprobe **7.1.1** together from `ffmpeg-7.1.1.tar.xz` (`./configure --disable-autodetect --disable-x86asm --disable-doc --disable-ffplay --disable-network`). Verify archive hashes before building; the Phase 0 bootstrap records them and the runtime identity. Use the same image for both builds of every reference byte-equality trial.

Preflight before writing: verify the interpreter and loaded SQLite version/source ID/options, create and query FTS5 in memory, and verify both media tool identities and CAF/codec support. Fail on mismatch. Windows uses the python.org CPython 3.13.3 AMD64 installer and [GyanD FFmpeg 7.1.1 x64 essentials ZIP](https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-essentials_build.zip), pinned by SHA-256 at bootstrap. Record the Windows SQLite version/options from that interpreter and require working FTS5. Run logical fixture checks and compare two local builds on the same runtime, not Windows bytes against Linux build bytes. When published artifacts exist, verify their downloaded hashes and read-only queries on Windows.

### Build Determinism Rules

1. Start with fresh files and reject existing databases/sidecars. Insert every table in ascending full primary-key order (all composite components; numeric order for integers, binary order for text). Assign generated integer IDs from 1 in ascending canonical natural-key order, never encounter order, unless the schema section defines the ID directly. Resolve derived sort ties by the whole natural key.
2. Store no timestamps in SQLite content or metadata tables. A release timestamp is an explicit input to manifest generation (§7.1), not read from the clock.
3. On each writer connection enable foreign keys before any transaction; `foreign_keys` is connection-local, not persisted. After inserts, run `foreign_key_check` and fail on any row. For each declared external-content FTS5 table, issue `INSERT INTO {fts_table}({fts_table}) VALUES('rebuild')` then `optimize` the same way. Omit only those FTS operations for packs declaring no FTS table. Run `ANALYZE`, commit, then `VACUUM` outside any open transaction.

The shared `pipelines/common/sqlite_utils.py` API is `open_pack(path: Path, schema_path: Path) -> sqlite3.Connection`, `write_pack_metadata(conn: sqlite3.Connection, metadata: Mapping[str, str], coverage: Mapping[str, tuple[int, str | None]]) -> None`, and `finalize_pack(conn: sqlite3.Connection, *, fts_tables: tuple[str, ...]) -> None`. `open_pack` sets the PRAGMAs and applies common SQL then the selected pack SQL; it returns the writer with no transaction open. `write_pack_metadata` inserts in key order inside the caller's data transaction. `finalize_pack` checks the FTS tuple against the schema, performs rule 3, and propagates errors; the caller closes and discards output on failure. Use read-only gate connections except for FTS content-consistency commands on temporary copies.

The writer uses explicit transaction control: the caller issues `BEGIN` before inserting rows and leaves that transaction open for finalization. `finalize_pack` requires that transaction, verifies foreign keys remain on, runs foreign-key check, FTS operations and `ANALYZE` in that transaction, commits it, then runs `VACUUM` in autocommit mode. This preserves insert/metadata atomicity up to finalization; discard the file if any step fails.

### Mandatory Metadata Tables (`specs/sql/common_meta.sql`)

Present in **every** SQLite pack without exception:

```sql
CREATE TABLE pack_meta (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
-- Mandatory keys:
-- pack_id, pack_version, schema_version, source_name, source_url,
-- source_version, license_spdx, license_ref, build_input_sha256,
-- completeness ('complete' | 'partial'), fts5_enabled ('true' | 'false')

CREATE TABLE coverage_report (
    metric TEXT NOT NULL PRIMARY KEY,
    count  INTEGER NOT NULL CHECK (typeof(count) = 'integer' AND count >= 0),
    notes  TEXT
);

```

Every listed `pack_meta` key is present exactly once, with a nonblank value, and there are no undeclared keys. `pack_id = catalog.id`, `schema_version = '1'`, and `pack_version`, `license_ref`, `completeness` and `fts5_enabled` agree with the catalog. The three `source_*` values describe the primary source identified by the pack license record; the record lists every source and notice. `build_input_sha256` hashes the canonical JSON **array** (including trailing LF) of only that pack's lock entries selected by registry membership (§3.1). Hadith additionally requires `matn_split_verified` (`'true'` exactly when at least one record is `source_verified`; otherwise `'false'`). Set `coverage_report.notes` to NULL in the fixture and v0.1.0 profiles.

| SQLite pack | `expected_total` population | Other mandatory coverage counts |
| --- | --- | --- |
| Core / extended scripts | Canonical numbered ayahs | `ar_present`: ayahs with every required script nonblank |
| Translation | Expected ayahs for the edition | For `ar`: `ar_present`; for `en`: `en_present`, `en_missing`; otherwise `available_total`. Count ayahs with nonblank translation. |
| Tafsir | Expected ayahs for the work | Same language metrics as translation, counting mapped ayahs with nonblank entry text (not entry count); HTML needs nonblank plain-text projection. |
| Morphology | Canonical `(surah, ayah, word)` keys in pinned Corpus input | `ar_present`: words with nonblank full and clean Arabic |
| WBW | Expected word keys for the edition | `ar_present`: words with nonblank Arabic; target-language metrics follow translation (count nonblank meanings; for `ar`, count keys with both nonblank Arabic fields). |
| Hadith | Expected record IDs for the collection | `ar_present`, `en_present`: IDs with nonblank corresponding full text; `en_missing`: complement of `en_present`. |

Count distinct members of the expected population. `en_missing = expected_total - en_present`. Blank means empty after Unicode whitespace trimming; do not trim persisted text. `completeness` is `complete` only when every expected member has all required content and applicable language coverage and no extra members; otherwise `partial`. Missing text permitted by a profile still needs correct metrics and flags. Catalog `coverage.available` counts members with all that content simultaneously (Hadith: both full texts; WBW: Arabic and meaning). §9 determines allowed partial coverage. No other persisted metrics in the fixture or v0.1.0 profiles.

The local QUL English WBW pack also records the number of removed terminal verse markers (`alignment_markers_removed`), the number and keys of rekeyed verses (`alignment_rekeyed_ayahs`), and each nonzero change in the source row count (`alignment_raw_delta:<verse_key>`). These metrics come from locked QUL meanings, not from the separately licensed morphology pack.

---

## 6. Canonical Database Schemas (v1)

Apply `specs/sql/common_meta.sql` then exactly one checked-in pack schema to a fresh file; do not reproduce DDL in application code. `ayahs_fts`, `translations_fts`, `tafsir_fts`, `word_translations_fts`, `hadiths_fts`, and `hadiths_full_fts` are the only FTS tables, in their respective packs. Extended scripts, morphology, and both Mushaf layouts declare none. Set `pack_meta.fts5_enabled` to `'true'` and catalog `requiresFts5` to `true` exactly for packs with a declared FTS table. Check #3 verifies both against the actual schema; #4 verifies indexed contents.

**Arabic FTS rule.** For all Arabic FTS search projections and client query literals, `pipelines/common/search_text.py` is authoritative (`normalize_arabic_search`):
- **Letter foldings:** Fold exactly `أ` U+0623, `إ` U+0625, `آ` U+0622, and `ٱ` U+0671 → `ا` U+0627; `ى` U+0649 → `ي` U+064A; `ة` U+0629 → `ه` U+0647.
- **Mark deletions:** Delete exactly code points U+0610–U+061A, U+0640, U+064B–U+065F, U+0670, U+06D6–U+06ED, U+0897–U+089F, U+08CA–U+08FF, U+10EC5, U+10EFA–U+10EFF, and presentation forms U+FE70–U+FE72, U+FE74, U+FE76–U+FE7F.
- **Preserved characters:** Keep unlisted characters unchanged, including `ء`, `ؤ`, `ئ`, Persian `ی` U+06CC, whole-word ligatures, and non-Arabic combining marks.
- **Projection-only boundary:** Do not apply Unicode normalization (no NFC/NFKD/NFKC), case change, whitespace change, or extra folding. Normalization applies strictly to search projections and client query literals; never modify persisted source text (including HTML). Do not extend normalization to English text, WBW translation/transliteration, or the non-FTS morphology retained `word_ar_clean`.
- **Target Arabic FTS indexes:**
  - **Phase 1:** Quran core `ayahs_fts` (`ayahs.text_ar_search` projected from `text_simple_clean`) and Jalalayn `tafsir_fts` (`text` projected to `text_ar_search`);
  - **Phase 2:** Canonical Hadith verified Matn in `hadiths_fts` (`arabic_matn_clean`) and unsplit Arabic in `hadiths_full_fts` (`arabic_full_clean`);
  - **Phase 3:** Five Arabic QUL Tafsirs in `tafsir_fts` (`text_ar_search` after plain-text projection);
  - **Phase 4:** Mashrūʿ Taqrīb `hadiths_fts` on `text_ar_search`.
- **Client query contract:** Normalize user literals via `arabic_match_literal` **before** quoting as FTS phrases (trim, double embedded `"`, and wrap in outer `"`) and binding to `MATCH ?`; empty normalization yields no search (`None`). See §8 for consumer implementation details and examples.
- **Tokenizer caveat:** SQLite's [`unicode61 remove_diacritics`](https://www.sqlite.org/fts5.html#unicode61_tokenizer) handles Latin-script diacritics, not Arabic. `رَبِّ` and `رب` must produce the same expected row IDs; the fixture's `كتب` query must return none.

### 6.1 `quran-core.db` (`specs/sql/quran_core.v1.sql`)

Built from `quran-data-Tanzil.xml`, `quran-uthmani.xml`, and `quran-simple-clean.xml`.

Read a single canonical coordinate map from `quran-data-Tanzil.xml`: sura `index` is the numeric surah ID, `ayas` its verse count and `start` is a **zero-based** prefix sum. `start_ayah_id = start + 1`; for a one-based `aya.index`, `ayah_id = start + aya.index` and `verse_key = sura.index:aya.index` in decimal. Map `name`, `tname`, `ename`, `type`, `order`, and `rukus` directly to the corresponding surah columns. Require contiguous sura IDs and exact prefix sums. In v0.1.0 require the full 114/6,236 inventory: `1:7 → 7`, `2:1 → 8`, `114:6 → 6236`.

Use the metadata's `juzs/juz`, `hizbs/quarter`, `manzils/manzil`, `rukus/ruku`, and `pages/page` start records: each ayah gets the `index` of the most recent start by `ayah_id`, including the start itself. Verify exact index ranges, strictly increasing valid start keys and first start `1:1`. Use `sajdas/sajda` `type` only at a matching coordinate; elsewhere `NULL`. Reject duplicate coordinates, missing or out-of-range keys and coordinate/ID discrepancies.

Use this map to join every Tanzil XML in §§6.1–6.4 by `(sura.index, aya.index)`, never by encounter order. Require each numbered text file's exact key set; a duplicate with a compensating omission fails. Preserve XML-decoded `text` without alteration. `bismillah` attributes are neither additional numbered ayahs nor part of the numbered `text`; v0.1.0 has no separate stored representation of the attributes.

Use strict UTF-8 and strict parsing of the XML body. The pinned Pickthall and Jalalayn editions have a single leading notice comment whose hyphen separators violate XML comment grammar. Preserve its exact bytes as rights evidence. For these two inputs only, after the XML declaration remove that single immediately following `<!-- … -->` block from the parser view (not the hashed input); parse the remaining XML strictly. Reject missing/unterminated notice blocks or malformed document bodies; do not repair any other content or allow DTDs/external entities. The seven other Tanzil XMLs are parsed as-is.

```sql
CREATE TABLE surahs (
    id                INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 114),
    name_ar           TEXT NOT NULL,
    name_tname        TEXT NOT NULL,
    name_en           TEXT NOT NULL,
    revelation_type   TEXT NOT NULL CHECK (revelation_type IN ('Meccan', 'Medinan')),
    revelation_order  INTEGER NOT NULL,
    ayah_count        INTEGER NOT NULL,
    ruku_count        INTEGER NOT NULL,
    start_ayah_id     INTEGER NOT NULL
);

CREATE TABLE ayahs (
    id                INTEGER PRIMARY KEY CHECK (id BETWEEN 1 AND 6236),
    surah_id          INTEGER NOT NULL REFERENCES surahs(id),
    ayah_number       INTEGER NOT NULL,
    verse_key         TEXT NOT NULL UNIQUE, -- "2:255"
    text_uthmani      TEXT NOT NULL,
    text_simple_clean TEXT NOT NULL,
    text_ar_search    TEXT NOT NULL, -- Arabic projection of text_simple_clean
    juz               INTEGER NOT NULL CHECK (juz BETWEEN 1 AND 30),
    hizb_quarter      INTEGER NOT NULL CHECK (hizb_quarter BETWEEN 1 AND 240),
    manzil            INTEGER NOT NULL CHECK (manzil BETWEEN 1 AND 7),
    ruku              INTEGER NOT NULL CHECK (ruku BETWEEN 1 AND 556),
    page_madani       INTEGER NOT NULL CHECK (page_madani BETWEEN 1 AND 604),
    sajdah_type       TEXT CHECK (sajdah_type IN ('recommended', 'obligatory')),
    UNIQUE (surah_id, ayah_number)
);

CREATE INDEX idx_ayahs_surah ON ayahs(surah_id, ayah_number);
CREATE INDEX idx_ayahs_juz   ON ayahs(juz);
CREATE INDEX idx_ayahs_page  ON ayahs(page_madani);

CREATE VIRTUAL TABLE ayahs_fts USING fts5(
    text_ar_search,
    content='ayahs',
    content_rowid='id',
    tokenize='unicode61'
);

```

### 6.2 `quran-scripts-extended.db` (`specs/sql/quran_scripts_extended.v1.sql`)

Built from Tanzil's four secondary XML scripts (`uthmani-min`, `simple`, `simple-plain`, `simple-min`) plus the metadata coordinate map (§6.1). Join all four script texts by the canonical keys and require all four for every key.

```sql
CREATE TABLE ayah_scripts (
    ayah_id           INTEGER PRIMARY KEY CHECK (ayah_id BETWEEN 1 AND 6236),
    verse_key         TEXT NOT NULL UNIQUE,
    text_uthmani_min  TEXT NOT NULL,
    text_simple       TEXT NOT NULL,
    text_simple_plain TEXT NOT NULL,
    text_simple_min   TEXT NOT NULL
);

```

### 6.3 `quran-tr-{lang}-{slug}.db` (`specs/sql/quran_translation.v1.sql`)

Use the §6.1 coordinate map and exact verse-key coverage for Pickthall. Store the edition text unchanged. Future editions reuse the SQL template.

```sql
CREATE TABLE translations (
    ayah_id     INTEGER PRIMARY KEY CHECK (ayah_id BETWEEN 1 AND 6236),
    surah_id    INTEGER NOT NULL,
    ayah_number INTEGER NOT NULL,
    verse_key   TEXT NOT NULL UNIQUE,
    text        TEXT NOT NULL
);

CREATE VIRTUAL TABLE translations_fts USING fts5(
    text,
    content='translations',
    content_rowid='ayah_id',
    tokenize='unicode61'
);

```

### 6.4 `quran-tafsir-{lang}-{slug}.db` (`specs/sql/quran_tafsir.v1.sql`)

Supports both 1:1 ayah tafsirs (`ar.jalalayn`) and multi-ayah grouped tafsirs (e.g., Ibn Kathir, Sa'di) without text duplication.

Jalalayn has one `plain` entry per XML aya using §6.1 coordinates. Grouped input uses `PLAN.md` §2; require ascending contiguous groups and exact expected coverage, with no overlap. `group_key` is `S:A` for a singleton or `S:A-B` for an inclusive same-surah range (`B > A`, decimal without leading zeros); reject cross-surah ranges. Entry ID is `start_ayah_id`. Create one map row for each covered ayah, with the matching `verse_key` and the ID of its containing entry; reject rows outside the interval or any extra/missing map rows.

```sql
CREATE TABLE tafsir_entries (
    id            INTEGER PRIMARY KEY,
    group_key     TEXT NOT NULL UNIQUE, -- "1:1" or "2:1-5"
    start_ayah_id INTEGER NOT NULL CHECK (start_ayah_id BETWEEN 1 AND 6236),
    end_ayah_id   INTEGER NOT NULL CHECK (end_ayah_id BETWEEN 1 AND 6236),
    text          TEXT NOT NULL,
    text_format   TEXT NOT NULL CHECK (text_format IN ('plain', 'html')),
    text_search   TEXT NOT NULL,
    CHECK (start_ayah_id <= end_ayah_id)
);

CREATE TABLE tafsir_ayah_map (
    ayah_id   INTEGER PRIMARY KEY CHECK (ayah_id BETWEEN 1 AND 6236),
    verse_key TEXT NOT NULL UNIQUE,
    entry_id  INTEGER NOT NULL REFERENCES tafsir_entries(id)
);

CREATE INDEX idx_tafsir_map_entry ON tafsir_ayah_map(entry_id);

CREATE VIRTUAL TABLE tafsir_fts USING fts5(
    text_search,
    content='tafsir_entries',
    content_rowid='id',
    tokenize='unicode61'
);

```

Populate `text_search` from plain text or a plain-text projection of retained HTML. Use Python `HTMLParser(convert_charrefs=True)`: collect text-node data in source order, omit comments and `script`/`style` bodies, and join runs with a single ASCII space; index no tag or attribute content. Apply the Arabic rule for `ar`; for other languages leave projected text unchanged. Rendering or sanitizing retained HTML is a consumer responsibility.

### 6.5 `quran-morphology.db` (`specs/sql/quran_morphology.v1.sql` — Isolated `GPL-3.0`)

Read only the named `quranic-corpus-morphology-0.4.txt` member of `quranic-corpus-morphology-0.4.zip`, never the adjacent extracted file. Skip the leading comment/blank lines after retaining both notices. Require the exact tab-separated header `LOCATION`, `FORM`, `TAG`, `FEATURES` and four columns per record. Parse `LOCATION` as `(surah:ayah:word:segment)` with positive decimal integers. Resolve verses with §6.1; word and segment numbering begin at 1 with no gaps, and each full coordinate is unique. Keep raw `FORM`, `TAG`, and `FEATURES` in segment columns; the first `|` token of `FEATURES` supplies `PREFIX`, `STEM` or `SUFFIX`. `FORM` is transliterated into `form_ar`.

Use the following ordered ASCII-to-Unicode pairs (hex code points, one-to-one by position), with ASCII space preserved. A single trailing `2` in a `LEM:` value is a Corpus variant label: preserve it in the raw lemma identity and omit it only in Arabic display, including `verb_ar`. Reject unmapped characters elsewhere in `FORM`, `LEM`, or `ROOT`; do not transliterate raw features. Small waw and ya are letters; keep them in the clean view.

```text
' > & < } A b p t v j H x d * r z s $ S D T Z E g _ f q k l m n h w Y y
0621 0623 0624 0625 0626 0627 0628 0629 062A 062B 062C 062D 062E 062F 0630 0631 0632 0633 0634 0635 0636 0637 0638 0639 063A 0640 0641 0642 0643 0644 0645 0646 0647 0648 0649 064A
F N K a u i ~ o ^ # ` { : @ " [ ; , . ! - + % ]
064B 064C 064D 064E 064F 0650 0651 0652 0653 0654 0670 0671 06DC 06DF 06E0 06E2 06E3 06E5 06E6 06E8 06EA 06EB 06EC 06ED
```

Group by numeric `(surah, ayah, word)`, concatenate Arabic forms by numeric segment number (no inserted separator), and derive `word_ar_clean` by deleting code points U+0610–U+061A, U+064B–U+065F, U+0670, U+06D6–U+06DC, U+06DF–U+06E4, U+06E7–U+06E8, and U+06EA–U+06ED. Choose the earliest verb stem (`TAG=V`) if present, otherwise the earliest `STEM`; if no stem, use the first segment's tag and no root/lemma. Use the selected stem's tag for `primary_pos` and its `ROOT:` and `LEM:` values when present, otherwise `NULL`; do not borrow from later stems. If a stem has `POS:`, it must agree with `TAG`. Verbs require root, lemma, and exactly one tense (`PERF`, `IMPF`, `IMPV`). Absent parenthesized form means I; explicit `(I)`–`(XII)` map to 1–12. Absent voice means `ACT`; `PASS` or `ACT` are valid explicit values. Fail duplicate or contradictory field values, invalid or malformed form/tense/voice markers; nonverbs leave verb fields null. Count each verb by word, not by affix or by all stems.

For verb stems, after `STEM|POS:V`, the remaining pipe-delimited tokens are a closed grammar: one nonempty `LEM:…`, one nonempty `ROOT:…`, one tense (`PERF`, `IMPF` or `IMPV`), and at most one each of form `(I)`–`(XII)`, voice `ACT`/`PASS`, person `1S`, `1P`, `2D` or `[23][FM][DPS]`, mood `MOOD:JUS`/`MOOD:SUBJ`, and special `SP:kaAd`/`SP:kaAn`. Those remaining tokens may appear in any order; reject unknown, empty, duplicated or out-of-grammar tokens. In particular `V)` and `PAS` are errors, not absent form/voice. Omitted form and voice still default to I and ACT. Preserve all original features.

Group roots by raw root and lemmas by raw lemma. Root totals count selected words; noun totals use `N` and `PN`, and verb totals use `V`. Set `root_ar_spaced` by joining transliterated root characters with ASCII spaces. For each lemma choose its most frequent `(root, primary_pos)` among selected words (ties: null root first, then raw root and POS in ascending binary order). The verb concordance groups by `(raw root, numeric form, raw lemma)`, a **lemma-level breakdown**; the [upstream concordance](https://corpus.quran.com/verbs.jsp) groups by root and form. Use the group lemma for `verb_buckwalter` and its transliteration for `verb_ar`. IDs use ascending raw root, raw lemma, or concordance tuple respectively; word IDs use numeric word coordinates, segment IDs numeric four-part coordinates. Ranks are one-based consecutive positions sorted by descending total count then the respective natural key ascending. Sum counters by tense and voice of the selected word.

Fixed fixture: `razaqo` + `na` followed by a backtick + `humo` at `2:3:7` with primary `STEM|POS:V|PERF|LEM:razaqa|ROOT:rzq|1P` gives one `(rzq, 1, razaqa)` occurrence, `PERF=1`, `ACT=1`. `qiyla` at `2:11:2` with `STEM|POS:V|PERF|PASS|LEM:qaAla|ROOT:qwl|3MS` gives `(qwl, 1, qaAla)` with `PERF=1`, `PASS=1`. The two groups tie; `qwl` ranks before `rzq`. Verify an additional multi-stem word and malformed form in tests; no live page is a test oracle.

```sql
-- All unique Quranic roots ranked by total & verbal frequency
CREATE TABLE roots (
    id                INTEGER PRIMARY KEY,
    root_buckwalter   TEXT NOT NULL UNIQUE, -- "qwl"
    root_ar           TEXT NOT NULL UNIQUE, -- "قول"
    root_ar_spaced    TEXT NOT NULL,        -- "ق و ل"
    total_occurrences INTEGER NOT NULL,
    verb_occurrences  INTEGER NOT NULL,
    noun_occurrences  INTEGER NOT NULL,
    frequency_rank    INTEGER NOT NULL      -- 1 = highest frequency root
);

-- Unique Lemmas linked to their Root
CREATE TABLE lemmas (
    id               INTEGER PRIMARY KEY,
    lemma_buckwalter TEXT NOT NULL UNIQUE,
    lemma_ar         TEXT NOT NULL,
    root_id          INTEGER REFERENCES roots(id),
    primary_pos      TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL,
    frequency_rank   INTEGER NOT NULL
);

-- Lemma-level verb concordance ordered by frequency
CREATE TABLE verb_concordance (
    id              INTEGER PRIMARY KEY,
    root_id         INTEGER NOT NULL REFERENCES roots(id),
    lemma_id        INTEGER NOT NULL REFERENCES lemmas(id),
    verb_ar         TEXT NOT NULL,          -- "قَالَ"
    verb_buckwalter TEXT NOT NULL,
    root_ar         TEXT NOT NULL,
    verb_form       INTEGER NOT NULL CHECK (verb_form BETWEEN 1 AND 12),
    verb_form_roman TEXT NOT NULL,          -- 'I'..'XII'
    total_count     INTEGER NOT NULL,       -- Total encounters in Quran
    perf_count      INTEGER NOT NULL DEFAULT 0,
    impf_count      INTEGER NOT NULL DEFAULT 0,
    impv_count      INTEGER NOT NULL DEFAULT 0,
    active_count    INTEGER NOT NULL DEFAULT 0,
    passive_count   INTEGER NOT NULL DEFAULT 0,
    frequency_rank  INTEGER NOT NULL        -- 1 = most frequent verb in Quran
);

CREATE INDEX idx_verb_conc_rank ON verb_concordance(frequency_rank);
CREATE INDEX idx_verb_conc_root ON verb_concordance(root_id, verb_form);
CREATE INDEX idx_verb_conc_form ON verb_concordance(verb_form, total_count DESC);

-- Word-level summary (joins 1:1 with quran-wbw-{lang}.db on word_key)
CREATE TABLE words (
    id                 INTEGER PRIMARY KEY,
    ayah_id            INTEGER NOT NULL CHECK (ayah_id BETWEEN 1 AND 6236),
    surah_id           INTEGER NOT NULL,
    ayah_number        INTEGER NOT NULL,
    word_number        INTEGER NOT NULL,
    word_key           TEXT NOT NULL UNIQUE, -- "2:255:1"
    word_ar_uthmani    TEXT NOT NULL,
    word_ar_clean      TEXT NOT NULL,
    primary_pos        TEXT NOT NULL,
    is_verb            INTEGER NOT NULL CHECK (is_verb IN (0, 1)),
    root_id            INTEGER REFERENCES roots(id),
    lemma_id           INTEGER REFERENCES lemmas(id),
    verb_concordance_id INTEGER REFERENCES verb_concordance(id),
    verb_form          INTEGER,
    verb_tense         TEXT CHECK (verb_tense IN ('PERF', 'IMPF', 'IMPV')),
    verb_voice         TEXT CHECK (verb_voice IN ('ACT', 'PASS')),
    UNIQUE (ayah_id, word_number)
);

CREATE INDEX idx_words_ayah ON words(ayah_id, word_number);
CREATE INDEX idx_words_root ON words(root_id);
CREATE INDEX idx_words_verb ON words(verb_concordance_id);

-- Full segment-level grammar breakdown (PREFIX + STEM + SUFFIX)
CREATE TABLE morphology_segments (
    id              INTEGER PRIMARY KEY,
    word_id         INTEGER NOT NULL REFERENCES words(id),
    segment_number  INTEGER NOT NULL,
    segment_role    TEXT NOT NULL CHECK (segment_role IN ('PREFIX', 'STEM', 'SUFFIX')),
    form_buckwalter TEXT NOT NULL,
    form_ar         TEXT NOT NULL,
    pos_tag         TEXT NOT NULL,
    features_raw    TEXT NOT NULL,
    UNIQUE (word_id, segment_number)
);

CREATE INDEX idx_segments_word ON morphology_segments(word_id, segment_number);

```

### 6.6 `quran-wbw-{lang}.db` (`specs/sql/quran_wbw.v1.sql`)

Built from QUL Word-by-Word JSON. Joins seamlessly with `quran-morphology.db` via `ATTACH DATABASE` on `word_key` (`surah:ayah:word`) without mixing GPL-3.0 and non-GPL licenses.

```sql
CREATE TABLE word_translations (
    id              INTEGER PRIMARY KEY,
    ayah_id         INTEGER NOT NULL CHECK (ayah_id BETWEEN 1 AND 6236),
    word_number     INTEGER NOT NULL,
    word_key        TEXT NOT NULL UNIQUE, -- "2:255:1"
    text_uthmani    TEXT NOT NULL,
    translation     TEXT NOT NULL,
    transliteration TEXT,
    UNIQUE (ayah_id, word_number)
);

CREATE INDEX idx_wbw_ayah ON word_translations(ayah_id, word_number);

CREATE VIRTUAL TABLE word_translations_fts USING fts5(
    translation,
    transliteration,
    content='word_translations',
    content_rowid='id',
    tokenize='unicode61'
);

```

### 6.7 `hadith-{book}.db` (`specs/sql/hadith_book.v1.sql`)

Enforces bilingual preservation (Arabic + English), explicit coverage flags, and strict non-heuristic Sanad/Matn separation.

```sql
CREATE TABLE books (
    id      INTEGER PRIMARY KEY,
    number  TEXT NOT NULL,
    name_ar TEXT,
    name_en TEXT
);

CREATE TABLE chapters (
    id       INTEGER PRIMARY KEY,
    book_id  INTEGER NOT NULL REFERENCES books(id),
    number   TEXT,
    title_ar TEXT,
    title_en TEXT
);

CREATE TABLE hadiths (
    id                INTEGER PRIMARY KEY,
    book_id           INTEGER NOT NULL REFERENCES books(id),
    chapter_id        INTEGER REFERENCES chapters(id),
    hadith_number     TEXT NOT NULL,
    urn               TEXT UNIQUE,

    -- Raw unmodified texts as received from source
    arabic_full       TEXT,
    english_full      TEXT,

    -- Sanad/Matn split fields: populated ONLY when upstream provides verified separation
    text_split_mode   TEXT NOT NULL CHECK (text_split_mode IN ('source_verified', 'unsplit_raw')),
    arabic_sanad      TEXT,
    arabic_matn       TEXT,
    arabic_matn_clean TEXT, -- §6 Arabic projection of verified Matn only
    arabic_full_clean TEXT, -- §6 Arabic projection of normalized unsplit text only
    english_narrator  TEXT,
    english_body      TEXT,

    -- Grading, references & explicit missing-data tracking
    grade_ar          TEXT,
    grade_en          TEXT,
    reference_json    TEXT,
    is_ar_missing     INTEGER NOT NULL DEFAULT 0 CHECK (is_ar_missing IN (0, 1)),
    is_en_missing     INTEGER NOT NULL DEFAULT 0 CHECK (is_en_missing IN (0, 1))
);

CREATE INDEX idx_hadiths_book   ON hadiths(book_id);
CREATE INDEX idx_hadiths_number ON hadiths(hadith_number);

-- FTS5 Policy:
-- Indexes verified Matn ONLY (never Sanad) + English body.
-- Unsplit rows always leave arabic_matn_clean NULL; never infer a split.
-- Pack-wide matn_split_verified is true iff at least one record is source_verified.
CREATE VIRTUAL TABLE hadiths_fts USING fts5(
    arabic_matn_clean,
    english_body,
    content='hadiths',
    content_rowid='id',
    tokenize='unicode61'
);

-- Secondary FTS5:
-- Indexes normalized unsplit Arabic ONLY for rows where text_split_mode == 'unsplit_raw'.
-- Rows with source_verified leave arabic_full_clean NULL.
CREATE VIRTUAL TABLE hadiths_full_fts USING fts5(
    arabic_full_clean,
    content='hadiths',
    content_rowid='id',
    tokenize='unicode61'
);

```

**Dual search surfaces policy:**
- **Primary `hadiths_fts`:** Indexes verified Matn only (`arabic_matn_clean`) and English body (`english_body`). Unsplit rows leave `arabic_matn_clean` NULL, preventing unverified Sanad chains from polluting Matn search results.
- **Secondary `hadiths_full_fts`:** Indexes normalized unsplit Arabic (`arabic_full_clean`) for rows where `text_split_mode == 'unsplit_raw'`. Rows with `source_verified` leave `arabic_full_clean` NULL.


### 6.8 `hadith-taqreeb-*.db` (`specs/sql/hadith_taqreeb_v1.sql`)

Dedicated schema for Mashrūʿ Taqrīb as-Sunnah (Ṣāliḥ Aḥmad ash-Shāmī) packs (`hadith-taqreeb-{jami,maalim,wajeez,kulliyyah}.db`). Preserves Arabic hierarchical structure (`maqsads` -> `kutub` -> `chapters` -> `hadiths`), footnotes, and ordered aṭrāf citations without inferring unverified sanad/matn separation (`text_split_mode = 'unsplit_raw'`).

```sql
CREATE TABLE books (
    book_id         INTEGER PRIMARY KEY CHECK (book_id > 0),
    title_ar        TEXT NOT NULL,
    title_en        TEXT NOT NULL,
    description_ar  TEXT NOT NULL,
    total_maqsads   INTEGER NOT NULL CHECK (total_maqsads > 0),
    total_hadiths   INTEGER NOT NULL CHECK (total_hadiths > 0)
);

CREATE TABLE maqsads (
    maqsad_id    INTEGER PRIMARY KEY CHECK (maqsad_id > 0),
    book_id      INTEGER NOT NULL REFERENCES books(book_id) CHECK (book_id > 0),
    maqsad_index INTEGER NOT NULL CHECK (maqsad_index > 0),
    name_ar      TEXT NOT NULL,
    name_en      TEXT NOT NULL,
    UNIQUE (book_id, maqsad_index)
);

CREATE TABLE kutub (
    ketab_id    INTEGER PRIMARY KEY CHECK (ketab_id > 0),
    maqsad_id   INTEGER NOT NULL REFERENCES maqsads(maqsad_id) CHECK (maqsad_id > 0),
    ketab_index INTEGER NOT NULL CHECK (ketab_index > 0),
    name_ar     TEXT NOT NULL,
    name_en     TEXT NOT NULL,
    UNIQUE (maqsad_id, ketab_index)
);

CREATE TABLE chapters (
    chapter_id    INTEGER PRIMARY KEY CHECK (chapter_id > 0),
    ketab_id      INTEGER NOT NULL REFERENCES kutub(ketab_id) CHECK (ketab_id > 0),
    chapter_index INTEGER NOT NULL CHECK (chapter_index > 0),
    name_ar       TEXT NOT NULL,
    name_en       TEXT NOT NULL,
    UNIQUE (ketab_id, chapter_index)
);

CREATE TABLE hadiths (
    id              INTEGER PRIMARY KEY CHECK (id > 0),
    chapter_id      INTEGER NOT NULL REFERENCES chapters(chapter_id) CHECK (chapter_id > 0),
    hadith_no       INTEGER NOT NULL CHECK (hadith_no > 0),
    text_ar         TEXT,
    text_ar_simple  TEXT NOT NULL,
    takhreej        TEXT,
    hukm            TEXT,
    page_no         INTEGER CHECK (page_no IS NULL OR page_no > 0),
    is_cross_ref    INTEGER NOT NULL CHECK (is_cross_ref IN (0, 1)),
    text_split_mode TEXT NOT NULL CHECK (text_split_mode = 'unsplit_raw'),
    text_ar_search  TEXT NOT NULL,
    CHECK (
        (is_cross_ref = 1 AND text_ar IS NULL AND takhreej IS NOT NULL AND length(trim(takhreej)) > 0)
        OR (is_cross_ref = 0 AND text_ar IS NOT NULL)
    )
);

CREATE INDEX idx_hadiths_chapter ON hadiths(chapter_id, id);
CREATE INDEX idx_hadiths_hadith_no ON hadiths(hadith_no, id);

CREATE TABLE footnotes (
    hadith_id INTEGER NOT NULL REFERENCES hadiths(id) CHECK (hadith_id > 0),
    number    INTEGER NOT NULL CHECK (number >= 0),
    text      TEXT NOT NULL,
    PRIMARY KEY (hadith_id, number)
);

CREATE TABLE hadith_atraf (
    hadith_id INTEGER NOT NULL REFERENCES hadiths(id) CHECK (hadith_id > 0),
    position  INTEGER NOT NULL CHECK (position > 0),
    text      TEXT NOT NULL,
    PRIMARY KEY (hadith_id, position)
);

CREATE VIRTUAL TABLE hadiths_fts USING fts5(
    text_ar_search,
    content='hadiths',
    content_rowid='id',
    tokenize='unicode61'
);
```

**Search projection & query binding policy:**
- **Search projection (`normalize_arabic_search(text: str) -> str`):** Applies the shared §6 Arabic FTS normalization contract. Persisted in `text_ar_search` derived from `text_ar` alone (`''` for NULL or empty text).
- **Literal match binding (`arabic_match_literal(text: str) -> str | None`):** Normalizes and trims query text via §6; returns `None` for blank or marks-only inputs. Wraps non-empty normalized text in double quotes `"` and escapes embedded double quotes `""`, preventing FTS syntax injection or unintentional operator execution. Callers bind the result to `MATCH ?` and must not execute queries when `None` is returned.
---

## 7. Distribution Manifests, Audio Engineering & Client Install Contract

### 7.1 `catalog/catalog.json` (Version 1)

All four schemas use JSON Schema Draft 2020-12. Fields are required and non-null unless marked otherwise; reject undeclared fields at each object level and duplicate JSON keys before validation. Integer fields require integer tokens, not booleans or decimal/exponent forms; nonempty strings must contain non-whitespace text. Strictly decode UTF-8; also reject lone surrogates and U+FFFD in decoded JSON keys/values (including escapes). Validate HTTPS URLs and UTC dates in addition to shape.

The catalog root has `catalogVersion` (integer `1`), `generatedAt` (valid UTC `YYYY-MM-DDTHH:MM:SSZ`), `releaseTag` (`content-vMAJOR.MINOR.PATCH`, nonnegative decimal components without leading zeroes), and `packs` (nonempty array). Common pack fields:

| Field | Contract |
| --- | --- |
| `id` | Lowercase ASCII slug (`[a-z0-9]+(?:-[a-z0-9]+)*`, full match) |
| `category` | `quran`, `hadith`, or `audio` |
| `version` | `MAJOR.MINOR.PATCH` as above |
| `format` | `sqlite` for `quran`/`hadith`; `audio` for `audio` |
| `languages` | Nonempty array of distinct two-letter lowercase ASCII language codes (`[a-z]{2}`); profiles enforce the actual languages of their inventory. |
| `completeness` | `complete` or `partial` |
| `licenseRef` | Exact-case `sources/licenses/{id}.json`, resolved in repo tree at `releaseTag` |
| `required` | Boolean: default install includes this pack; only `quran-core` is required in v0.1.0 |

SQLite records additionally require `compression` (`none`), `schemaVersion` (integer `1`), `url` (absolute HTTPS URL with basename `{id}.db`), `sizeBytes` (positive integer), `sha256` (64 lowercase hex digits, full match), `coverage` (object with nonnegative integer `expected`, `available` from §5), and `requiresFts5` (boolean). Audio records instead require `audioManifestRef` (literal `audio-manifest.json`, relative to the published catalog URL); no SQLite-only fields are allowed. The manifest has no optional fields in v1. Structural example (synthetic digest and host):

```json
{
  "catalogVersion": 1,
  "generatedAt": "2026-01-01T00:00:00Z",
  "releaseTag": "content-v0.1.0",
  "packs": [
    {"id":"audio-adhan-core","category":"audio","version":"0.1.0","format":"audio","languages":["ar"],"completeness":"complete","licenseRef":"sources/licenses/audio-adhan-core.json","required":false,"audioManifestRef":"audio-manifest.json"},
    {"id":"quran-core","category":"quran","version":"0.1.0","format":"sqlite","languages":["ar"],"completeness":"complete","licenseRef":"sources/licenses/quran-core.json","required":true,"compression":"none","schemaVersion":1,"url":"https://example.org/content-v0.1.0/quran-core.db","sizeBytes":4096,"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","coverage":{"expected":6236,"available":6236},"requiresFts5":true}
  ]
}
```

The generator takes a single explicit UTC release timestamp for both renders. Sort packs by `id`, tracks by `slug`, and languages lexically. Canonical bytes are Python `json.dumps` with `sort_keys=True`, `ensure_ascii=False`, `separators=(",", ":")`, `allow_nan=False`, encoded in UTF-8 without BOM followed by exactly one LF. For `durationSeconds` round the exact rational to whole milliseconds (half-up), then divide the integer milliseconds by 1000 using Python float so e.g. 2,000 ms serializes as `2.0` and 1,500 ms as `1.5`; reject a rounded zero. Hash finalized asset bytes. The gate, not JSON Schema, checks duplicate IDs, duplicate track slugs or asset names, filename/path relationships, exact inventory and coverage, and file/manifest/profile equality. In v0.1.0 all pack versions are `0.1.0`; only Pickthall uses `["en"]`, all other packs use `["ar"]`. The asset URL base is `https://github.com/{owner}/{repo}/releases/download/content-v0.1.0/`; both downloadable manifests live at the same base.

`sources/release-v0.1.0.json` is the reviewed input, not a downloadable manifest. Fields: `repository` (`owner/repo` matching the release target), `generatedAt` (explicit UTC timestamp), `bundlePaths` (sorted distinct lock paths acquired only via the authorized bundle in `PLAN.md` §2; empty if unused), and `companions` (objects sorted by `filename` with `filename`, positive `sizeBytes`, lowercase `sha256`, and `sourcePath`). Names are safe flat ASCII basenames under §3.1, distinct from manifests/pack assets. `sourcePath` is `null` for the mandatory generated `quran-morphology-source.zip` only, or an exact-case regular file path under `sources/licenses/evidence/` for each separately required notice copied verbatim. Task 1.7 owns assembling both kinds. `python -m pipelines.release.generate_catalog --sources-root S --asset-root OUT` reads this descriptor, registry/lock, pack licenses and finalized flat asset files; it writes only `OUT/catalog.json` and `OUT/audio-manifest.json` (refuse existing files). The gate checks companion hashes and inventory; release notes give companion hashes and links to tagged license records.

### 7.2 Audio Architecture & Apple iOS Notification Compliance

`catalog/audio-manifest.json` contains `audioManifestVersion` (integer `1`), `releaseTag` (same form/value as the catalog), `packId` (literal `audio-adhan-core`), and `tracks` (nonempty array). Each track has `slug` (same slug grammar as pack ID), `reciter` (nonempty string), `full` and `ios` (asset objects). Each asset has `filename` (matching the variant's pattern below), `url` (absolute HTTPS asset URL), `sizeBytes` (positive integer), `sha256` (64 lowercase hex), `codec` (`aac` for full, `adpcm_ima_qt` for iOS) and `durationSeconds` (positive finite JSON number). No optional or nullable fields. Record duration from final `ffprobe` audio-stream timing, rounded to nearest millisecond, half-up, for display; the gate compares the unrounded iOS duration to the cap.

```json
{
  "audioManifestVersion":1,
  "releaseTag":"content-v0.1.0",
  "packId":"audio-adhan-core",
  "tracks":[
    {"slug":"fixture","reciter":"Synthetic tone","full":{"filename":"adhan-fixture-full.m4a","url":"https://example.org/content-v0.1.0/adhan-fixture-full.m4a","sizeBytes":1024,"sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","codec":"aac","durationSeconds":1.5},"ios":{"filename":"adhan-fixture-ios-notif.caf","url":"https://example.org/content-v0.1.0/adhan-fixture-ios-notif.caf","sizeBytes":512,"sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","codec":"adpcm_ima_qt","durationSeconds":1.5}}
  ]
}
```

Encode from audio stream `0:a:0`, one channel with FFmpeg's standard mono mix and no other streams. Use §5's binaries and FFmpeg arguments `-nostdin`, `-threads 1` for input and output, `-filter_threads 1`, `-map_metadata -1`, `-map_chapters -1`, and bitexact muxer/codec flags (`-fflags +bitexact`, `-flags:a +bitexact` on output). Strip source tags; no injected timestamps or artwork. Use two-pass `loudnorm` with target `I=-16:LRA=7:TP=-1.5`: first measure the mono source, then pass `input_i`, `input_lra`, `input_tp`, `input_thresh`, and `target_offset` as `measured_I`, `measured_LRA`, `measured_TP`, `measured_thresh`, and `offset`, with `linear=true` (FFmpeg's dynamic fallback when linear is infeasible). Fail on missing/nonfinite measurements. The same normalized signal feeds both renditions before encoding.

1. **Full Adhan (`adhan-{slug}-full.m4a`)**:
* Encode all normalized audio as M4A (MP4 audio container), FFmpeg native AAC-LC at `128 kbps`, `44.1 kHz` mono.
* Used for in-app playback and Android foreground/custom notification channels.


2. **iOS Notification Clip (`adhan-{slug}-ios-notif.caf`)**:
* Apple's `UNNotificationSound` requires custom alert sounds to be **strictly less than 30 seconds** (otherwise iOS silently falls back to the default system sound).
* Audio data must be encoded in **Linear PCM, MA4 (IMA/ADPCM / `ima4`), µLaw, or aLaw** and packaged in `.caf`, `.wav`, or `.aiff`.
* Project encode target: **≤28.5 seconds** with a 1.5 s fade-out. The project gate requires CAF container, `ima4` on-disk tag (FFmpeg codec `adpcm_ima_qt`), and probed duration **strictly <29.0 seconds**; reject exactly 29.0 seconds and alternate codecs even if iOS supports them. Run `ffmpeg` and `ffprobe` with argument arrays and `shell=False`. See the [FFmpeg CAF codec mapping](https://github.com/FFmpeg/FFmpeg/blob/n7.1/libavformat/caf.c).
* For the iOS rendition resample the normalized signal to `32 kHz` mono. Starting at the reviewed `clipStartMs` (32 samples per ms; fixture starts at 0), keep at most 912,000 samples (28.5 s) and stop at EOF if shorter. Fail if the start is at or beyond EOF. Apply a linear fade to the last `min(48000, retained_samples)` samples before encoding, not at a fixed offset from the maximum window. Do not pad to the target length. Use the CAF muxer and `adpcm_ima_qt` encoder.
* Probe each master for bitrate, sample rate and duration. The shared `pipelines/common/audio_probe.py` API `probe_asset(path: Path, *, variant: Literal["full", "ios"]) -> tuple[str, Fraction]` checks the final file and returns `(codec_name, duration_seconds)` from `ffprobe -v error -of json -show_format -show_streams`. Require exactly one stream, mono audio. For full, require MP4/M4A (`format_name` list contains `mp4`), `codec_name=aac` and `sample_rate=44100`; for iOS require `format_name=caf`, `codec_tag_string=ima4`, `codec_name=adpcm_ima_qt` and `sample_rate=32000`. Compute exact duration as the integer `duration_ts` multiplied by the rational `time_base`; reject missing, invalid or nonpositive values. Builders and manifest generation probe both variants. The gate only probes iOS, applies the strict cap, and checks full-audio bytes against the already probed manifest hash. Facts are not sidecar assets.
* **Client Placement Requirement:** Consuming iOS apps must store downloaded `.caf` files in `/Library/Sounds` inside the app container (or shared App Group container), or bundle them in the main app bundle.



### 7.3 GitHub Releases Storage and Publication

* Each individual release asset must remain **strictly under 2 GiB**.
* Up to **1,000 assets** may be attached to a single release tag, with no documented total release size cap or public download bandwidth fee.
* Large Quran recitation libraries are not uploaded to GitHub Releases. The closed v1 audio schema is Adhan-only; recitation timings have a separate local manifest contract.

### 7.3.1 Private local Adhan intake inventory (`sources/adhan-local-intake.json`)

The private local Adhan intake is separate from the shared production registry and lock. `sources/adhan-local-intake.json` tracks 25 local master MP3 files staged from `Contant/adhan/` to `raw-inputs/audio/adhan-local/` by `python -m pipelines.builders.build_audio_adhan_local stage`. It records local provenance and rights-review metadata for local-only verification; it is not a release clearance artifact.

`python -m pipelines.builders.build_audio_adhan_local build --sources-root S --output-dir O` builds exactly 50 derivatives from those 25 masters: one full M4A (`adhan-{slug}-full.m4a`) plus one iOS CAF (`adhan-{slug}-ios-notif.caf`) per slug. Derivatives must satisfy the same codec/sample-rate/duration/probe rules as §7.2, and local quality gates verify deterministic bytes across independent builds.

Because this intake is private and rights remain pending, none of these local-only tracks or derivatives are included in release profiles, release manifests, or public uploads.

Use the tag, dual-build, draft and public verification sequence in `PLAN.md` Task 1.7. Commit the canonical manifests with the reviewed lock and license records before tagging; compare both tagged rebuilds to those bytes and gate both before any upload. Validate draft attachments, then publish and recheck public URLs.

### Local recitation timing manifests (Phase 3)

`specs/recitation-manifest.schema.json` defines one `audio-recitation-{edition}.json` per pinned reciter/style. Each lists all 6,236 verses in numeric order, with exact source HTTPS URLs and 1-based word positions. Timing intervals are integer milliseconds relative to the start of each ayah file. Reported duration is nullable and independent of the timings; numeric source seconds are rounded to milliseconds half-up, never used to clamp intervals.

Valid intervals retain source order, including repeated positions and overlaps. The 18 known nonpositive Al-Tablawi intervals are excluded from playback timings and preserved in `invalidSourceSegments` with their original one-based source indexes. The pipeline does not read or download audio.

### 7.4 Atomic Client Installation Protocol (`user.db` Separation)

Consuming apps must never mutate content `.db` files; user state lives in a separate `user.db`:

1. For `format: sqlite`, fetch `catalog.json` and diff `version`/`sha256` against `installed_packs` in `user.db`. For `format: audio`, resolve `audioManifestRef` relative to the catalog URL and verify individual track assets against that manifest; do not open it as SQLite.
2. Download pack to a temporary staging path (`tmp/{id}.{version}.db.part`).
3. Verify exact `sizeBytes` and `sha256`.
4. Open read-only to verify `PRAGMA quick_check = ok` and `PRAGMA user_version == schemaVersion`.
5. Atomically move (`rename`) into the final content directory, update `user.db`, and delete the superseded file.

---

## 8. Cross-Platform SQLite & FTS5 Compatibility

* **Expo / React Native:** `expo-sqlite` supports pre-bundled databases (`assetSource`), local file-system databases, and FTS5 via `enableFTS: true` in app config.
* **Other Platforms (Flutter, Native iOS/Android, Desktop, Web):** Consumers of SQLite packs with `requiresFts5: true` need a SQLite runtime with FTS5. Verify by opening the actual pack read-only and executing the advertised FTS index query path; a SQLite version string alone is not a capability check.
* **Mandatory cross-client literal contract:** Query literals for Arabic FTS must match `pipelines/common/search_text.py` exactly: apply `normalize_arabic_search`, trim Unicode whitespace, return no query when the result is empty, otherwise escape embedded `"` as `""`, wrap in outer `"..."`, and bind to `MATCH ?`.
* **Reference examples (all clients must match):**

| User input | Bound literal |
| --- | --- |
| `رَبِّ العالمين` | `"رب العالمين"` |
| `رب العالمين` | `"رب العالمين"` |
| `كتب` (fixture negative) | `"كتب"` |
| marks-only / blank | `None` (skip query) |

---

## 9. CI Quality Gate

From the repository root:

```sh
python -m pipelines.validators.quality_gate --profile fixture --candidate A --rebuild B --sources-root S --expectations E
python -m pipelines.validators.quality_gate --profile v0.1.0 --candidate A --rebuild B --sources-root S
python -m pipelines.validators.quality_gate --profile hadith-pilot --candidate A --rebuild B --sources-root S
python -m pipelines.validators.quality_gate --profile hadith-local --candidate A --rebuild B --sources-root S
python -m pipelines.validators.quality_gate --profile phase3-local --candidate A --rebuild B --sources-root S
python -m pipelines.validators.quality_gate --profile hadith-taqreeb-local --candidate A --rebuild B --sources-root S
```

`A` and `B` are distinct, independently built clean directories with the same exact-case flat artifact paths. Fixture and release roots include `catalog.json` and `audio-manifest.json`; local profiles use the inventory below. `S/raw-inputs/` contains staged inputs; `S/sources/` contains `registry.json`, `manifest.lock.json` and `licenses/{id}.json`. Only release mode reads `S/sources/release-v0.1.0.json` (§7.1). Fixture mode alone requires `--expectations` pointing to the checked-in `tests/fixtures/expectations.json`. Validate both roots independently before comparing them; Phase 3 may derive a disposable reference from the locked inputs and never mutates either root or uploads artifacts. CLI misuse exits `2`; validation exits `1` with the check number, offending path or pack/key, and reason; exit `0` only if every applicable check passes. A missing expected item is a failure, not a skip.

Reject `A` and `B` with the same filesystem identity (#6). Manifest/asset entries must be regular files, not links or junctions; reject file inode aliases across the roots. The workflow creates and builds each clean root independently; the gate establishes byte equality, not build history.

Run in this order, failing fast at the first offending asset in lexical path order for each stage: parse CLI and fixture oracle (missing/malformed oracle or bad flags: exit `2` naming the input); runtime preflight (exit `1`, `check 0 (preflight)` and failed capability); root identity (#6); manifest text encoding (#2), schemas and asset inventory/size/hash/probes (#5); source lock and clearance (#7); SQLite schema/integrity (#3); SQLite metadata-to-source reconciliation (#7); record keys (#1); text and source equivalence (#2); coverage (#1), FTS (#4), Tafsir intervals (#9); finally cross-root equality (#6). Report dependent unrun checks as blocked, not not-applicable. Compare keys without decoding non-key content until #2; invalid text keys fail #2 during key validation.

| Profile | Required checks and scope |
| --- | --- |
| `fixture` (Phase 0) | #1–#7 and #9 as applicable to the synthetic pack inventory. #8 is not applicable: no production QAC/QUL pair. No release or data-clearance claim follows from fixtures. |
| `v0.1.0` (Phase 1) | #1–#7 and #9 as applicable to all five SQLite packs and both variants of each of the six tracks. #8 is not applicable: no QUL WBW pack. No partial coverage or missing item permitted. |
| `hadith-pilot` (Local only) | Flat DB-only roots for `hadith-bukhari.db` and `hadith-muslim.db`. Verifies canonical lock, pending licenses ('Clearance pending (local verification only)'), SQLite schema, keys, text, coverage, dual FTS token coverage and queries, and cross-root byte equality. Manifest checks and #8/#9 not applicable. |
| `hadith-local` (Local only) | Flat DB-only roots for all nine Hadith packs (`bukhari`, `muslim`, `nasai`, `abudawud`, `tirmidhi`, `ibnmajah`, `malik`, `darimi`, `ahmad`). Same local checks across the entire nine-pack inventory. |
| `phase3-local` (Local only) | Exactly 12 SQLite packs and seven recitation timing manifests from `pipelines.release.build_phase3_local`: rebuilt `quran-core.db` and `quran-morphology.db` plus 10 QUL SQLite packs (2 translations, 5 tafsirs, 2 mushaf layouts, 1 WBW), with no catalog or audio files. Reconciles both roots against locked sources, applies #8 and #9, and checks all 19 sizes and SHA-256 digests. Passing this profile does not clear redistribution rights. |
| `hadith-taqreeb-local` (Phase 4, local only) | Flat DB-only roots for the four Taqreeb packs (`hadith-taqreeb-jami.db`, `hadith-taqreeb-maalim.db`, `hadith-taqreeb-wajeez.db`, `hadith-taqreeb-kulliyyah.db`). Verifies canonical lock, pending licenses ('Clearance pending (local verification only)'), SQLite schema, source fidelity and row-by-row traversal against original Realm JSON exports, exact anomaly preservation, coverage metrics, FTS5 token search and integrity, and cross-root byte equality. Manifest checks and #8/#9 not applicable. |

Build and verify the Phase 3 roots in the pinned Linux runtime, with networking disabled:

```sh
python -m pipelines.release.build_phase3_local --sources-root S --output-dir A
python -m pipelines.release.build_phase3_local --sources-root S --output-dir B
python -m pipelines.validators.quality_gate --profile phase3-local --candidate A --rebuild B --sources-root S
python -m pipelines.validators.phase3_concordance A
python -m pipelines.validators.phase3_concordance B
```

Build and verify the Phase 4 Taqreeb roots in the pinned Linux runtime, with networking disabled:

```sh
for OUT in A B; do
  for suffix in jami maalim wajeez kulliyyah; do
    python -m pipelines.builders.hadith_taqreeb_builder --pack-id "hadith-taqreeb-$suffix" --sources-root S --output-dir "$OUT"
  done
done
python -m pipelines.validators.quality_gate --profile hadith-taqreeb-local --candidate A --rebuild B --sources-root S
```

For v0.1.0 #4 restricts each smoke query to `rowid=2` and expects `[2]`: core `رَبِّ العالمين` and `رب العالمين`, Jalalayn the same pair, Pickthall `Lord of the Worlds`. The negative literal `zzzzfixturemissing` returns `[]` for each table at that row. Perform queries using the §6 literal binding rule. Fixture #4 uses the checked-in oracle instead.

| # | Applies to | Pass criterion |
| --- | --- | --- |
| 1 | All packs | Exact expected packs and record keys; derive coverage (§5) and compare metadata/catalog. For fixture use `PLAN.md` Phase 0. For v0.1.0 compare the entire 114-surah/6,236-ayah mapping to locked Tanzil metadata (each stored `(ayah_id, surah_id, ayah_number, verse_key)` component must agree where present; verify core boundaries and sajdah on every row); verify Corpus word/segment keys for morphology and the six track slugs in `PLAN.md`. |
| 2 | Text-bearing inputs, SQLite text and JSON documents | Strict UTF-8 including decoded JSON keys/values (§7.1), no U+FFFD, required fields nonblank; nullable fields follow their schema. Non-null SQLite text fields must use TEXT storage and decode strictly. Validate canonical JSON inputs, compare retained source text, recompute declared search projections, and validate Hadith flags and split evidence when applicable. |
| 3 | SQLite packs | `integrity_check` returns `ok`, `foreign_key_check` returns no rows; verify encoding, page size, non-WAL file with no sidecars, `user_version`, schema, `pack_meta` and `coverage_report`. Compare user tables/indexes/constraints/FTS against a fresh reference schema from §6, excluding SQLite-generated internals and FTS shadow tables. The writer test, not reopening a published DB, proves connection-local foreign keys and `journal_mode=DELETE`. |
| 4 | Declared FTS5 tables only | On a temporary DB copy run `INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)` to compare external index against content; test exact ordered row IDs for declared Arabic/English queries, including positive and negative controls in fixture mode. Never validate index population by counting rows on an external-content FTS table. |
| 5 | All distributable assets; media probe for iOS variants only | Validate manifest schemas, canonical encoding, identities, URLs and exact inventory; size/hash-check every described file including release companions against the reviewed descriptor, and cap every attachment at `<2 GiB`, maximum 1,000 attachments. Manifests do not hash themselves; compare their bytes in #6 and to the tagged copies. Independently probe iOS variants using §7.2 and check `0 < duration < 29.0` before rounding; compare recorded duration. |
| 6 | Both complete builds | Compare sizes and SHA-256 for all declared distributable bytes, including every audio variant, both manifests and any required companion attachments; equal counts or regenerated hashes alone are not a pass. Use the same pinned environment for both builds. |
| 7 | All packs; SQLite metadata only for SQLite packs | Re-verify canonical lock and exact profile path-to-pack assignments (`PLAN.md` §2/Phase 0), not just agreement among supplied files. Require `fixture` records or approved production evidence (§3.2); reconcile membership, `licenseRef`, all source records and track slugs/reciters. For SQLite verify primary `source_*`, `license_spdx` and the pack-scoped `build_input_sha256`. |
| 8 | QAC plus QUL WBW only | Compare each ayah's word count after deterministic handling of waqf/pause tokens; report each differing key. |
| 9 | Tafsir only | Validate interval/key grammar, nonoverlap, exact expected coverage and one map row per covered verse pointing to its containing entry. A gap, duplicate, shifted pointer or key outside its entry interval fails. |