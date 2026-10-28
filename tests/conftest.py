import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_logins(monkeypatch):
    """A machine's real claude.ai logins must never reach a test."""
    for name in list(os.environ):
        if name.startswith(("CLAUDE_AI_", "CLAUDE_RC_")) or name in ("CLAUDE_CODE_OAUTH_TOKEN", "TS_AUTHKEY"):
            monkeypatch.delenv(name)
