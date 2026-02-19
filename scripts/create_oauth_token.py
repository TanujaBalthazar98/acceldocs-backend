#!/usr/bin/env python3
"""Create Google OAuth token file for local Drive access (no service-account key).

Usage:
  GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... \
  python scripts/create_oauth_token.py --out oauth-token.json
"""

import argparse
import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="oauth-token.json")
    args = parser.parse_args()

    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("Missing GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars")
        return 1

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    out_path = Path(args.out)
    out_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Wrote OAuth token: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
