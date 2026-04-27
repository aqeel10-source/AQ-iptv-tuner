"""Unit tests for channel model: quality detection, normalization, User filtering."""
import time

import pytest
import main


class TestQualityDetection:
    """_detect_quality_token and _detect_quality parse quality from channel names."""

    def test_detect_4k(self):
        q, p = main._detect_quality_token("ESPN 4K")
        assert q == "4K"
        assert p == 50

    def test_detect_uhd(self):
        q, p = main._detect_quality_token("BBC UHD")
        assert q == "4K"

    def test_detect_fhd(self):
        q, p = main._detect_quality_token("Sky Sports FHD")
        assert q == "FHD"
        assert p == 40

    def test_detect_1080p(self):
        q, p = main._detect_quality_token("Channel 1080p")
        assert q == "FHD"

    def test_detect_hevc(self):
        q, p = main._detect_quality_token("NBC HEVC")
        assert q == "HEVC"
        assert p == 15

    def test_detect_h265(self):
        q, p = main._detect_quality_token("Fox H265")
        assert q == "HEVC"

    def test_detect_hd(self):
        q, p = main._detect_quality_token("CNN HD")
        assert q == "HD"
        assert p == 30

    def test_detect_sd(self):
        q, p = main._detect_quality_token("TBS SD")
        assert q == "SD"
        assert p == 10

    def test_detect_720p_as_sd(self):
        q, p = main._detect_quality_token("ESPN 720p")
        assert q == "SD"

    def test_no_quality_token(self):
        q, p = main._detect_quality_token("Plain Channel")
        assert q is None
        assert p == 0

    def test_detect_quality_group_takes_priority(self):
        q, p = main._detect_quality(name="Some Channel", group="4K Movies")
        assert q == "4K"

    def test_detect_quality_fallback_to_name(self):
        q, p = main._detect_quality(name="ESPN FHD", group="Sports")
        assert q == "FHD"

    def test_detect_quality_default_hd(self):
        q, p = main._detect_quality(name="Plain Channel", group="General")
        assert q == "HD"
        assert p == 30


class TestNormalize:
    """_normalize strips punctuation and collapses whitespace."""

    def test_replaces_dots_underscores_pipes(self):
        assert main._normalize("A.B_C|D") == "A B C D"

    def test_collapses_whitespace(self):
        assert main._normalize("A   B") == "A B"

    def test_strips_special_chars(self):
        result = main._normalize("  ★ESPN★  ")
        assert "ESPN" in result


class TestBaseName:
    """_base_name strips quality tokens for channel deduplication."""

    def test_strips_quality_token(self):
        base = main._base_name("ESPN HD")
        assert "HD" not in base
        assert "ESPN" in base

    def test_strips_4k(self):
        base = main._base_name("BBC 4K")
        assert "4K" not in base
        assert "BBC" in base

    def test_preserves_non_quality_words(self):
        base = main._base_name("National Geographic Wild")
        assert "National" in base
        assert "Geographic" in base


class TestQualityVariant:
    """QualityVariant data class stores per-variant data."""

    def test_creation(self):
        v = main.QualityVariant(stream_id="100", quality="FHD",
                                priority=40, orig_name="ESPN FHD")
        assert v.stream_id == "100"
        assert v.quality == "FHD"
        assert v.priority == 40


class TestChannelCandidate:
    """ChannelCandidate tracks upstream sources for dispatcher."""

    def test_defaults(self):
        c = main.ChannelCandidate(sub_name="S1", stream_id="100")
        assert c.quality == "HD"
        assert c.priority == 1
        assert c.last_fail_ts == 0.0
        assert c.fail_count == 0

    def test_failure_tracking(self):
        c = main.ChannelCandidate(sub_name="S1", stream_id="100")
        c.last_fail_ts = time.time()
        c.fail_count = 3
        assert c.fail_count == 3


class TestChannel:
    """Channel data class with candidates list."""

    def test_creation(self):
        ch = main.Channel(id="ch1", name="ESPN", number="100",
                          logo="", group="Sports", stream_id="500")
        assert ch.name == "ESPN"
        assert ch.candidates == []

    def test_candidates_list(self):
        cands = [main.ChannelCandidate(sub_name="S1", stream_id="1")]
        ch = main.Channel(id="ch1", name="ESPN", number="100",
                          logo="", group="Sports", stream_id="500",
                          candidates=cands)
        assert len(ch.candidates) == 1


class TestUserModel:
    """User data class: creation, expiry, serialization."""

    def test_not_expired_when_zero(self):
        u = main.User(username="test", password="x", expires_at=0)
        assert u.is_expired() is False

    def test_not_expired_future(self):
        u = main.User(username="test", password="x",
                      expires_at=time.time() + 86400)
        assert u.is_expired() is False

    def test_expired_past(self):
        u = main.User(username="test", password="x",
                      expires_at=time.time() - 1)
        assert u.is_expired() is True

    def test_to_dict_excludes_password(self):
        u = main.User(username="test", password="secret")
        d = u.to_dict()
        assert "password" not in d
        assert d["username"] == "test"

    def test_to_dict_includes_password(self):
        u = main.User(username="test", password="secret")
        d = u.to_dict(include_password=True)
        assert d["password"] == "secret"

    def test_from_dict_round_trip(self):
        u = main.User(username="test", password="pw", max_connections=3,
                      allowed_groups=["Sports"], notes="VIP")
        d = u.to_dict(include_password=True)
        u2 = main.User.from_dict(d)
        assert u2.username == "test"
        assert u2.max_connections == 3
        assert u2.allowed_groups == ["Sports"]
        assert u2.notes == "VIP"

    def test_from_dict_defaults(self):
        u = main.User.from_dict({})
        assert u.username == ""
        assert u.enabled is True
        assert u.max_connections == 1


class TestCheckChannelAllowed:
    """_check_channel_allowed enforces user-level visibility gates."""

    def test_allowed_when_no_restrictions(self, make_user, make_channel):
        user = make_user(username="viewer")
        ch = make_channel(name="ESPN", group="Sports")
        assert main._check_channel_allowed(user, ch.id) is True

    def test_allowed_channels_filter(self, make_user, make_channel):
        ch1 = make_channel(name="ESPN", group="Sports")
        ch2 = make_channel(name="CNN", number="200", group="News")
        user = make_user(username="viewer", allowed_channels=[ch1.id])
        assert main._check_channel_allowed(user, ch1.id) is True
        assert main._check_channel_allowed(user, ch2.id) is False

    def test_allowed_groups_filter(self, make_user, make_channel):
        ch1 = make_channel(name="ESPN", group="Sports")
        ch2 = make_channel(name="CNN", number="200", group="News")
        user = make_user(username="viewer", allowed_groups=["Sports"])
        assert main._check_channel_allowed(user, ch1.id) is True
        assert main._check_channel_allowed(user, ch2.id) is False

    def test_dead_channel_blocked(self, make_user, make_channel):
        ch = make_channel(name="ESPN", group="Sports")
        user = make_user(username="viewer")
        # Mark channel as dead
        main._DEAD_CHANNELS[ch.id] = {"count": 10, "last_ts": time.time()}
        assert main._check_channel_allowed(user, ch.id) is False

    def test_nonexistent_channel(self, make_user):
        user = make_user(username="viewer")
        assert main._check_channel_allowed(user, "doesnt_exist") is False


class TestUserFilteredChannels:
    """_user_filtered_channels returns the intersection of live + allowed."""

    def test_returns_all_for_unrestricted_user(self, make_user, make_channel):
        user = make_user(username="viewer")
        make_channel(name="ESPN", group="Sports")
        make_channel(name="CNN", number="200", group="News")
        filtered = main._user_filtered_channels(user)
        assert len(filtered) == 2

    def test_filters_by_group(self, make_user, make_channel):
        user = make_user(username="viewer", allowed_groups=["Sports"])
        make_channel(name="ESPN", group="Sports")
        make_channel(name="CNN", number="200", group="News")
        filtered = main._user_filtered_channels(user)
        assert len(filtered) == 1
        assert any("espn" in k for k in filtered)


class TestQPRIO:
    """Quality priority ordering: HEVC > 4K > HD > FHD > SD."""

    def test_priority_values(self):
        assert main.QPRIO["4K"] == 50
        assert main.QPRIO["UHD"] == 50
        assert main.QPRIO["FHD"] == 40
        assert main.QPRIO["HD"] == 30
        assert main.QPRIO["HEVC"] == 15
        assert main.QPRIO["SD"] == 10

    def test_4k_highest(self):
        assert main.QPRIO["4K"] > main.QPRIO["FHD"]
        assert main.QPRIO["4K"] > main.QPRIO["HD"]
        assert main.QPRIO["4K"] > main.QPRIO["SD"]

    def test_fhd_above_hd(self):
        assert main.QPRIO["FHD"] > main.QPRIO["HD"]

    def test_hd_above_sd(self):
        assert main.QPRIO["HD"] > main.QPRIO["SD"]
