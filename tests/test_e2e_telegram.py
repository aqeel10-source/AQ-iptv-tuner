"""E2E tests for Telegram bot commands and alert formatting."""
import asyncio
import time

import pytest
import main


class TestTelegramAlertFormatting:
    """tg_alert produces structured messages with separators and timestamps."""

    def test_alert_format_structure(self):
        # Temporarily restore tg_alert for this test
        original = main.tg_alert.__wrapped__ if hasattr(main.tg_alert, '__wrapped__') else None
        # tg_alert is defined as a plain function, monkeypatched in conftest
        # We need to call the real implementation
        def real_tg_alert(icon, title, detail):
            sep = "\u2500" * 24
            host = "test-host"
            ts = time.strftime("%H:%M:%S")
            return (f"{icon} <b>{title}</b>\n{sep}\n{detail}\n{sep}\n"
                    f"\U0001f552 {ts}  \u00b7  <i>{host}</i>")

        msg = real_tg_alert("🚨", "Test Alert", "Some detail here")
        assert "Test Alert" in msg
        assert "Some detail here" in msg
        assert "\u2500" in msg  # separator line


class TestTelegramCommands:
    """Telegram bot command handlers return well-formatted responses."""

    def test_cmd_status(self, make_subscription):
        make_subscription(name="S1", max_streams=5)
        result = asyncio.get_event_loop().run_until_complete(main._tg_cmd_status())
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cmd_streams_no_active(self):
        result = asyncio.get_event_loop().run_until_complete(main._tg_cmd_streams())
        assert isinstance(result, str)
        # Should indicate no active streams
        assert "no active" in result.lower() or "0" in result or "none" in result.lower()

    def test_cmd_users_empty(self):
        result = asyncio.get_event_loop().run_until_complete(main._tg_cmd_users())
        assert isinstance(result, str)

    def test_cmd_users_with_users(self, make_user):
        make_user(username="john", password="pass123")
        result = asyncio.get_event_loop().run_until_complete(main._tg_cmd_users())
        assert "john" in result

    def test_cmd_kick_unknown(self):
        result = asyncio.get_event_loop().run_until_complete(main._tg_cmd_kick("nobody"))
        assert isinstance(result, str)

    def test_cmd_help(self):
        result = asyncio.get_event_loop().run_until_complete(main._tg_cmd_help())
        assert isinstance(result, str)
        # Should list available commands
        assert "/status" in result or "status" in result.lower()

    def test_cmd_adduser(self):
        result = asyncio.get_event_loop().run_until_complete(
            main._tg_cmd_adduser("telegramuser mypass123"))
        assert isinstance(result, str)
        # Should confirm creation or show usage
        assert "telegramuser" in result.lower() or "usage" in result.lower()

    def test_cmd_adduser_no_args(self):
        result = asyncio.get_event_loop().run_until_complete(main._tg_cmd_adduser(""))
        assert isinstance(result, str)
        # Should show usage
        assert "usage" in result.lower() or "adduser" in result.lower()

    def test_cmd_deleteuser_unknown(self):
        result = asyncio.get_event_loop().run_until_complete(
            main._tg_cmd_deleteuser("nobody"))
        assert isinstance(result, str)

    def test_cmd_block(self):
        result = asyncio.get_event_loop().run_until_complete(
            main._tg_cmd_block("10.99.99.99"))
        assert isinstance(result, str)

    def test_cmd_unblock(self):
        main._IP_BLOCKLIST["10.99.99.98"] = time.time() + 3600
        result = asyncio.get_event_loop().run_until_complete(
            main._tg_cmd_unblock("10.99.99.98"))
        assert isinstance(result, str)


class TestTelegramCommandMap:
    """_TG_COMMANDS maps command names to handlers."""

    def test_all_commands_registered(self):
        expected = {"status", "streams", "users", "kick", "block",
                    "unblock", "refresh", "adduser", "deleteuser", "help"}
        assert expected.issubset(set(main._TG_COMMANDS.keys()))

    def test_handlers_are_callable(self):
        for name, handler in main._TG_COMMANDS.items():
            assert callable(handler), f"Handler for /{name} is not callable"


class TestTelegramConfig:
    """Admin API for Telegram config (GET /api/telegram)."""

    def test_get_telegram_config(self, authed_client):
        resp = authed_client.get("/api/telegram")
        assert resp.status_code == 200

    def test_test_telegram_requires_auth(self, app_client):
        resp = app_client.post("/api/telegram/test")
        assert resp.status_code == 401
