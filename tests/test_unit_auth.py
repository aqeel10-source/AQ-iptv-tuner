"""Unit tests for auth: password verification, sessions, rate limiting, blocklist."""
import hashlib
import secrets
import time
import asyncio

import pytest
import bcrypt
import main


class TestVerifyAdminPassword:
    """_verify_admin_password handles bcrypt and SHA256 fallback."""

    def test_bcrypt_correct(self):
        pw = "mysecretpass"
        hashed = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4)).decode()
        assert main._verify_admin_password(pw, hashed) is True

    def test_bcrypt_wrong(self):
        hashed = bcrypt.hashpw(b"correct", bcrypt.gensalt(rounds=4)).decode()
        assert main._verify_admin_password("wrong", hashed) is False

    def test_sha256_correct(self):
        pw = "legacypass"
        sha = hashlib.sha256(pw.encode()).hexdigest()
        assert main._verify_admin_password(pw, sha) is True

    def test_sha256_wrong(self):
        sha = hashlib.sha256(b"correct").hexdigest()
        assert main._verify_admin_password("wrong", sha) is False


class TestVerifyXtreamPassword:
    """_verify_password handles bcrypt and legacy plaintext comparison."""

    def test_bcrypt_match(self):
        pw = "userpass"
        hashed = main._hash_password(pw)
        assert main._verify_password(pw, hashed) is True

    def test_bcrypt_mismatch(self):
        hashed = main._hash_password("correct")
        assert main._verify_password("wrong", hashed) is False

    def test_plaintext_match(self):
        assert main._verify_password("plain123", "plain123") is True

    def test_plaintext_mismatch(self):
        assert main._verify_password("wrong", "plain123") is False

    def test_hash_password_produces_bcrypt(self):
        h = main._hash_password("test")
        assert h.startswith("$2b$")


class TestMigratePlaintextPasswords:
    """_migrate_plaintext_passwords auto-hashes plain passwords."""

    def test_migrates_plaintext(self):
        user = main.User(username="test", password="plaintext123")
        migrated = main._migrate_plaintext_passwords([user])
        assert migrated is True
        assert user.password.startswith("$2b$")

    def test_skips_already_hashed(self):
        hashed = main._hash_password("already")
        user = main.User(username="test", password=hashed)
        migrated = main._migrate_plaintext_passwords([user])
        assert migrated is False
        assert user.password == hashed

    def test_returns_false_when_none_migrated(self):
        assert main._migrate_plaintext_passwords([]) is False


class TestCheckSession:
    """_check_session validates dashboard session tokens."""

    def test_valid_session(self):
        token = "valid_token"
        main._sessions[token] = time.time() + 3600
        from unittest.mock import MagicMock
        request = MagicMock()
        request.cookies.get.return_value = token
        assert main._check_session(request) is True

    def test_expired_session_removed(self):
        token = "expired_token"
        main._sessions[token] = time.time() - 1
        from unittest.mock import MagicMock
        request = MagicMock()
        request.cookies.get.return_value = token
        assert main._check_session(request) is False
        assert token not in main._sessions

    def test_missing_token(self):
        from unittest.mock import MagicMock
        request = MagicMock()
        request.cookies.get.return_value = ""
        assert main._check_session(request) is False

    def test_unknown_token(self):
        from unittest.mock import MagicMock
        request = MagicMock()
        request.cookies.get.return_value = "does_not_exist"
        assert main._check_session(request) is False

    def test_no_auth_config_returns_true(self, monkeypatch):
        """When no auth block in config, all requests pass."""
        monkeypatch.setattr(main, "_auth_cfg", lambda: None)
        from unittest.mock import MagicMock
        request = MagicMock()
        assert main._check_session(request) is True


class TestLoginRateLimit:
    """_check_login_rate enforces per-IP attempt limits."""

    def test_allows_first_five(self):
        ip = "10.0.0.1"
        for _ in range(5):
            assert main._check_login_rate(ip) is None

    def test_blocks_sixth(self):
        ip = "10.0.0.2"
        for _ in range(5):
            main._check_login_rate(ip)
        retry = main._check_login_rate(ip)
        assert retry is not None
        assert retry > 0

    def test_per_ip_isolation(self):
        for _ in range(5):
            main._check_login_rate("10.0.0.3")
        assert main._check_login_rate("10.0.0.3") is not None
        assert main._check_login_rate("10.0.0.4") is None


class TestIPBlocklist:
    """_is_blocked, blocklist expiry, and persistence."""

    def test_unblocked_by_default(self):
        assert main._is_blocked("10.0.0.1") is False

    def test_blocked_when_added(self):
        main._IP_BLOCKLIST["10.0.0.1"] = time.time() + 3600
        assert main._is_blocked("10.0.0.1") is True

    def test_expired_block_clears(self):
        main._IP_BLOCKLIST["10.0.0.1"] = time.time() - 1
        assert main._is_blocked("10.0.0.1") is False
        assert "10.0.0.1" not in main._IP_BLOCKLIST

    def test_different_ip_unaffected(self):
        main._IP_BLOCKLIST["10.0.0.1"] = time.time() + 3600
        assert main._is_blocked("10.0.0.2") is False


class TestRecordAuthFailure:
    """_record_auth_failure builds up the failure log and triggers blocks."""

    def test_threshold_triggers_block(self):
        ip = "10.99.0.1"
        loop = asyncio.get_event_loop()
        for i in range(20):
            loop.run_until_complete(main._record_auth_failure(ip, f"u{i}", "bad"))
        assert main._is_blocked(ip) is True

    def test_below_threshold_no_block(self):
        ip = "10.99.0.2"
        loop = asyncio.get_event_loop()
        for i in range(19):
            loop.run_until_complete(main._record_auth_failure(ip, f"u{i}", "bad"))
        assert main._is_blocked(ip) is False

    def test_auth_success_clears_fail_log(self):
        ip = "10.99.0.3"
        loop = asyncio.get_event_loop()
        for i in range(5):
            loop.run_until_complete(main._record_auth_failure(ip, f"u{i}", "bad"))
        assert ip in main._AUTH_FAIL_LOG
        main._record_auth_success(ip)
        assert ip not in main._AUTH_FAIL_LOG


class TestAuthenticateUser:
    """_authenticate_user verifies Xtream credentials with all checks."""

    def test_valid_credentials(self, make_user):
        user = make_user(username="john", password="pass123")
        result = main._authenticate_user("john", "pass123")
        assert result is not None
        assert result.username == "john"

    def test_wrong_password(self, make_user):
        make_user(username="john", password="pass123")
        assert main._authenticate_user("john", "wrong") is None

    def test_unknown_user(self):
        assert main._authenticate_user("nobody", "pass") is None

    def test_disabled_user(self, make_user):
        make_user(username="john", password="pass123", enabled=False)
        assert main._authenticate_user("john", "pass123") is None

    def test_expired_user(self, make_user):
        make_user(username="john", password="pass123", expires_at=time.time() - 1)
        assert main._authenticate_user("john", "pass123") is None

    def test_case_insensitive_lookup(self, make_user):
        make_user(username="John", password="pass123")
        result = main._authenticate_user("john", "pass123")
        assert result is not None
        assert result.username == "John"

    def test_empty_credentials_rejected(self):
        assert main._authenticate_user("", "") is None
        assert main._authenticate_user("user", "") is None
        assert main._authenticate_user("", "pass") is None
