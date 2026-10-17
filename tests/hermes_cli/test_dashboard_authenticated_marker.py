"""Plugins see an auth marker only after the dashboard gates admit a request."""
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def marker_client(monkeypatch):
    routes = list(web_server.app.router.routes)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "fixture-session-token")
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "bound_host", "127.0.0.1", raising=False)

    @web_server.app.get("/api/contract/authenticated-marker")
    async def marker(request: Request):
        return {"authenticated": getattr(request.state, "dashboard_authenticated", False)}

    # Install before the dashboard's existing SPA catch-all route.
    web_server.app.router.routes.insert(0, web_server.app.router.routes.pop())

    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    try:
        yield client
    finally:
        client.close()
        web_server.app.router.routes[:] = routes


def test_session_token_admits_private_marker_without_exposing_credential(marker_client):
    path = "/api/contract/authenticated-marker"
    assert marker_client.get(path).status_code == 401
    response = marker_client.get(path, headers={"X-Hermes-Session-Token": "fixture-session-token"})
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}
    assert "fixture-session-token" not in response.text


def test_cookie_gate_cannot_be_bypassed_by_the_loopback_marker(marker_client, monkeypatch):
    monkeypatch.setattr(web_server.app.state, "auth_required", True)
    response = marker_client.get("/api/contract/authenticated-marker",
                                 headers={"X-Hermes-Session-Token": "fixture-session-token"})
    assert response.status_code == 401
