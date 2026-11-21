CREATE TABLE pack_meta (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
-- Mandatory keys:
-- pack_id, pack_version, schema_version, source_name, source_url,
-- source_version, license_spdx, license_ref, build_input_sha256,
-- completeness ('complete' | 'partial'), fts5_enabled ('true' | 'false')

CREATE TABLE coverage_report (
    metric TEXT NOT NULL PRIMARY KEY,
    count  INTEGER NOT NULL CHECK (typeof(count) = 'integer' AND count >= 0),
    notes  TEXT
);
