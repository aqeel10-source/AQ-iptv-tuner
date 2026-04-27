"""Unit tests for config loading, saving, backup rotation, and utilities."""
import json
import os
import tempfile
import time

import pytest
import main


class TestLoadConfig:
    """load_config() with mtime cache and env-var overlays."""

    def test_returns_dict(self):
        cfg = main.load_config()
        assert isinstance(cfg, dict)
        assert "server" in cfg

    def test_caches_by_mtime(self):
        cfg1 = main.load_config()
        cfg2 = main.load_config()
        assert cfg1 is cfg2  # same object (cached)

    def test_invalidates_on_mtime_change(self):
        cfg1 = main.load_config()
        # Force mtime change
        main._CFG_CACHE["mtime"] = 0.0
        main._CFG_CACHE["data"] = None
        cfg2 = main.load_config()
        assert cfg2 is not cfg1

    def test_env_var_overlay_telegram(self, monkeypatch):
        main._CFG_CACHE["data"] = None
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env_token_123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "env_chat_456")
        cfg = main.load_config()
        assert cfg["telegram"]["bot_token"] == "env_token_123"
        assert cfg["telegram"]["chat_id"] == "env_chat_456"
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
        monkeypatch.delenv("TELEGRAM_CHAT_ID")
        main._CFG_CACHE["data"] = None


class TestSaveConfig:
    """save_config() with atomic write and backup creation."""

    def test_atomic_write(self):
        cfg = main.load_config()
        cfg["_test_key"] = "hello"
        main.save_config(cfg)
        # Re-read from disk
        main._CFG_CACHE["data"] = None
        cfg2 = main.load_config()
        assert cfg2["_test_key"] == "hello"
        # Cleanup
        del cfg2["_test_key"]
        main.save_config(cfg2)

    def test_creates_backup(self):
        cfg = main.load_config()
        main.save_config(cfg)
        assert os.path.isdir(main.BACKUP_DIR)
        backups = [f for f in os.listdir(main.BACKUP_DIR) if f.startswith("config-")]
        assert len(backups) >= 1

    def test_backup_rotation_prunes_old(self):
        cfg = main.load_config()
        # Create many backups
        for _ in range(10):
            main.save_config(cfg)
        backups = [f for f in os.listdir(main.BACKUP_DIR) if f.startswith("config-")]
        keep = main._ops_cfg()["backup_count"]
        assert len(backups) <= keep + 2  # small tolerance for timing


class TestBackupConfigSnapshot:
    """_backup_config_snapshot writes timestamped files and prunes old ones."""

    def test_creates_snapshot_file(self):
        cfg = {"test": True}
        main._backup_config_snapshot(cfg)
        backups = [f for f in os.listdir(main.BACKUP_DIR) if f.startswith("config-")]
        assert len(backups) >= 1

    def test_snapshot_content_matches(self):
        cfg = {"snapshot_check": 42}
        main._backup_config_snapshot(cfg)
        # Find the most recently modified backup
        backups = [f for f in os.listdir(main.BACKUP_DIR) if f.startswith("config-")]
        backups.sort(key=lambda f: os.path.getmtime(os.path.join(main.BACKUP_DIR, f)),
                     reverse=True)
        with open(os.path.join(main.BACKUP_DIR, backups[0])) as f:
            data = json.load(f)
        assert data["snapshot_check"] == 42


class TestRedact:
    """_redact() scrubs credentials from log messages."""

    def test_redacts_username(self):
        s = "http://example.com?username=admin&password=secret"
        r = main._redact(s)
        assert "admin" not in r
        assert "secret" not in r
        assert "username=<redacted>" in r
        assert "password=<redacted>" in r

    def test_preserves_non_credential_content(self):
        s = "Connection to host failed"
        assert main._redact(s) == s

    def test_handles_multiple_occurrences(self):
        s = "url1?username=a&password=b url2?username=c&password=d"
        r = main._redact(s)
        assert r.count("<redacted>") == 4

    def test_case_insensitive(self):
        s = "?Username=admin&PASSWORD=secret"
        r = main._redact(s)
        assert "admin" not in r
        assert "secret" not in r


class TestSubEnvSlug:
    """_sub_env_slug() converts subscription names to env-var format."""

    def test_basic_conversion(self):
        assert main._sub_env_slug("IGate TV") == "IGATE_TV"

    def test_special_characters(self):
        assert main._sub_env_slug("my-sub.name") == "MY_SUB_NAME"

    def test_already_uppercase(self):
        assert main._sub_env_slug("ALREADY_GOOD") == "ALREADY_GOOD"

    def test_leading_trailing_special(self):
        assert main._sub_env_slug("--test--") == "TEST"

    def test_numbers_preserved(self):
        assert main._sub_env_slug("Sub 123") == "SUB_123"

    def test_empty_string(self):
        assert main._sub_env_slug("") == ""


class TestOpsConfig:
    """_ops_cfg() returns operational config with defaults."""

    def test_default_backup_count(self):
        cfg = main._ops_cfg()
        assert isinstance(cfg["backup_count"], int)
        assert cfg["backup_count"] >= 1
