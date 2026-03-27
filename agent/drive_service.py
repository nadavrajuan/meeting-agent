# agent/drive_service.py
"""Google Drive helper utilities."""

import io
import os
import re
import wsgiref.simple_server
import wsgiref.util
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qs

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaInMemoryUpload

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def _run_oauth_flow(flow: InstalledAppFlow, port: int) -> Credentials:
    """Run OAuth flow with server bound to 0.0.0.0 for Docker compatibility."""
    # Capture the authorization response via a minimal WSGI app
    captured = {}

    def wsgi_app(environ, start_response):
        captured["uri"] = wsgiref.util.request_uri(environ)
        start_response("200 OK", [("Content-Type", "text/html")])
        return [b"<h1>Authentication complete. You may close this window.</h1>"]

    # Bind to 0.0.0.0 so Docker port-forwarding can reach us,
    # but tell Google to redirect to localhost (which the user's browser resolves)
    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, state = flow.authorization_url(prompt="consent")
    print(f"Please visit this URL to authorize:\n\n{auth_url}\n")

    server = wsgiref.simple_server.make_server("0.0.0.0", port, wsgi_app)
    while True:
        server.handle_request()
        if "uri" in captured:
            parsed_check = urlparse(captured["uri"])
            if parse_qs(parsed_check.query).get("state", [None])[0] == state:
                break
            print(f"Ignoring stale callback (wrong state), waiting for new authorization...")
            captured.clear()

    # Extract code from the captured redirect URI
    parsed = urlparse(captured["uri"])
    code = parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        raise RuntimeError(f"No authorization code in callback: {captured.get('uri')}")

    flow.fetch_token(code=code)
    return flow.credentials


def get_google_creds(
    credentials_path: str = "credentials.json",
    token_path: str = "token.json",
    auth_port: int = 8080,
) -> Credentials:
    creds = None
    if os.path.exists(token_path) and os.path.getsize(token_path) > 0:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = _run_oauth_flow(flow, auth_port)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return creds


def get_drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds)


def get_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)


class DriveClient:
    def __init__(self, creds: Credentials):
        self.service = get_drive_service(creds)

    def list_folder_contents(
        self,
        folder_id: str,
        modified_after: Optional[datetime] = None,
    ) -> list[dict]:
        """List files/folders in a Drive folder, optionally filtered by modified time."""
        query = f"'{folder_id}' in parents and trashed = false"
        if modified_after:
            ts = modified_after.strftime("%Y-%m-%dT%H:%M:%S")
            query += f" and modifiedTime > '{ts}'"

        results = []
        page_token = None
        while True:
            resp = (
                self.service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType, modifiedTime, createdTime)",
                    pageToken=page_token,
                    orderBy="createdTime desc",
                )
                .execute()
            )
            results.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results

    def list_folder_contents_by_name_pattern(
        self, folder_id: str, pattern: str
    ) -> list[dict]:
        """Find files matching a name pattern (case-insensitive contains)."""
        query = (
            f"'{folder_id}' in parents and trashed = false "
            f"and name contains '{pattern}'"
        )
        resp = (
            self.service.files()
            .list(
                q=query,
                fields="files(id, name, mimeType, modifiedTime)",
            )
            .execute()
        )
        return resp.get("files", [])

    def read_doc_as_text(self, file_id: str) -> str:
        """Export a Google Doc as plain text."""
        try:
            request = self.service.files().export_media(
                fileId=file_id, mimeType="text/plain"
            )
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue().decode("utf-8", errors="replace")
        except Exception:
            # Try as regular file download
            request = self.service.files().get_media(fileId=file_id)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue().decode("utf-8", errors="replace")

    def create_folder(self, name: str, parent_id: str) -> dict:
        file_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        folder = self.service.files().create(body=file_metadata, fields="id, webViewLink").execute()
        return folder

    def create_doc_from_text(
        self, name: str, content: str, parent_id: str, mimetype: str = "text/html"
    ) -> dict:
        """Create a Google Doc from HTML or plain text content."""
        file_metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.document",
            "parents": [parent_id],
        }
        media = MediaInMemoryUpload(
            content.encode("utf-8"),
            mimetype=mimetype,
            resumable=False,
        )
        doc = (
            self.service.files()
            .create(body=file_metadata, media_body=media, fields="id, webViewLink")
            .execute()
        )
        return doc

    def search_drive(self, query: str, max_results: int = 10) -> list[dict]:
        """Full-text search across all Drive files."""
        safe_query = query.replace("'", "\\'")
        resp = (
            self.service.files()
            .list(
                q=f"fullText contains '{safe_query}' and trashed = false",
                fields="files(id, name, mimeType, webViewLink, modifiedTime)",
                pageSize=max_results,
                orderBy="modifiedTime desc",
            )
            .execute()
        )
        return resp.get("files", [])

    def find_context_doc_near_date(
        self,
        context_folder_id: str,
        target_date: datetime,
        labels: list[str],
        days_window: int = 7,
        exclude_ids: set = None,
    ) -> Optional[dict]:
        """
        Find an extra-context doc in the context folder that:
        1. Was created/modified within `days_window` days of target_date
        2. Mentions at least one of the labels in its name or content
        """
        from datetime import timedelta

        start = (target_date - timedelta(days=days_window)).strftime("%Y-%m-%dT%H:%M:%S")
        end = (target_date + timedelta(days=days_window)).strftime("%Y-%m-%dT%H:%M:%S")

        query = (
            f"'{context_folder_id}' in parents and trashed = false "
            f"and modifiedTime >= '{start}' and modifiedTime <= '{end}'"
        )
        resp = (
            self.service.files()
            .list(q=query, fields="files(id, name, mimeType, modifiedTime)")
            .execute()
        )
        files = resp.get("files", [])

        # Filter out already-used doc IDs
        if exclude_ids:
            files = [f for f in files if f["id"] not in exclude_ids]

        # Filter by label mention in name
        if labels:
            label_lower = [l.lower() for l in labels]
            for f in files:
                if any(l in f["name"].lower() for l in label_lower):
                    return f
        return files[0] if files else None
