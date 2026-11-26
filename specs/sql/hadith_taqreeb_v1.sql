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
