-- migrations/005_plan_approval.sql
-- Adds planning & approval support to action_items and meetings

ALTER TABLE action_items
    ADD COLUMN IF NOT EXISTS plan_output_type        TEXT,
    ADD COLUMN IF NOT EXISTS plan_resources          TEXT,
    ADD COLUMN IF NOT EXISTS plan_notes              TEXT,
    ADD COLUMN IF NOT EXISTS feasibility             TEXT,   -- feasible|partial|not_feasible
    ADD COLUMN IF NOT EXISTS approved                BOOLEAN,
    ADD COLUMN IF NOT EXISTS approved_max_iterations INT DEFAULT 1;

ALTER TABLE meetings
    ADD COLUMN IF NOT EXISTS extra_context_text TEXT,
    ADD COLUMN IF NOT EXISTS approval_status    TEXT;   -- NULL|awaiting_approval|approved|rejected

INSERT INTO prompt_templates (name, template, description)
VALUES (
  'plan_action_items',
$$You are a meeting assistant AI. Below is a list of action items from a meeting.
For each action item, analyze what executing it would require and produce a plan.

## Meeting Summary
{executive_summary}

## Action Items
{action_items_json}

For each action item, determine:
1. output_type: the type of output this task produces.
   Options: "email", "document", "research", "draft", "code", "calendar", "analysis", "other"
2. resources_needed: what tools or access is needed (e.g. "Gmail", "Google Drive", "Web search", "None").
   Be specific but concise.
3. feasibility: can the AI agent complete this task?
   Options: "feasible" (yes fully), "partial" (partially), "not_feasible" (requires human action only)
4. plan_notes: a brief 1-2 sentence description of how the agent would execute this task.

Respond ONLY with valid JSON in this exact format (no extra text):
{
  "plans": [
    {
      "index": 0,
      "output_type": "email",
      "resources_needed": "Gmail",
      "feasibility": "feasible",
      "plan_notes": "Draft and send a follow-up email summarizing the agreed terms."
    }
  ]
}

The index field must match the 0-based position of each item in the action_items list above.
Include one plan object for every action item.$$,
  'Planning pass: analyzes each action item before execution'
)
ON CONFLICT (name) DO NOTHING;
