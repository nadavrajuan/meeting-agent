# agent/chat.py
"""
Interactive meeting chatbot handler.

System prompt is loaded from the 'meeting_chat' prompt_template (editable via
the Prompts UI) and filled with live meeting data.  An optional
system_prompt_override replaces the template entirely for the duration of one
send_message() call (stored in caller, not persisted).

Tool-use loop (OpenAI only) supports:
  - search_gmail       : search Gmail for relevant emails
  - read_email         : fetch full body of a specific email
  - create_document    : create a styled Google Doc and return its URL
"""

import json
import os
from datetime import datetime
from typing import Optional

from .db import DB
from .drive_service import DriveClient, get_google_creds
from .gmail_service import GmailClient
from .graph import _fmt, _markdown_to_html
from .llm_client import LLMClient


# ─── Tool definitions ────────────────────────────────────────────────────────

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_gmail",
            "description": (
                "Search Gmail for emails related to this meeting or a topic the user mentioned. "
                "Use proactively when the question might benefit from email context or history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Gmail search query (same syntax as the Gmail search box). "
                            "E.g. 'from:alice@example.com subject:proposal after:2024/01/01'"
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Fetch the full text body of a specific email found via search_gmail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "The email ID returned by search_gmail.",
                    }
                },
                "required": ["email_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": (
                "Create a formatted Google Doc containing a deliverable (draft, report, prompt, "
                "analysis, etc.) and return a shareable link. Use whenever the user wants output "
                "saved as a document, or the answer is long enough to warrant a doc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short document title."},
                    "content": {
                        "type": "string",
                        "description": (
                            "Full document content in markdown. "
                            "Use # h1, ## h2, **bold**, - lists, etc."
                        ),
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_drive",
            "description": (
                "Search Google Drive for relevant documents, reports, or files. "
                "Use when the user asks about past deliverables, shared files, or references a document."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords or phrase to search for across all Drive documents.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_drive_document",
            "description": "Fetch the full text of a specific Google Drive document found via search_drive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "The file ID returned by search_drive.",
                    }
                },
                "required": ["file_id"],
            },
        },
    },
]

# ─── Fallback template (used when DB has none) ───────────────────────────────

_FALLBACK_TEMPLATE = """You are an intelligent meeting assistant with complete context about the following meeting. Help the user understand the meeting, answer follow-up questions, and complete tasks related to it.

## Meeting: {meeting_name}
**Date:** {date}
**Participants:** {participants}
**Topics / Labels:** {labels}

## Executive Summary
{summary}

## Key Points
{key_points}

## Decisions Made
{decisions}

## Action Items
{action_items}
{extra_context_section}
{output_section}

## Full Meeting Transcript
{transcription}

---

## Tool Use Instructions

You have five tools available. Think carefully before each answer:

- **search_gmail** — Search Gmail for relevant emails. Use proactively when the question involves past communication or email context would strengthen your answer.
- **read_email** — Fetch the full body of a specific email from search_gmail results.
- **search_drive** — Search Google Drive for relevant documents, reports, or files.
- **read_drive_document** — Fetch the full text of a specific Drive document found via search_drive.
- **create_document** — Create a formatted Google Doc with a complete deliverable and return a shareable link. Use whenever the user asks you to write, draft, or generate something they'd want saved. Always return the link.

Think step by step. If the request involves generating a deliverable, use create_document. If it could benefit from email context, search_gmail first. If they reference a document or past work, search_drive."""


# ─── System prompt builder ────────────────────────────────────────────────────

def build_chat_system_prompt(
    meeting: dict,
    action_items: list,
    people: list,
    labels: list,
    include_transcript: bool = True,
) -> str:
    """Build the resolved system prompt from the DB template + meeting data."""
    # Load template from DB
    with DB() as db:
        template = db.get_prompt("meeting_chat") or _FALLBACK_TEMPLATE

    date_str = ""
    if meeting.get("meeting_date"):
        try:
            date_str = datetime.fromisoformat(str(meeting["meeting_date"])).strftime("%B %d, %Y")
        except Exception:
            date_str = str(meeting.get("meeting_date", ""))

    participants = ", ".join(p["name"] for p in (people or [])) or "—"
    label_names = ", ".join(l["name"] for l in (labels or [])) or "—"

    # key_points and decisions stored as JSON in meetings table
    key_points_raw = meeting.get("key_points") or []
    if isinstance(key_points_raw, str):
        try:
            key_points_raw = json.loads(key_points_raw)
        except Exception:
            key_points_raw = []
    decisions_raw = meeting.get("decisions") or []
    if isinstance(decisions_raw, str):
        try:
            decisions_raw = json.loads(decisions_raw)
        except Exception:
            decisions_raw = []

    key_points_text = "\n".join(f"- {p}" for p in key_points_raw) if key_points_raw else "—"
    decisions_text = "\n".join(f"- {d}" for d in decisions_raw) if decisions_raw else "—"

    ai_lines = []
    for a in (action_items or []):
        status = a.get("status", "open")
        assignee = a.get("assignee_name") or "TBD"
        due = a.get("due_date") or "TBD"
        result_link = f" [result doc: {a['result_doc_url']}]" if a.get("result_doc_url") else ""
        ai_lines.append(
            f"- [{status.upper()}] {a['description']} "
            f"(assignee: {assignee}, due: {due}){result_link}"
        )
    action_items_text = "\n".join(ai_lines) if ai_lines else "None"

    extra_context = (meeting.get("extra_context_text") or "").strip()
    extra_context_section = (
        f"\n## Extra Context & Instructions\n{extra_context[:6000]}"
        if extra_context else ""
    )

    output_folder = (meeting.get("output_folder_url") or "").strip()
    output_section = (
        f"\n## Output Folder (Google Drive)\n{output_folder}"
        if output_folder else ""
    )

    transcription = (
        (meeting.get("raw_transcript_text") or "").strip() or "No transcript available."
        if include_transcript else "{transcription}"
    )

    return _fmt(
        template,
        meeting_name=meeting.get("drive_folder_name", "Meeting"),
        date=date_str or "—",
        participants=participants,
        labels=label_names,
        summary=meeting.get("summary") or "No summary available.",
        key_points=key_points_text,
        decisions=decisions_text,
        action_items=action_items_text,
        extra_context_section=extra_context_section,
        output_section=output_section,
        transcription=transcription or "No transcript available.",
    )


# ─── Chat handler ─────────────────────────────────────────────────────────────

class MeetingChatHandler:
    def __init__(self, meeting_id: str):
        self.meeting_id = meeting_id
        creds = get_google_creds(
            os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"),
            os.getenv("GOOGLE_TOKEN_PATH", "token.json"),
        )
        self.drive = DriveClient(creds)
        self.gmail = GmailClient(creds)
        self.llm = LLMClient()

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _meeting_context(self):
        with DB() as db:
            meeting = db.get_meeting(self.meeting_id)
            action_items = db.get_meeting_action_items(self.meeting_id)
            people = db.fetchall(
                "SELECT p.* FROM people p "
                "JOIN meeting_people mp ON mp.person_id=p.id "
                "WHERE mp.meeting_id=%s",
                (self.meeting_id,),
            )
            labels = db.fetchall(
                "SELECT l.* FROM labels l "
                "JOIN meeting_labels ml ON ml.label_id=l.id "
                "WHERE ml.meeting_id=%s",
                (self.meeting_id,),
            )
        return meeting, list(action_items), list(people), list(labels)

    def get_history(self) -> list[dict]:
        with DB() as db:
            rows = db.fetchall(
                "SELECT id, role, content, metadata, created_at "
                "FROM chat_messages WHERE meeting_id=%s ORDER BY created_at",
                (self.meeting_id,),
            )
        return [dict(r) for r in rows]

    def get_resolved_system_prompt(self) -> str:
        """Return the resolved system prompt for UI display.
        All placeholders are filled except {transcription}, which stays as the literal text '{transcription}'."""
        meeting, action_items, people, labels = self._meeting_context()
        if not meeting:
            return ""
        return build_chat_system_prompt(meeting, action_items, people, labels, include_transcript=False)

    def clear_history(self):
        with DB() as db:
            db.execute("DELETE FROM chat_messages WHERE meeting_id=%s", (self.meeting_id,))

    def _save(self, role: str, content: str, metadata: dict = None):
        with DB() as db:
            db.execute(
                "INSERT INTO chat_messages(meeting_id, role, content, metadata) "
                "VALUES(%s,%s,%s,%s)",
                (self.meeting_id, role, content,
                 json.dumps(metadata) if metadata else None),
            )

    # ── Tool execution ────────────────────────────────────────────────────────

    def _run_tool(self, name: str, args: dict) -> tuple[str, Optional[str]]:
        if name == "search_gmail":
            query = args.get("query", "")
            emails = self.gmail.search_emails(query, max_results=12)
            if not emails:
                return "No emails found for that query.", None
            lines = []
            for e in emails:
                lines.append(
                    f"ID: {e['id']}\nFrom: {e['from']}\n"
                    f"Subject: {e['subject']}\nDate: {e['date']}\n"
                    f"Snippet: {e['snippet']}\n---"
                )
            return "\n".join(lines), None

        if name == "read_email":
            body = self.gmail.get_email_body(args.get("email_id", ""))
            return (body[:6000] if body else "Could not retrieve email body."), None

        if name == "create_document":
            output_folder_id = os.getenv("GOOGLE_DRIVE_OUTPUT_FOLDER_ID", "").strip()
            if not output_folder_id:
                return "Cannot create document: GOOGLE_DRIVE_OUTPUT_FOLDER_ID is not configured.", None
            title = args.get("title", "Chat Document")
            content_md = args.get("content", "")
            date_str = datetime.now().strftime("%B %d, %Y")
            html = (
                "<!DOCTYPE html><html><head><meta charset='UTF-8'></head>"
                "<body style=\"font-family:'Segoe UI',Arial,sans-serif;max-width:820px;"
                "margin:0 auto;padding:40px 32px;color:#1e293b\">"
                f"<p style='color:#64748b;font-size:13px;margin-bottom:16px'>"
                f"Generated {date_str} · Meeting Chat</p>"
                f"<div style='font-size:14px'>{_markdown_to_html(content_md)}</div>"
                "<hr style='border:none;border-top:1px solid #e2e8f0;margin-top:40px'/>"
                "<p style='font-size:12px;color:#94a3b8;margin-top:12px'>"
                "Generated by Meeting Agent Chat</p>"
                "</body></html>"
            )
            doc = self.drive.create_doc_from_text(title, html, output_folder_id)
            url = doc.get("webViewLink", "")
            return f"Document created: {url}", url

        if name == "search_drive":
            query = args.get("query", "")
            files = self.drive.search_drive(query, max_results=10)
            if not files:
                return "No documents found for that query.", None
            lines = []
            for f in files:
                lines.append(
                    f"ID: {f['id']}\nName: {f['name']}\nType: {f.get('mimeType','')}\n"
                    f"Modified: {f.get('modifiedTime','')}\nLink: {f.get('webViewLink','')}\n---"
                )
            return "\n".join(lines), None

        if name == "read_drive_document":
            file_id = args.get("file_id", "")
            text = self.drive.read_doc_as_text(file_id)
            return (text[:6000] if text else "Could not retrieve document text."), None

        return f"Unknown tool: {name}", None

    # ── OpenAI tool-use loop ──────────────────────────────────────────────────

    def _openai_loop(self, system: str, messages: list) -> tuple[str, list, list]:
        tool_calls_used: list = []
        doc_urls: list = []
        loop_msgs = [{"role": "system", "content": system}] + messages

        for _ in range(8):
            resp = self.llm.client.chat.completions.create(
                model=self.llm.model,
                messages=loop_msgs,
                tools=CHAT_TOOLS,
                tool_choice="auto",
                timeout=120,
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or "", tool_calls_used, doc_urls

            loop_msgs.append(msg)
            for tc in msg.tool_calls:
                tool_name = tc.function.name
                args = json.loads(tc.function.arguments)
                tool_calls_used.append({"tool": tool_name, "args": args})
                result_text, doc_url = self._run_tool(tool_name, args)
                if doc_url:
                    doc_urls.append(doc_url)
                loop_msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                })

        # Safety: force final answer without tools
        loop_msgs.append({
            "role": "user",
            "content": "Please provide your final answer based on the information gathered.",
        })
        final = self.llm.client.chat.completions.create(
            model=self.llm.model, messages=loop_msgs, timeout=120,
        )
        return final.choices[0].message.content or "", tool_calls_used, doc_urls

    # ── Public API ────────────────────────────────────────────────────────────

    def send_message(self, user_message: str, system_prompt_override: str = None) -> dict:
        meeting, action_items, people, labels = self._meeting_context()
        if not meeting:
            return {"error": "Meeting not found"}

        system_prompt = (
            system_prompt_override.strip()
            if system_prompt_override and system_prompt_override.strip()
            else build_chat_system_prompt(meeting, action_items, people, labels)
        )

        history_rows = self.get_history()
        history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

        self._save("user", user_message)
        messages = history + [{"role": "user", "content": user_message}]

        tool_calls_used: list = []
        doc_urls: list = []

        if self.llm.provider == "openai":
            response_text, tool_calls_used, doc_urls = self._openai_loop(system_prompt, messages)
        else:
            turns = "\n".join(
                f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
                for m in messages
            )
            response_text = self.llm.complete(system_prompt + "\n\n" + turns)

        metadata = {}
        if tool_calls_used:
            metadata["tool_calls_used"] = tool_calls_used
        if doc_urls:
            metadata["doc_urls"] = doc_urls
        self._save("assistant", response_text, metadata or None)

        return {
            "response": response_text,
            "tool_calls_used": tool_calls_used,
            "doc_urls": doc_urls,
        }
