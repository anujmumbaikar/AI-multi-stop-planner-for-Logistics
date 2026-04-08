"""
auth_setup.py  —  Run ONCE to generate credentials/token.json
Opens a browser window asking you to log in with your Google account
and grant Gmail + Sheets access. After approval, saves token.json locally.

Usage:
    python auth_setup.py
"""

import os
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

CREDENTIALS_PATH = "credentials/credentials.json"
TOKEN_PATH = "credentials/token.json"


def main():
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"\n[ERROR] {CREDENTIALS_PATH} not found.")
        print("Download it from Google Cloud Console:")
        print("  APIs & Services → Credentials → OAuth 2.0 Client IDs → Download JSON")
        print(f"  Save it as: {CREDENTIALS_PATH}\n")
        return

    os.makedirs("credentials", exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)

    # Save as JSON (Credentials.to_json() returns a string)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    print(f"\n[OK] token.json saved to: {TOKEN_PATH}")
    print("You can now run: python main.py\n")


if __name__ == "__main__":
    main()
