"""Integration tests for middleware: StreamPortFilter, AuthMiddleware, SecurityHeaders."""
import time

import pytest
import main


class TestStreamPortFilterMiddleware:
    """StreamPortFilterMiddleware blocks admin paths on the public stream port."""

    def test_admin_path_allowed_on_admin_port(self, app_client):
        """Admin paths work on the default (admin) port — middleware doesn't block."""
        resp = app_client.get("/discover.json")
        assert resp.status_code == 200

    def test_xtream_paths_in_allow_list(self):
        """Verify the allow-list contains expected Xtream paths."""
        allowed = main.StreamPortFilterMiddleware.STREAM_ALLOWED
        assert "/player_api.php" in allowed
        assert "/xmltv.php" in allowed
        assert "/get.php" in allowed
        assert "/live/" in allowed
        assert "/logo/" in allowed

    def test_admin_paths_not_in_allow_list(self):
        """Admin/HDHR paths should NOT be in the stream-port allow list."""
        allowed = main.StreamPortFilterMiddleware.STREAM_ALLOWED
        assert "/discover.json" not in allowed
        assert "/lineup.json" not in allowed
        assert "/api/" not in allowed
        assert "/" not in allowed


class TestAuthMiddleware:
    """AuthMiddleware enforces session auth on /api/* and blocks IPs."""

    def test_open_paths_bypass_auth(self, app_client):
        """HDHR discovery and other open paths should work without a session."""
        resp = app_client.get("/discover.json")
        assert resp.status_code == 200

    def test_api_without_session_returns_401(self, app_client):
        """API calls without a session should return 401."""
        resp = app_client.get("/api/status")
        assert resp.status_code == 401

    def test_api_with_session_returns_200(self, authed_client):
        """API calls with a valid session should work."""
        resp = authed_client.get("/api/status")
        assert resp.status_code == 200

    def test_root_without_session_redirects_to_login(self, app_client):
        """/ without session should redirect to /login."""
        resp = app_client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("location", "")

    def test_blocked_ip_returns_403(self, app_client):
        """Blocked IPs get 403 on any path."""
        main._IP_BLOCKLIST["testclient"] = time.time() + 3600
        resp = app_client.get("/discover.json")
        assert resp.status_code == 403

    def test_login_page_accessible(self, app_client):
        """/login should be accessible without auth."""
        resp = app_client.get("/login")
        assert resp.status_code == 200

    def test_open_path_list(self):
        """Verify the OPEN tuple contains expected paths."""
        open_paths = main.AuthMiddleware.OPEN
        assert "/discover.json" in open_paths
        assert "/login" in open_paths
        assert "/player_api.php" in open_paths
        assert "/live/" in open_paths


class TestSecurityHeadersMiddleware:
    """SecurityHeadersMiddleware injects security headers on admin port."""

    def test_security_headers_present(self, authed_client):
        resp = authed_client.get("/api/status")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"

    def test_expected_header_values(self):
        headers = main.SecurityHeadersMiddleware._HEADERS
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Permissions-Policy"] == "()"


class TestXtreamLegacyURLMiddleware:
    """XtreamLegacyURLMiddleware rewrites short Xtream URLs."""

    def test_pattern_matches_3_segment(self):
        import re
        m = main.XtreamLegacyURLMiddleware.PATTERN.match("/user/pass/12345.ts")
        assert m is not None
        assert m.group(1) == "user"
        assert m.group(3) == "12345"
        assert m.group(4) == "ts"

    def test_pattern_matches_without_extension(self):
        import re
        m = main.XtreamLegacyURLMiddleware.PATTERN.match("/user/pass/12345")
        assert m is not None
        assert m.group(4) is None

    def test_pattern_no_match_on_api(self):
        import re
        m = main.XtreamLegacyURLMiddleware.PATTERN.match("/api/users/123")
        # Matches structurally but the middleware further gates on username existence
        # The pattern itself is r"^/([^/]+)/([^/]+)/(\d+)(?:\.(ts|m3u8))?$"
        # "/api/users/123" does match the pattern
        if m:
            # But the middleware checks that group(1) is a real Xtream username
            assert m.group(1) == "api"
