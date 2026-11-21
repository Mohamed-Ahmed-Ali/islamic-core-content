CREATE TABLE ayah_scripts (
    ayah_id           INTEGER PRIMARY KEY CHECK (ayah_id BETWEEN 1 AND 6236),
    verse_key         TEXT NOT NULL UNIQUE,
    text_uthmani_min  TEXT NOT NULL,
    text_simple       TEXT NOT NULL,
    text_simple_plain TEXT NOT NULL,
    text_simple_min   TEXT NOT NULL
);
