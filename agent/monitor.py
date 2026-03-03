# agent/monitor.py
"""Drive folder monitor – detects new meeting folders and queues them."""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from .db import DB
from .drive_service import DriveClient, get_google_creds
from .graph import get_meeting_graph
from .state import MeetingState


def get_drive_client() -> DriveClient:
    creds = get_google_creds(
        os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"),
        os.getenv("GOOGLE_TOKEN_PATH", "token.json"),
    )
    return DriveClient(creds)


def run_monitor(
    last_n: Optional[int] = None,
    max_iterations: Optional[int] = None,
    skip_email_search: bool = False,
    skip_action_items: bool = False,
    require_approval: bool = False,
):
    """
    Main entry point.
    - If last_n is set, processes the N most recent folders (first-run mode).
    - Otherwise, processes only folders newer than last_run_at.
    """
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
    if not folder_id:
        raise ValueError("GOOGLE_DRIVE_FOLDER_ID not set")

    drive = get_drive_client()

    with DB() as db:
        last_run_str = db.get_state("last_run_at")

    last_run_dt: Optional[datetime] = None
    if last_run_str and not last_n:
        last_run_dt = datetime.fromisoformat(last_run_str)

    print(f"[monitor] Scanning folder {folder_id} (last_run={last_run_dt}, last_n={last_n})")

    # List all sub-folders (meeting folders)
    items = drive.list_folder_contents(folder_id, modified_after=last_run_dt)
    folders = [i for i in items if i.get("mimeType") == "application/vnd.google-apps.folder"]

    # Sort by createdTime desc
    folders.sort(key=lambda x: x.get("createdTime", ""), reverse=True)

    if last_n is not None:
        folders = folders[:last_n]

    print(f"[monitor] Found {len(folders)} new folder(s) to process")

    run_settings = {
        "max_iterations": max_iterations if max_iterations is not None else int(os.getenv("MAX_ITERATIONS", 5)),
        "skip_email_search": skip_email_search,
        "skip_action_items": skip_action_items,
        "require_approval": require_approval,
    }

    for folder in reversed(folders):  # Process oldest first
        process_folder(folder, drive, run_settings)

    # Update last_run_at
    with DB() as db:
        db.set_state("last_run_at", datetime.now(timezone.utc).isoformat())

    print("[monitor] Done.")


def process_folder(folder: dict, drive: DriveClient, run_settings: dict = None):
    folder_id = folder["id"]
    folder_name = folder["name"]

    with DB() as db:
        if db.meeting_exists(folder_id):
            print(f"[monitor] Skipping already-processed folder: {folder_name}")
            return

        # Parse meeting date from folder name if possible
        meeting_date = None
        for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%Y%m%d"]:
            import re
            date_match = re.search(r"\d{4}[-_]\d{2}[-_]\d{2}|\d{8}", folder_name)
            if date_match:
                try:
                    meeting_date = datetime.strptime(
                        date_match.group().replace("_", "-"), "%Y-%m-%d"
                    ).replace(tzinfo=timezone.utc).isoformat()
                    break
                except Exception:
                    pass

        meeting_id = db.create_meeting(
            drive_folder_id=folder_id,
            drive_folder_name=folder_name,
            meeting_date=meeting_date,
            status="pending",
        )
        run_id = db.create_run(meeting_id=meeting_id, run_type="meeting")

    print(f"[monitor] Processing folder: {folder_name} (meeting_id={meeting_id})")

    initial_state: MeetingState = {
        "meeting_id": meeting_id,
        "drive_folder_id": folder_id,
        "drive_folder_name": folder_name,
        "meeting_date": meeting_date,
        "transcript_doc_id": None,
        "transcript_text": None,
        "summary_meta_doc_id": None,
        "summary_meta_text": None,
        "extra_context_doc_id": None,
        "extra_context_text": None,
        "participants": [],
        "participant_emails": {},
        "labels": [],
        "tags": [],
        "executive_summary": None,
        "key_points": [],
        "action_items": [],
        "decisions": [],
        "important_notes_addressed": None,
        "iteration": 0,
        "max_iterations": (run_settings or {}).get("max_iterations", int(os.getenv("MAX_ITERATIONS", 5))),
        "tasks_to_execute": [],
        "task_results": [],
        "action_item_db_ids": [],
        "skip_email_search": (run_settings or {}).get("skip_email_search", False),
        "skip_action_items": (run_settings or {}).get("skip_action_items", False),
        "require_approval": (run_settings or {}).get("require_approval", False),
        "action_item_plans": [],
        "approval_status": None,
        "related_emails": [],
        "related_meetings": [],
        "output_folder_id": None,
        "output_folder_url": None,
        "email_sent": False,
        "run_id": run_id,
        "run_log": [],
        "errors": [],
    }

    graph = get_meeting_graph()
    final_state = graph.invoke(initial_state)

    # Save run log
    run_log = final_state.get("run_log", [])
    summary_log = "\n".join(
        f"[{e['step']}] {e['detail']}" for e in run_log
    )
    with DB() as db:
        db.finish_run(
            run_id,
            status="error" if final_state.get("errors") else "done",
            summary_log=summary_log,
            full_log=run_log,
            error="\n".join(final_state.get("errors", [])) or None,
        )

    print(f"[monitor] Finished: {folder_name}")
