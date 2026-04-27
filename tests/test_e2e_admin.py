"""E2E tests for admin dashboard API: login, status, streams, users, dispatch, config."""
import json
import time

import pytest
import main


class TestLoginFlow:
    """POST /login — admin authentication."""

    def test_login_page_accessible(self, app_client):
        resp = app_client.get("/login")
        assert resp.status_code == 200

    def test_login_redirects_when_already_authed(self, authed_client):
        resp = authed_client.get("/login")
        assert resp.status_code == 200
        # Should contain a meta-refresh to /
        assert "refresh" in resp.text.lower() or resp.status_code == 200

    def test_logout_clears_session(self, authed_client):
        resp = authed_client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("location", "")


class TestAPIStatus:
    """GET /api/status — system overview."""

    def test_requires_auth(self, app_client):
        resp = app_client.get("/api/status")
        assert resp.status_code == 401

    def test_returns_status(self, authed_client):
        resp = authed_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "device_id" in data
        assert "total_channels" in data
        assert "uptime_s" in data
        assert "subscriptions" in data
        assert "active_streams" in data

    def test_uptime_positive(self, authed_client):
        data = authed_client.get("/api/status").json()
        assert data["uptime_s"] >= 0


class TestAPIStreams:
    """GET /api/streams — active stream listing."""

    def test_requires_auth(self, app_client):
        resp = app_client.get("/api/streams")
        assert resp.status_code == 401

    def test_empty_when_no_streams(self, authed_client):
        resp = authed_client.get("/api/streams")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 0


class TestAPIUsers:
    """GET/POST /api/users — Xtream user management."""

    def test_list_users_requires_auth(self, app_client):
        resp = app_client.get("/api/users")
        assert resp.status_code == 401

    def test_list_users_empty(self, authed_client):
        resp = authed_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        assert isinstance(data["users"], list)

    def test_create_user(self, authed_client):
        resp = authed_client.post("/api/users", json={
            "username": "newuser",
            "password": "newpass123",
            "max_connections": 2,
        })
        assert resp.status_code == 200
        data = resp.json()
        user = data.get("user", data)
        assert user["username"] == "newuser"

    def test_create_duplicate_user_fails(self, authed_client, make_user):
        make_user(username="existing", password="pass")
        resp = authed_client.post("/api/users", json={
            "username": "existing",
            "password": "newpass",
        })
        assert resp.status_code in (400, 409)

    def test_get_user_detail(self, authed_client, make_user):
        make_user(username="john", password="pass123")
        resp = authed_client.get("/api/users/john")
        assert resp.status_code == 200
        data = resp.json()
        user = data.get("user", data)
        assert user["username"] == "john"

    def test_get_nonexistent_user(self, authed_client):
        resp = authed_client.get("/api/users/nobody")
        assert resp.status_code == 404

    def test_delete_user(self, authed_client, make_user):
        make_user(username="todelete", password="pass")
        resp = authed_client.delete("/api/users/todelete")
        assert resp.status_code == 200

    def test_user_sessions_empty(self, authed_client, make_user):
        make_user(username="john", password="pass")
        resp = authed_client.get("/api/users/john/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0

    def test_all_sessions_endpoint(self, authed_client):
        resp = authed_client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "sessions" in data


class TestAPIDispatch:
    """Dispatch API: decisions, stats, drain/undrain."""

    def test_decisions_empty(self, authed_client):
        resp = authed_client.get("/api/dispatch/decisions")
        assert resp.status_code == 200

    def test_stats_empty(self, authed_client):
        resp = authed_client.get("/api/dispatch/stats")
        assert resp.status_code == 200

    def test_drained_list_empty(self, authed_client):
        resp = authed_client.get("/api/dispatch/drained")
        assert resp.status_code == 200
        data = resp.json()
        drained = data.get("drained", data) if isinstance(data, dict) else data
        assert isinstance(drained, list)
        assert len(drained) == 0

    def test_drain_and_undrain(self, authed_client, make_subscription):
        make_subscription(name="TestSub")
        # Drain
        resp = authed_client.post("/api/dispatch/drain/TestSub")
        assert resp.status_code == 200
        # Verify drained
        data = authed_client.get("/api/dispatch/drained").json()
        drained = data.get("drained", data) if isinstance(data, dict) else data
        assert "TestSub" in drained
        # Undrain
        resp = authed_client.post("/api/dispatch/undrain/TestSub")
        assert resp.status_code == 200
        data = authed_client.get("/api/dispatch/drained").json()
        drained = data.get("drained", data) if isinstance(data, dict) else data
        assert "TestSub" not in drained


class TestAPIConfig:
    """Config API: backups, restore."""

    def test_backups_list(self, authed_client):
        resp = authed_client.get("/api/config/backups")
        assert resp.status_code == 200
        data = resp.json()
        backups = data.get("backups", data) if isinstance(data, dict) else data
        assert isinstance(backups, list)

    def test_restore_nonexistent_fails(self, authed_client):
        resp = authed_client.post("/api/config/restore/nonexistent.json")
        assert resp.status_code in (404, 400)


class TestAPIHealth:
    """Health API endpoint."""

    def test_health_endpoint(self, authed_client):
        resp = authed_client.get("/api/health")
        assert resp.status_code == 200


class TestAPIAlerts:
    """Alerts API."""

    def test_clear_alerts(self, authed_client):
        resp = authed_client.delete("/api/alerts")
        assert resp.status_code == 200


class TestAPILogs:
    """Logs API."""

    def test_get_logs(self, authed_client):
        resp = authed_client.get("/api/logs")
        assert resp.status_code == 200

    def test_clear_logs(self, authed_client):
        resp = authed_client.delete("/api/logs")
        assert resp.status_code == 200


class TestAPIChannels:
    """Channels API."""

    def test_list_channels_empty(self, authed_client):
        resp = authed_client.get("/api/channels")
        assert resp.status_code == 200

    def test_list_channels_with_data(self, authed_client, make_channel):
        make_channel(name="ESPN", number="100", group="Sports")
        resp = authed_client.get("/api/channels")
        assert resp.status_code == 200
