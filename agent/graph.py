# agent/graph.py
"""
LangGraph-based meeting processing agent.

Graph topology:
  fetch_documents → parse_extra_context → summarize_meeting
    → save_to_db → search_emails → execute_action_items (loop)
    → create_drive_outputs → send_email_report → [done]
"""

import json
import os
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, StateGraph

from .db import DB
from .drive_service import DriveClient
from .gmail_service import GmailClient
from .llm_client import LLMClient
from .state import MeetingState


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt(template: str, **kwargs) -> str:
    """Replace {key} placeholders without choking on JSON braces in the template."""
    for k, v in kwargs.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def _log(state: MeetingState, step: str, detail: str, level: str = "info", data: dict = None) -> list[dict]:
    entry = {
        "step": step,
        "detail": detail,
        "level": level,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if data:
        entry["data"] = data
    print(f"[{level.upper()}] {step}: {detail}")
    # Write to DB immediately for real-time UI monitoring
    run_id = state.get("run_id")
    if run_id:
        try:
            with DB() as db:
                db.append_run_log(run_id, entry)
        except Exception as e:
            print(f"[WARN] Failed to persist log entry: {e}")
    return state.get("run_log", []) + [entry]


def _get_clients():
    from .drive_service import get_google_creds
    creds = get_google_creds(
        os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"),
        os.getenv("GOOGLE_TOKEN_PATH", "token.json"),
    )
    drive = DriveClient(creds)
    gmail = GmailClient(creds)
    llm = LLMClient()
    return drive, gmail, llm


# ─── Node: Fetch Documents ─────────────────────────────────────────────────────

def node_fetch_documents(state: MeetingState) -> MeetingState:
    drive, _, _ = _get_clients()
    folder_id = state["drive_folder_id"]
    log = state.get("run_log", [])

    # Find transcript doc
    transcript_files = drive.list_folder_contents_by_name_pattern(folder_id, "transcript")
    if not transcript_files:
        transcript_files = drive.list_folder_contents_by_name_pattern(folder_id, "Transcript")

    transcript_text = None
    transcript_doc_id = None
    if transcript_files:
        transcript_doc_id = transcript_files[0]["id"]
        transcript_text = drive.read_doc_as_text(transcript_doc_id)
        log = _log(state, "fetch_documents", f"Reading transcript: {transcript_files[0]['name']} ({len(transcript_text)} chars)",
                   data={"transcript_preview": transcript_text[:2000]})
        state["run_log"] = log

    # Find summary/meta doc
    summary_files = drive.list_folder_contents_by_name_pattern(folder_id, "summery")
    if not summary_files:
        summary_files = drive.list_folder_contents_by_name_pattern(folder_id, "summary")
    summary_meta_text = None
    summary_meta_doc_id = None
    if summary_files:
        summary_meta_doc_id = summary_files[0]["id"]
        summary_meta_text = drive.read_doc_as_text(summary_meta_doc_id)
        log = _log(state, "fetch_documents", f"Reading meta summary: {summary_files[0]['name']}")

    state["transcript_doc_id"] = transcript_doc_id
    state["transcript_text"] = transcript_text
    state["summary_meta_doc_id"] = summary_meta_doc_id
    state["summary_meta_text"] = summary_meta_text
    state["run_log"] = log
    state["errors"] = state.get("errors", [])
    if not transcript_text:
        state["errors"].append("No transcript document found in folder")
        log = _log(state, "fetch_documents", "No transcript found", "warning")
    return state


# ─── Node: Parse Extra Context ─────────────────────────────────────────────────

def node_parse_extra_context(state: MeetingState) -> MeetingState:
    context_folder_id = os.getenv("GOOGLE_EXTRA_CONTEXT_FOLDER_ID", "").strip()
    if not context_folder_id or context_folder_id.startswith("#"):
        state["extra_context_text"] = None
        return state

    drive, _, _ = _get_clients()
    log = state.get("run_log", [])

    # Load already-used doc IDs so we never reuse context for another meeting
    with DB() as db:
        used_ids = db.get_used_context_doc_ids()

    # Try to find context doc near meeting date matching labels, excluding already-used ones
    meeting_date = datetime.now(timezone.utc)
    if state.get("meeting_date"):
        try:
            meeting_date = datetime.fromisoformat(state["meeting_date"])
        except Exception:
            pass

    context_doc = drive.find_context_doc_near_date(
        context_folder_id,
        meeting_date,
        state.get("labels", []),
        exclude_ids=used_ids,
    )
    if context_doc:
        text = drive.read_doc_as_text(context_doc["id"])
        state["extra_context_doc_id"] = context_doc["id"]
        state["extra_context_text"] = text
        # Mark this doc as used for this meeting so it won't be reused
        with DB() as db:
            db.mark_context_doc_used(context_doc["id"], state["meeting_id"])
        log = _log(state, "parse_extra_context", f"Found extra context: {context_doc['name']}",
                   data={"doc_id": context_doc["id"], "doc_name": context_doc["name"],
                         "content_preview": text[:1000]})
    else:
        state["extra_context_text"] = None
        log = _log(state, "parse_extra_context", f"No extra context doc found (skipped {len(used_ids)} already-used docs)")

    state["run_log"] = log
    return state


# ─── Node: Summarize Meeting ───────────────────────────────────────────────────

def node_summarize_meeting(state: MeetingState) -> MeetingState:
    if not state.get("transcript_text"):
        state["run_log"] = _log(state, "summarize", "Skipping – no transcript", "warning")
        return state

    _, _, llm = _get_clients()
    log = state.get("run_log", [])
    log = _log(state, "summarize", "Calling LLM to summarize meeting...")
    state["run_log"] = log

    with DB() as db:
        prompt_template = db.get_prompt("summarize_meeting")

    extra_instructions = state.get("extra_context_text") or "None provided."
    important_notes = _extract_important_notes(state.get("extra_context_text") or "")

    prompt = _fmt(
        prompt_template,
        transcript=state["transcript_text"][:80000],
        extra_instructions=extra_instructions[:5000],
        important_notes=important_notes[:3000],
    )

    log = _log(state, "summarize", "Sending prompt to LLM...",
               data={"full_prompt": prompt})
    state["run_log"] = log

    try:
        result = llm.complete_json(prompt)
        state["participants"] = result.get("participants", [])
        state["executive_summary"] = result.get("executive_summary", "")
        state["key_points"] = result.get("key_points", [])
        state["action_items"] = result.get("action_items", [])
        state["decisions"] = result.get("decisions", [])
        state["tags"] = result.get("tags", [])
        state["important_notes_addressed"] = result.get("important_notes_addressed", "")
        log = _log(state, "summarize",
                   f"Summary done. {len(state['action_items'])} action items, {len(state['participants'])} participants.",
                   data={"llm_response": result})
    except Exception as e:
        state["errors"] = state.get("errors", []) + [f"Summarize error: {e}"]
        log = _log(state, "summarize", f"Error: {e}", "error")

    state["run_log"] = log
    return state


def _extract_important_notes(context_text: str) -> str:
    """Extract important notes section from extra context."""
    if not context_text:
        return ""
    lower = context_text.lower()
    for marker in ["important notes", "important things", "key notes"]:
        idx = lower.find(marker)
        if idx >= 0:
            return context_text[idx:idx + 3000]
    return ""


# ─── Node: Save to DB ──────────────────────────────────────────────────────────

def node_save_to_db(state: MeetingState) -> MeetingState:
    log = _log(state, "save_to_db", "Saving meeting data to database...")
    state["run_log"] = log

    with DB() as db:
        db.update_meeting(
            state["meeting_id"],
            summary=state.get("executive_summary"),
            key_points=json.dumps(state.get("key_points", [])),
            decisions=json.dumps(state.get("decisions", [])),
            raw_transcript_text=state.get("transcript_text") or "",
            status="processing",
            processed_at=datetime.now(timezone.utc),
        )

        # Upsert people and propagate their personal labels to the meeting
        person_label_ids = set()
        for name in state.get("participants", []):
            email = state.get("participant_emails", {}).get(name)
            person_id = db.upsert_person(name, email)
            db.link_person_to_meeting(state["meeting_id"], person_id)
            for label_id in db.get_person_label_ids(person_id):
                person_label_ids.add(label_id)

        for label_id in person_label_ids:
            db.link_label_to_meeting(state["meeting_id"], label_id)

        # Upsert labels/tags
        for tag in state.get("tags", []) + state.get("labels", []):
            label_id = db.get_or_create_label(tag)
            db.link_label_to_meeting(state["meeting_id"], label_id)

        # Create action items and store their DB IDs for later result write-back
        action_ids = []
        for item in state.get("action_items", []):
            aid = db.create_action_item(
                state["meeting_id"],
                item.get("description", ""),
                item.get("assignee"),
                item.get("due_date"),
            )
            action_ids.append(aid)

    state["action_item_db_ids"] = action_ids

    # Execute unless skipped
    if state.get("skip_action_items"):
        state["tasks_to_execute"] = []
        state["task_results"] = []
        state["iteration"] = 0
        state["run_log"] = _log(state, "save_to_db", f"Saved. {len(action_ids)} action items. Action item execution skipped by settings.")
    else:
        state["tasks_to_execute"] = state.get("action_items", [])
        state["task_results"] = []
        state["iteration"] = 0
        state["run_log"] = _log(state, "save_to_db", f"Saved. {len(action_ids)} action items created, {len(state['tasks_to_execute'])} queued for execution.")
    return state


# ─── Node: Search Emails ───────────────────────────────────────────────────────

def node_search_emails(state: MeetingState) -> MeetingState:
    if state.get("skip_email_search"):
        state["related_emails"] = []
        state["run_log"] = _log(state, "search_emails", "Skipped by settings.")
        return state

    _, gmail, _ = _get_clients()
    log = _log(state, "search_emails", "Searching Gmail for related emails...")
    state["run_log"] = log

    participant_emails = state.get("participant_emails", {})
    email_addresses = [e for e in participant_emails.values() if e]
    keywords = state.get("tags", [])[:3]

    related_emails = []
    if email_addresses:
        related_emails = gmail.search_emails_for_people(email_addresses, keywords)
        log = _log(state, "search_emails", f"Found {len(related_emails)} related emails.",
                   data={"query_emails": email_addresses, "query_keywords": keywords,
                         "emails": [{"subject": e.get("subject"), "from": e.get("from"), "snippet": e.get("snippet")} for e in related_emails]})
    else:
        log = _log(state, "search_emails", "No participant emails known, skipping Gmail search.")

    state["related_emails"] = related_emails
    state["run_log"] = log
    return state


# ─── Node: Execute Action Items ────────────────────────────────────────────────

def node_execute_action_items(state: MeetingState) -> MeetingState:
    tasks = state.get("tasks_to_execute", [])
    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", int(os.getenv("MAX_ITERATIONS", 5)))

    if not tasks or iteration >= max_iter:
        state["run_log"] = _log(state, "execute_tasks", f"No tasks to execute or max iterations reached ({iteration}/{max_iter})")
        return state

    # Build owner + feasibility filter
    owner_name = (os.getenv("AGENT_OWNER_NAME", "") or "").strip().lower()
    plans = state.get("action_item_plans", [])
    feasibility_by_idx = {p.get("index", -1): p.get("feasibility", "unknown") for p in plans}
    action_items_list = state.get("action_items", [])
    action_item_db_ids = state.get("action_item_db_ids", [])

    _, _, llm = _get_clients()

    with DB() as db:
        prompt_template = db.get_prompt("execute_action_item")

    task_results = state.get("task_results", [])
    context = "\n".join([
        f"Email: {e['subject']} from {e['from']}: {e['snippet']}"
        for e in state.get("related_emails", [])[:5]
    ])

    executed = 0
    skipped = 0

    for task in tasks:
        task_desc = task.get("description", "")

        # Find index in action_items_list and DB ID
        task_idx = None
        db_id = None
        for j, ai in enumerate(action_items_list):
            if ai.get("description", "") == task_desc:
                task_idx = j
                if j < len(action_item_db_ids):
                    db_id = action_item_db_ids[j]
                break

        # Skip if not feasible for AI execution
        feasibility = feasibility_by_idx.get(task_idx, "unknown") if task_idx is not None else "unknown"
        if feasibility == "not_feasible":
            if db_id:
                with DB() as db2:
                    db2.update_action_item(db_id, status="skipped",
                                           result="Skipped: not feasible for AI execution")
            state["run_log"] = _log(state, "execute_tasks", f"Skipped (not feasible): {task_desc[:70]}")
            skipped += 1
            continue

        # Skip if assigned to someone other than the owner
        if owner_name:
            assignee = (task.get("assignee") or "").strip().lower()
            if assignee and owner_name not in assignee:
                if db_id:
                    with DB() as db2:
                        db2.update_action_item(
                            db_id, status="skipped",
                            result=f"Skipped: assigned to {task.get('assignee')}, not to agent owner"
                        )
                state["run_log"] = _log(state, "execute_tasks",
                                        f"Skipped (not owner's task — {task.get('assignee')}): {task_desc[:60]}")
                skipped += 1
                continue

        important_notes = _extract_important_notes(state.get("extra_context_text") or "")
        prompt = _fmt(
            prompt_template,
            action_item=task_desc,
            meeting_summary=state.get("executive_summary", ""),
            extra_instructions=(state.get("extra_context_text") or "")[:5000],
            important_notes=important_notes[:3000] if important_notes else "None.",
            context=context[:3000],
        )
        log = _log(state, "execute_tasks", f"Executing: {task_desc[:80]}...",
                   data={"full_prompt": prompt, "task": task_desc,
                         "assignee": task.get("assignee"), "due_date": task.get("due_date")})
        state["run_log"] = log
        try:
            result = llm.complete(prompt)
            task_results.append({
                "task": task_desc,
                "assignee": task.get("assignee"),
                "due_date": task.get("due_date"),
                "result": result,
                "iteration": iteration,
                "db_id": db_id,
            })
            if db_id:
                with DB() as db2:
                    db2.update_action_item(db_id, status="done", result=result[:10000])
            log = _log(state, "execute_tasks", f"Done: {task_desc[:60]}",
                       data={"result": result})
            state["run_log"] = log
            executed += 1
        except Exception as e:
            task_results.append({
                "task": task_desc,
                "assignee": task.get("assignee"),
                "due_date": task.get("due_date"),
                "result": f"Error: {e}",
                "iteration": iteration,
                "db_id": db_id,
            })
            if db_id:
                with DB() as db2:
                    db2.update_action_item(db_id, status="error", result=f"Error: {e}")
            log = _log(state, "execute_tasks", f"Error on task: {e}", "error")
            state["run_log"] = log

    state["task_results"] = task_results
    state["iteration"] = iteration + 1
    state["tasks_to_execute"] = []
    state["run_log"] = _log(state, "execute_tasks",
                            f"Completed: {executed} executed, {skipped} skipped.")
    return state


# ─── Node: Plan Action Items ───────────────────────────────────────────────────

def node_plan_action_items(state: MeetingState) -> MeetingState:
    action_items = state.get("action_items", [])
    if not action_items or state.get("skip_action_items"):
        state["action_item_plans"] = []
        state["run_log"] = _log(state, "plan", "No action items to plan, skipping.")
        return state

    _, _, llm = _get_clients()
    log = _log(state, "plan", f"Planning {len(action_items)} action items...")
    state["run_log"] = log

    with DB() as db:
        prompt_template = db.get_prompt("plan_action_items")

    action_items_json = json.dumps([
        {"index": i, "description": a.get("description", ""),
         "assignee": a.get("assignee"), "due_date": a.get("due_date")}
        for i, a in enumerate(action_items)
    ], indent=2)

    prompt = _fmt(
        prompt_template,
        executive_summary=state.get("executive_summary", ""),
        action_items_json=action_items_json,
        extra_instructions=(state.get("extra_context_text") or "None provided.")[:5000],
    )

    log = _log(state, "plan", "Sending plan request to LLM...", data={"full_prompt": prompt})
    state["run_log"] = log

    plans = []
    try:
        result = llm.complete_json(prompt)
        plans = result.get("plans", [])
        state["action_item_plans"] = plans

        action_item_db_ids = state.get("action_item_db_ids", [])
        with DB() as db:
            for plan in plans:
                idx = plan.get("index", -1)
                if 0 <= idx < len(action_item_db_ids):
                    db.update_action_item_plan(
                        action_item_db_ids[idx],
                        plan_output_type=plan.get("output_type", "other"),
                        plan_resources=plan.get("resources_needed", ""),
                        plan_notes=plan.get("plan_notes", ""),
                        feasibility=plan.get("feasibility", "unknown"),
                        short_name=plan.get("short_name"),
                    )

        log = _log(state, "plan", f"Planning complete. {len(plans)} plans generated.", data={"plans": plans})
    except Exception as e:
        state["errors"] = state.get("errors", []) + [f"Plan error: {e}"]
        log = _log(state, "plan", f"Planning error: {e}", "error")
        state["action_item_plans"] = []

    # If require_approval, pause and store context for recovery
    if state.get("require_approval"):
        with DB() as db:
            db.set_meeting_approval_status(
                state["meeting_id"],
                "awaiting_approval",
                extra_context_text=(state.get("extra_context_text") or "")[:50000],
            )
        state["approval_status"] = "awaiting_approval"
        log = _log(state, "plan", "Paused for approval — review tasks in the UI to approve and execute.")

    state["run_log"] = log
    return state


def after_plan(state: MeetingState) -> str:
    """Route: skip execution and go straight to outputs if awaiting approval."""
    if state.get("require_approval") and state.get("approval_status") == "awaiting_approval":
        return "create_drive_outputs"
    return "search_emails"


def should_iterate(state: MeetingState) -> str:
    """Edge condition: keep iterating if there are more tasks."""
    if state.get("tasks_to_execute") and state.get("iteration", 0) < state.get("max_iterations", 5):
        return "execute_action_items"
    return "create_drive_outputs"


# ─── Node: Create Drive Outputs ────────────────────────────────────────────────

def node_create_drive_outputs(state: MeetingState) -> MeetingState:
    output_parent = os.getenv("GOOGLE_DRIVE_OUTPUT_FOLDER_ID", "")
    if not output_parent:
        state["run_log"] = _log(state, "create_outputs", "No output folder configured, skipping.", "warning")
        return state

    drive, _, _ = _get_clients()
    folder_name = f"Meeting Output - {state.get('drive_folder_name', state['meeting_id'])}"
    log = _log(state, "create_outputs", f"Creating output folder: {folder_name}")
    state["run_log"] = log

    try:
        folder = drive.create_folder(folder_name, output_parent)
        folder_id = folder["id"]
        folder_url = folder.get("webViewLink", "")

        summary_content = _build_summary_doc(state)
        drive.create_doc_from_text("Meeting Summary", summary_content, folder_id)

        for tr in state.get("task_results", []):
            db_id = tr.get("db_id")
            short_name = None
            if db_id:
                with DB() as db:
                    row = db.fetchone("SELECT short_name FROM action_items WHERE id=%s", (db_id,))
                    if row:
                        short_name = row.get("short_name")
            doc_name = short_name or tr.get("task", "Task")[:60]
            doc_content = _build_single_task_doc(tr, state)
            doc = drive.create_doc_from_text(doc_name, doc_content, folder_id)
            if db_id:
                with DB() as db:
                    db.update_action_item(db_id, result_doc_id=doc["id"],
                                         result_doc_url=doc.get("webViewLink", ""))

        log_content = json.dumps(state.get("run_log", []), indent=2)
        drive.create_doc_from_text("Agent Run Log", log_content, folder_id)

        with DB() as db:
            db.update_meeting(
                state["meeting_id"],
                output_folder_id=folder_id,
                output_folder_url=folder_url,
                status="done",
            )

        state["output_folder_id"] = folder_id
        state["output_folder_url"] = folder_url
        state["run_log"] = _log(state, "create_outputs", f"Output folder created: {folder_url}")
    except Exception as e:
        state["errors"] = state.get("errors", []) + [f"Drive output error: {e}"]
        state["run_log"] = _log(state, "create_outputs", f"Failed to create Drive outputs: {e}", "error")
        with DB() as db:
            db.update_meeting(state["meeting_id"], status="done")

    return state


def _build_summary_doc(state: MeetingState) -> str:
    title = state.get("drive_folder_name", "Meeting")
    date_str = state.get("meeting_date", "")
    if date_str:
        try:
            date_str = datetime.fromisoformat(str(date_str)).strftime("%B %d, %Y")
        except Exception:
            pass
    participants = ", ".join(state.get("participants", [])) or "—"
    labels = ", ".join(state.get("tags", []) + state.get("labels", [])) or "—"

    key_points_html = "".join(
        f"<li style='margin-bottom:6px'>{p}</li>" for p in state.get("key_points", [])
    ) or "<li>—</li>"

    action_rows = "".join(
        f"""<tr style='background:{"#f8fafc" if i%2==0 else "#ffffff"}'>
              <td style='padding:10px 14px;border:1px solid #e2e8f0'>{a.get('description','')}</td>
              <td style='padding:10px 14px;border:1px solid #e2e8f0;white-space:nowrap'>{a.get('assignee','TBD')}</td>
              <td style='padding:10px 14px;border:1px solid #e2e8f0;white-space:nowrap'>{a.get('due_date','TBD')}</td>
            </tr>"""
        for i, a in enumerate(state.get("action_items", []))
    ) or "<tr><td colspan='3' style='padding:10px 14px;color:#94a3b8'>No action items</td></tr>"

    decisions_html = "".join(
        f"<li style='margin-bottom:6px'>{d}</li>" for d in state.get("decisions", [])
    ) or "<li>—</li>"

    notes_section = ""
    if state.get("important_notes_addressed"):
        notes_section = f"""
        <h2 style='color:#1e293b;margin-top:36px;font-size:18px'>📌 Important Notes</h2>
        <p style='line-height:1.7;color:#374151;background:#fefce8;padding:16px;border-radius:8px;border-left:4px solid #f59e0b'>
          {state["important_notes_addressed"]}
        </p>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;max-width:820px;margin:0 auto;padding:40px 32px;color:#1e293b">

<h1 style="font-size:26px;font-weight:700;border-bottom:3px solid #6366f1;padding-bottom:14px;margin-bottom:24px">
  📋 {title}
</h1>

<table style="width:100%;border-collapse:collapse;margin-bottom:28px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0">
  <tr><td style="padding:10px 18px;font-weight:600;color:#6366f1;width:140px">📅 Date</td>
      <td style="padding:10px 18px">{date_str or '—'}</td></tr>
  <tr style="background:#fff"><td style="padding:10px 18px;font-weight:600;color:#6366f1">👥 Participants</td>
      <td style="padding:10px 18px">{participants}</td></tr>
  <tr><td style="padding:10px 18px;font-weight:600;color:#6366f1">🏷️ Labels</td>
      <td style="padding:10px 18px">{labels}</td></tr>
</table>

<h2 style="color:#1e293b;margin-top:32px;font-size:18px">📝 Executive Summary</h2>
<p style="line-height:1.8;color:#374151;background:#eff6ff;padding:18px;border-radius:8px;border-left:4px solid #6366f1;margin-top:10px">
  {state.get("executive_summary", "—")}
</p>

<h2 style="color:#1e293b;margin-top:36px;font-size:18px">💡 Key Points</h2>
<ul style="line-height:1.8;color:#374151;padding-left:20px;margin-top:10px">
  {key_points_html}
</ul>

<h2 style="color:#1e293b;margin-top:36px;font-size:18px">✅ Action Items</h2>
<table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:14px">
  <thead>
    <tr style="background:#6366f1;color:#fff">
      <th style="padding:11px 14px;text-align:left;border:1px solid #4f46e5">Description</th>
      <th style="padding:11px 14px;text-align:left;border:1px solid #4f46e5;width:140px">Assignee</th>
      <th style="padding:11px 14px;text-align:left;border:1px solid #4f46e5;width:120px">Due Date</th>
    </tr>
  </thead>
  <tbody>{action_rows}</tbody>
</table>

<h2 style="color:#1e293b;margin-top:36px;font-size:18px">🔑 Decisions Made</h2>
<ul style="line-height:1.8;color:#374151;padding-left:20px;margin-top:10px">
  {decisions_html}
</ul>
{notes_section}

<hr style="border:none;border-top:1px solid #e2e8f0;margin-top:40px"/>
<p style="font-size:12px;color:#94a3b8;margin-top:12px">Generated by Meeting Agent</p>
</body></html>"""


def _inline_md(text: str) -> str:
    """Convert inline markdown (bold, italic, inline code) to HTML."""
    import re
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'`([^`]+?)`', r'<code style="background:#f1f5f9;padding:1px 5px;border-radius:3px;font-family:monospace;font-size:13px">\1</code>', text)
    return text


def _markdown_to_html(text: str) -> str:
    """Convert basic markdown to styled HTML for Google Docs rendering."""
    import re
    lines = text.split('\n')
    html_lines = []
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            html_lines.append('</ul>')
            in_ul = False
        if in_ol:
            html_lines.append('</ol>')
            in_ol = False

    for line in lines:
        stripped = line.strip()

        if stripped in ('---', '***', '___'):
            close_lists()
            html_lines.append('<hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0"/>')
            continue

        if stripped.startswith('# '):
            close_lists()
            content = _inline_md(stripped[2:].strip())
            html_lines.append(f'<h1 style="font-size:20px;font-weight:700;color:#1e293b;border-bottom:2px solid #6366f1;padding-bottom:8px;margin:28px 0 14px">{content}</h1>')
        elif stripped.startswith('## '):
            close_lists()
            content = _inline_md(stripped[3:].strip())
            html_lines.append(f'<h2 style="font-size:17px;font-weight:600;color:#1e293b;margin:22px 0 10px">{content}</h2>')
        elif stripped.startswith('### '):
            close_lists()
            content = _inline_md(stripped[4:].strip())
            html_lines.append(f'<h3 style="font-size:15px;font-weight:600;color:#374151;margin:16px 0 8px">{content}</h3>')
        elif re.match(r'^[-*] ', stripped):
            if in_ol:
                html_lines.append('</ol>')
                in_ol = False
            if not in_ul:
                html_lines.append('<ul style="margin:8px 0;padding-left:24px;color:#374151;line-height:1.7">')
                in_ul = True
            html_lines.append(f'<li style="margin-bottom:4px">{_inline_md(stripped[2:].strip())}</li>')
        elif re.match(r'^\d+\. ', stripped):
            if in_ul:
                html_lines.append('</ul>')
                in_ul = False
            if not in_ol:
                html_lines.append('<ol style="margin:8px 0;padding-left:24px;color:#374151;line-height:1.7">')
                in_ol = True
            content = re.sub(r'^\d+\. ', '', stripped)
            html_lines.append(f'<li style="margin-bottom:4px">{_inline_md(content.strip())}</li>')
        elif not stripped:
            close_lists()
            html_lines.append('<br>')
        else:
            close_lists()
            html_lines.append(f'<p style="margin:6px 0;color:#374151;line-height:1.7">{_inline_md(stripped)}</p>')

    close_lists()
    return '\n'.join(html_lines)


def _build_single_task_doc(tr: dict, state: MeetingState) -> str:
    title = state.get("drive_folder_name", "Meeting")
    date_str = state.get("meeting_date", "")
    if date_str:
        try:
            date_str = datetime.fromisoformat(str(date_str)).strftime("%B %d, %Y")
        except Exception:
            pass

    result_html = _markdown_to_html(tr.get("result", ""))
    assignee = tr.get("assignee") or "—"
    due_date = tr.get("due_date") or "—"
    task_desc = tr.get("task", "")

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;max-width:820px;margin:0 auto;padding:40px 32px;color:#1e293b">

<p style="color:#64748b;font-size:13px;margin-bottom:6px">{title} &nbsp;·&nbsp; {date_str or '—'}</p>
<h1 style="font-size:22px;font-weight:700;border-bottom:3px solid #6366f1;padding-bottom:12px;margin-bottom:20px">
  🤖 {task_desc}
</h1>

<table style="width:100%;border-collapse:collapse;margin-bottom:28px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0;font-size:14px">
  <tr><td style="padding:9px 16px;font-weight:600;color:#6366f1;width:120px">Assignee</td>
      <td style="padding:9px 16px">{assignee}</td></tr>
  <tr style="background:#fff"><td style="padding:9px 16px;font-weight:600;color:#6366f1">Due Date</td>
      <td style="padding:9px 16px">{due_date}</td></tr>
</table>

<div style="font-size:14px">{result_html}</div>

<hr style="border:none;border-top:1px solid #e2e8f0;margin-top:40px"/>
<p style="font-size:12px;color:#94a3b8;margin-top:12px">Generated by Meeting Agent</p>
</body></html>"""


# ─── Node: Send Email Report ───────────────────────────────────────────────────

def node_send_email_report(state: MeetingState) -> MeetingState:
    send_to = os.getenv("SEND_SUMMARY_TO", "")
    if not send_to:
        state["run_log"] = _log(state, "send_email", "No recipient configured, skipping.", "warning")
        state["email_sent"] = False
        return state

    _, gmail, _ = _get_clients()
    log = _log(state, "send_email", f"Sending summary email to {send_to}...")
    state["run_log"] = log

    subject = f"Meeting Summary: {state.get('drive_folder_name', 'Meeting')}"
    body = _build_email_html(state)
    sent = gmail.send_email(send_to, subject, body)
    state["email_sent"] = sent
    state["run_log"] = _log(state, "send_email", "Email sent!" if sent else "Email failed.", "info" if sent else "error")
    return state


def _build_email_html(state: MeetingState) -> str:
    action_items_html = "".join(
        f"<li><b>{a.get('assignee', 'TBD')}</b>: {a.get('description', '')} <i>(Due: {a.get('due_date', 'TBD')})</i></li>"
        for a in state.get("action_items", [])
    )
    tasks_html = "".join(
        f"<details><summary><b>Iteration {tr.get('iteration',0)+1}</b>: {tr.get('task','')[:80]}...</summary><p>{tr.get('result','')[:2000]}</p></details>"
        for tr in state.get("task_results", [])
    )
    output_link = state.get("output_folder_url", "")
    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:800px;margin:0 auto;">
    <h1>📋 Meeting Summary</h1>
    <h2>{state.get('drive_folder_name', 'Meeting')}</h2>
    <p><b>Date:</b> {state.get('meeting_date', 'Unknown')}</p>
    <p><b>Participants:</b> {', '.join(state.get('participants', []))}</p>
    <p><b>Labels:</b> {', '.join(state.get('tags', []) + state.get('labels', []))}</p>
    <hr/>
    <h3>📝 Executive Summary</h3>
    <p>{state.get('executive_summary', 'N/A')}</p>
    <h3>✅ Action Items</h3>
    <ul>{action_items_html}</ul>
    {f'<h3>🤖 Tasks Executed</h3>{tasks_html}' if tasks_html else ''}
    {f'<hr/><p><a href="{output_link}">📁 View Full Output in Google Drive</a></p>' if output_link else ''}
    </body></html>
    """


# ─── Build LangGraph ──────────────────────────────────────────────────────────

def build_meeting_graph() -> StateGraph:
    """Full pipeline: fetch → parse → summarize → save → plan → (approve?) → search → execute → outputs → email."""
    g = StateGraph(MeetingState)

    g.add_node("fetch_documents", node_fetch_documents)
    g.add_node("parse_extra_context", node_parse_extra_context)
    g.add_node("summarize_meeting", node_summarize_meeting)
    g.add_node("save_to_db", node_save_to_db)
    g.add_node("plan_action_items", node_plan_action_items)
    g.add_node("search_emails", node_search_emails)
    g.add_node("execute_action_items", node_execute_action_items)
    g.add_node("create_drive_outputs", node_create_drive_outputs)
    g.add_node("send_email_report", node_send_email_report)

    g.set_entry_point("fetch_documents")
    g.add_edge("fetch_documents", "parse_extra_context")
    g.add_edge("parse_extra_context", "summarize_meeting")
    g.add_edge("summarize_meeting", "save_to_db")
    g.add_edge("save_to_db", "plan_action_items")
    g.add_conditional_edges("plan_action_items", after_plan)
    g.add_edge("search_emails", "execute_action_items")
    g.add_conditional_edges("execute_action_items", should_iterate)
    g.add_edge("create_drive_outputs", "send_email_report")
    g.add_edge("send_email_report", END)

    return g.compile()


def build_execution_graph() -> StateGraph:
    """Execution-only pipeline: search_emails → execute → outputs → email (entry point after approval)."""
    g = StateGraph(MeetingState)

    g.add_node("search_emails", node_search_emails)
    g.add_node("execute_action_items", node_execute_action_items)
    g.add_node("create_drive_outputs", node_create_drive_outputs)
    g.add_node("send_email_report", node_send_email_report)

    g.set_entry_point("search_emails")
    g.add_edge("search_emails", "execute_action_items")
    g.add_conditional_edges("execute_action_items", should_iterate)
    g.add_edge("create_drive_outputs", "send_email_report")
    g.add_edge("send_email_report", END)

    return g.compile()


MEETING_GRAPH = None
EXECUTION_GRAPH = None


def get_meeting_graph():
    global MEETING_GRAPH
    if MEETING_GRAPH is None:
        MEETING_GRAPH = build_meeting_graph()
    return MEETING_GRAPH


def get_execution_graph():
    global EXECUTION_GRAPH
    if EXECUTION_GRAPH is None:
        EXECUTION_GRAPH = build_execution_graph()
    return EXECUTION_GRAPH
