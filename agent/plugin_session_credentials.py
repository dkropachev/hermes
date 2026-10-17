"""Native sessions borrow access tokens; Hermes retains rotating credentials."""
from __future__ import annotations

import threading

_lock = threading.Lock()
_profile_locks: dict[str, threading.Lock] = {}


def session_credentials(profile, *, rejected_token=None, previous_account=None):
    """Serialize refresh and let concurrent sessions adopt a peer's rotation.

    The existing resolver owns auth-store transactions, pool selection, source
    locking and persistence. No refresh token is copied into a Codex home.
    """
    from agent.codex_headers import codex_account_headers
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials

    with _lock:
        profile_lock = _profile_locks.setdefault(str(profile), threading.Lock())
    with profile_lock:
        credentials = resolve_codex_runtime_credentials()
        token = credentials["api_key"]
        account = codex_account_headers(token).get("ChatGPT-Account-ID")
        if previous_account and account != previous_account:
            raise PermissionError("Native session account changed; reopen explicitly")
        if rejected_token and rejected_token == token:
            credentials = resolve_codex_runtime_credentials(force_refresh=True)
            token = credentials["api_key"]
            account = codex_account_headers(token).get("ChatGPT-Account-ID")
        if not account or previous_account and account != previous_account:
            raise PermissionError("Native session requires a stable Codex account identity")
        return {"accessToken": token, "chatgptAccountId": account}
