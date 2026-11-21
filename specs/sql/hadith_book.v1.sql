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
