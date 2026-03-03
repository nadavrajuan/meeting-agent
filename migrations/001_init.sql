-- migrations/001_init.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─── Labels ───────────────────────────────────────────────────────────────────
CREATE TABLE labels (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL UNIQUE,
    color       TEXT DEFAULT '#6366f1',
    description TEXT,
    keywords    TEXT[],          -- auto-match these keywords in transcripts
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── People ───────────────────────────────────────────────────────────────────
CREATE TABLE people (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name       TEXT NOT NULL,
    email      TEXT,
    notes      TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE people_labels (
    person_id UUID REFERENCES people(id) ON DELETE CASCADE,
    label_id  UUID REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (person_id, label_id)
);

-- ─── Meetings ─────────────────────────────────────────────────────────────────
CREATE TABLE meetings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    drive_folder_id     TEXT NOT NULL UNIQUE,
    drive_folder_name   TEXT NOT NULL,
    transcript_doc_id   TEXT,
    summary_doc_id      TEXT,           -- the "summery" meta file
    meeting_date        TIMESTAMPTZ,
    title               TEXT,
    summary             TEXT,           -- LLM-generated summary
    raw_transcript_text TEXT,
    extra_context_doc_id TEXT,
    status              TEXT DEFAULT 'pending',  -- pending|processing|done|error
    iteration_count     INT DEFAULT 0,
    output_folder_id    TEXT,           -- drive folder for outputs
    output_folder_url   TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    processed_at        TIMESTAMPTZ
);

CREATE TABLE meeting_labels (
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    label_id   UUID REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (meeting_id, label_id)
);

CREATE TABLE meeting_people (
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    person_id  UUID REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (meeting_id, person_id)
);

-- ─── Action Items ─────────────────────────────────────────────────────────────
CREATE TABLE action_items (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    meeting_id    UUID REFERENCES meetings(id) ON DELETE CASCADE,
    description   TEXT NOT NULL,
    assignee_name TEXT,
    due_date      TEXT,
    status        TEXT DEFAULT 'open',   -- open|in_progress|done|skipped
    result        TEXT,                  -- LLM output from executing this task
    iteration     INT DEFAULT 0,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Emails ───────────────────────────────────────────────────────────────────
CREATE TABLE emails (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    gmail_id    TEXT UNIQUE,
    subject     TEXT,
    from_addr   TEXT,
    to_addrs    TEXT[],
    date        TIMESTAMPTZ,
    snippet     TEXT,
    body        TEXT,
    meeting_id  UUID REFERENCES meetings(id),
    person_id   UUID REFERENCES people(id),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Agent Runs ───────────────────────────────────────────────────────────────
CREATE TABLE agent_runs (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    meeting_id   UUID REFERENCES meetings(id),
    run_type     TEXT DEFAULT 'meeting',  -- meeting|daily|weekly
    started_at   TIMESTAMPTZ DEFAULT NOW(),
    ended_at     TIMESTAMPTZ,
    status       TEXT DEFAULT 'running',  -- running|done|error
    summary_log  TEXT,                   -- high-level step summary
    full_log     JSONB,                  -- detailed step-by-step trace
    error        TEXT
);

-- ─── Agent State (for cron tracking) ─────────────────────────────────────────
CREATE TABLE agent_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO agent_state (key, value) VALUES
    ('last_run_at', NULL),
    ('last_processed_folder_time', NULL);

-- ─── Prompt Templates ─────────────────────────────────────────────────────────
CREATE TABLE prompt_templates (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name       TEXT NOT NULL UNIQUE,
    template   TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO prompt_templates (name, template, description) VALUES
('summarize_meeting',
$$You are an expert meeting summarizer. Given the following meeting transcript, produce a structured summary.

## Instructions
- Identify all participants
- Write a concise executive summary (3-5 sentences)
- List all key discussion points
- Extract all action items with assignee and due date if mentioned
- List any decisions made
- Add relevant tags (topic areas)

## Important Notes to Address
{important_notes}

## Extra Instructions
{extra_instructions}

## Transcript
{transcript}

Respond in JSON:
{
  "participants": ["name1", "name2"],
  "executive_summary": "...",
  "key_points": ["..."],
  "action_items": [{"description": "...", "assignee": "...", "due_date": "..."}],
  "decisions": ["..."],
  "tags": ["..."],
  "important_notes_addressed": "..."
}$$,
'Main meeting summarization prompt'),

('execute_action_item',
$$You are an AI assistant helping to execute a meeting action item.

## Action Item
{action_item}

## Meeting Context
{meeting_summary}

---

## ⚠️ Important Notes (HIGH PRIORITY — read before doing anything else)
{important_notes}

## Extra Instructions
{extra_instructions}

## Available Context (related emails / previous meetings)
{context}

---

## How to Respond

**Before writing output, do this:**
1. Scan the "Important Notes" and "Extra Instructions" sections above.
2. Identify which notes/instructions are directly relevant to THIS specific action item.
3. You MUST address every relevant note with high priority in your response.

**Structure your entire response using this exact format:**

# [Short title describing the deliverable]

[THE ACTUAL DELIVERABLE — complete and immediately usable.
If this task is to write a prompt → put the full prompt here.
If this task is to draft an email → put the full email here.
If this task is research → put the key finding/answer here.
If this task is analysis → put the conclusion/summary here.
Make it concrete, specific, and ready to use. Do NOT just describe what to do — do it.]

---

## Relevant Notes Applied
[List which Important Notes and Extra Instructions were relevant to this task and exactly how you addressed each one. If none applied, say "None applicable."]

## Context Used
[Brief note on any emails or meeting context that shaped the output. If none, say "None available."]

## Notes & Next Steps
[Any assumptions made, limitations of the output, or recommended follow-up actions.]$$,
'Executes a specific action item from a meeting'),

('daily_summary',
$$Create a daily summary digest for {date}.

## Meetings Processed
{meetings}

## Emails Found
{emails}

## Instructions
- What were the main themes today?
- What action items are outstanding?
- What progress was made?
- Any important follow-ups needed?$$,
'Daily digest summary prompt'),

('weekly_summary',
$$Create a weekly summary digest for the week of {week_start} to {week_end}.

## Data
{data}

## Instructions
- High-level overview of the week
- Key decisions made
- Progress on ongoing initiatives
- Outstanding action items
- People most involved this week$$,
'Weekly digest summary prompt');
