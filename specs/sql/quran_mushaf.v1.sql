CREATE TABLE layout_info (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    name           TEXT NOT NULL,
    page_count     INTEGER NOT NULL,
    lines_per_page INTEGER NOT NULL,
    font_name      TEXT NOT NULL
);

CREATE TABLE page_lines (
    page_number   INTEGER NOT NULL,
    line_number   INTEGER NOT NULL,
    line_type     TEXT NOT NULL,
    is_centered   INTEGER NOT NULL,
    first_word_id INTEGER,
    last_word_id  INTEGER,
    surah_number  INTEGER,
    PRIMARY KEY (page_number, line_number)
);

CREATE TABLE tajweed_ayahs (
    ayah_id     INTEGER PRIMARY KEY,
    verse_key   TEXT NOT NULL UNIQUE,
    text_markup TEXT NOT NULL
);
