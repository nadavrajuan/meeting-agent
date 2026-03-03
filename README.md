# Meeting Transcription Agent

A LangGraph-powered agent that monitors Google Drive folders, processes meeting transcripts, and produces rich summaries with action items, tags, and follow-up tasks.

## Features

- 📁 **Google Drive Monitoring** – Watches a folder for new meeting subfolders
- 📝 **Transcript Processing** – Extracts summaries, tags, action items via LLM
- 🧠 **Extra Context Injection** – Pulls contextual docs to enrich prompts dynamically
- 👥 **People & Label Management** – Tags participants, maps to labels + emails
- 📧 **Gmail Integration** – Searches emails related to meetings and participants
- 🔁 **Multi-Iteration Tasks** – Follows up on action items in subsequent agent runs
- 📊 **Agent Visualization** – View graph topology and per-run execution traces
- 📅 **Daily/Weekly Summaries** – Aggregated summaries over time windows
- 🗄️ **PostgreSQL Database** – Persistent store for meetings, people, labels, runs
- 🌐 **Web UI** – Manage labels, people, prompts, and browse meeting history
- 🐳 **Docker** – Fully containerized

## Architecture

```
Google Drive (source)
    ↓
LangGraph Agent (Python)
    ├── Drive Monitor Node
    ├── Transcript Extractor Node
    ├── Extra Context Node
    ├── LLM Summarizer Node (GPT-4 / Gemini)
    ├── Action Item Executor Node (multi-iteration)
    ├── Gmail Search Node
    ├── Email Sender Node
    └── Drive Output Writer Node
         ↓
PostgreSQL (state + history)
         ↓
FastAPI Backend + React Frontend (management UI)
```

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose
- Google Cloud Project with:
  - Drive API enabled
  - Gmail API enabled
  - OAuth 2.0 credentials (download as `credentials.json`)
- OpenAI API key (or Gemini)

### 2. Environment Setup

```bash
cp .env.example .env
# Fill in your keys
```

### 3. Google OAuth

Place your `credentials.json` in the project root, then:

```bash
docker compose run --rm agent python scripts/auth_google.py
```

This opens a browser for OAuth consent and saves `token.json`.

### 4. Run

```bash
docker compose up
```

- **Agent UI**: http://localhost:3000
- **API docs**: http://localhost:8000/docs

### 5. First Run (Process N Last Folders)

```bash
docker compose run --rm agent python scripts/first_run.py --last-n 5
```

After first run, the agent tracks the last-processed timestamp and only picks up new folders automatically.

## Configuration

All prompts are editable via the UI or directly in `prompts/`. Key prompt files:

- `prompts/summarize_meeting.txt` – Main summarization template
- `prompts/extract_action_items.txt` – Action item extraction
- `prompts/daily_summary.txt` – Daily digest template
- `prompts/weekly_summary.txt` – Weekly digest template

Extra context docs in Drive inject additional instructions per-meeting dynamically.

## Scheduling

The agent runs on a cron schedule (default: every 30 minutes). Configure in `.env`:

```
AGENT_CRON_SCHEDULE=*/30 * * * *
```

## Database Schema

See `migrations/001_init.sql` for full schema. Key tables:

- `meetings` – Meeting metadata, summary, tags, status
- `people` – Participants with emails and label assignments  
- `labels` – User-defined labels
- `action_items` – Per-meeting tasks and their execution status
- `agent_runs` – Full execution traces per run
- `emails` – Cached Gmail results linked to meetings/people
