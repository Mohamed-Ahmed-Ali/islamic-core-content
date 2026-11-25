CREATE TABLE translations (
    ayah_id     INTEGER PRIMARY KEY CHECK (ayah_id BETWEEN 1 AND 6236),
    surah_id    INTEGER NOT NULL,
    ayah_number INTEGER NOT NULL,
    verse_key   TEXT NOT NULL UNIQUE,
    text        TEXT NOT NULL,
    text_search TEXT NOT NULL
);

CREATE TABLE translation_footnotes (
    ayah_id     INTEGER NOT NULL REFERENCES translations(ayah_id),
    footnote_id INTEGER NOT NULL,
    text        TEXT NOT NULL,
    PRIMARY KEY (ayah_id, footnote_id)
);

CREATE INDEX idx_translation_footnotes_ayah_id ON translation_footnotes(ayah_id);
CREATE INDEX idx_translation_footnotes_footnote_id ON translation_footnotes(footnote_id);

CREATE VIRTUAL TABLE translations_fts USING fts5(
    text_search,
    content='translations',
    content_rowid='ayah_id',
    tokenize='unicode61'
);
