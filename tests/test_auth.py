import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import _auth


def test_get_access_token_fails_closed_on_bad_credentials(tmp_path):
    # A missing/invalid service account key must raise, not silently fall back to
    # whatever ambient identity (ADC, a locally logged-in gcloud CLI) happens to be
    # active on the machine — that identity can have different project/billing access.
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(Exception):
        _auth.get_access_token(str(missing))
