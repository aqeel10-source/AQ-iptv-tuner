"""E2E tests for Xtream Codes API endpoints: player_api.php, get.php."""
import time

import pytest
import main


class TestPlayerAPI:
    """GET /player_api.php — Xtream authentication and data queries."""

    def test_missing_credentials_returns_error(self, app_client):
        resp = app_client.get("/player_api.php")
        # Should return auth failure (401 or 403)
        assert resp.status_code in (401, 403, 422)

    def test_wrong_credentials_returns_error(self, app_client, make_user):
        make_user(username="john", password="correct")
        resp = app_client.get("/player_api.php?username=john&password=wrong")
        assert resp.status_code in (401, 403)

    def test_valid_auth_returns_user_info(self, app_client, make_user):
        make_user(username="john", password="mypass", max_connections=2)
        resp = app_client.get("/player_api.php?username=john&password=mypass")
        assert resp.status_code == 200
        data = resp.json()
        assert "user_info" in data
        assert "server_info" in data

    def test_user_info_fields(self, app_client, make_user):
        make_user(username="john", password="mypass", max_connections=2)
        data = app_client.get("/player_api.php?username=john&password=mypass").json()
        ui = data["user_info"]
        assert ui["username"] == "john"
        assert str(ui["max_connections"]) == "2"
        assert ui["status"] == "Active"

    def test_disabled_user_rejected(self, app_client, make_user):
        make_user(username="john", password="mypass", enabled=False)
        resp = app_client.get("/player_api.php?username=john&password=mypass")
        assert resp.status_code in (401, 403)

    def test_expired_user_rejected(self, app_client, make_user):
        make_user(username="john", password="mypass", expires_at=time.time() - 86400)
        resp = app_client.get("/player_api.php?username=john&password=mypass")
        assert resp.status_code in (401, 403)

    def test_get_live_categories(self, app_client, make_user, make_channel):
        make_user(username="john", password="mypass")
        make_channel(name="ESPN", group="Sports", number="100")
        resp = app_client.get(
            "/player_api.php?username=john&password=mypass&action=get_live_categories")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_get_live_streams(self, app_client, make_user, make_channel):
        make_user(username="john", password="mypass")
        make_channel(name="ESPN", group="Sports", number="100")
        resp = app_client.get(
            "/player_api.php?username=john&password=mypass&action=get_live_streams")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


class TestXtreamUserInfo:
    """_xtream_user_info returns structured user metadata."""

    def test_active_user(self, make_user):
        user = make_user(username="john", password="mypass", max_connections=3)
        info = main._xtream_user_info(user)
        assert info["username"] == "john"
        assert str(info["max_connections"]) == "3"
        assert info["status"] == "Active"

    def test_expired_user_status(self, make_user):
        user = make_user(username="john", password="mypass",
                         expires_at=time.time() - 86400)
        info = main._xtream_user_info(user)
        assert info["status"] == "Expired"


class TestXtreamServerInfo:
    """_xtream_server_info returns server metadata."""

    def test_has_required_fields(self):
        info = main._xtream_server_info()
        assert "url" in info
        assert "port" in info
        assert "server_protocol" in info
        assert "timestamp_now" in info
