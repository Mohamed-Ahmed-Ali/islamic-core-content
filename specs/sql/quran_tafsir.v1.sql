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
