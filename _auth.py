"""Shared GCP auth helpers used by both video_gen.py (Veo) and video_gen_omni.py (Omni Flash)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_access_token(credentials_path: str) -> str:
    # Try service account key first
    try:
        creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES
        )
        creds.refresh(AuthRequest())
        return creds.token
    except Exception:
        pass

    # Try ADC
    try:
        creds, _ = google.auth.default(scopes=SCOPES)
        creds.refresh(AuthRequest())
        return creds.token
    except Exception:
        pass

    # Fall back to gcloud CLI
    try:
        out = subprocess.check_output(
            ["/Users/xasxcy/google-cloud-sdk/bin/gcloud", "auth", "print-access-token"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            return out
    except Exception:
        pass

    raise RuntimeError("Failed to obtain GCP access token")
