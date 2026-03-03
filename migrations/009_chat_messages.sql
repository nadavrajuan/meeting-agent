-- migrations/009_chat_messages.sql
-- Per-meeting chat history for the interactive meeting chatbot

CREATE TABLE IF NOT EXISTS chat_messages (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,   -- 'user' | 'assistant'
    content    TEXT NOT NULL,
    metadata   JSONB,           -- {doc_urls: [...], tool_calls_used: [...]}
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS chat_messages_meeting_id_idx ON chat_messages(meeting_id, created_at);
