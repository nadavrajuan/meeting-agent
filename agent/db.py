# agent/db.py
"""PostgreSQL database access layer."""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2
import psycopg2.extras


def get_conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "db"),
        port=int(os.getenv("POSTGRES_PORT", 5432)),
        dbname=os.getenv("POSTGRES_DB", "meeting_agent"),
        user=os.getenv("POSTGRES_USER", "agent"),
        password=os.getenv("POSTGRES_PASSWORD", "changeme"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


class DB:
    def __init__(self):
        self.conn = get_conn()
        self.conn.autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if args[0]:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()

    def execute(self, sql: str, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur

    def fetchone(self, sql: str, params=None) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def fetchall(self, sql: str, params=None) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    # ── Agent State ───────────────────────────────────────────────────────
    def get_state(self, key: str) -> Optional[str]:
        row = self.fetchone("SELECT value FROM agent_state WHERE key = %s", (key,))
        return row["value"] if row else None

    def set_state(self, key: str, value: str):
        self.execute(
            "INSERT INTO agent_state(key, value, updated_at) VALUES(%s, %s, NOW()) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
            (key, value),
        )

    # ── Meetings ──────────────────────────────────────────────────────────
    def meeting_exists(self, drive_folder_id: str) -> bool:
        row = self.fetchone(
            "SELECT id FROM meetings WHERE drive_folder_id = %s", (drive_folder_id,)
        )
        return row is not None

    def create_meeting(self, **kwargs) -> str:
        mid = str(uuid.uuid4())
        kwargs["id"] = mid
        cols = ", ".join(kwargs.keys())
        vals = ", ".join(["%s"] * len(kwargs))
        self.execute(
            f"INSERT INTO meetings ({cols}) VALUES ({vals})",
            list(kwargs.values()),
        )
        return mid

    def update_meeting(self, meeting_id: str, **kwargs):
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        self.execute(
            f"UPDATE meetings SET {sets} WHERE id = %s",
            list(kwargs.values()) + [meeting_id],
        )

    def get_meeting(self, meeting_id: str) -> Optional[dict]:
        return self.fetchone("SELECT * FROM meetings WHERE id = %s", (meeting_id,))

    # ── People ────────────────────────────────────────────────────────────
    def upsert_person(self, name: str, email: str = None) -> str:
        row = self.fetchone("SELECT id FROM people WHERE name = %s", (name,))
        if row:
            if email:
                self.execute("UPDATE people SET email = %s WHERE id = %s", (email, row["id"]))
            return str(row["id"])
        pid = str(uuid.uuid4())
        self.execute(
            "INSERT INTO people(id, name, email) VALUES(%s, %s, %s)",
            (pid, name, email),
        )
        return pid

    def link_person_to_meeting(self, meeting_id: str, person_id: str):
        self.execute(
            "INSERT INTO meeting_people(meeting_id, person_id) VALUES(%s, %s) ON CONFLICT DO NOTHING",
            (meeting_id, person_id),
        )

    def get_all_people(self) -> list[dict]:
        return self.fetchall(
            "SELECT p.*, array_agg(l.name) FILTER (WHERE l.name IS NOT NULL) as labels "
            "FROM people p "
            "LEFT JOIN people_labels pl ON pl.person_id = p.id "
            "LEFT JOIN labels l ON l.id = pl.label_id "
            "GROUP BY p.id ORDER BY p.name"
        )

    # ── Labels ────────────────────────────────────────────────────────────
    def get_or_create_label(self, name: str) -> str:
        row = self.fetchone("SELECT id FROM labels WHERE name = %s", (name,))
        if row:
            return str(row["id"])
        lid = str(uuid.uuid4())
        self.execute("INSERT INTO labels(id, name) VALUES(%s, %s)", (lid, name))
        return lid

    def link_label_to_meeting(self, meeting_id: str, label_id: str):
        self.execute(
            "INSERT INTO meeting_labels(meeting_id, label_id) VALUES(%s, %s) ON CONFLICT DO NOTHING",
            (meeting_id, label_id),
        )

    def get_all_labels(self) -> list[dict]:
        return self.fetchall("SELECT * FROM labels ORDER BY name")

    def get_person_label_ids(self, person_id: str) -> list[str]:
        """Return label IDs assigned to a person."""
        rows = self.fetchall(
            "SELECT label_id FROM people_labels WHERE person_id = %s", (person_id,)
        )
        return [str(r["label_id"]) for r in rows]

    # ── Action Items ──────────────────────────────────────────────────────
    def create_action_item(self, meeting_id: str, description: str, assignee: str = None, due_date: str = None) -> str:
        aid = str(uuid.uuid4())
        self.execute(
            "INSERT INTO action_items(id, meeting_id, description, assignee_name, due_date) VALUES(%s,%s,%s,%s,%s)",
            (aid, meeting_id, description, assignee, due_date),
        )
        return aid

    def update_action_item(self, action_id: str, **kwargs):
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        self.execute(
            f"UPDATE action_items SET {sets} WHERE id = %s",
            list(kwargs.values()) + [action_id],
        )

    def get_meeting_action_items(self, meeting_id: str) -> list[dict]:
        return self.fetchall(
            "SELECT * FROM action_items WHERE meeting_id = %s ORDER BY created_at",
            (meeting_id,),
        )

    def update_action_item_plan(self, action_id: str, plan_output_type: str,
                                plan_resources: str, plan_notes: str, feasibility: str,
                                short_name: str = None):
        self.execute(
            "UPDATE action_items SET plan_output_type=%s, plan_resources=%s, plan_notes=%s, feasibility=%s, short_name=%s WHERE id=%s",
            (plan_output_type, plan_resources, plan_notes, feasibility, short_name, action_id),
        )

    def update_action_item_approval(self, action_id: str, approved: bool, approved_max_iterations: int = 1):
        self.execute(
            "UPDATE action_items SET approved=%s, approved_max_iterations=%s WHERE id=%s",
            (approved, approved_max_iterations, action_id),
        )

    def set_meeting_approval_status(self, meeting_id: str, status: str, extra_context_text: str = None):
        if extra_context_text is not None:
            self.execute(
                "UPDATE meetings SET approval_status=%s, extra_context_text=%s WHERE id=%s",
                (status, extra_context_text, meeting_id),
            )
        else:
            self.execute(
                "UPDATE meetings SET approval_status=%s WHERE id=%s",
                (status, meeting_id),
            )

    # ── Agent Runs ────────────────────────────────────────────────────────
    def create_run(self, meeting_id: str = None, run_type: str = "meeting") -> str:
        rid = str(uuid.uuid4())
        self.execute(
            "INSERT INTO agent_runs(id, meeting_id, run_type) VALUES(%s,%s,%s)",
            (rid, meeting_id, run_type),
        )
        return rid

    def finish_run(self, run_id: str, status: str, summary_log: str, full_log: list, error: str = None):
        self.execute(
            "UPDATE agent_runs SET ended_at=NOW(), status=%s, summary_log=%s, full_log=%s, error=%s WHERE id=%s",
            (status, summary_log, json.dumps(full_log), error, run_id),
        )

    def append_run_log(self, run_id: str, entry: dict):
        """Write a single log entry to run_log_entries for real-time monitoring."""
        self.execute(
            "INSERT INTO run_log_entries(run_id, step, detail, level, data, ts) VALUES(%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                entry["step"],
                entry["detail"],
                entry.get("level", "info"),
                json.dumps(entry["data"]) if entry.get("data") else None,
                entry["ts"],
            ),
        )

    def get_run_log_entries(self, run_id: str, since_id: int = 0) -> list[dict]:
        return self.fetchall(
            "SELECT id, step, detail, level, data, ts FROM run_log_entries "
            "WHERE run_id=%s AND id > %s ORDER BY id",
            (run_id, since_id),
        )

    # ── Prompt Templates ─────────────────────────────────────────────────
    def get_prompt(self, name: str) -> str:
        row = self.fetchone("SELECT template FROM prompt_templates WHERE name = %s", (name,))
        return row["template"] if row else ""

    def update_prompt(self, name: str, template: str):
        self.execute(
            "UPDATE prompt_templates SET template=%s, updated_at=NOW() WHERE name=%s",
            (template, name),
        )

    # ── Context Notes ─────────────────────────────────────────────────────
    def create_context_note(self, title: str, content: str, drive_doc_id: str = None, drive_doc_url: str = None) -> str:
        nid = str(uuid.uuid4())
        self.execute(
            "INSERT INTO context_notes(id, title, content, drive_doc_id, drive_doc_url) VALUES(%s,%s,%s,%s,%s)",
            (nid, title, content, drive_doc_id, drive_doc_url),
        )
        return nid

    def get_context_notes(self) -> list[dict]:
        return self.fetchall("SELECT * FROM context_notes ORDER BY created_at DESC")

    def delete_context_note(self, note_id: str):
        self.execute("DELETE FROM context_notes WHERE id=%s", (note_id,))

    def get_used_context_doc_ids(self) -> set:
        rows = self.fetchall("SELECT drive_doc_id FROM used_context_docs")
        return {r["drive_doc_id"] for r in rows}

    def mark_context_doc_used(self, drive_doc_id: str, meeting_id: str):
        self.execute(
            "INSERT INTO used_context_docs(drive_doc_id, meeting_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (drive_doc_id, meeting_id),
        )

    # ── Search ────────────────────────────────────────────────────────────
    def search_meetings(self, label_names: list[str] = None, person_names: list[str] = None,
                        keyword: str = None) -> list[dict]:
        conditions = ["1=1"]
        params = []
        sql = """
            SELECT DISTINCT m.*,
                array_agg(DISTINCT l.name) FILTER (WHERE l.name IS NOT NULL) as labels,
                array_agg(DISTINCT p.name) FILTER (WHERE p.name IS NOT NULL) as people
            FROM meetings m
            LEFT JOIN meeting_labels ml ON ml.meeting_id = m.id
            LEFT JOIN labels l ON l.id = ml.label_id
            LEFT JOIN meeting_people mp ON mp.meeting_id = m.id
            LEFT JOIN people p ON p.id = mp.person_id
        """
        if label_names:
            conditions.append("l.name = ANY(%s)")
            params.append(label_names)
        if person_names:
            conditions.append("p.name = ANY(%s)")
            params.append(person_names)
        if keyword:
            conditions.append("(m.summary ILIKE %s OR m.title ILIKE %s)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        sql += " WHERE " + " AND ".join(conditions)
        sql += " GROUP BY m.id ORDER BY m.meeting_date DESC NULLS LAST"
        return self.fetchall(sql, params)
