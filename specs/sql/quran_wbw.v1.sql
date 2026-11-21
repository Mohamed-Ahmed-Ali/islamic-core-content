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
