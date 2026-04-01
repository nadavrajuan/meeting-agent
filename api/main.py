# api/main.py
"""FastAPI backend for the meeting agent management UI."""

import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.db import DB
from agent.monitor import run_monitor
from agent.digest import run_daily_summary, run_weekly_summary

app = FastAPI(title="Meeting Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Labels ───────────────────────────────────────────────────────────────────

class LabelCreate(BaseModel):
    name: str
    color: Optional[str] = "#6366f1"
    description: Optional[str] = None
    keywords: Optional[list[str]] = []


@app.get("/labels")
def get_labels():
    with DB() as db:
        return db.get_all_labels()


@app.post("/labels")
def create_label(body: LabelCreate):
    with DB() as db:
        lid = db.get_or_create_label(body.name)
        db.execute(
            "UPDATE labels SET color=%s, description=%s, keywords=%s WHERE id=%s",
            (body.color, body.description, body.keywords, lid),
        )
        return db.fetchone("SELECT * FROM labels WHERE id=%s", (lid,))


@app.delete("/labels/{label_id}")
def delete_label(label_id: str):
    with DB() as db:
        db.execute("DELETE FROM labels WHERE id=%s", (label_id,))
    return {"ok": True}


# ─── People ───────────────────────────────────────────────────────────────────

class PersonUpdate(BaseModel):
    email: Optional[str] = None
    notes: Optional[str] = None
    label_ids: Optional[list[str]] = None


@app.get("/people")
def get_people():
    with DB() as db:
        return db.get_all_people()


@app.get("/people/{person_id}/meetings")
def get_person_meetings(person_id: str):
    with DB() as db:
        return db.get_person_meetings(person_id)


@app.put("/people/{person_id}")
def update_person(person_id: str, body: PersonUpdate):
    with DB() as db:
        if body.email is not None:
            db.execute("UPDATE people SET email=%s WHERE id=%s", (body.email, person_id))
        if body.notes is not None:
            db.execute("UPDATE people SET notes=%s WHERE id=%s", (body.notes, person_id))
        if body.label_ids is not None:
            db.execute("DELETE FROM people_labels WHERE person_id=%s", (person_id,))
            for lid in body.label_ids:
                db.execute(
                    "INSERT INTO people_labels(person_id, label_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (person_id, lid),
                )
        return db.fetchone("SELECT * FROM people WHERE id=%s", (person_id,))


# ─── Meetings ─────────────────────────────────────────────────────────────────

@app.get("/meetings")
def search_meetings(
    label: Optional[str] = None,
    person: Optional[str] = None,
    keyword: Optional[str] = None,
):
    with DB() as db:
        labels = [label] if label else None
        people = [person] if person else None
        return db.search_meetings(label_names=labels, person_names=people, keyword=keyword)


@app.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: str):
    with DB() as db:
        meeting = db.get_meeting(meeting_id)
        if not meeting:
            raise HTTPException(404, "Meeting not found")
        action_items = db.get_meeting_action_items(meeting_id)
        people = db.fetchall(
            "SELECT p.* FROM people p JOIN meeting_people mp ON mp.person_id=p.id WHERE mp.meeting_id=%s",
            (meeting_id,),
        )
        labels = db.fetchall(
            "SELECT l.* FROM labels l JOIN meeting_labels ml ON ml.label_id=l.id WHERE ml.meeting_id=%s",
            (meeting_id,),
        )
        return {**dict(meeting), "action_items": action_items, "people": people, "labels": labels}


@app.put("/meetings/{meeting_id}/labels")
def update_meeting_labels(meeting_id: str, label_ids: list[str]):
    with DB() as db:
        db.execute("DELETE FROM meeting_labels WHERE meeting_id=%s", (meeting_id,))
        for lid in label_ids:
            db.link_label_to_meeting(meeting_id, lid)
    return {"ok": True}


# ─── Action Items ─────────────────────────────────────────────────────────────

class ActionItemUpdate(BaseModel):
    status: Optional[str] = None
    result: Optional[str] = None


@app.put("/action-items/{item_id}")
def update_action_item(item_id: str, body: ActionItemUpdate):
    with DB() as db:
        updates = {k: v for k, v in body.dict().items() if v is not None}
        if updates:
            db.update_action_item(item_id, **updates)
    return {"ok": True}


# ─── Prompts ──────────────────────────────────────────────────────────────────

class PromptUpdate(BaseModel):
    template: str


@app.get("/prompts")
def get_prompts():
    with DB() as db:
        return db.fetchall("SELECT * FROM prompt_templates ORDER BY name")


@app.get("/prompts/{name}")
def get_prompt(name: str):
    with DB() as db:
        row = db.fetchone("SELECT * FROM prompt_templates WHERE name=%s", (name,))
        if not row:
            raise HTTPException(404, "Prompt not found")
        return row


@app.put("/prompts/{name}")
def update_prompt(name: str, body: PromptUpdate):
    with DB() as db:
        db.update_prompt(name, body.template)
    return {"ok": True}


# ─── Agent Runs ───────────────────────────────────────────────────────────────

@app.get("/runs")
def get_runs(limit: int = 50):
    with DB() as db:
        return db.fetchall(
            "SELECT r.*, m.drive_folder_name as meeting_name FROM agent_runs r "
            "LEFT JOIN meetings m ON m.id=r.meeting_id "
            "ORDER BY r.started_at DESC LIMIT %s",
            (limit,),
        )


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    with DB() as db:
        row = db.fetchone("SELECT * FROM agent_runs WHERE id=%s", (run_id,))
        if not row:
            raise HTTPException(404, "Run not found")
        return row


@app.get("/runs/{run_id}/log")
def get_run_log(run_id: str, since_id: int = 0):
    """Poll for new log entries since a given entry ID. Returns entries + run status."""
    with DB() as db:
        run = db.fetchone("SELECT id, status, ended_at FROM agent_runs WHERE id=%s", (run_id,))
        if not run:
            raise HTTPException(404, "Run not found")
        entries = db.get_run_log_entries(run_id, since_id)
    return {
        "entries": entries,
        "running": run["ended_at"] is None,
        "status": run["status"],
    }


# ─── Context Notes ────────────────────────────────────────────────────────────

class ContextNoteCreate(BaseModel):
    title: str
    content: str


@app.get("/context-notes")
def get_context_notes():
    with DB() as db:
        return db.get_context_notes()


@app.post("/context-notes")
def create_context_note(body: ContextNoteCreate):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from agent.drive_service import get_google_creds, DriveClient

    output_folder_id = os.getenv("GOOGLE_EXTRA_CONTEXT_FOLDER_ID", "").strip()

    drive_doc_id = None
    drive_doc_url = None

    if output_folder_id and not output_folder_id.startswith("#"):
        try:
            creds = get_google_creds(
                credentials_path=os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"),
                token_path=os.getenv("GOOGLE_TOKEN_PATH", "token.json"),
            )
            drive = DriveClient(creds)
            from datetime import date
            doc_name = f"{body.title} — {date.today().isoformat()}"
            html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;max-width:800px;margin:0 auto;padding:40px">
<h1 style="color:#1e293b;border-bottom:3px solid #6366f1;padding-bottom:12px">{body.title}</h1>
<p style="font-size:13px;color:#94a3b8">Created: {date.today().strftime("%B %d, %Y")}</p>
<div style="line-height:1.8;color:#374151;margin-top:24px;white-space:pre-wrap">{body.content}</div>
</body></html>"""
            result = drive.create_doc_from_text(doc_name, html, output_folder_id)
            drive_doc_id = result.get("id")
            drive_doc_url = result.get("webViewLink")
        except Exception as e:
            # Save to DB even if Drive fails
            pass

    with DB() as db:
        nid = db.create_context_note(body.title, body.content, drive_doc_id, drive_doc_url)
        return db.fetchone("SELECT * FROM context_notes WHERE id=%s", (nid,))


@app.delete("/context-notes/{note_id}")
def delete_context_note(note_id: str):
    with DB() as db:
        db.delete_context_note(note_id)
    return {"ok": True}


# ─── DB Management ────────────────────────────────────────────────────────────

class DeleteDBRequest(BaseModel):
    delete_labels: bool = False
    delete_people: bool = False


@app.get("/db/backup")
def backup_db():
    host = os.getenv("POSTGRES_HOST", "db")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "agent")
    password = os.getenv("POSTGRES_PASSWORD", "changeme")
    dbname = os.getenv("POSTGRES_DB", "meeting_agent")
    filename = f"meeting_agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"

    env = {**os.environ, "PGPASSWORD": password}
    proc = subprocess.Popen(
        ["pg_dump", "-h", host, "-p", port, "-U", user, dbname],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )

    def stream():
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            yield chunk
        proc.wait()

    return StreamingResponse(
        stream(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/db")
def delete_db_data(body: DeleteDBRequest):
    with DB() as db:
        db.execute("DELETE FROM agent_runs")
        db.execute("DELETE FROM meeting_people")
        db.execute("DELETE FROM meeting_labels")
        db.execute("DELETE FROM action_items")
        db.execute("DELETE FROM meetings")
        db.execute("DELETE FROM agent_state WHERE key = 'last_run_at'")
        if body.delete_people:
            db.execute("DELETE FROM people_labels")
            db.execute("DELETE FROM people")
        if body.delete_labels:
            db.execute("DELETE FROM meeting_labels")
            db.execute("DELETE FROM labels")
    return {"ok": True}


# ─── Agent Trigger ────────────────────────────────────────────────────────────

class TriggerRequest(BaseModel):
    last_n: Optional[int] = None
    max_iterations: Optional[int] = None
    skip_email_search: bool = False
    skip_action_items: bool = False
    require_approval: bool = False


@app.post("/trigger")
def trigger_agent(body: TriggerRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(
        run_monitor,
        last_n=body.last_n,
        max_iterations=body.max_iterations,
        skip_email_search=body.skip_email_search,
        skip_action_items=body.skip_action_items,
        require_approval=body.require_approval,
    )
    return {"message": "Agent triggered", "last_n": body.last_n, "settings": body.dict()}


# ─── Plan Review & Approval ───────────────────────────────────────────────────

@app.get("/meetings/{meeting_id}/plan")
def get_meeting_plan(meeting_id: str):
    """Return the meeting summary + all action items with their plan data."""
    with DB() as db:
        meeting = db.get_meeting(meeting_id)
        if not meeting:
            raise HTTPException(404, "Meeting not found")
        action_items = db.get_meeting_action_items(meeting_id)
    return {"meeting": dict(meeting), "action_items": action_items}


class ApprovalItem(BaseModel):
    action_item_id: str
    approved: bool
    max_iterations: int = 1
    description: Optional[str] = None


class ApprovalRequest(BaseModel):
    approvals: list[ApprovalItem]


@app.put("/meetings/{meeting_id}/plan")
def update_meeting_plan(meeting_id: str, body: ApprovalRequest):
    """Save per-task approve/reject decisions before triggering execution."""
    with DB() as db:
        for a in body.approvals:
            if a.description is not None:
                db.update_action_item(a.action_item_id, description=a.description)
            db.update_action_item_approval(
                a.action_item_id,
                approved=a.approved,
                approved_max_iterations=a.max_iterations,
            )
    return {"ok": True}


@app.post("/meetings/{meeting_id}/execute")
def execute_meeting(meeting_id: str, background_tasks: BackgroundTasks):
    """Trigger the execution phase for an approved meeting."""
    with DB() as db:
        meeting = db.get_meeting(meeting_id)
        if not meeting:
            raise HTTPException(404, "Meeting not found")
        db.set_meeting_approval_status(meeting_id, "approved")
    background_tasks.add_task(_run_execution_phase, meeting_id)
    return {"message": "Execution phase started", "meeting_id": meeting_id}


def _run_execution_phase(meeting_id: str):
    """Run the execution sub-graph for an already-planned meeting."""
    from agent.graph import get_execution_graph
    from agent.state import MeetingState

    with DB() as db:
        meeting = db.get_meeting(meeting_id)
        action_item_rows = db.get_meeting_action_items(meeting_id)
        run_id = db.create_run(meeting_id=meeting_id, run_type="execution")

    # Only include approved tasks
    approved_rows = [r for r in action_item_rows if r.get("approved")]
    action_items = [
        {"description": r["description"], "assignee": r.get("assignee_name"),
         "due_date": str(r["due_date"]) if r.get("due_date") else None}
        for r in approved_rows
    ]
    action_item_db_ids = [str(r["id"]) for r in approved_rows]
    approved_max_iters = max(
        (r.get("approved_max_iterations") or 1) for r in approved_rows
    ) if approved_rows else 1

    state: MeetingState = {
        "meeting_id": meeting_id,
        "drive_folder_id": str(meeting["drive_folder_id"]),
        "drive_folder_name": str(meeting["drive_folder_name"]),
        "meeting_date": str(meeting.get("meeting_date") or ""),
        "transcript_doc_id": None,
        "transcript_text": None,
        "summary_meta_doc_id": None,
        "summary_meta_text": None,
        "extra_context_doc_id": None,
        "extra_context_text": str(meeting.get("extra_context_text") or ""),
        "participants": [],
        "participant_emails": {},
        "labels": [],
        "tags": [],
        "executive_summary": str(meeting.get("summary") or ""),
        "key_points": [],
        "action_items": action_items,
        "decisions": [],
        "important_notes_addressed": None,
        "iteration": 0,
        "max_iterations": approved_max_iters,
        "tasks_to_execute": action_items,
        "task_results": [],
        "action_item_db_ids": action_item_db_ids,
        "skip_email_search": False,
        "skip_action_items": False,
        "require_approval": False,
        "action_item_plans": [],
        "approval_status": "approved",
        "related_emails": [],
        "related_meetings": [],
        "output_folder_id": str(meeting.get("output_folder_id") or "") or None,
        "output_folder_url": str(meeting.get("output_folder_url") or "") or None,
        "email_sent": False,
        "run_id": run_id,
        "run_log": [],
        "errors": [],
    }

    graph = get_execution_graph()
    final_state = graph.invoke(state)

    run_log = final_state.get("run_log", [])
    summary_log = "\n".join(f"[{e['step']}] {e['detail']}" for e in run_log)
    with DB() as db:
        db.finish_run(
            run_id,
            status="error" if final_state.get("errors") else "done",
            summary_log=summary_log,
            full_log=run_log,
            error="\n".join(final_state.get("errors", [])) or None,
        )


# ─── Meeting Chat ─────────────────────────────────────────────────────────────

class ChatMessageRequest(BaseModel):
    message: str
    system_prompt_override: Optional[str] = None


@app.post("/meetings/{meeting_id}/chat")
async def chat_with_meeting(meeting_id: str, body: ChatMessageRequest):
    """Send a message to the meeting chatbot and get an AI response."""
    import asyncio
    from agent.chat import MeetingChatHandler
    handler = MeetingChatHandler(meeting_id)
    # Run the blocking LLM call in a thread so the event loop stays free
    result = await asyncio.to_thread(
        handler.send_message, body.message, body.system_prompt_override
    )
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@app.get("/meetings/{meeting_id}/chat")
def get_chat_history(meeting_id: str):
    """Return the full chat history for a meeting."""
    with DB() as db:
        rows = db.fetchall(
            "SELECT id, role, content, metadata, created_at "
            "FROM chat_messages WHERE meeting_id=%s ORDER BY created_at",
            (meeting_id,),
        )
    return list(rows)


@app.get("/meetings/{meeting_id}/chat-prompt")
def get_chat_prompt(meeting_id: str):
    """Return the fully resolved system prompt for the meeting chat."""
    from agent.chat import MeetingChatHandler
    handler = MeetingChatHandler(meeting_id)
    prompt = handler.get_resolved_system_prompt()
    if not prompt:
        raise HTTPException(404, "Meeting not found")
    return {"prompt": prompt}


@app.delete("/meetings/{meeting_id}/chat")
def clear_chat_history(meeting_id: str):
    """Clear the chat history for a meeting."""
    with DB() as db:
        db.execute("DELETE FROM chat_messages WHERE meeting_id=%s", (meeting_id,))
    return {"ok": True}


@app.post("/trigger/daily")
def trigger_daily(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_daily_summary)
    return {"message": "Daily summary triggered"}


@app.post("/trigger/weekly")
def trigger_weekly(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_weekly_summary)
    return {"message": "Weekly summary triggered"}


# ─── Google Auth ─────────────────────────────────────────────────────────────

_auth_lock = threading.Lock()
_pending_flows: dict = {}  # state -> flow (for web callback)

def _token_path():
    return os.getenv("GOOGLE_TOKEN_PATH", "token.json")

def _credentials_path():
    return os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")

def _oauth_redirect_uri():
    app_url = os.getenv("APP_URL", "").rstrip("/")
    if app_url:
        return f"{app_url}/api/auth/google/callback"
    return "http://localhost:8082/"

@app.get("/auth/google/status")
def google_auth_status():
    token_path = _token_path()
    if not os.path.exists(token_path) or os.path.getsize(token_path) == 0:
        return {"valid": False, "reason": "no_token"}
    try:
        from google.oauth2.credentials import Credentials
        from agent.drive_service import SCOPES
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if creds.valid:
            return {"valid": True}
        if creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                with open(token_path, "w") as f:
                    f.write(creds.to_json())
                return {"valid": True}
            except Exception as e:
                return {"valid": False, "reason": "token_expired", "detail": str(e)}
        return {"valid": False, "reason": "no_refresh_token"}
    except Exception as e:
        return {"valid": False, "reason": "invalid_token", "detail": str(e)}


@app.post("/auth/google/start")
def google_auth_start():
    """Kick off the OAuth flow. Returns the URL the user must visit."""
    creds_path = _credentials_path()
    if not os.path.exists(creds_path):
        raise HTTPException(400, "credentials.json not found")

    from google_auth_oauthlib.flow import InstalledAppFlow, Flow
    from agent.drive_service import SCOPES

    redirect_uri = _oauth_redirect_uri()

    if redirect_uri.startswith("http://localhost"):
        # Dev: spin up a local callback server on port 8082
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
        flow.redirect_uri = redirect_uri
        auth_url, state = flow.authorization_url(prompt="consent")

        def _wait_for_callback():
            import wsgiref.simple_server, wsgiref.util
            from urllib.parse import urlparse, parse_qs
            captured = {}

            def wsgi_app(environ, start_response):
                captured["uri"] = wsgiref.util.request_uri(environ)
                start_response("200 OK", [("Content-Type", "text/html")])
                return [b"<h1>Authentication complete. You may close this window.</h1>"]

            server = wsgiref.simple_server.make_server("0.0.0.0", 8082, wsgi_app)
            while True:
                server.handle_request()
                if "uri" in captured:
                    parsed = urlparse(captured["uri"])
                    if parse_qs(parsed.query).get("state", [None])[0] == state:
                        break
                    captured.clear()

            code = parse_qs(urlparse(captured["uri"]).query).get("code", [None])[0]
            if code:
                flow.fetch_token(code=code)
                with open(_token_path(), "w") as f:
                    f.write(flow.credentials.to_json())

        with _auth_lock:
            t = threading.Thread(target=_wait_for_callback, daemon=True)
            t.start()
    else:
        # Production: use FastAPI callback endpoint
        flow = Flow.from_client_secrets_file(creds_path, SCOPES, redirect_uri=redirect_uri)
        auth_url, state = flow.authorization_url(prompt="consent", access_type="offline")
        _pending_flows[state] = flow

    return {"auth_url": auth_url}


@app.get("/auth/google/callback")
def google_auth_callback(code: str = None, state: str = None, error: str = None):
    """OAuth2 callback — Google redirects here after the user grants access."""
    from fastapi.responses import HTMLResponse
    if error:
        return HTMLResponse(f"<h2>Authentication failed: {error}</h2><p>You may close this window.</p>", status_code=400)
    flow = _pending_flows.pop(state, None)
    if not flow:
        return HTMLResponse("<h2>Invalid or expired session.</h2><p>Please try again from the app.</p>", status_code=400)
    flow.fetch_token(code=code)
    with open(_token_path(), "w") as f:
        f.write(flow.credentials.to_json())
    return HTMLResponse("<h2>Authentication complete.</h2><p>You may close this window and return to the app.</p>")


# ─── Graph Topology ──────────────────────────────────────────────────────────

@app.get("/graph/topology")
def get_graph_topology():
    """Return the LangGraph node/edge structure for visualization."""
    return {
        "nodes": [
            {"id": "fetch_documents", "label": "Fetch Documents", "type": "input"},
            {"id": "parse_extra_context", "label": "Parse Extra Context", "type": "process"},
            {"id": "summarize_meeting", "label": "Summarize Meeting", "type": "llm"},
            {"id": "save_to_db", "label": "Save to Database", "type": "process"},
            {"id": "search_emails", "label": "Search Emails", "type": "process"},
            {"id": "execute_action_items", "label": "Execute Action Items", "type": "llm"},
            {"id": "create_drive_outputs", "label": "Create Drive Outputs", "type": "output"},
            {"id": "send_email_report", "label": "Send Email Report", "type": "output"},
        ],
        "edges": [
            {"from": "fetch_documents", "to": "parse_extra_context"},
            {"from": "parse_extra_context", "to": "summarize_meeting"},
            {"from": "summarize_meeting", "to": "save_to_db"},
            {"from": "save_to_db", "to": "search_emails"},
            {"from": "search_emails", "to": "execute_action_items"},
            {"from": "execute_action_items", "to": "execute_action_items", "label": "iterate", "conditional": True},
            {"from": "execute_action_items", "to": "create_drive_outputs", "conditional": True},
            {"from": "create_drive_outputs", "to": "send_email_report"},
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("API_PORT", 8000)))
