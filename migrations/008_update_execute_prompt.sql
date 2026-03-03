-- migrations/008_update_execute_prompt.sql
-- Revamps execute_action_item prompt to:
--   1. Read important_notes and extra_instructions and apply relevant ones with HIGH PRIORITY
--   2. Lead the response with the direct deliverable (not a long preamble)
--   3. Use structured markdown headers so the output doc renders cleanly

UPDATE prompt_templates
SET template = $$You are an AI assistant helping to execute a meeting action item.

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
updated_at = NOW()
WHERE name = 'execute_action_item';
