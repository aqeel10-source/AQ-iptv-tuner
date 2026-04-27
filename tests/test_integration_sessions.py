"""Integration tests for Xtream user sessions: registration, max_connections, kick, GC."""
import asyncio
import time

import pytest
import main


class TestRegisterUserSession:
    """_register_user_session enforces max_connections and tracks state."""

    def test_register_success(self, make_user):
        user = make_user(username="viewer", max_connections=2)
        loop = asyncio.get_event_loop()
        sess = loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        assert sess is not None
        assert sess.username == "viewer"
        assert sess.channel_key == "ch1"
        assert sess.id in main._USER_SESSIONS

    def test_max_connections_enforced(self, make_user):
        user = make_user(username="viewer", max_connections=1)
        loop = asyncio.get_event_loop()
        s1 = loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        assert s1 is not None
        s2 = loop.run_until_complete(
            main._register_user_session(user, "ch2", "10.0.0.1", "VLC"))
        assert s2 is None  # at cap

    def test_bumps_user_counters(self, make_user):
        user = make_user(username="viewer", max_connections=3)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        assert user.total_streams == 1
        assert user.active_connections == 1
        assert user.last_seen_ts > 0

    def test_multiple_sessions_tracked(self, make_user):
        user = make_user(username="viewer", max_connections=3)
        loop = asyncio.get_event_loop()
        for i in range(3):
            loop.run_until_complete(
                main._register_user_session(user, f"ch{i}", "10.0.0.1", "VLC"))
        assert user.active_connections == 3
        assert len(main._USER_SESSIONS) == 3


class TestUnregisterUserSession:
    """_unregister_user_session cleans up state on disconnect."""

    def test_unregister_removes_session(self, make_user):
        user = make_user(username="viewer", max_connections=2)
        loop = asyncio.get_event_loop()
        sess = loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        loop.run_until_complete(main._unregister_user_session(sess.id, 5000))
        assert sess.id not in main._USER_SESSIONS
        assert user.active_connections == 0

    def test_unregister_accumulates_bytes(self, make_user):
        user = make_user(username="viewer", max_connections=2)
        loop = asyncio.get_event_loop()
        sess = loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        loop.run_until_complete(main._unregister_user_session(sess.id, 10000))
        assert user.bytes_served == 10000

    def test_unregister_nonexistent_noop(self):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main._unregister_user_session("doesnt_exist"))
        # Should not raise

    def test_frees_slot_for_new_session(self, make_user):
        user = make_user(username="viewer", max_connections=1)
        loop = asyncio.get_event_loop()
        s1 = loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        loop.run_until_complete(main._unregister_user_session(s1.id))
        s2 = loop.run_until_complete(
            main._register_user_session(user, "ch2", "10.0.0.1", "VLC"))
        assert s2 is not None


class TestKickUserSession:
    """_kick_user_session flags sessions for teardown."""

    def test_kick_marks_session(self, make_user):
        user = make_user(username="viewer", max_connections=2)
        loop = asyncio.get_event_loop()
        sess = loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        result = loop.run_until_complete(main._kick_user_session(sess.id))
        assert result is True
        assert sess.kicked is True

    def test_kick_nonexistent_returns_false(self):
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(main._kick_user_session("fake_id"))
        assert result is False


class TestKickAllUserSessions:
    """_kick_all_user_sessions flags all sessions for a user."""

    def test_kicks_all_sessions(self, make_user):
        user = make_user(username="viewer", max_connections=5)
        loop = asyncio.get_event_loop()
        sids = []
        for i in range(3):
            sess = loop.run_until_complete(
                main._register_user_session(user, f"ch{i}", "10.0.0.1", "VLC"))
            sids.append(sess.id)
        n = loop.run_until_complete(main._kick_all_user_sessions("viewer"))
        assert n == 3
        for sid in sids:
            assert main._USER_SESSIONS[sid].kicked is True

    def test_case_insensitive_kick(self, make_user):
        user = make_user(username="Viewer", max_connections=2)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        n = loop.run_until_complete(main._kick_all_user_sessions("viewer"))
        assert n == 1

    def test_kick_all_cleanup_on_kill_all_bug(self, make_user):
        """The bug that motivated Phase 16: kill_all must clean _USER_SESSIONS."""
        user = make_user(username="viewer", max_connections=2)
        loop = asyncio.get_event_loop()
        s1 = loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        # Kick the session
        loop.run_until_complete(main._kick_all_user_sessions("viewer"))
        assert s1.kicked is True
        # After kick, unregister should clean up properly
        loop.run_until_complete(main._unregister_user_session(s1.id))
        assert s1.id not in main._USER_SESSIONS
        assert user.active_connections == 0
        # User can now reconnect
        s2 = loop.run_until_complete(
            main._register_user_session(user, "ch2", "10.0.0.1", "VLC"))
        assert s2 is not None


class TestUserSessionCount:
    """_user_session_count correctly counts active (non-kicked) sessions."""

    def test_empty_returns_zero(self):
        assert main._user_session_count("nobody") == 0

    def test_counts_active(self, make_user):
        user = make_user(username="viewer", max_connections=5)
        loop = asyncio.get_event_loop()
        for i in range(3):
            loop.run_until_complete(
                main._register_user_session(user, f"ch{i}", "10.0.0.1", "VLC"))
        assert main._user_session_count("viewer") == 3

    def test_excludes_kicked(self, make_user):
        user = make_user(username="viewer", max_connections=5)
        loop = asyncio.get_event_loop()
        s1 = loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        loop.run_until_complete(
            main._register_user_session(user, "ch2", "10.0.0.1", "VLC"))
        loop.run_until_complete(main._kick_user_session(s1.id))
        assert main._user_session_count("viewer") == 1

    def test_case_insensitive(self, make_user):
        user = make_user(username="Viewer", max_connections=5)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            main._register_user_session(user, "ch1", "10.0.0.1", "VLC"))
        assert main._user_session_count("viewer") == 1
        assert main._user_session_count("VIEWER") == 1
