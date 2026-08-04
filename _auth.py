"""Shared GCP auth helpers used by both video_gen.py (Veo) and video_gen_omni.py (Omni Flash)."""

from __future__ import annotations

import os
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
    # Fail closed on the explicitly supplied service account key: a missing, revoked, or
    # malformed key must surface as an error, never fall through to whatever ambient identity
    # (ADC, a locally logged-in gcloud CLI) happens to be active on the machine — that identity
    # can have different project/billing access, so a silent fallback risks submitting (and
    # paying for) a request under the wrong account without telling the caller.
    creds = service_account.Credentials.from_service_account_file(
        credentials_path, scopes=SCOPES
    )
    creds.refresh(AuthRequest())
    return creds.token
