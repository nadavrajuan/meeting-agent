# agent/digest.py
"""Daily and Weekly digest generators."""

import os
from datetime import datetime, timedelta, timezone

from .db import DB
from .gmail_service import GmailClient
from .drive_service import get_google_creds
from .llm_client import LLMClient


def run_daily_summary(target_date: datetime = None):
    if not target_date:
        target_date = datetime.now(timezone.utc) - timedelta(days=1)

    start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    with DB() as db:
        meetings = db.fetchall(
            "SELECT * FROM meetings WHERE meeting_date >= %s AND meeting_date < %s AND status='done'",
            (start, end),
        )
        prompt_template = db.get_prompt("daily_summary")
        run_id = db.create_run(run_type="daily")

    meetings_text = "\n\n".join(
        f"### {m['drive_folder_name']}\n{m.get('summary', 'No summary available')}"
        for m in meetings
    ) or "No meetings processed today."

    llm = LLMClient()
    prompt = prompt_template.format(
        date=start.strftime("%Y-%m-%d"),
        meetings=meetings_text,
        emails="(Email digest not yet implemented for daily - see individual meeting emails)",
    )

    result = llm.complete(prompt)

    _send_digest_email(
        subject=f"Daily Summary – {start.strftime('%B %d, %Y')}",
        body=result,
    )

    with DB() as db:
        db.finish_run(run_id, "done", result, [])

    print(f"[digest] Daily summary sent for {start.date()}")


def run_weekly_summary(week_start: datetime = None):
    if not week_start:
        today = datetime.now(timezone.utc)
        week_start = today - timedelta(days=today.weekday() + 7)

    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)

    with DB() as db:
        meetings = db.fetchall(
            "SELECT m.*, array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL) as people "
            "FROM meetings m "
            "LEFT JOIN meeting_people mp ON mp.meeting_id = m.id "
            "LEFT JOIN people p ON p.id = mp.person_id "
            "WHERE m.meeting_date >= %s AND m.meeting_date < %s AND m.status='done' "
            "GROUP BY m.id",
            (week_start, week_end),
        )
        action_items = db.fetchall(
            "SELECT ai.*, m.drive_folder_name as meeting_name FROM action_items ai "
            "JOIN meetings m ON m.id = ai.meeting_id "
            "WHERE ai.created_at >= %s AND ai.created_at < %s",
            (week_start, week_end),
        )
        prompt_template = db.get_prompt("weekly_summary")
        run_id = db.create_run(run_type="weekly")

    data = {
        "meetings": [
            {
                "name": m["drive_folder_name"],
                "date": str(m.get("meeting_date", "")),
                "summary": (m.get("summary") or "")[:500],
                "people": m.get("people") or [],
            }
            for m in meetings
        ],
        "action_items": [
            {
                "meeting": ai["meeting_name"],
                "description": ai["description"],
                "assignee": ai.get("assignee_name"),
                "status": ai["status"],
            }
            for ai in action_items
        ],
    }

    import json
    llm = LLMClient()
    prompt = prompt_template.format(
        week_start=week_start.strftime("%B %d, %Y"),
        week_end=week_end.strftime("%B %d, %Y"),
        data=json.dumps(data, indent=2, default=str),
    )

    result = llm.complete(prompt)
    _send_digest_email(
        subject=f"Weekly Summary – Week of {week_start.strftime('%B %d, %Y')}",
        body=result,
    )

    with DB() as db:
        db.finish_run(run_id, "done", result, [])

    print(f"[digest] Weekly summary sent for {week_start.date()} – {week_end.date()}")


def _send_digest_email(subject: str, body: str):
    send_to = os.getenv("SEND_SUMMARY_TO", "")
    if not send_to:
        print("[digest] No SEND_SUMMARY_TO configured")
        return
    creds = get_google_creds(
        os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"),
        os.getenv("GOOGLE_TOKEN_PATH", "token.json"),
    )
    gmail = GmailClient(creds)
    html = f"<html><body><pre style='font-family:Arial;white-space:pre-wrap'>{body}</pre></body></html>"
    gmail.send_email(send_to, subject, html)
