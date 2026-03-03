# agent/state.py
from typing import Any, Optional
from typing_extensions import TypedDict


class MeetingState(TypedDict):
    """State object passed through LangGraph nodes."""

    # ── Input ──────────────────────────────────────────────────────────────
    meeting_id: str                      # UUID in DB
    drive_folder_id: str
    drive_folder_name: str
    meeting_date: Optional[str]

    # ── Documents ──────────────────────────────────────────────────────────
    transcript_doc_id: Optional[str]
    transcript_text: Optional[str]
    summary_meta_doc_id: Optional[str]
    summary_meta_text: Optional[str]
    extra_context_doc_id: Optional[str]
    extra_context_text: Optional[str]

    # ── Derived ────────────────────────────────────────────────────────────
    participants: list[str]
    participant_emails: dict[str, str]   # name -> email
    labels: list[str]
    tags: list[str]
    executive_summary: Optional[str]
    key_points: list[str]
    action_items: list[dict]
    decisions: list[str]
    important_notes_addressed: Optional[str]

    # ── Iteration tracking ────────────────────────────────────────────────
    iteration: int
    max_iterations: int
    tasks_to_execute: list[dict]         # action items to follow up on
    task_results: list[dict]
    action_item_db_ids: list[str]        # DB IDs of created action items (parallel to action_items)

    # ── Run settings (passed in from trigger) ─────────────────────────────
    skip_email_search: bool
    skip_action_items: bool
    require_approval: bool               # if True, pause after planning for human review

    # ── Planning ───────────────────────────────────────────────────────────
    action_item_plans: list[dict]        # per-task plan from node_plan_action_items
    approval_status: Optional[str]       # None|awaiting_approval|approved|rejected

    # ── Related context ────────────────────────────────────────────────────
    related_emails: list[dict]
    related_meetings: list[dict]

    # ── Outputs ────────────────────────────────────────────────────────────
    output_folder_id: Optional[str]
    output_folder_url: Optional[str]
    email_sent: bool

    # ── Run logging ────────────────────────────────────────────────────────
    run_id: str
    run_log: list[dict]                  # step-by-step trace
    errors: list[str]
