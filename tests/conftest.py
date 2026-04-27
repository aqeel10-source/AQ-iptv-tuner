"""Shared fixtures for the AQ-IPTV test suite.

Sets up a temporary config environment so `import main` doesn't touch real files.
Must be imported before any test that uses `main`.
"""
import asyncio
import json
import os
import sys
import tempfile
import time

import pytest

# ── Create temp dir and point env vars there BEFORE main.py is imported ──────
_TMPDIR = tempfile.mkdtemp(prefix="aq_iptv_test_")
os.environ["CONFIG_FILE"] = os.path.join(_TMPDIR, "config.json")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "analytics.db")
# Prevent Telegram sends during tests
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.environ.pop("TELEGRAM_CHAT_ID", None)

# Write a minimal config so main.py can import
_MINIMAL_CONFIG = {
    "server": {"port": 5004, "stream_port": 34343, "device_id": "TEST1234"},
    "auth": {
        "username": "admin",
        "password_hash": "$2b$12$LJ3m4ys3Lf0Xg8VYdYfKduNqsODJmCzqp8Z1aZtFhR8P3.example",
    },
    "subscriptions": [],
    "allowed_groups": [],
    "xtream": {
        "enabled": True,
        "auth_failure_threshold": 10,
        "auth_failure_window_seconds": 60,
        "session_idle_seconds": 60,
    },
    "dispatch": {
        "enabled": True,
        "sticky_seconds": 30,
        "max_failovers_per_session": 3,
    },
    "telegram": {},
    "quality_preference": ["HEVC", "4K", "FHD", "HD", "SD"],
}
with open(os.environ["CONFIG_FILE"], "w") as f:
    json.dump(_MINIMAL_CONFIG, f)

# Add app/ to sys.path so `import main` works
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import main  # noqa: E402


# ── Silence Telegram + state side effects globally ───────────────────────────
@pytest.fixture(autouse=True)
def _silence_side_effects(monkeypatch):
    """Prevent Telegram sends and mute state alerts/logs in all tests."""
    async def _noop(*a, **kw):
        pass

    monkeypatch.setattr(main, "tg_send", _noop)
    monkeypatch.setattr(main, "tg_alert", lambda *a, **kw: "")
    main.state.log_event = lambda *a, **kw: None
    main.state.add_alert = lambda *a, **kw: None


@pytest.fixture(autouse=True)
def _reset_global_state():
    """Reset all mutable global state between tests."""
    yield
    # Auth state
    main._sessions.clear()
    main._IP_BLOCKLIST.clear()
    main._BLOCK_FAIL_LOG.clear()
    main._LOGIN_ATTEMPTS.clear()
    main._AUTH_FAIL_LOG.clear()
    main._AUTH_FAIL_ALERTED.clear()
    # Dispatcher state
    main.dispatcher._cache.clear()
    main._DISPATCH_STATS.clear()
    main._DISPATCH_NO_CANDIDATE_EVENTS = 0
    main._DISPATCH_FAILOVER_LIMIT_HITS = 0
    main._DRAINED_SUBS.clear()
    # Xtream sessions
    main._USER_SESSIONS.clear()
    # Channels
    main.state.channels.clear()
    main.state.mux.clear()
    main.state.users.clear()
    main._DEAD_CHANNELS.clear()
    main._SUB_HEALTH.clear()
    main.state.subscriptions.clear()
    main.state.account_info.clear()
    # Config cache
    main._CFG_CACHE["data"] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def make_subscription():
    """Factory to create a Subscription and register it in state."""
    def _make(name="TestSub", url="http://fake.tv", username="u", password="p",
              max_streams=5, priority=1, enabled=True):
        sub = main.Subscription(
            name=name, url=url, username=username, password=password,
            max_streams=max_streams, priority=priority, enabled=enabled)
        main.state.subscriptions.append(sub)
        return sub
    return _make


@pytest.fixture
def make_channel():
    """Factory to create a Channel with candidates and register it in state."""
    def _make(name="Test Channel", number="100", group="Sports",
              sub_name="TestSub", stream_id="12345", quality="HD",
              candidates=None):
        ch = main.Channel(
            id=f"test_{name.lower().replace(' ', '_')}",
            name=name, number=number, logo="", group=group,
            stream_id=stream_id, quality=quality, sub_name=sub_name)
        if candidates:
            ch.candidates = candidates
        elif sub_name:
            ch.candidates = [main.ChannelCandidate(
                sub_name=sub_name, stream_id=stream_id,
                quality=quality, priority=1)]
        main.state.channels[ch.id] = ch
        return ch
    return _make


@pytest.fixture
def make_user():
    """Factory to create an Xtream User and register it in state."""
    def _make(username="testuser", password="testpass", enabled=True,
              max_connections=1, expires_at=0, allowed_groups=None,
              allowed_channels=None, allowed_subscriptions=None):
        hashed = main._hash_password(password)
        user = main.User(
            username=username, password=hashed, enabled=enabled,
            created_at=time.time(), expires_at=expires_at,
            max_connections=max_connections,
            allowed_groups=allowed_groups or [],
            allowed_channels=allowed_channels or [],
            allowed_subscriptions=allowed_subscriptions or [])
        main.state.users.append(user)
        return user
    return _make


@pytest.fixture
def make_sub_health():
    """Factory to create and register a SubHealth instance."""
    def _make(name="TestSub", fetch_ok=True, tcp_ok=True):
        h = main.SubHealth(name=name)
        if fetch_ok:
            h.record_fetch(True, 500, channels=100)
            # Fill history to get full score
            for _ in range(12):
                h.fetch_history.append(True)
        if tcp_ok:
            h.record_tcp(True, 50)
        main._SUB_HEALTH[name] = h
        return h
    return _make


@pytest.fixture
def app_client():
    """FastAPI TestClient for E2E tests."""
    from starlette.testclient import TestClient
    # Create a client that skips the lifespan (we manage state manually)
    client = TestClient(main.app, raise_server_exceptions=False)
    return client


@pytest.fixture
def authed_client(app_client):
    """TestClient with a valid admin session cookie."""
    token = "test_session_token_abc123"
    main._sessions[token] = time.time() + 86400  # valid for 24h
    app_client.cookies.set("iptv_session", token)
    return app_client
