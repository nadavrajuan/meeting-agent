-- migrations/004_run_log_entries.sql
-- Real-time per-entry log storage so the UI can poll during a run
CREATE TABLE IF NOT EXISTS run_log_entries (
    id        BIGSERIAL PRIMARY KEY,
    run_id    UUID    NOT NULL,
    step      TEXT    NOT NULL,
    detail    TEXT    NOT NULL,
    level     TEXT    DEFAULT 'info',
    data      JSONB,
    ts        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rle_run_id ON run_log_entries(run_id, id);
