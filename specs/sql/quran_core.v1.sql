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
