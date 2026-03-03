# agent/gmail_service.py
"""Gmail search and send utilities."""

import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from googleapiclient.discovery import build


class GmailClient:
    def __init__(self, creds):
        self.service = build("gmail", "v1", credentials=creds)

    def search_emails(
        self,
        query: str,
        max_results: int = 20,
    ) -> list[dict]:
        """Search Gmail with a query string."""
        results = []
        try:
            resp = self.service.users().messages().list(
                userId="me", q=query, maxResults=max_results
            ).execute()
            messages = resp.get("messages", [])
            for msg in messages:
                detail = self.service.users().messages().get(
                    userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "To", "Date"]
                ).execute()
                headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
                results.append({
                    "id": msg["id"],
                    "subject": headers.get("Subject", ""),
                    "from": headers.get("From", ""),
                    "to": headers.get("To", ""),
                    "date": headers.get("Date", ""),
                    "snippet": detail.get("snippet", ""),
                })
        except Exception as e:
            print(f"Gmail search error: {e}")
        return results

    def get_email_body(self, message_id: str) -> str:
        """Get the text body of an email."""
        try:
            msg = self.service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
            return self._extract_body(msg.get("payload", {}))
        except Exception as e:
            return f"Error fetching email: {e}"

    def _extract_body(self, payload: dict) -> str:
        if "parts" in payload:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain":
                    data = part.get("body", {}).get("data", "")
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        return ""

    def send_email(self, to: str, subject: str, body_html: str, from_addr: str = None) -> bool:
        """Send an email."""
        try:
            msg = MIMEMultipart("alternative")
            msg["To"] = to
            msg["Subject"] = subject
            if from_addr:
                msg["From"] = from_addr
            msg.attach(MIMEText(body_html, "html"))
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            self.service.users().messages().send(
                userId="me", body={"raw": raw}
            ).execute()
            return True
        except Exception as e:
            print(f"Email send error: {e}")
            return False

    def search_emails_for_people(self, people_emails: list[str], keywords: list[str] = None) -> list[dict]:
        """Search for emails involving specific people."""
        all_emails = []
        for email in people_emails:
            query = f"(from:{email} OR to:{email})"
            if keywords:
                kw_part = " OR ".join(f'"{k}"' for k in keywords)
                query += f" ({kw_part})"
            results = self.search_emails(query, max_results=10)
            all_emails.extend(results)
        # Deduplicate by id
        seen = set()
        unique = []
        for e in all_emails:
            if e["id"] not in seen:
                seen.add(e["id"])
                unique.append(e)
        return unique
