-- migrations/002_context_notes.sql
CREATE TABLE IF NOT EXISTS context_notes (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    drive_doc_id  TEXT,
    drive_doc_url TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
