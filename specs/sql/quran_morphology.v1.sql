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
