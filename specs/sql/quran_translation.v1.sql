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
