-- migrations/007_task_doc_short_name.sql
-- Per-task Drive docs + short human-readable task name

ALTER TABLE action_items
    ADD COLUMN IF NOT EXISTS short_name    TEXT,
    ADD COLUMN IF NOT EXISTS result_doc_id  TEXT,
    ADD COLUMN IF NOT EXISTS result_doc_url TEXT;

-- Update plan_action_items to also return a short_name per task
UPDATE prompt_templates
SET template = $$You are a meeting assistant AI. Below is a list of action items from a meeting.
For each action item, analyze what executing it would require and produce a plan.

## Extra Instructions & Context
{extra_instructions}

## Meeting Summary
{executive_summary}

## Action Items
{action_items_json}

For each action item, determine:
1. short_name: a concise 5-7 word title for this task (e.g. "Draft follow-up email to client").
2. output_type: the type of output this task produces.
   Options: "email", "document", "research", "draft", "code", "calendar", "analysis", "other"
3. resources_needed: what tools or access is needed (e.g. "Gmail", "Google Drive", "Web search", "None").
   Be specific but concise.
4. feasibility: can the AI agent complete this task?
   Options: "feasible" (yes fully), "partial" (partially), "not_feasible" (requires human action only)
5. plan_notes: a brief 1-2 sentence description of how the agent would execute this task.
   Pay close attention to any specific execution methods or preferences in the Extra Instructions.

Respond ONLY with valid JSON in this exact format (no extra text):
{
  "plans": [
    {
      "index": 0,
      "short_name": "Draft follow-up email to client",
      "output_type": "email",
      "resources_needed": "Gmail",
      "feasibility": "feasible",
      "plan_notes": "Draft and send a follow-up email summarizing the agreed terms."
    }
  ]
}

The index field must match the 0-based position of each item in the action_items list above.
Include one plan object for every action item.$$
WHERE name = 'plan_action_items';
