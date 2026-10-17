"""Concurrent native sessions share the host-owned credential refresh path."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from agent.plugin_session_credentials import session_credentials


def token(value, account="account"):
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account}, "jti": value}
    return "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"


def test_peer_refresh_is_adopted_without_replaying_rejected_token(monkeypatch):
    from hermes_cli import auth_codex
    old, fresh = token("old"), token("fresh")
    current = [old]
    rotations = []
    barrier = threading.Barrier(8)

    def resolve(*, force_refresh=False):
        if force_refresh:
            rotations.append(current[0])
            current[0] = fresh
        return {"api_key": current[0]}

    monkeypatch.setattr(auth_codex, "resolve_codex_runtime_credentials", resolve)

    def refresh(_):
        barrier.wait(timeout=5)
        return session_credentials("test-profile", rejected_token=old, previous_account="account")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(refresh, range(8)))
    assert rotations == [old]
    assert all(result == {"accessToken": fresh, "chatgptAccountId": "account"} for result in results)


def test_account_change_and_malformed_identity_are_rejected(monkeypatch):
    from hermes_cli import auth_codex
    monkeypatch.setattr(auth_codex, "resolve_codex_runtime_credentials", lambda **kwargs: {"api_key": token("other", "different")})
    with pytest.raises(PermissionError, match="account changed"):
        session_credentials("test-profile", previous_account="account")
    monkeypatch.setattr(auth_codex, "resolve_codex_runtime_credentials", lambda **kwargs: {"api_key": "malformed"})
    with pytest.raises(PermissionError, match="account identity"):
        session_credentials("test-profile")
