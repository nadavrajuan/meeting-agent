-- migrations/003_context_used.sql
CREATE TABLE IF NOT EXISTS used_context_docs (
    drive_doc_id TEXT PRIMARY KEY,
    meeting_id   UUID NOT NULL,
    used_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Better execute_action_item prompt
UPDATE prompt_templates SET template = $$You are an AI assistant executing a concrete action item from a meeting.

## Action Item
{action_item}

## Meeting Summary
{meeting_summary}

## Related Emails & Context
{context}

## Extra Instructions
{extra_instructions}

---

Execute this action item completely and produce a concrete, ready-to-use deliverable.

Depending on the action item, produce one of:
- A drafted email or message (ready to send)
- A structured plan or timeline with specific dates/owners
- A written document, proposal, or analysis
- A research summary with findings and recommendations
- A decision framework with pros/cons

Be specific, detailed, and actionable. Do not just describe what to do — actually produce the output. Reference the meeting context and emails where relevant.

End your response with:
**Status**: [Complete / Needs more info]
**Next Step**: [One concrete immediate action]$$
WHERE name = 'execute_action_item';
