#!/usr/bin/env python3
# scripts/auth_google.py
"""Run this once to authenticate with Google and save token.json."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from agent.drive_service import get_google_creds

auth_port = int(os.getenv("OAUTH_PORT", "8080"))
print(f"Starting OAuth flow on port {auth_port}.")
print(f"If running in Docker, use: docker compose run -p {auth_port}:{auth_port} api python scripts/auth_google.py")
print("Then open the printed URL in your browser.\n")

creds = get_google_creds(
    credentials_path=os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json"),
    token_path=os.getenv("GOOGLE_TOKEN_PATH", "token.json"),
    auth_port=auth_port,
)
print("Authentication successful! token.json saved.")
print(f"   Scopes: {creds.scopes}")
